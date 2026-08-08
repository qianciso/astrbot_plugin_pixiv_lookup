"""从 Pixiv-Shaft 词典生成插件使用的常用属性标签 TSV。

运行时插件不会联网。本脚本只在维护词典时使用，并固定读取经过审核的上游提交。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
import urllib.request
from pathlib import Path

UPSTREAM_COMMIT = "0281abe3864612ecb88aac3df3ce0f87c531bd38"
UPSTREAM_URL = (
    "https://raw.githubusercontent.com/CeuiLiSA/Pixiv-Shaft/"
    f"{UPSTREAM_COMMIT}/app/src/google/assets/synonym_dict_builtin.json"
)

# 只选取常用、适合公开环境的属性类标签，不导入角色、作品和成人词条。
SELECTED_TARGETS = (
    # 发色与发型
    "黒髪",
    "白髪",
    "銀髪",
    "金髪",
    "赤髪",
    "青髪",
    "ピンク髪",
    "オレンジ髪",
    "ロングヘア",
    "ショートヘア",
    "セミロング",
    "ツインテール",
    "ポニーテール",
    "おさげ",
    "三つ編み",
    "アホ毛",
    # 瞳色
    "碧眼",
    "赤目",
    "紫目",
    "オッドアイ",
    # 配饰与身体特征
    "眼鏡",
    "リボン",
    "カチューシャ",
    "イヤリング",
    "ピアス",
    "首輪",
    "マスク",
    "手袋",
    "猫耳",
    "犬耳",
    "狐耳",
    "うさみみ",
    "角",
    "翼",
    "尻尾",
    # 服装
    "制服",
    "セーラー服",
    "スーツ",
    "ドレス",
    "パーカー",
    "水着",
    "ビキニ",
    "浴衣",
    "着物",
    "メイド服",
    "巫女",
    "軍服",
    "エプロン",
    "ニーソックス",
    "タイツ",
    "ハイヒール",
    "ブーツ",
    # 表情、姿态与构图
    "笑顔",
    "ウィンク",
    "赤面",
    "横顔",
    "後ろ姿",
    "見上げる",
    "ピースサイン",
    "風景",
    "背景",
    "モノクロ",
    "ハイアングル",
)

# 上游分组中少量别名过宽或语义不同，不适合作为自动搜索转换。
EXCLUDED_ALIASES = {
    "ドレス": {"スカート", "skirt", "裙子"},
    "リボン": {"莉寶"},
    "角": {"号角"},
    "ブーツ": {"靴"},
    "タイツ": {"tight"},
    "猫耳": {"cat girl"},
    "マスク": {"仮面"},
    "尻尾": {"尾"},
    "横顔": {"profile"},
}

# Shaft 没有覆盖或其目标不适合作为搜索词的少量人工审核映射。
MANUAL_GROUPS = {
    "茶髪": ("茶发", "茶髮", "棕发", "棕髮", "brown hair", "brunette"),
    "緑髪": ("绿发", "綠髮", "green hair"),
    "紫髪": ("紫发", "紫髮", "purple hair"),
    "緑眼": ("绿眼", "綠眼", "绿色眼睛", "green eyes"),
    "金眼": ("金色眼睛", "金色瞳孔", "golden eyes"),
    "帽子": ("帽子", "hat"),
    "ヘアピン": ("发夹", "髮夾", "hairpin", "hair clip"),
    "ストッキング": ("丝袜", "絲襪", "stockings"),
    "白衣": ("白大褂", "实验服", "實驗服", "lab coat"),
    "ジャージ": ("运动服", "運動服", "tracksuit"),
    "全身": ("全身像", "full body"),
    "逆光": ("逆光", "backlighting"),
    "シルエット": ("剪影", "silhouette"),
    "泣き顔": ("哭脸", "哭臉", "crying face"),
    "寝顔": ("睡颜", "睡顏", "sleeping face"),
    "見返り": ("回头", "回頭", "looking back"),
    "腕組み": ("抱臂", "双臂交叉", "雙臂交叉", "arms crossed"),
    "指差し": ("指向", "指着", "pointing"),
    # 保留 v1.2 已提供的两个作品/角色兼容映射。
    "アズールレーン": ("碧蓝航线", "碧藍航線", "Azur Lane"),
    "アーミヤ(アークナイツ)": ("阿米娅", "阿米婭", "Amiya"),
}

_KANA_OR_HANGUL = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
_HAN = re.compile(r"[\u3400-\u9fff]")
_ASCII_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _&+./'()\-]*")


def normalize(value: str) -> str:
    """统一全半角、首尾空白和英文大小写，用于重复检测。"""

    return unicodedata.normalize("NFKC", value).strip().casefold()


def display_alias(value: str) -> str:
    """规范 TSV 展示文本；纯 ASCII 别名统一小写，便于人工维护。"""

    value = unicodedata.normalize("NFKC", value).strip()
    return value.lower() if value.isascii() else value


def is_supported_alias(value: str) -> bool:
    """只保留中文或英文别名；日文原词无需转换，韩文暂不收录。"""

    value = unicodedata.normalize("NFKC", value).strip()
    if not value or "\t" in value or "\n" in value or _KANA_OR_HANGUL.search(value):
        return False
    return bool(_HAN.search(value) or _ASCII_ALIAS.fullmatch(value))


def load_source(path: Path | None, proxy: str) -> dict:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        UPSTREAM_URL,
        headers={"User-Agent": "AstrBot-Pixiv-Lookup-Dictionary-Builder/1.2"},
    )
    with opener.open(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def build_rows(source: dict) -> tuple[list[tuple[str, str]], list[str]]:
    targets = {
        str(item.get("name") or "").strip(): item
        for item in source.get("targets", [])
        if isinstance(item, dict)
    }
    missing = [name for name in SELECTED_TARGETS if name not in targets]
    if missing:
        raise ValueError("上游词典缺少已选择目标：" + "、".join(missing))

    candidates: list[tuple[str, str]] = []
    for target_name in SELECTED_TARGETS:
        excluded = {normalize(item) for item in EXCLUDED_ALIASES.get(target_name, set())}
        for item in targets[target_name].get("synonyms") or []:
            alias = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
            if is_supported_alias(alias) and normalize(alias) not in excluded:
                candidates.append((alias, target_name))
    for target_name, aliases in MANUAL_GROUPS.items():
        candidates.extend((alias, target_name) for alias in aliases)

    resolved: dict[str, tuple[str, str]] = {}
    conflicts: dict[str, set[str]] = {}
    for alias, target in candidates:
        key = normalize(alias)
        current = resolved.get(key)
        if current is None:
            resolved[key] = (display_alias(alias), target.strip())
        elif current[1] != target:
            conflicts.setdefault(key, {current[1]}).add(target)

    for key in conflicts:
        resolved.pop(key, None)
    rows = sorted(resolved.values(), key=lambda item: (normalize(item[1]), normalize(item[0])))
    conflict_messages = [
        f"{key}: {' | '.join(sorted(values))}"
        for key, values in sorted(conflicts.items())
    ]
    return rows, conflict_messages


def write_tsv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(("alias", "target"))
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="本地 Shaft JSON；省略则下载固定版本")
    parser.add_argument("--proxy", default="", help="可选 HTTP 代理，例如 http://127.0.0.1:7890")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tag_aliases.tsv",
    )
    args = parser.parse_args()

    source = load_source(args.source, args.proxy)
    rows, conflicts = build_rows(source)
    write_tsv(args.output, rows)
    print(f"已生成 {args.output}：{len(rows)} 条映射")
    if conflicts:
        print("以下歧义别名未写入：")
        for item in conflicts:
            print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
