"""与上游 API 和 AstrBot 解耦的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Rating(str, Enum):
    """Pixiv 作品分级。"""

    SAFE = "safe"
    R18 = "r18"
    R18G = "r18g"
    UNKNOWN = "unknown"

    @property
    def display_name(self) -> str:
        return {
            Rating.SAFE: "全年龄",
            Rating.R18: "R-18",
            Rating.R18G: "R-18G",
            Rating.UNKNOWN: "未知",
        }[self]


@dataclass(slots=True, frozen=True)
class ArtworkPage:
    """一幅作品中某一页的各尺寸原始地址。"""

    index: int
    urls: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Artwork:
    """发送流程需要的 Pixiv 作品信息。"""

    illust_id: int
    title: str
    author_name: str
    author_id: int | None
    author_account: str
    create_date: str
    artwork_type: str
    caption: str
    tags: tuple[str, ...]
    rating: Rating
    width: int | None
    height: int | None
    pages: tuple[ArtworkPage, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(slots=True, frozen=True)
class DownloadedImage:
    """已经校验、可交给 OneBot 发送的图片。"""

    data: bytes
    content_type: str
    quality: str
    proxy_host: str
