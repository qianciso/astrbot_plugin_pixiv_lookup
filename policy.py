"""作品内容分级策略。"""

from .models import Artwork, Rating


class R18ContentPolicy:
    """未知分级默认拒绝，避免元数据异常导致敏感内容泄漏。"""

    def rejection_reason(
        self,
        artwork: Artwork,
        r18_enabled: bool,
        r18g_enabled: bool,
    ) -> str | None:
        if artwork.rating is Rating.UNKNOWN:
            return "无法可靠确认该作品的内容分级，为安全起见未发送图片。"
        if artwork.rating is Rating.R18 and not r18_enabled:
            return "该作品分级为 R-18，当前 R18 开关未开启，不能发送。"
        if artwork.rating is Rating.R18G and not r18g_enabled:
            return "该作品分级为 R-18G，当前 R18G 开关未开启，不能发送。"
        return None
