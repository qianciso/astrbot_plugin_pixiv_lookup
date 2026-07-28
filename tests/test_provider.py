from __future__ import annotations

import asyncio
import sys
import types

import pytest

from astrbot_plugin_pixiv_lookup.exceptions import (
    ArtworkNotFoundError,
    ConfigurationError,
    MetadataError,
    ProviderError,
)
from astrbot_plugin_pixiv_lookup.models import Rating
from astrbot_plugin_pixiv_lookup.provider import (
    PixivProvider,
    classify_rating,
    clean_caption,
    parse_artwork,
)


def single_illust(**overrides):
    data = {
        "id": 123456,
        "title": "测试作品",
        "type": "illust",
        "x_restrict": 0,
        "caption": "<p>第一行<br>第二行 &amp; 更多</p>",
        "create_date": "2026-07-28T08:00:00+00:00",
        "page_count": 1,
        "width": 1600,
        "height": 900,
        "user": {"id": 42, "name": "作者", "account": "artist"},
        "tags": [{"name": "风景"}],
        "image_urls": {
            "large": "https://i.pximg.net/img-original/test_large.jpg",
            "medium": "https://i.pximg.net/img-original/test_medium.jpg",
        },
        "meta_single_page": {
            "original_image_url": "https://i.pximg.net/img-original/test.jpg",
        },
    }
    data.update(overrides)
    return data


def test_parse_single_artwork_and_clean_caption():
    artwork = parse_artwork(single_illust())
    assert artwork.illust_id == 123456
    assert artwork.rating is Rating.SAFE
    assert artwork.page_count == 1
    assert artwork.pages[0].urls["original"].endswith("test.jpg")
    assert artwork.caption == "第一行\n第二行 & 更多"


def test_parse_multi_page_artwork():
    pages = [
        {"image_urls": {"large": f"https://i.pximg.net/p{i}.jpg"}}
        for i in range(3)
    ]
    artwork = parse_artwork(single_illust(page_count=3, meta_pages=pages))
    assert artwork.page_count == 3
    assert artwork.pages[2].index == 3
    assert artwork.pages[2].urls["large"].endswith("p2.jpg")


def test_incomplete_multi_page_is_rejected():
    with pytest.raises(MetadataError):
        parse_artwork(single_illust(page_count=2, meta_pages=[]))


def test_missing_optional_fields_use_stable_fallbacks():
    artwork = parse_artwork(
        {
            "id": 7,
            "x_restrict": 0,
            "image_urls": {"large": "https://i.pximg.net/7.jpg"},
        }
    )
    assert artwork.title == "未命名作品"
    assert artwork.author_name == "未知作者"
    assert artwork.author_id is None
    assert artwork.page_count == 1


def test_missing_id_is_rejected():
    with pytest.raises(MetadataError):
        parse_artwork({"x_restrict": 0, "image_urls": {"large": "https://i.pximg.net/7.jpg"}})


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, Rating.SAFE), (1, Rating.R18), (2, Rating.R18G), (9, Rating.UNKNOWN)],
)
def test_x_restrict_is_authoritative(value, expected):
    assert classify_rating({"x_restrict": value}, ("R-18G",)) is expected


def test_tags_only_classify_when_x_restrict_is_missing():
    assert classify_rating({}, ("R-18",)) is Rating.R18
    assert classify_rating({}, ("R-18G",)) is Rating.R18G
    assert classify_rating({}, ("风景",)) is Rating.UNKNOWN


def test_caption_has_length_limit():
    assert clean_caption("a" * 1000, limit=20) == "a" * 19 + "..."


@pytest.mark.anyio
async def test_provider_login_detail_proxy_and_close(monkeypatch):
    instances = []

    class FakeAPI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.login_token = None
            self.closed = False
            instances.append(self)

        async def login(self, refresh_token):
            self.login_token = refresh_token

        async def illust_detail(self, illust_id):
            return {"illust": single_illust(id=illust_id)}

        async def close(self):
            self.closed = True

    module = types.ModuleType("pixivpy_async")
    module.AppPixivAPI = FakeAPI
    monkeypatch.setitem(sys.modules, "pixivpy_async", module)

    provider = PixivProvider("secret-token", "http://127.0.0.1:7890", 1)
    artwork = await provider.get_artwork(765)
    assert artwork.illust_id == 765
    assert instances[0].kwargs == {"proxy": "http://127.0.0.1:7890"}
    assert instances[0].login_token == "secret-token"
    await provider.close()
    assert instances[0].closed


@pytest.mark.anyio
async def test_provider_requires_token_and_valid_http_proxy(monkeypatch):
    with pytest.raises(ConfigurationError):
        await PixivProvider("", "", 1).get_artwork(1)

    module = types.ModuleType("pixivpy_async")
    module.AppPixivAPI = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "pixivpy_async", module)
    with pytest.raises(ConfigurationError, match="http"):
        await PixivProvider("token", "socks5://127.0.0.1:1080", 1).get_artwork(1)


@pytest.mark.anyio
async def test_provider_maps_timeout_not_found_and_bad_response():
    provider = PixivProvider("token", "", 0.001)

    class TimeoutAPI:
        async def illust_detail(self, illust_id):
            await asyncio.Event().wait()

    provider._api = TimeoutAPI()
    with pytest.raises(ProviderError, match="超时"):
        await provider.get_artwork(1)

    class NotFoundAPI:
        async def illust_detail(self, illust_id):
            return {"error": {"message": "not found"}}

    provider._api = NotFoundAPI()
    with pytest.raises(ArtworkNotFoundError):
        await provider.get_artwork(1)

    class InvalidAPI:
        async def illust_detail(self, illust_id):
            return []

    provider._api = InvalidAPI()
    with pytest.raises(ProviderError, match="格式"):
        await provider.get_artwork(1)
