from __future__ import annotations

import os

import pytest
from astrbot_plugin_pixiv_lookup.models import Rating
from astrbot_plugin_pixiv_lookup.provider import PixivProvider


@pytest.mark.live
@pytest.mark.anyio
async def test_live_safe_artwork_metadata():
    """仅在开发者显式提供凭据和已确认全年龄 ID 时访问真实 Pixiv API。"""

    token = os.getenv("PIXIV_REFRESH_TOKEN", "").strip()
    illust_id = os.getenv("PIXIV_SAFE_ILLUST_ID", "").strip()
    if not token or not illust_id:
        pytest.skip("未设置可选真实网络测试环境变量")
    if not illust_id.isdigit():
        pytest.fail("PIXIV_SAFE_ILLUST_ID 必须是数字")

    provider = PixivProvider(
        token,
        os.getenv("PIXIV_API_PROXY", "").strip(),
        timeout=30,
    )
    try:
        artwork = await provider.get_artwork(int(illust_id))
        assert artwork.rating is Rating.SAFE
        assert artwork.pages
    finally:
        await provider.close()


@pytest.mark.live
@pytest.mark.anyio
async def test_live_safe_artist_metadata():
    """可选验证画师列表；调用者必须选择确认不含敏感内容的测试画师。"""

    token = os.getenv("PIXIV_REFRESH_TOKEN", "").strip()
    artist_id = os.getenv("PIXIV_SAFE_ARTIST_ID", "").strip()
    if not token or not artist_id:
        pytest.skip("未设置可选真实画师测试环境变量")
    if not artist_id.isdigit():
        pytest.fail("PIXIV_SAFE_ARTIST_ID 必须是数字")

    provider = PixivProvider(
        token,
        os.getenv("PIXIV_API_PROXY", "").strip(),
        timeout=30,
    )
    try:
        result = await provider.get_artist_artworks(int(artist_id), 1)
        assert result.profile.user_id == int(artist_id)
        if result.entries and result.entries[0].artwork is not None:
            assert result.entries[0].artwork.rating is Rating.SAFE
    finally:
        await provider.close()
