"""Pixiv 标签查询的参数、别名和轻量过滤工具。

这个模块不依赖 AstrBot 或 Pixiv 客户端，便于单元测试和后续替换标签解析策略。
"""

from __future__ import annotations

import csv
import re
import shlex
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

MAX_TAGS = 10
MAX_TAG_LATEST = 20
MAX_SEARCH_PAGE = 100_000


class TagAliasProvider(Protocol):
    """把用户输入的标签转换为 Pixiv 更常用的官方标签。"""

    def translate(self, tag: str) -> str:
        """返回搜索用标签；没有别名时返回原值。"""


class BuiltinTagAliasProvider:
    """从 UTF-8 TSV 加载经过筛选的中文、英文常用标签映射。"""

    DEFAULT_PATH = Path(__file__).with_name("tag_aliases.tsv")
    FALLBACK_ALIASES = {
        "黑发": "黒髪",
        "碧蓝航线": "アズールレーン",
        "阿米娅": "アーミヤ(アークナイツ)",
    }
    # 保留旧属性名，避免已有扩展代码依赖该常量时立即失效。
    ALIASES = FALLBACK_ALIASES

    def __init__(self, path: Path | str | None = None, *, strict: bool = False) -> None:
        self.load_error: Exception | None = None
        try:
            if path is None:
                self.aliases = dict(_load_default_aliases())
            else:
                self.aliases = _read_alias_tsv(Path(path))
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            if strict:
                raise
            self.load_error = exc
            self.aliases = {
                _normalize_alias(alias): target
                for alias, target in self.FALLBACK_ALIASES.items()
            }

    def translate(self, tag: str) -> str:
        return self.aliases.get(_normalize_alias(tag), tag)


def _normalize_alias(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _read_alias_tsv(path: Path) -> dict[str, str]:
    """读取一行一个映射的 TSV，并拒绝同一别名指向不同目标。"""

    aliases: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        meaningful_lines = (
            line
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        )
        reader = csv.DictReader(meaningful_lines, delimiter="\t")
        if reader.fieldnames != ["alias", "target"]:
            raise ValueError("标签词典表头必须为 alias<TAB>target")
        for row in reader:
            alias = str(row.get("alias") or "").strip()
            target = str(row.get("target") or "").strip()
            key = _normalize_alias(alias)
            if not key or not target:
                raise ValueError(f"标签词典第 {reader.line_num} 行存在空字段")
            if "\t" in target or "\n" in target:
                raise ValueError(f"标签词典第 {reader.line_num} 行目标格式无效")
            existing = aliases.get(key)
            if existing is not None and existing != target:
                raise ValueError(
                    f"标签词典第 {reader.line_num} 行与已有别名冲突：{alias}"
                )
            aliases[key] = target
    if not aliases:
        raise ValueError("标签词典为空")
    return aliases


@lru_cache(maxsize=1)
def _load_default_aliases() -> dict[str, str]:
    """默认词典随插件版本固定，进程内只读取一次。"""

    return _read_alias_tsv(BuiltinTagAliasProvider.DEFAULT_PATH)


@dataclass(slots=True, frozen=True)
class TagSearchQuery:
    """规范化后的标签查询参数。"""

    required_tags: tuple[str, ...]
    optional_tags: tuple[str, ...]
    excluded_tags: tuple[str, ...]
    translated_required_tags: tuple[str, ...]
    translated_optional_tags: tuple[str, ...]
    translated_excluded_tags: tuple[str, ...]
    discarded_tags: tuple[str, ...]
    number: int
    mode: str
    page: int
    parse_notice: str = ""

    @property
    def search_word(self) -> str:
        """Pixiv API 的基础搜索词；可选标签不会变成必须条件。"""

        return " ".join(self.translated_required_tags)


@dataclass(slots=True, frozen=True)
class TagSearchEntry:
    """搜索结果中按当前页重新编号的一项作品。"""

    position: int
    illust_id: int | None
    artwork: object | None


@dataclass(slots=True, frozen=True)
class TagSearchPage:
    """一个 Pixiv 搜索页及其实际可见作品。"""

    entries: tuple[TagSearchEntry, ...]
    page: int
    exhausted: bool
    used_search_word: str
    fallback_notice: str = ""


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"\d+", value))


def _positive_number(raw: str, label: str) -> int:
    if not _is_number(raw) or int(raw) <= 0:
        raise ValueError(f"{label}必须是正整数。")
    return int(raw)


def _split_control_suffix(tokens: list[str]) -> tuple[list[str], list[str]]:
    controls: list[str] = []
    while tokens and _is_number(tokens[-1]):
        controls.insert(0, tokens.pop())
    if len(controls) > 3:
        raise ValueError(
            "用法：/pt <标签> [N] [0|1] [搜索页]；最多只能包含数量/排名、模式和页码。"
        )
    return tokens, controls


def _dedupe_tokens(tokens: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    discarded: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        prefix = token[0] if token[:1] in {"+", "-", "＋", "－", "—", "–"} else ""
        value = token[1:].strip() if prefix else token.strip()
        if not value:
            raise ValueError("标签不能只有 + 或 - 符号。")
        if not any(character.isalnum() for character in value):
            raise ValueError(f"标签 {value!r} 不包含可搜索的文字或数字。")
        normalized = value.casefold()
        key = f"{prefix}:{normalized}"
        if key in seen:
            continue
        seen.add(key)
        if len(kept) < MAX_TAGS:
            kept.append(prefix + value)
        else:
            discarded.append(prefix + value)
    return kept, discarded


def _tokenize_tag_query(raw: str) -> list[str]:
    """解析空格、逗号和引号；引号让英文短语作为一个标签。"""

    normalized = str(raw or "").strip().replace("，", ",").replace("、", ",")
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=",")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return [item for item in lexer if item != ","]
    except ValueError as exc:
        raise ValueError("标签引号没有正确闭合，请成对使用英文引号。") from exc


def parse_tag_command_args(
    raw: str,
    *,
    translate_enabled: bool = True,
    alias_provider: TagAliasProvider | None = None,
) -> TagSearchQuery:
    """解析 `/pt` 标签、模式和搜索结果页。

    两个数字后缀中，第二个数字只有 0/1 才被当作模式；否则按页码解释，
    这是为了兼容用户输入 `/pt 黑发 2 2` 时忘记模式参数的情况。
    """

    tokens = _tokenize_tag_query(raw)
    if not tokens:
        raise ValueError("请至少提供一个标签，例如 /pt 黑发。")

    tokens, controls = _split_control_suffix(tokens)
    if not tokens:
        raise ValueError("请至少提供一个标签，不能只填写数字参数。")

    number = 1
    mode = "pick"
    page = 1
    parse_notice = ""
    if len(controls) == 1:
        number = _positive_number(controls[0], "数量或排名")
    elif len(controls) == 2:
        number = _positive_number(controls[0], "数量或排名")
        if controls[1] in {"0", "1"}:
            mode = "latest" if controls[1] == "1" else "pick"
        else:
            page = _positive_number(controls[1], "搜索页")
            parse_notice = "未填写模式参数，已按默认 pick 模式解析最后一个数字为搜索页。"
    elif len(controls) == 3:
        number = _positive_number(controls[0], "数量或排名")
        if controls[1] not in {"0", "1"}:
            raise ValueError("模式参数只能是 0（pick）或 1（latest）。")
        mode = "latest" if controls[1] == "1" else "pick"
        page = _positive_number(controls[2], "搜索页")

    if mode == "latest" and number > MAX_TAG_LATEST:
        raise ValueError("latest 模式一次最多返回 20 个作品。")
    if page > MAX_SEARCH_PAGE:
        raise ValueError(f"搜索页不能超过 {MAX_SEARCH_PAGE}。")

    kept, discarded = _dedupe_tokens(tokens)
    required: list[str] = []
    optional: list[str] = []
    excluded: list[str] = []
    for token in kept:
        prefix = token[:1]
        value = token[1:] if prefix in {"+", "-", "＋", "－", "—", "–"} else token
        if prefix in {"-", "－", "—", "–"}:
            excluded.append(value.strip())
        elif prefix in {"+", "＋"}:
            optional.append(value.strip())
        else:
            required.append(value.strip())

    if not required and optional:
        required.append(optional.pop(0))
        parse_notice = (parse_notice + " " if parse_notice else "") + (
            "未提供普通标签，已将第一个 +标签作为基础搜索词。"
        )
    if not required:
        raise ValueError("请至少提供一个普通标签或 +标签。")

    required_keys = {item.casefold() for item in required}
    optional_keys = {item.casefold() for item in optional}
    excluded_keys = {item.casefold() for item in excluded}
    conflicts = (required_keys | optional_keys) & excluded_keys
    if conflicts:
        names = [
            item
            for item in (*required, *optional, *excluded)
            if item.casefold() in conflicts
        ]
        raise ValueError("标签不能同时包含并排除：" + "、".join(dict.fromkeys(names)))
    optional = [item for item in optional if item.casefold() not in required_keys]

    alias = alias_provider or BuiltinTagAliasProvider()
    translate = alias.translate if translate_enabled else lambda value: value
    return TagSearchQuery(
        required_tags=tuple(required),
        optional_tags=tuple(optional),
        excluded_tags=tuple(excluded),
        translated_required_tags=tuple(translate(item) for item in required),
        translated_optional_tags=tuple(translate(item) for item in optional),
        translated_excluded_tags=tuple(translate(item) for item in excluded),
        discarded_tags=tuple(discarded),
        number=number,
        mode=mode,
        page=page,
        parse_notice=parse_notice.strip(),
    )


def has_tag(artwork: object, candidates: tuple[str, ...]) -> bool:
    """检查作品原始标签或翻译标签是否命中候选。"""

    if not candidates:
        return False
    tags = getattr(artwork, "tags", ()) or ()
    translated_tags = getattr(artwork, "translated_tags", ()) or ()
    wanted = {item.casefold() for item in candidates}
    return any(
        str(tag or "").strip().casefold() in wanted
        for tag in (*tags, *translated_tags)
    )
