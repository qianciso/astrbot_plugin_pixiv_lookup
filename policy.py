"""作品内容分级策略。"""

from .models import Artwork, Rating


class R18ContentPolicy:
    """未知分级默认拒绝，避免元数据异常导致敏感内容泄漏。"""

    def rejection_reason(self, artwork: Artwork, r18_enabled: bool) -> str | None:
        if artwork.rating is Rating.UNKNOWN:
            return "无法可靠确认该作品的内容分级，为安全起见未发送图片。"
        if artwork.rating in {Rating.R18, Rating.R18G} and not r18_enabled:
            return (
                f"该作品分级为 {artwork.rating.display_name}，"
                "当前不能发送。"
            )
        return None
