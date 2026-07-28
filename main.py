"""AstrBot 插件入口。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .exceptions import (
    ArtworkNotFoundError,
    ConfigurationError,
    ImageDownloadError,
    MessageSendError,
    MetadataError,
    ProviderError,
)
from .formatting import format_artwork_info
from .image_proxy import PixivImageProxy
from .messaging import OneBotMessageSender
from .policy import R18ContentPolicy
from .provider import PixivProvider
from .recall import RecallManager
from .storage import PluginLogManager

PLUGIN_NAME = "astrbot_plugin_pixiv_lookup"
PLUGIN_VERSION = "v1.0.0"
DEFAULT_COMMAND = "pi"
COMMAND_PATTERN = re.compile(r"^[^\s/]{1,32}$")


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


class PixivLookupPlugin(Star):
    """按 Pixiv 作品 ID 查询并通过 QQ 发送指定页面。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self.provider: PixivProvider | None = None
        self.image_proxy: PixivImageProxy | None = None
        self.sender = OneBotMessageSender()
        self.policy = R18ContentPolicy()
        self.file_logs: PluginLogManager | None = None
        self.recall_manager: RecallManager | None = None
        self._command_record_created = False

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self.config.get(key, default) or default).strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.casefold() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _handler_full_name() -> str:
        handler = PixivLookupPlugin.pixiv_lookup
        return f"{handler.__module__}_{handler.__name__}"

    async def _configure_command(self) -> None:
        try:
            command = normalize_command_name(self.config.get("command_name", DEFAULT_COMMAND))
        except ValueError as exc:
            logger.error(f"[PixivLookup] 自定义命令无效，继续使用 /{DEFAULT_COMMAND}: {exc}")
            if self.file_logs:
                self.file_logs.warning("command_invalid", error_type=type(exc).__name__)
            return
        try:
            from astrbot.core.star.command_management import rename_command

            await rename_command(self._handler_full_name(), command, aliases=[])
            self._command_record_created = True
            logger.info(f"[PixivLookup] 命令已注册为 /{command}")
        except Exception as exc:
            logger.error(
                f"[PixivLookup] 命令 /{command} 注册失败，继续使用 /{DEFAULT_COMMAND}: "
                f"{type(exc).__name__}"
            )
            if self.file_logs:
                self.file_logs.warning(
                    "command_rename_failed",
                    command=command,
                    error_type=type(exc).__name__,
                )
            if command != DEFAULT_COMMAND:
                # 自定义名称冲突时显式恢复原生命令。这样即使上次进程异常退出并
                # 留下了命令数据库记录，本次加载也不会继续使用过期名称。
                try:
                    from astrbot.core.star.command_management import rename_command

                    await rename_command(
                        self._handler_full_name(),
                        DEFAULT_COMMAND,
                        aliases=[],
                    )
                    self._command_record_created = True
                except Exception as fallback_exc:
                    logger.error(
                        f"[PixivLookup] 恢复 /{DEFAULT_COMMAND} 失败: "
                        f"{type(fallback_exc).__name__}"
                    )
                    if self.file_logs:
                        self.file_logs.warning(
                            "command_fallback_failed",
                            error_type=type(fallback_exc).__name__,
                        )

    async def initialize(self) -> None:
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        retention = self._cfg_int("log_retention_days", 7, 1, 30)
        self.file_logs = PluginLogManager(data_dir, retention)
        removed = await asyncio.to_thread(self.file_logs.cleanup_once)
        self.file_logs.start()
        if removed:
            self.file_logs.info("startup_log_cleanup", removed=removed)

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
        await self._configure_command()
        self.file_logs.info("plugin_initialized", version=PLUGIN_VERSION)
        logger.info(f"[PixivLookup] 插件已加载，版本 {PLUGIN_VERSION}")

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

            rejection = self.policy.rejection_reason(
                artwork,
                self._cfg_bool("r18_enabled", False),
            )
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
                if self.recall_manager is None:
                    # 未建立撤回管理器时不允许静默留下 R18 消息，立即尝试撤回。
                    await self.sender.recall(
                        event.bot,
                        message_id,
                        str(event.get_self_id() or ""),
                    )
                    raise MessageSendError("撤回管理器不可用，R18 消息已立即撤回")
                self.recall_manager.schedule(
                    event.bot,
                    message_id,
                    str(event.get_self_id() or ""),
                    self._cfg_int("r18_recall_seconds", 120, 5, 120),
                )

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

    async def _delete_command_record(self) -> None:
        if not self._command_record_created:
            return
        try:
            from astrbot.core import db_helper

            await db_helper.delete_command_configs([self._handler_full_name()])
        except Exception as exc:
            logger.warning(
                "[PixivLookup] 清理命令配置记录失败: "
                f"error_type={type(exc).__name__}"
            )
        self._command_record_created = False

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
