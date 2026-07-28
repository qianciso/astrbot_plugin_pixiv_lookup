"""限定 Pixiv 图片反代的下载实现。"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .exceptions import ImageDownloadError, ImageTooLargeError, MetadataError
from .models import ArtworkPage, DownloadedImage

ALLOWED_SOURCE_HOSTS = frozenset({"i.pximg.net", "i.pixiv.re", "i.pixiv.nl"})
ALLOWED_PROXY_HOSTS = ("i.pixiv.re", "i.pixiv.nl")
MAX_IMAGE_BYTES = 50 * 1024 * 1024
QUALITY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "original": ("original", "large", "medium", "square_medium"),
    "large": ("large", "medium", "square_medium"),
    "medium": ("medium", "square_medium"),
    "square_medium": ("square_medium",),
}


def proxy_order(primary: str) -> tuple[str, str]:
    """返回经过白名单校验的主、备反代顺序。"""

    normalized = primary.strip().casefold()
    if normalized not in ALLOWED_PROXY_HOSTS:
        normalized = ALLOWED_PROXY_HOSTS[0]
    backup = next(host for host in ALLOWED_PROXY_HOSTS if host != normalized)
    return normalized, backup


def rewrite_image_url(source_url: str, proxy_host: str) -> str:
    """只替换已知 Pixiv 图片主机，避免配置或 API 数据形成 SSRF。"""

    parsed = urlsplit(source_url)
    source_host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or source_host not in ALLOWED_SOURCE_HOSTS:
        raise MetadataError("图片地址不是允许的 Pixiv 图片主机")
    if proxy_host not in ALLOWED_PROXY_HOSTS:
        raise MetadataError("图片反代主机不在允许列表中")
    return urlunsplit(("https", proxy_host, parsed.path, parsed.query, ""))


def quality_candidates(page: ArtworkPage, preferred: str) -> list[tuple[str, str]]:
    order = QUALITY_FALLBACKS.get(preferred, QUALITY_FALLBACKS["large"])
    return [(quality, page.urls[quality]) for quality in order if page.urls.get(quality)]


class PixivImageProxy:
    """通过 i.pixiv.re/i.pixiv.nl 下载并校验图片。"""

    def __init__(self, primary_host: str, timeout: float) -> None:
        self.hosts = proxy_order(primary_host)
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _download(self, url: str, quality: str, host: str) -> DownloadedImage:
        session = self._get_session()
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": "AstrBot-Pixiv-Lookup/1.0",
                    "Referer": "https://www.pixiv.net/",
                    "Accept": "image/*",
                },
                # 不跟随反代返回的跳转，避免响应把下载引向未授权主机。
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ImageDownloadError(f"反代返回 HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if not content_type.casefold().startswith("image/"):
                    raise ImageDownloadError("反代响应不是图片")
                if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                    raise ImageTooLargeError("图片超过 50 MiB")

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ImageTooLargeError("图片超过 50 MiB")
                    chunks.append(chunk)
                if not chunks:
                    raise ImageDownloadError("反代返回了空图片")
                return DownloadedImage(
                    data=b"".join(chunks),
                    content_type=content_type,
                    quality=quality,
                    proxy_host=host,
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ImageDownloadError("反代网络请求失败") from exc

    async def fetch(self, page: ArtworkPage, preferred_quality: str) -> DownloadedImage:
        candidates = quality_candidates(page, preferred_quality)
        if not candidates:
            raise MetadataError("所选页面没有可用尺寸")

        failures: list[str] = []
        for quality, source_url in candidates:
            for host in self.hosts:
                try:
                    url = rewrite_image_url(source_url, host)
                    return await self._download(url, quality, host)
                except ImageDownloadError as exc:
                    failures.append(type(exc).__name__)
                    continue
        summary = ",".join(failures[-4:]) or "no_candidate"
        raise ImageDownloadError(f"所有图片反代均不可用 ({summary})")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
