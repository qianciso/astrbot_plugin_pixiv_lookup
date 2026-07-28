"""后续扩展使用的最小接口。

新增元数据源、图片反代策略或消息平台时，实现对应 Protocol 即可，不需要修改
主命令的数据流。
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import Artwork, ArtworkPage, DownloadedImage


class ArtworkProvider(Protocol):
    async def get_artwork(self, illust_id: int) -> Artwork:
        """获取并标准化一个作品。"""

    async def close(self) -> None:
        """关闭上游客户端。"""


class ImageProxyStrategy(Protocol):
    async def fetch(self, page: ArtworkPage, preferred_quality: str) -> DownloadedImage:
        """从允许的反代下载一页图片。"""

    async def close(self) -> None:
        """关闭下载会话。"""


class MessageSender(Protocol):
    async def send_artwork(
        self,
        event: Any,
        info_text: str,
        page_text: str,
        image: DownloadedImage,
        *,
        as_forward: bool,
    ) -> str:
        """发送作品并返回 OneBot 消息 ID。"""

    async def recall(self, bot: Any, message_id: str, self_id: str = "") -> None:
        """撤回由机器人发送的消息。"""


class ContentPolicy(Protocol):
    def rejection_reason(self, artwork: Artwork, r18_enabled: bool) -> str | None:
        """允许发送时返回 None，否则返回面向用户的原因。"""
