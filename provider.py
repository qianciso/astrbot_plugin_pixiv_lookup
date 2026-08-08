"""Pixiv 官方 App API 元数据提供器。"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .exceptions import (
    ArtistNotFoundError,
    ArtworkNotFoundError,
    ConfigurationError,
    MetadataError,
    ProviderError,
)
from .models import (
    ArtistArtworkEntry,
    ArtistProfile,
    ArtistWorks,
    Artwork,
    ArtworkPage,
    Rating,
)
from .tag_search import TagSearchEntry, TagSearchPage, has_tag

PIXIV_SEARCH_BATCH_SIZE = 30
TAG_SEARCH_PAGE_SIZE = 60


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


def _extract_translated_tags(raw_tags: object) -> tuple[str, ...]:
    tags: list[str] = []
    if not isinstance(raw_tags, list):
        return ()
    for raw in raw_tags:
        value = raw.get("translated_name") if isinstance(raw, dict) else None
        value = str(value or "").strip()
        if value and value not in tags:
            tags.append(value)
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
    translated_tags = _extract_translated_tags(illust.get("tags"))
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
        translated_tags=translated_tags,
    )


def _error_text(response: dict[str, Any]) -> str:
    """把上游错误压缩为只用于分类的文本，不写入插件日志。"""

    return str(response.get("error") or "").casefold()


def _looks_not_found(message: str) -> bool:
    return any(marker in message for marker in ("404", "not found", "見つかりません"))


def _optional_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


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

    async def _get_artist_profile(self, api: Any, artist_id: int) -> ArtistProfile:
        try:
            response = await asyncio.wait_for(
                api.user_detail(artist_id),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ProviderError("Pixiv 画师资料请求超时") from exc
        except Exception as exc:
            if _looks_not_found(str(exc).casefold()):
                raise ArtistNotFoundError("画师不存在") from exc
            raise ProviderError("Pixiv 画师资料请求失败") from exc

        if not isinstance(response, dict):
            raise ProviderError("Pixiv 画师资料返回格式异常")
        if response.get("error"):
            if _looks_not_found(_error_text(response)):
                raise ArtistNotFoundError("画师不存在")
            raise ProviderError("Pixiv 拒绝了画师查询")
        user = response.get("user")
        if not isinstance(user, dict):
            raise ArtistNotFoundError("画师不存在或不可见")
        response_id = _optional_positive_int(user.get("id"))
        if response_id is None:
            raise ProviderError("Pixiv 画师资料缺少合法 ID")
        profile_data = response.get("profile")
        total_illusts = (
            _optional_nonnegative_int(profile_data.get("total_illusts"))
            if isinstance(profile_data, dict)
            else None
        )
        return ArtistProfile(
            user_id=response_id,
            name=str(user.get("name") or "未知画师").strip(),
            account=str(user.get("account") or "").strip(),
            total_illusts=total_illusts,
        )

    async def get_artist_artworks(
        self,
        artist_id: int,
        limit: int,
        start_position: int = 1,
    ) -> ArtistWorks:
        """从 ``start_position`` 开始读取 ``limit`` 项画师插画。

        App API 的 ``type=illust`` 会包含普通插画和 ugoira 静态预览。这里仍显式
        过滤 ``manga``，防止上游返回范围变化时把漫画意外纳入 v1.1 的结果。

        ``pick`` 查询会把目标排名换算为 API offset，只拉取目标处开始的一页，
        因而排名不受批量返回上限影响，也不会为较大的排名下载前面所有元数据。
        """

        limit = max(1, min(int(limit), 20))
        start_position = max(1, int(start_position))
        api = await self._get_api()
        profile = await self._get_artist_profile(api, artist_id)
        entries: list[ArtistArtworkEntry] = []
        seen_ids: set[int] = set()
        offset: int | None = start_position - 1 if start_position > 1 else None
        seen_offsets: set[int] = {offset} if offset is not None else set()
        exhausted = False

        # 正常 API 一页已经足够覆盖 20 项；页数上限只用于防御异常 next_url 循环。
        for _ in range(10):
            kwargs: dict[str, object] = {"user_id": artist_id, "type": "illust"}
            if offset is not None:
                kwargs["offset"] = offset
            try:
                response = await asyncio.wait_for(
                    api.user_illusts(**kwargs),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise ProviderError("Pixiv 画师作品请求超时") from exc
            except Exception as exc:
                if _looks_not_found(str(exc).casefold()):
                    raise ArtistNotFoundError("画师不存在") from exc
                raise ProviderError("Pixiv 画师作品请求失败") from exc

            if not isinstance(response, dict):
                raise ProviderError("Pixiv 画师作品返回格式异常")
            if response.get("error"):
                if _looks_not_found(_error_text(response)):
                    raise ArtistNotFoundError("画师不存在")
                raise ProviderError("Pixiv 拒绝了画师作品查询")
            raw_illusts = response.get("illusts")
            if not isinstance(raw_illusts, list):
                raise ProviderError("Pixiv 画师作品列表格式异常")

            for raw in raw_illusts:
                if len(entries) >= limit:
                    break
                if isinstance(raw, dict) and str(raw.get("type") or "").casefold() == "manga":
                    continue
                illust_id = (
                    _optional_positive_int(raw.get("id")) if isinstance(raw, dict) else None
                )
                if illust_id is not None:
                    if illust_id in seen_ids:
                        continue
                    seen_ids.add(illust_id)

                artwork: Artwork | None = None
                if isinstance(raw, dict):
                    try:
                        artwork = parse_artwork(raw)
                    except MetadataError:
                        # 失败项计入原始排名，交给命令层汇总，而不是使用更早作品补位。
                        artwork = None
                entries.append(
                    ArtistArtworkEntry(
                        position=start_position + len(entries),
                        illust_id=illust_id,
                        artwork=artwork,
                    ),
                )

            if len(entries) >= limit:
                break
            next_url = response.get("next_url")
            if not isinstance(next_url, str) or not next_url.strip():
                exhausted = True
                break
            values = parse_qs(urlsplit(next_url).query).get("offset", [])
            next_offset = _optional_positive_int(values[0] if values else None)
            if next_offset is None or next_offset in seen_offsets:
                raise ProviderError("Pixiv 画师作品分页信息异常")
            seen_offsets.add(next_offset)
            offset = next_offset
        else:
            raise ProviderError("Pixiv 画师作品分页次数异常")

        return ArtistWorks(profile=profile, entries=tuple(entries), exhausted=exhausted)

    @staticmethod
    def _raw_is_ai(illust: object) -> bool:
        """兼容 Pixiv 搜索结果中的 AI 字段和标签字段。"""

        if not isinstance(illust, dict):
            return False
        for key in ("illust_ai_type", "illustAiType", "ai_type", "aiType"):
            try:
                if int(illust.get(key)) == 2:
                    return True
            except (TypeError, ValueError):
                pass
        tags = _extract_tags(illust.get("tags"))
        ai_words = {"ai", "ai生成", "ai-generated", "ai辅助"}
        return any(tag.casefold() in ai_words for tag in tags)

    async def search_tag_page(
        self,
        word: str,
        *,
        page: int,
        search_target: str,
        sort: str,
        allow_ai: bool,
        excluded_tags: tuple[str, ...] = (),
    ) -> TagSearchPage:
        """读取最多 60 项的标签搜索页，并完成 AI 与排除标签过滤。

        Pixiv App API 单次通常只返回 30 项，因此一个与官网一致的逻辑页由
        两个连续 API 批次组成：第 N 页从 ``(N - 1) * 60`` 开始读取。
        """

        if not str(word or "").strip():
            raise ProviderError("Pixiv 标签搜索词为空")
        page = max(1, int(page))
        api = await self._get_api()
        normalized_word = str(word).strip()
        page_start = (page - 1) * TAG_SEARCH_PAGE_SIZE
        raw_illusts: list[object] = []
        has_next_batch = False
        for batch_index in range(TAG_SEARCH_PAGE_SIZE // PIXIV_SEARCH_BATCH_SIZE):
            offset = page_start + batch_index * PIXIV_SEARCH_BATCH_SIZE
            try:
                response = await asyncio.wait_for(
                    api.search_illust(
                        normalized_word,
                        search_target=search_target,
                        sort=sort,
                        offset=offset or None,
                    ),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise ProviderError("Pixiv 标签搜索请求超时") from exc
            except Exception as exc:
                if _looks_not_found(str(exc).casefold()):
                    raise ProviderError("Pixiv 标签搜索不可用") from exc
                raise ProviderError("Pixiv 标签搜索请求失败") from exc

            if not isinstance(response, dict):
                raise ProviderError("Pixiv 标签搜索返回格式异常")
            if response.get("error"):
                raise ProviderError("Pixiv 标签搜索被拒绝")
            batch_illusts = response.get("illusts")
            if not isinstance(batch_illusts, list):
                raise ProviderError("Pixiv 标签搜索作品列表格式异常")
            raw_illusts.extend(batch_illusts)

            next_url = response.get("next_url")
            has_next_batch = isinstance(next_url, str) and bool(next_url.strip())
            if not has_next_batch:
                break

        entries: list[TagSearchEntry] = []
        seen_ids: set[int] = set()
        for raw in raw_illusts:
            if not allow_ai and self._raw_is_ai(raw):
                continue
            if not isinstance(raw, dict):
                entries.append(TagSearchEntry(len(entries) + 1, None, None))
                continue
            try:
                artwork = parse_artwork(raw)
            except MetadataError:
                artwork = None
            if artwork is not None and excluded_tags and has_tag(artwork, excluded_tags):
                continue
            illust_id = _optional_positive_int(raw.get("id"))
            if illust_id is not None:
                if illust_id in seen_ids:
                    continue
                seen_ids.add(illust_id)
            entries.append(TagSearchEntry(len(entries) + 1, illust_id, artwork))

        return TagSearchPage(
            entries=tuple(entries),
            page=page,
            exhausted=not has_next_batch,
            used_search_word=normalized_word,
        )

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
                with suppress(Exception):
                    await asyncio.wait_for(result, timeout=5)
