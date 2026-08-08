"""AstrBot 插件入口。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .exceptions import (
    ArtistNotFoundError,
    ArtworkNotFoundError,
    BatchMessageSendError,
    ConfigurationError,
    ImageDownloadError,
    MessageSendError,
    MetadataError,
    ProviderError,
)
from .formatting import format_artwork_info
from .image_proxy import PixivImageProxy
from .messaging import OneBotMessageSender
from .models import ArtistProfile, ArtworkMessageItem, Rating
from .policy import R18ContentPolicy
from .provider import PixivProvider
from .recall import RecallManager
from .storage import PluginLogManager
from .tag_search import BuiltinTagAliasProvider, TagSearchQuery, parse_tag_command_args

PLUGIN_NAME = "astrbot_plugin_pixiv_lookup"
PLUGIN_VERSION = "v1.2.0"
DEFAULT_COMMAND = "pi"
DEFAULT_ARTIST_COMMAND = "pa"
DEFAULT_TAG_COMMAND = "pt"
DEFAULT_HELP_COMMAND = "phelp"
DEFAULT_ARTIST_MAX_RESULTS = 10
MAX_ARTIST_MAX_RESULTS = 20
TEMP_ARTWORK_COMMAND = "pixiv_lookup_internal_pi_12"
TEMP_ARTIST_COMMAND = "pixiv_lookup_internal_pa_12"
TEMP_TAG_COMMAND = "pixiv_lookup_internal_pt_12"
COMMAND_PATTERN = re.compile(r"^[^\s/]{1,32}$")

# 配置页使用 AstrBot 的 object/items 原生分组。这里集中维护字段归属，既用于
# 运行时读取，也用于把 v1.0-v1.2 早期版本的扁平配置无损迁移到新结构。
CONFIG_GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "global_settings": (
        "r18_enabled",
        "r18g_enabled",
        "r18_recall_seconds",
        "image_size",
        "send_as_forward",
        "primary_image_proxy",
        "request_timeout",
        "log_retention_days",
    ),
    "pixiv_connection": (
        "pixiv_refresh_token",
        "pixiv_api_proxy",
    ),
    "artwork_query": ("command_name",),
    "artist_query": (
        "artist_command_name",
        "artist_max_results",
    ),
    "tag_query": (
        "tag_command_name",
        "tag_search_target",
        "tag_allow_ai",
        "tag_sort",
        "tag_popular_fallback_enabled",
        "tag_popular_bookmark_threshold",
        "tag_translate_enabled",
    ),
}
CONFIG_KEY_GROUP = {
    key: group
    for group, keys in CONFIG_GROUP_FIELDS.items()
    for key in keys
}


def normalize_command_name(raw: object) -> str:
    """配置页允许填写 pi 或 /pi，内部统一保存为不带唤醒前缀的片段。"""

    value = str(raw or "").strip()
    if value.startswith("/"):
        value = value[1:]
    if not COMMAND_PATTERN.fullmatch(value):
        raise ValueError("命令只能是 1-32 个不含空格和斜杠的字符")
    return value


def parse_command_args(raw: str) -> tuple[int, int]:
    """解析 `/pi <作品ID> [页码]` 中由 GreedyStr 接收的参数。"""

    args = raw.split()
    if not 1 <= len(args) <= 2:
        raise ValueError("用法：/pi <作品ID> [页码]，页码默认为 1。")
    if not args[0].isdigit() or len(args[0]) > 20 or int(args[0]) <= 0:
        raise ValueError("作品 ID 必须是正整数。")
    if len(args) == 2 and (not args[1].isdigit() or int(args[1]) <= 0):
        raise ValueError("页码必须是从 1 开始的正整数。")
    return int(args[0]), int(args[1]) if len(args) == 2 else 1


@dataclass(slots=True, frozen=True)
class ArtistQuery:
    """标准化后的画师查询参数。"""

    artist_id: int
    number: int
    mode: str


def _parse_positive_id(raw: str, label: str) -> int:
    if not raw.isdigit() or len(raw) > 20 or int(raw) <= 0:
        raise ValueError(f"{label}必须是正整数。")
    return int(raw)


def _parse_artist_latest_count(raw: str, latest_limit: int) -> int:
    """解析 latest 返回数量；上限来自配置，但插件硬上限始终为 20。"""

    if not raw.isdigit() or len(raw) > 20:
        raise ValueError(f"latest 模式的返回数量 N 必须是 1-{latest_limit} 的整数。")
    number = int(raw)
    if not 1 <= number <= latest_limit:
        raise ValueError(f"latest 模式的返回数量 N 必须是 1-{latest_limit} 的整数。")
    return number


def _parse_artist_pick_position(raw: str) -> int:
    """解析 pick 排名；它不是返回数量，因此不受批量返回上限影响。"""

    if not raw.isdigit() or len(raw) > 20 or int(raw) <= 0:
        raise ValueError("pick 模式的作品位置 N 必须是正整数。")
    return int(raw)


def parse_artist_command_args(
    raw: str,
    latest_limit: int = DEFAULT_ARTIST_MAX_RESULTS,
) -> ArtistQuery:
    """解析 `/pa` 的数字模式与可读模式，并统一为 latest/pick。"""

    try:
        configured_limit = int(latest_limit)
    except (TypeError, ValueError):
        configured_limit = DEFAULT_ARTIST_MAX_RESULTS
    latest_limit = max(1, min(configured_limit, MAX_ARTIST_MAX_RESULTS))

    args = raw.split()
    usage = (
        "用法：/pa <画师ID> [N] [1|0]，或 /pa <画师ID> "
        "latest [N]、/pa <画师ID> pick <N>。"
    )
    if not 1 <= len(args) <= 3:
        raise ValueError(usage)
    artist_id = _parse_positive_id(args[0], "画师 ID ")
    if len(args) == 1:
        return ArtistQuery(artist_id, 1, "latest")

    keyword = args[1].casefold()
    if keyword == "latest":
        if len(args) == 2:
            return ArtistQuery(artist_id, 1, "latest")
        return ArtistQuery(
            artist_id,
            _parse_artist_latest_count(args[2], latest_limit),
            "latest",
        )
    if keyword == "pick":
        if len(args) != 3:
            raise ValueError("pick 模式必须指定第 N 个最新作品，例如 /pa 123 pick 3。")
        return ArtistQuery(artist_id, _parse_artist_pick_position(args[2]), "pick")

    if len(args) == 2:
        return ArtistQuery(
            artist_id,
            _parse_artist_latest_count(args[1], latest_limit),
            "latest",
        )
    if args[2] not in {"0", "1"}:
        raise ValueError("模式参数只能是 1（最新 N 个）或 0（第 N 个最新作品）。")
    if args[2] == "1":
        number = _parse_artist_latest_count(args[1], latest_limit)
        return ArtistQuery(artist_id, number, "latest")
    return ArtistQuery(artist_id, _parse_artist_pick_position(args[1]), "pick")


class PixivLookupPlugin(Star):
    """按作品 ID、画师 ID 或标签查询 Pixiv 并通过 QQ 发送。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._migrate_grouped_config()
        self.provider: PixivProvider | None = None
        self.image_proxy: PixivImageProxy | None = None
        self.sender = OneBotMessageSender()
        self.policy = R18ContentPolicy()
        self.tag_alias_provider = BuiltinTagAliasProvider()
        self.file_logs: PluginLogManager | None = None
        self.recall_manager: RecallManager | None = None
        self._command_records: set[str] = set()
        self._active_commands = {
            "artwork": DEFAULT_COMMAND,
            "artist": DEFAULT_ARTIST_COMMAND,
            "tag": DEFAULT_TAG_COMMAND,
            "help": DEFAULT_HELP_COMMAND,
        }

    def _migrate_grouped_config(self) -> None:
        """把旧版顶层配置移动到 UI 分组中，并尽可能保存迁移结果。"""

        if not isinstance(self.config, MutableMapping):
            return
        moved = 0
        for group_name, keys in CONFIG_GROUP_FIELDS.items():
            current_group = self.config.get(group_name)
            group = dict(current_group) if isinstance(current_group, Mapping) else {}
            changed = False
            for key in keys:
                if key not in self.config:
                    continue
                group[key] = self.config.pop(key)
                moved += 1
                changed = True
            if changed:
                self.config[group_name] = group
        if not moved:
            return
        persisted = False
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
                persisted = True
            except Exception as exc:
                logger.warning(
                    "[PixivLookup] 配置分组迁移保存失败: "
                    f"error_type={type(exc).__name__}"
                )
        logger.info(
            f"[PixivLookup] 已迁移旧版配置到分组界面: fields={moved}, "
            f"persisted={persisted}"
        )

    def _cfg_value(self, key: str, default=None):
        """优先读取分组字段，并兼容尚未迁移的旧版顶层配置。"""

        group_name = CONFIG_KEY_GROUP.get(key)
        if group_name:
            group = self.config.get(group_name, {})
            if isinstance(group, Mapping) and key in group:
                return group[key]
        return self.config.get(key, default)

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._cfg_value(key, default) or default).strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg_value(key, default)
        if isinstance(value, str):
            return value.casefold() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._cfg_value(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _handler_full_name(handler=None) -> str:
        handler = handler or PixivLookupPlugin.pixiv_lookup
        return f"{handler.__module__}_{handler.__name__}"

    def _configured_command(self, key: str, default: str) -> str:
        try:
            return normalize_command_name(self._cfg_value(key, default))
        except ValueError as exc:
            logger.error(f"[PixivLookup] {key} 无效，继续使用 /{default}: {exc}")
            if self.file_logs:
                self.file_logs.warning(
                    "command_invalid",
                    config_key=key,
                    error_type=type(exc).__name__,
                )
            return default

    async def _register_command(self, handler, command: str, default: str) -> str:
        """注册可重命名命令，并返回最终用于帮助信息的命令名。"""

        handler_name = self._handler_full_name(handler)
        try:
            from astrbot.core.star.command_management import rename_command

            await rename_command(handler_name, command, aliases=[])
            self._command_records.add(handler_name)
            logger.info(f"[PixivLookup] 命令已注册为 /{command}")
            return command
        except Exception as exc:
            logger.error(
                f"[PixivLookup] 命令 /{command} 注册失败，继续使用 /{default}: "
                f"{type(exc).__name__}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "command_rename_failed",
                    command=command,
                    error_type=type(exc).__name__,
                )
            if command != default:
                # 自定义名称冲突时显式恢复原生命令。这样即使上次进程异常退出并
                # 留下了命令数据库记录，本次加载也不会继续使用过期名称。
                try:
                    from astrbot.core.star.command_management import rename_command

                    await rename_command(handler_name, default, aliases=[])
                    self._command_records.add(handler_name)
                    return default
                except Exception as fallback_exc:
                    logger.error(
                        f"[PixivLookup] 恢复 /{default} 失败: "
                        f"{type(fallback_exc).__name__}"
                    )
                    if self.file_logs:
                        self.file_logs.warning(
                            "command_fallback_failed",
                            command=default,
                            error_type=type(fallback_exc).__name__,
                        )
            return default

    async def _park_command(self, handler, temporary_command: str) -> None:
        """先移开原生命令，允许用户安全地交换三个查询命令名称。"""

        handler_name = self._handler_full_name(handler)
        try:
            from astrbot.core.star.command_management import rename_command

            await rename_command(handler_name, temporary_command, aliases=[])
            self._command_records.add(handler_name)
        except Exception as exc:
            logger.warning(
                f"[PixivLookup] 临时迁移命令失败: error_type={type(exc).__name__}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "command_parking_failed",
                    error_type=type(exc).__name__,
                )

    async def _configure_commands(self) -> None:
        artwork_command = self._configured_command("command_name", DEFAULT_COMMAND)
        artist_command = self._configured_command(
            "artist_command_name",
            DEFAULT_ARTIST_COMMAND,
        )
        tag_command = self._configured_command("tag_command_name", DEFAULT_TAG_COMMAND)
        configured = [artwork_command, artist_command, tag_command]
        defaults = [DEFAULT_COMMAND, DEFAULT_ARTIST_COMMAND, DEFAULT_TAG_COMMAND]
        for index, command in enumerate(configured):
            if command.casefold() != DEFAULT_HELP_COMMAND.casefold():
                continue
            logger.error(
                f"[PixivLookup] /{command} 是保留的帮助命令，"
                f"已恢复 /{defaults[index]}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "command_reserved_conflict",
                    command=command,
                    fallback=defaults[index],
                )
            configured[index] = defaults[index]
        artwork_command, artist_command, tag_command = configured
        if len({item.casefold() for item in configured}) != len(configured):
            logger.error(
                "[PixivLookup] 作品、画师和标签查询命令不能同名，已恢复 /pi、/pa、/pt"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "command_group_conflict",
                    commands=tuple(configured),
                )
            artwork_command = DEFAULT_COMMAND
            artist_command = DEFAULT_ARTIST_COMMAND
            tag_command = DEFAULT_TAG_COMMAND
        await self._park_command(
            PixivLookupPlugin.pixiv_lookup,
            TEMP_ARTWORK_COMMAND,
        )
        await self._park_command(
            PixivLookupPlugin.artist_lookup,
            TEMP_ARTIST_COMMAND,
        )
        await self._park_command(
            PixivLookupPlugin.tag_lookup,
            TEMP_TAG_COMMAND,
        )
        self._active_commands["artwork"] = await self._register_command(
            PixivLookupPlugin.pixiv_lookup,
            artwork_command,
            DEFAULT_COMMAND,
        )
        self._active_commands["artist"] = await self._register_command(
            PixivLookupPlugin.artist_lookup,
            artist_command,
            DEFAULT_ARTIST_COMMAND,
        )
        self._active_commands["tag"] = await self._register_command(
            PixivLookupPlugin.tag_lookup,
            tag_command,
            DEFAULT_TAG_COMMAND,
        )

    async def initialize(self) -> None:
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        retention = self._cfg_int("log_retention_days", 7, 1, 30)
        self.file_logs = PluginLogManager(data_dir, retention)
        removed = await asyncio.to_thread(self.file_logs.cleanup_once)
        self.file_logs.start()
        if removed:
            self.file_logs.info("startup_log_cleanup", removed=removed)
        if self.tag_alias_provider.load_error is None:
            self.file_logs.info(
                "tag_alias_dictionary_loaded",
                entries=len(self.tag_alias_provider.aliases),
            )
        else:
            self.file_logs.warning(
                "tag_alias_dictionary_fallback",
                error_type=type(self.tag_alias_provider.load_error).__name__,
            )

        timeout = float(self._cfg_int("request_timeout", 30, 5, 120))
        self.provider = PixivProvider(
            refresh_token=self._cfg_str("pixiv_refresh_token"),
            api_proxy=self._cfg_str("pixiv_api_proxy"),
            timeout=timeout,
        )
        self.image_proxy = PixivImageProxy(
            primary_host=self._cfg_str("primary_image_proxy", "i.pixiv.re"),
            timeout=timeout,
        )
        self.recall_manager = RecallManager(
            self.sender,
            self.file_logs.info,
            self.file_logs.warning,
        )
        await self._configure_commands()
        self.file_logs.info("plugin_initialized", version=PLUGIN_VERSION)
        logger.info(f"[PixivLookup] 插件已加载，版本 {PLUGIN_VERSION}")

    def _rejection_reason(self, artwork) -> str | None:
        return self.policy.rejection_reason(
            artwork,
            self._cfg_bool("r18_enabled", False),
            self._cfg_bool("r18g_enabled", False),
        )

    @staticmethod
    def _batch_send_failure_text(
        content_name: str,
        message_ids: tuple[str, ...],
        *,
        sensitive: bool,
    ) -> str:
        """根据已确认的消息 ID 区分部分失败与整批状态未知。"""

        if message_ids:
            suffix = "；其余作品发送失败或状态未知"
            if sensitive:
                suffix += "，请检查会话中是否有未能自动撤回的图片"
            return f"{content_name}仅部分确认发送成功{suffix}"

        if sensitive:
            return (
                f"{content_name}发送失败或状态未知：QQ/NapCat 发送图片时发生错误或超时，"
                "插件未取得消息 ID，无法安排自动撤回，请立即检查会话"
            )
        return (
            f"{content_name}发送失败或状态未知："
            "QQ/NapCat 未返回可确认的发送结果，请稍后重试"
        )

    def _log_batch_send_failure(
        self,
        query_type: str,
        content_type: str,
        exc: BatchMessageSendError,
    ) -> None:
        """只记录安全的异常类型和成功批次数，不写图片或 OneBot 载荷。"""

        if self.file_logs is None:
            return
        cause = exc.__cause__
        self.file_logs.warning(
            "batch_message_send_failed",
            query_type=query_type,
            content_type=content_type,
            confirmed_messages=len(exc.message_ids),
            error_type=type(cause).__name__ if cause is not None else type(exc).__name__,
        )

    async def _schedule_recalls(
        self,
        event: AstrMessageEvent,
        message_ids: tuple[str, ...] | list[str],
    ) -> None:
        """为敏感消息安排撤回；管理器缺失时立即撤回已经发送的消息。"""

        if not message_ids:
            return
        self_id = str(event.get_self_id() or "")
        if self.recall_manager is None:
            for message_id in message_ids:
                await self.sender.recall(event.bot, message_id, self_id)
            raise MessageSendError("撤回管理器不可用，敏感消息已立即撤回")
        delay = self._cfg_int("r18_recall_seconds", 120, 5, 120)
        for message_id in message_ids:
            self.recall_manager.schedule(event.bot, message_id, self_id, delay)

    def _help_text(self) -> str:
        """生成面向普通用户的完整命令说明，并展示当前实际命令名。"""

        artwork = f"/{self._active_commands['artwork']}"
        artist = f"/{self._active_commands['artist']}"
        tag = f"/{self._active_commands['tag']}"
        help_command = f"/{self._active_commands['help']}"
        artist_limit = self._cfg_int(
            "artist_max_results",
            DEFAULT_ARTIST_MAX_RESULTS,
            1,
            MAX_ARTIST_MAX_RESULTS,
        )
        return (
            f"Pixiv 查询 {PLUGIN_VERSION} 指令帮助\n\n"
            f"1. 作品 ID 查询\n"
            f"格式：{artwork} <作品ID> [图片页码]\n"
            f"说明：图片页码从 1 开始，省略时返回第 1 幅。\n"
            f"示例：{artwork} 12345678\n"
            f"      {artwork} 12345678 2\n\n"
            f"2. 画师查询\n"
            f"格式：{artist} <画师ID> [数量或排名] [1|0]\n"
            f"      {artist} <画师ID> latest [数量]\n"
            f"      {artist} <画师ID> pick <排名>\n"
            f"说明：1/latest 返回最新 N 个作品，当前上限为 {artist_limit}；"
            f"0/pick 返回第 N 个最新作品，不受批量上限限制。省略参数时返回最新 1 个。\n"
            f"示例：{artist} 123456 5 1\n"
            f"      {artist} 123456 pick 13\n\n"
            f"3. 标签查询\n"
            f"格式：{tag} <标签...> [数量或排名] [1|0] [搜索页]\n"
            f"说明：标签可用空格、中英文逗号分隔；普通标签必须包含，+标签为可选，"
            f"-标签会被排除。0/pick 返回当前搜索页第 N 项，1/latest 返回前 N 项"
            f"（最多 20 项）；每个搜索页最多 60 项。带空格的英文短语必须使用英文"
            f"引号。省略参数时返回第 1 页第 1 项。\n"
            f"示例：{tag} 黑发\n"
            f"      {tag} \"black hair\"\n"
            f"      {tag} 黑发 +蓝发 -R18\n"
            f"      {tag} 黑发 3 1 2\n"
            f"      {tag} 黑发 2 2（按默认 pick 查询第 2 页第 2 项）\n"
            f"提示：标签或画师查询遇到多图作品时只发送首图，其他图片使用 "
            f"{artwork} <作品ID> <图片页码> 查询。\n\n"
            f"4. 查看本帮助\n"
            f"格式：{help_command}\n\n"
            "R18/R18G、AI、图片尺寸和发送方式由插件配置页控制。"
        )

    @filter.command("phelp")
    async def pixiv_help(self, event: AstrMessageEvent):
        """返回当前版本全部命令的格式、参数说明和示例。"""

        event.stop_event()
        yield event.plain_result(self._help_text())

    @filter.command("pi")
    async def pixiv_lookup(
        self,
        event: AstrMessageEvent,
        query: GreedyStr = GreedyStr,
    ):
        """查询 Pixiv 作品。用法：/pi <作品ID> [页码]"""

        event.stop_event()
        raw_query = "" if query is GreedyStr else str(query or "")
        try:
            illust_id, page_index = parse_command_args(raw_query)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        if self.provider is None or self.image_proxy is None:
            yield event.plain_result("插件尚未完成初始化，请稍后重试。")
            return

        try:
            artwork = await self.provider.get_artwork(illust_id)
            if page_index > artwork.page_count:
                yield event.plain_result(
                    f"该作品共有 {artwork.page_count} 幅，页码 {page_index} 超出范围。",
                )
                return

            rejection = self._rejection_reason(artwork)
            if rejection:
                if self.file_logs:
                    self.file_logs.info(
                        "content_blocked",
                        illust_id=illust_id,
                        rating=artwork.rating.value,
                    )
                yield event.plain_result(rejection)
                return

            preferred_quality = self._cfg_str("image_size", "large")
            image = await self.image_proxy.fetch(
                artwork.pages[page_index - 1],
                preferred_quality,
            )
            info_text = format_artwork_info(artwork, page_index, image.quality)
            page_text = f"Pixiv {artwork.illust_id}：第 {page_index}/{artwork.page_count} 幅"
            message_id = await self.sender.send_artwork(
                event,
                info_text,
                page_text,
                image,
                as_forward=self._cfg_bool("send_as_forward", True),
            )

            if artwork.rating.value in {"r18", "r18g"}:
                await self._schedule_recalls(event, (message_id,))

            if self.file_logs:
                self.file_logs.info(
                    "artwork_sent",
                    illust_id=illust_id,
                    page=page_index,
                    pages=artwork.page_count,
                    quality=image.quality,
                    proxy=image.proxy_host,
                    rating=artwork.rating.value,
                )
        except ConfigurationError:
            yield event.plain_result(
                "Pixiv 尚未配置完成：请在插件配置中填写 refresh token；"
                "大陆网络如无法访问 Pixiv API，还需填写 HTTP 代理。",
            )
        except ArtworkNotFoundError:
            yield event.plain_result("未找到该作品：作品可能不存在、已删除或当前账号不可见。")
        except MetadataError:
            yield event.plain_result("作品元数据不完整，无法安全确认分级或图片地址，已停止发送。")
        except ProviderError as exc:
            if self.file_logs:
                self.file_logs.warning(
                    "provider_failed",
                    illust_id=illust_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result(
                "Pixiv 元数据请求失败，请检查 refresh token、网络或 API 代理后重试。",
            )
        except ImageDownloadError as exc:
            if self.file_logs:
                self.file_logs.warning(
                    "image_download_failed",
                    illust_id=illust_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result(
                "图片获取失败：i.pixiv.re 与 i.pixiv.nl 均不可用，或返回内容无效。",
            )
        except MessageSendError as exc:
            if self.file_logs:
                self.file_logs.warning(
                    "message_send_failed",
                    illust_id=illust_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result(
                "QQ 消息发送或撤回保障失败，请检查 NapCat/OneBot 连接与机器人权限。",
            )
        except Exception as exc:
            logger.error(
                f"[PixivLookup] 未预期错误: illust_id={illust_id} "
                f"error_type={type(exc).__name__}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "unexpected_error",
                    illust_id=illust_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result("处理作品时发生未预期错误，请查看插件日志。")

    @staticmethod
    def _artist_summary(profile: ArtistProfile, query: ArtistQuery) -> str:
        """格式化可安全单独发送的画师资料，不包含任何作品图片。"""

        artist = f"画师：{profile.name}（ID {profile.user_id}"
        if profile.account:
            artist += f"，账号 {profile.account}"
        artist += "）"
        if query.mode == "latest":
            query_text = f"查询：最新 {query.number} 个插画/动图作品"
        else:
            query_text = f"查询：第 {query.number} 个最新插画/动图作品"
        return f"{artist}\n{query_text}"

    @classmethod
    def _artist_header(
        cls,
        profile: ArtistProfile,
        query: ArtistQuery,
        group_name: str,
    ) -> str:
        return f"{cls._artist_summary(profile, query)}\n分组：{group_name}"

    @filter.command("pa")
    async def artist_lookup(
        self,
        event: AstrMessageEvent,
        query: GreedyStr = GreedyStr,
    ):
        """查询画师最新插画。用法：/pa <画师ID> [N] [1|0]。"""

        event.stop_event()
        raw_query = "" if query is GreedyStr else str(query or "")
        try:
            artist_query = parse_artist_command_args(
                raw_query,
                self._cfg_int(
                    "artist_max_results",
                    DEFAULT_ARTIST_MAX_RESULTS,
                    1,
                    MAX_ARTIST_MAX_RESULTS,
                ),
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        if self.provider is None or self.image_proxy is None:
            yield event.plain_result("插件尚未完成初始化，请稍后重试。")
            return

        artist_id = artist_query.artist_id
        try:
            if artist_query.mode == "pick":
                result = await self.provider.get_artist_artworks(
                    artist_id,
                    1,
                    start_position=artist_query.number,
                )
            else:
                result = await self.provider.get_artist_artworks(
                    artist_id,
                    artist_query.number,
                )
            if not result.entries:
                if artist_query.mode == "pick":
                    if result.profile.total_illusts is not None:
                        message = (
                            f"该画师当前只有 {result.profile.total_illusts} 个可见的插画/"
                            f"动图作品，无法返回第 {artist_query.number} 个最新作品。"
                        )
                    else:
                        message = f"无法返回该画师第 {artist_query.number} 个最新作品。"
                    yield event.plain_result(message)
                    return
                yield event.plain_result("该画师当前没有可见的插画或动图作品。")
                return

            if artist_query.mode == "pick":
                selected = [result.entries[0]]
            else:
                selected = list(result.entries[: artist_query.number])

            problems: list[str] = []
            if artist_query.mode == "latest" and len(result.entries) < artist_query.number:
                problems.append(
                    f"请求 {artist_query.number} 个作品，当前只有 {len(result.entries)} 个可见作品"
                )

            safe_items: list[ArtworkMessageItem] = []
            sensitive_items: list[ArtworkMessageItem] = []
            blocked: list[str] = []
            metadata_failed: list[str] = []
            download_failed: list[str] = []
            preferred_quality = self._cfg_str("image_size", "large")

            for entry in selected:
                id_text = str(entry.illust_id) if entry.illust_id is not None else "未知ID"
                artwork = entry.artwork
                if artwork is None:
                    metadata_failed.append(id_text)
                    continue
                rejection = self._rejection_reason(artwork)
                if rejection:
                    blocked.append(f"{artwork.illust_id}（{artwork.rating.display_name}）")
                    if self.file_logs:
                        self.file_logs.info(
                            "artist_content_blocked",
                            artist_id=artist_id,
                            illust_id=artwork.illust_id,
                            rating=artwork.rating.value,
                        )
                    continue
                try:
                    image = await self.image_proxy.fetch(
                        artwork.pages[0],
                        preferred_quality,
                    )
                except (ImageDownloadError, MetadataError) as exc:
                    download_failed.append(str(artwork.illust_id))
                    if self.file_logs:
                        self.file_logs.warning(
                            "artist_image_download_failed",
                            artist_id=artist_id,
                            illust_id=artwork.illust_id,
                            error_type=type(exc).__name__,
                        )
                    continue

                page_text = (
                    f"画师作品排名：第 {entry.position} 个最新作品；"
                    f"Pixiv {artwork.illust_id}：第 1/{artwork.page_count} 幅"
                )
                if artwork.page_count > 1:
                    page_text += f"；其他页可使用 /pi {artwork.illust_id} <页码> 查询"
                item = ArtworkMessageItem(
                    info_text=format_artwork_info(artwork, 1, image.quality),
                    page_text=page_text,
                    image=image,
                )
                if artwork.rating in {Rating.R18, Rating.R18G}:
                    sensitive_items.append(item)
                else:
                    safe_items.append(item)

            if blocked:
                problems.append("因分级设置跳过：" + "、".join(blocked))
            if metadata_failed:
                problems.append("元数据异常：" + "、".join(metadata_failed))
            if download_failed:
                problems.append("图片下载失败：" + "、".join(download_failed))

            safe_message_ids: tuple[str, ...] = ()
            sensitive_message_ids: tuple[str, ...] = ()
            if safe_items:
                try:
                    safe_message_ids = await self.sender.send_artworks(
                        event,
                        self._artist_header(result.profile, artist_query, "全年龄作品"),
                        safe_items,
                        as_forward=self._cfg_bool("send_as_forward", True),
                    )
                except BatchMessageSendError as exc:
                    safe_message_ids = exc.message_ids
                    self._log_batch_send_failure("artist", "safe", exc)
                    problems.append(
                        self._batch_send_failure_text(
                            "全年龄作品",
                            safe_message_ids,
                            sensitive=False,
                        ),
                    )
                except MessageSendError:
                    problems.append("全年龄作品发送失败")

            if sensitive_items:
                try:
                    sensitive_message_ids = await self.sender.send_artworks(
                        event,
                        self._artist_header(
                            result.profile,
                            artist_query,
                            "敏感作品（将自动撤回）",
                        ),
                        sensitive_items,
                        as_forward=self._cfg_bool("send_as_forward", True),
                    )
                except BatchMessageSendError as exc:
                    sensitive_message_ids = exc.message_ids
                    await self._schedule_recalls(event, sensitive_message_ids)
                    self._log_batch_send_failure("artist", "sensitive", exc)
                    problems.append(
                        self._batch_send_failure_text(
                            "敏感作品",
                            sensitive_message_ids,
                            sensitive=True,
                        ),
                    )
                except MessageSendError:
                    problems.append("敏感作品发送失败")
                else:
                    await self._schedule_recalls(event, sensitive_message_ids)

            if problems:
                prefix = ""
                if blocked and not (safe_items or sensitive_items):
                    # 所有可处理作品都被分级开关拦截时，画师资料仍可安全返回。
                    prefix = self._artist_summary(result.profile, artist_query) + "\n"
                yield event.plain_result(
                    prefix
                    + "画师查询提示：\n"
                    + "\n".join(f"- {item}" for item in problems),
                )

            if self.file_logs:
                self.file_logs.info(
                    "artist_query_completed",
                    artist_id=artist_id,
                    mode=artist_query.mode,
                    requested=artist_query.number,
                    candidates=len(selected),
                    safe_items=len(safe_items),
                    sensitive_items=len(sensitive_items),
                    blocked=len(blocked),
                    metadata_failed=len(metadata_failed),
                    download_failed=len(download_failed),
                    safe_messages=len(safe_message_ids),
                    sensitive_messages=len(sensitive_message_ids),
                )
        except ConfigurationError:
            yield event.plain_result(
                "Pixiv 尚未配置完成：请在插件配置中填写 refresh token；"
                "大陆网络如无法访问 Pixiv API，还需填写 HTTP 代理。",
            )
        except ArtistNotFoundError:
            yield event.plain_result("未找到该画师：账号可能不存在、已停用或当前账号不可见。")
        except ProviderError as exc:
            if self.file_logs:
                self.file_logs.warning(
                    "artist_provider_failed",
                    artist_id=artist_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result(
                "Pixiv 画师资料请求失败，请检查 refresh token、网络或 API 代理后重试。",
            )
        except MessageSendError as exc:
            if self.file_logs:
                self.file_logs.warning(
                    "artist_message_send_failed",
                    artist_id=artist_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result(
                "QQ 消息发送或撤回保障失败，请检查 NapCat/OneBot 连接与机器人权限。",
            )
        except Exception as exc:
            logger.error(
                f"[PixivLookup] 画师查询未预期错误: artist_id={artist_id} "
                f"error_type={type(exc).__name__}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "artist_unexpected_error",
                    artist_id=artist_id,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result("处理画师作品时发生未预期错误，请查看插件日志。")

    @staticmethod
    def _tag_header(query: TagSearchQuery, used_word: str, group_name: str) -> str:
        """构建标签搜索分组标题，明确展示用户词和实际搜索词。"""

        input_tags = "、".join(
            [
                *(query.required_tags),
                *(f"+{item}" for item in query.optional_tags),
                *(f"-{item}" for item in query.excluded_tags),
            ],
        )
        mode_text = "最新结果" if query.mode == "latest" else f"第 {query.number} 项"
        return (
            f"标签：{input_tags}\n"
            f"实际搜索词：{used_word}\n"
            f"搜索页：第 {query.page} 页；模式：{mode_text}\n"
            f"分组：{group_name}"
        )

    @staticmethod
    def _tag_sort_value(raw: str) -> str:
        value = str(raw or "").strip().casefold()
        return value if value in {"date_desc", "date_asc", "popular_desc"} else "date_desc"

    def _tag_popular_threshold(self) -> int:
        allowed = (100, 250, 500, 1000, 5000, 7500, 10000, 20000, 30000, 50000)
        try:
            value = int(self._cfg_value("tag_popular_bookmark_threshold", 500))
        except (TypeError, ValueError):
            value = 500
        return value if value in allowed else 500

    @filter.command("pt")
    async def tag_lookup(
        self,
        event: AstrMessageEvent,
        query: GreedyStr = GreedyStr,
    ):
        """按标签查询 Pixiv 作品。用法：/pt <标签> [N] [0|1] [搜索页]。"""

        event.stop_event()
        raw_query = "" if query is GreedyStr else str(query or "")
        try:
            tag_query = parse_tag_command_args(
                raw_query,
                translate_enabled=self._cfg_bool("tag_translate_enabled", True),
                alias_provider=self.tag_alias_provider,
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        if self.provider is None or self.image_proxy is None:
            yield event.plain_result("插件尚未完成初始化，请稍后重试。")
            return

        search_target = self._cfg_str("tag_search_target", "partial_match_for_tags")
        if search_target not in {
            "partial_match_for_tags",
            "exact_match_for_tags",
            "title_and_caption",
        }:
            search_target = "partial_match_for_tags"
        sort = self._tag_sort_value(self._cfg_str("tag_sort", "date_desc"))
        allow_ai = self._cfg_bool("tag_allow_ai", False)
        fallback_enabled = self._cfg_bool("tag_popular_fallback_enabled", True)
        threshold = self._tag_popular_threshold()
        base_word = tag_query.search_word.strip()
        # users入り 是 Pixiv 的普通标签。开关开启后直接把它作为独立标签追加到
        # 搜索词末尾，而不是等官方热门排序失败后才临时追加。
        has_direct_users_tag = "users入り" in base_word.casefold()
        auto_users_tag = fallback_enabled and not has_direct_users_tag
        used_word = (
            f"{base_word} {threshold}users入り"
            if auto_users_tag
            else base_word
        )
        fallback_notice = ""

        async def fetch(word: str, requested_sort: str):
            return await self.provider.search_tag_page(
                word,
                page=tag_query.page,
                search_target=search_target,
                sort=requested_sort,
                allow_ai=allow_ai,
                excluded_tags=tag_query.translated_excluded_tags,
            )

        try:
            try:
                result = await fetch(used_word, sort)
            except ProviderError:
                if sort != "popular_desc":
                    raise
                # 官方热门排序可能仅向会员开放。降级排序时沿用同一个搜索词，
                # 确保已启用的 users入り 收藏门槛不会被悄悄移除。
                result = await fetch(used_word, "date_desc")
                fallback_notice = "官方热门排序不可用，已改用日期降序。"

            if not result.entries:
                notice = f"未找到符合条件的作品。\n实际搜索词：{used_word}"
                if fallback_notice:
                    notice += "\n" + fallback_notice
                yield event.plain_result(notice)
                return

            if tag_query.mode == "pick":
                if tag_query.number > len(result.entries):
                    yield event.plain_result(
                        f"第 {tag_query.page} 页只有 {len(result.entries)} 个符合条件的作品，"
                        f"无法返回第 {tag_query.number} 项。",
                    )
                    return
                selected = [result.entries[tag_query.number - 1]]
            else:
                selected = list(result.entries[: tag_query.number])

            problems: list[str] = []
            if tag_query.mode == "latest" and len(result.entries) < tag_query.number:
                problems.append(
                    f"请求 {tag_query.number} 个作品，第 {tag_query.page} 页实际只有 "
                    f"{len(result.entries)} 个符合条件的作品"
                )
            if tag_query.parse_notice:
                problems.append(tag_query.parse_notice)
            if tag_query.discarded_tags:
                problems.append("已舍弃超过 10 个标签：" + "、".join(tag_query.discarded_tags))
            if fallback_notice:
                problems.append(fallback_notice)

            safe_items: list[ArtworkMessageItem] = []
            sensitive_items: list[ArtworkMessageItem] = []
            blocked: list[str] = []
            metadata_failed: list[str] = []
            download_failed: list[str] = []
            preferred_quality = self._cfg_str("image_size", "large")

            for entry in selected:
                id_text = str(entry.illust_id) if entry.illust_id is not None else "未知ID"
                artwork = entry.artwork
                if artwork is None:
                    metadata_failed.append(id_text)
                    continue
                rejection = self._rejection_reason(artwork)
                if rejection:
                    blocked.append(f"{artwork.illust_id}（{artwork.rating.display_name}）")
                    if self.file_logs:
                        self.file_logs.info(
                            "tag_content_blocked",
                            illust_id=artwork.illust_id,
                            rating=artwork.rating.value,
                        )
                    continue
                try:
                    # 标签搜索的最后一个数字是搜索结果页，多图作品固定发送首图。
                    image = await self.image_proxy.fetch(artwork.pages[0], preferred_quality)
                except (ImageDownloadError, MetadataError):
                    download_failed.append(id_text)
                    continue

                page_text = (
                    f"标签搜索结果：第 {entry.position} 项（搜索页 {tag_query.page}）；"
                    f"Pixiv {artwork.illust_id}：第 1/{artwork.page_count} 幅"
                )
                if artwork.page_count > 1:
                    page_text += f"；其他页可使用 /pi {artwork.illust_id} <页码> 查询"
                item = ArtworkMessageItem(
                    info_text=format_artwork_info(artwork, 1, image.quality),
                    page_text=page_text,
                    image=image,
                )
                if artwork.rating in {Rating.R18, Rating.R18G}:
                    sensitive_items.append(item)
                else:
                    safe_items.append(item)

            if blocked:
                problems.append("因分级设置跳过：" + "、".join(blocked))
            if metadata_failed:
                problems.append("元数据异常：" + "、".join(metadata_failed))
            if download_failed:
                problems.append("图片下载失败：" + "、".join(download_failed))

            safe_message_ids: tuple[str, ...] = ()
            sensitive_message_ids: tuple[str, ...] = ()
            if safe_items:
                try:
                    safe_message_ids = await self.sender.send_artworks(
                        event,
                        self._tag_header(tag_query, used_word, "全年龄作品"),
                        safe_items,
                        as_forward=self._cfg_bool("send_as_forward", True),
                    )
                except BatchMessageSendError as exc:
                    safe_message_ids = exc.message_ids
                    self._log_batch_send_failure("tag", "safe", exc)
                    problems.append(
                        self._batch_send_failure_text(
                            "全年龄作品",
                            safe_message_ids,
                            sensitive=False,
                        ),
                    )
                except MessageSendError:
                    problems.append("全年龄作品发送失败")

            if sensitive_items:
                try:
                    sensitive_message_ids = await self.sender.send_artworks(
                        event,
                        self._tag_header(tag_query, used_word, "敏感作品（将自动撤回）"),
                        sensitive_items,
                        as_forward=self._cfg_bool("send_as_forward", True),
                    )
                except BatchMessageSendError as exc:
                    sensitive_message_ids = exc.message_ids
                    await self._schedule_recalls(event, sensitive_message_ids)
                    self._log_batch_send_failure("tag", "sensitive", exc)
                    problems.append(
                        self._batch_send_failure_text(
                            "敏感作品",
                            sensitive_message_ids,
                            sensitive=True,
                        ),
                    )
                except MessageSendError:
                    problems.append("敏感作品发送失败")
                else:
                    await self._schedule_recalls(event, sensitive_message_ids)

            if problems:
                prefix = ""
                if blocked and not (safe_items or sensitive_items):
                    prefix = self._tag_header(
                        tag_query,
                        used_word,
                        "无可发送作品",
                    ) + "\n"
                yield event.plain_result(
                    prefix
                    + "标签查询提示：\n"
                    + "\n".join(f"- {item}" for item in problems),
                )
            if self.file_logs:
                self.file_logs.info(
                    "tag_query_completed",
                    mode=tag_query.mode,
                    page=tag_query.page,
                    requested=tag_query.number,
                    candidates=len(selected),
                    safe_items=len(safe_items),
                    sensitive_items=len(sensitive_items),
                    blocked=len(blocked),
                    metadata_failed=len(metadata_failed),
                    download_failed=len(download_failed),
                    safe_messages=len(safe_message_ids),
                    sensitive_messages=len(sensitive_message_ids),
                )
        except ConfigurationError:
            yield event.plain_result(
                "Pixiv 尚未配置完成：请在插件配置中填写 refresh token；"
                "大陆网络如无法访问 Pixiv API，还需填写 HTTP 代理。",
            )
        except ProviderError as exc:
            if self.file_logs:
                self.file_logs.warning(
                    "tag_provider_failed",
                    page=tag_query.page,
                    error_type=type(exc).__name__,
                )
            yield event.plain_result(
                "Pixiv 标签搜索失败，请检查 refresh token、网络或 API 代理后重试。",
            )
        except MessageSendError:
            yield event.plain_result(
                "QQ 消息发送或撤回保障失败，请检查 NapCat/OneBot 连接与机器人权限。",
            )
        except Exception as exc:
            logger.error(
                f"[PixivLookup] 标签查询未预期错误: error_type={type(exc).__name__}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "tag_unexpected_error",
                    error_type=type(exc).__name__,
                )
            yield event.plain_result("处理标签搜索时发生未预期错误，请查看插件日志。")

    async def _delete_command_record(self) -> None:
        if not self._command_records:
            return
        try:
            from astrbot.core import db_helper

            await db_helper.delete_command_configs(sorted(self._command_records))
        except Exception as exc:
            logger.warning(
                "[PixivLookup] 清理命令配置记录失败: "
                f"error_type={type(exc).__name__}"
            )
        self._command_records.clear()

    async def terminate(self) -> None:
        if self.recall_manager is not None:
            await self.recall_manager.shutdown()
            self.recall_manager = None
        if self.image_proxy is not None:
            await self.image_proxy.close()
            self.image_proxy = None
        if self.provider is not None:
            await self.provider.close()
            self.provider = None
        await self._delete_command_record()
        if self.file_logs is not None:
            self.file_logs.info("plugin_terminated", version=PLUGIN_VERSION)
            await self.file_logs.close()
            self.file_logs = None
        logger.info("[PixivLookup] 插件已停止")
