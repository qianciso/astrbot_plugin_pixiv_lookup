"""面向 QQ 的作品信息格式化。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Artwork

QUALITY_NAMES = {
    "original": "original（原图）",
    "large": "large（大图）",
    "medium": "medium（中图）",
    "square_medium": "square_medium（方形缩略图）",
}


def _format_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value


def format_artwork_info(artwork: Artwork, page_index: int, quality: str) -> str:
    """只展示确实取得的字段，避免输出大量“未知”。"""

    lines = [
        f"作品：{artwork.title}",
        f"作品 ID：{artwork.illust_id}",
    ]
    author = artwork.author_name
    author_details: list[str] = []
    if artwork.author_id is not None:
        author_details.append(f"ID {artwork.author_id}")
    if artwork.author_account:
        author_details.append(f"账号 {artwork.author_account}")
    if author_details:
        author += f"（{'，'.join(author_details)}）"
    lines.append(f"作者：{author}")
    if artwork.create_date:
        lines.append(f"日期：{_format_date(artwork.create_date)}")
    if artwork.artwork_type:
        lines.append(f"类型：{artwork.artwork_type}")
    lines.extend(
        [
            f"分级：{artwork.rating.display_name}",
            f"页码：第 {page_index}/{artwork.page_count} 幅",
            f"图片尺寸：{QUALITY_NAMES.get(quality, quality)}",
        ],
    )
    if artwork.width and artwork.height:
        lines.append(f"原始分辨率：{artwork.width} x {artwork.height}")
    if artwork.tags:
        tag_text = "、".join(artwork.tags[:30])
        if len(tag_text) > 400:
            tag_text = tag_text[:397] + "..."
        lines.append(f"标签：{tag_text}")
    if artwork.caption:
        lines.append(f"简介：{artwork.caption}")
    return "\n".join(lines)
