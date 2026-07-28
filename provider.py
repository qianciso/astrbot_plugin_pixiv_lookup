"""Pixiv 官方 App API 元数据提供器。"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .exceptions import (
    ArtworkNotFoundError,
    ConfigurationError,
    MetadataError,
    ProviderError,
)
from .models import Artwork, ArtworkPage, Rating


class _CaptionParser(HTMLParser):
    """把 Pixiv caption 中的 HTML 转为紧凑纯文本。"""

    _BREAK_TAGS = {"br", "p", "div", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_caption(raw: object, limit: int = 500) -> str:
    """清理说明文本并限制 QQ 消息长度。"""

    parser = _CaptionParser()
    try:
        parser.feed(str(raw or ""))
        parser.close()
        text = unescape("".join(parser.parts))
    except Exception:
        text = str(raw or "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    compact = "\n".join(line for line in lines if line)
    if len(compact) > limit:
        return compact[: limit - 1].rstrip() + "..."
    return compact


def _extract_tags(raw_tags: object) -> tuple[str, ...]:
    tags: list[str] = []
    if not isinstance(raw_tags, list):
        return ()
    for raw in raw_tags:
        name = raw.get("name") if isinstance(raw, dict) else raw
        name = str(name or "").strip()
        if name and name not in tags:
            tags.append(name)
    return tuple(tags)


def classify_rating(illust: dict[str, Any], tags: tuple[str, ...]) -> Rating:
    """优先使用 x_restrict；字段缺失时才使用标签兜底。"""

    if "x_restrict" in illust:
        try:
            value = int(illust["x_restrict"])
        except (TypeError, ValueError):
            return Rating.UNKNOWN
        return {0: Rating.SAFE, 1: Rating.R18, 2: Rating.R18G}.get(
            value,
            Rating.UNKNOWN,
        )

    normalized = {
        re.sub(r"[^a-z0-9]", "", tag.casefold())
        for tag in tags
    }
    if "r18g" in normalized:
        return Rating.R18G
    if "r18" in normalized:
        return Rating.R18
    # 没有权威字段且标签也没有分级，并不足以证明作品是全年龄。
    return Rating.UNKNOWN


def _normalize_urls(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    allowed = {"original", "large", "medium", "square_medium"}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if key in allowed and isinstance(value, str) and value.startswith("https://")
    }


def parse_artwork(illust: dict[str, Any]) -> Artwork:
    """把 Pixiv 返回结构转换为严格的内部模型。"""

    try:
        illust_id = int(illust["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MetadataError("作品元数据缺少合法 ID") from exc

    tags = _extract_tags(illust.get("tags"))
    rating = classify_rating(illust, tags)
    try:
        declared_page_count = int(illust.get("page_count", 1))
    except (TypeError, ValueError) as exc:
        raise MetadataError("作品页数无效") from exc
    if declared_page_count < 1:
        raise MetadataError("作品页数无效")

    pages: list[ArtworkPage] = []
    if declared_page_count == 1:
        urls = _normalize_urls(illust.get("image_urls"))
        single = illust.get("meta_single_page")
        if isinstance(single, dict):
            original = single.get("original_image_url")
            if isinstance(original, str) and original.startswith("https://"):
                urls["original"] = original
        if not urls:
            raise MetadataError("作品没有可用图片地址")
        pages.append(ArtworkPage(index=1, urls=urls))
    else:
        raw_pages = illust.get("meta_pages")
        if not isinstance(raw_pages, list) or len(raw_pages) < declared_page_count:
            raise MetadataError("多图作品的页面元数据不完整")
        for index, raw_page in enumerate(raw_pages[:declared_page_count], 1):
            raw_urls = raw_page.get("image_urls") if isinstance(raw_page, dict) else None
            urls = _normalize_urls(raw_urls)
            if not urls:
                raise MetadataError(f"第 {index} 页没有可用图片地址")
            pages.append(ArtworkPage(index=index, urls=urls))

    user = illust.get("user") if isinstance(illust.get("user"), dict) else {}

    def optional_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return Artwork(
        illust_id=illust_id,
        title=str(illust.get("title") or "未命名作品").strip(),
        author_name=str(user.get("name") or "未知作者").strip(),
        author_id=optional_int(user.get("id")),
        author_account=str(user.get("account") or "").strip(),
        create_date=str(illust.get("create_date") or "").strip(),
        artwork_type=str(illust.get("type") or "").strip(),
        caption=clean_caption(illust.get("caption")),
        tags=tags,
        rating=rating,
        width=optional_int(illust.get("width")),
        height=optional_int(illust.get("height")),
        pages=tuple(pages),
    )


class PixivProvider:
    """复用登录状态、按需刷新会话的异步 Pixiv 客户端。"""

    def __init__(self, refresh_token: str, api_proxy: str, timeout: float) -> None:
        self.refresh_token = refresh_token.strip()
        self.api_proxy = api_proxy.strip()
        self.timeout = timeout
        self._api: Any = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _get_api(self) -> Any:
        if not self.refresh_token:
            raise ConfigurationError("未配置 Pixiv refresh token")
        async with self._lock:
            if self._closed:
                raise ProviderError("Pixiv 客户端已经关闭")
            if self._api is not None:
                return self._api
            try:
                from pixivpy_async import AppPixivAPI
            except ImportError as exc:
                raise ConfigurationError("缺少 pixivpy-async 依赖") from exc

            kwargs: dict[str, object] = {}
            if self.api_proxy:
                if not self.api_proxy.startswith("http://"):
                    raise ConfigurationError("Pixiv API 代理仅支持 http:// 地址")
                kwargs["proxy"] = self.api_proxy
            api = AppPixivAPI(**kwargs)
            try:
                await asyncio.wait_for(
                    api.login(refresh_token=self.refresh_token),
                    timeout=self.timeout,
                )
            except Exception as exc:
                close = getattr(api, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                raise ProviderError("Pixiv 登录失败") from exc
            self._api = api
            return api

    async def get_artwork(self, illust_id: int) -> Artwork:
        api = await self._get_api()
        try:
            response = await asyncio.wait_for(
                api.illust_detail(illust_id),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderError("Pixiv 请求超时") from exc
        except Exception as exc:
            message = str(exc).casefold()
            if "404" in message or "not found" in message:
                raise ArtworkNotFoundError("作品不存在") from exc
            raise ProviderError("Pixiv 请求失败") from exc

        if not isinstance(response, dict):
            raise ProviderError("Pixiv 返回格式异常")
        error = response.get("error")
        if error:
            message = str(error).casefold()
            if "not found" in message or "見つかりません" in message or "404" in message:
                raise ArtworkNotFoundError("作品不存在")
            raise ProviderError("Pixiv 拒绝了作品查询")
        illust = response.get("illust")
        if not isinstance(illust, dict):
            raise ArtworkNotFoundError("作品不存在或不可见")
        return parse_artwork(illust)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            api = self._api
            self._api = None
        if api is None:
            return
        close = getattr(api, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                try:
                    await asyncio.wait_for(result, timeout=5)
                except Exception:
                    pass
