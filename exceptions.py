"""插件内部使用的稳定异常类型。

上游库的异常文本可能包含请求地址或其他实现细节，因此主命令只根据这些异常
类型生成面向用户的固定提示，原始异常只以类型名写入日志。
"""


class PixivLookupError(Exception):
    """插件可预期异常的基类。"""


class ConfigurationError(PixivLookupError):
    """插件配置缺失或不合法。"""


class ArtworkNotFoundError(PixivLookupError):
    """作品不存在、已删除或当前账号不可见。"""


class MetadataError(PixivLookupError):
    """作品元数据不完整，无法安全发送。"""


class ProviderError(PixivLookupError):
    """Pixiv API 请求失败。"""


class ImageDownloadError(PixivLookupError):
    """所有允许的图片反代均不可用。"""


class ImageTooLargeError(ImageDownloadError):
    """图片超过单次传输的安全上限。"""


class MessageSendError(PixivLookupError):
    """OneBot 消息发送失败或未返回消息 ID。"""
