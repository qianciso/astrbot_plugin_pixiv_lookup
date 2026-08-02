from __future__ import annotations

import pytest
from astrbot_plugin_pixiv_lookup.formatting import format_artwork_info
from astrbot_plugin_pixiv_lookup.models import Artwork, ArtworkPage, Rating
from astrbot_plugin_pixiv_lookup.policy import R18ContentPolicy


def make_artwork(rating: Rating = Rating.SAFE) -> Artwork:
    return Artwork(
        illust_id=123,
        title="测试作品",
        author_name="作者",
        author_id=42,
        author_account="artist",
        create_date="2026-07-28T08:00:00+00:00",
        artwork_type="illust",
        caption="作品简介",
        tags=("风景", "原创"),
        rating=rating,
        width=1600,
        height=900,
        pages=(
            ArtworkPage(1, {"large": "https://i.pximg.net/1.jpg"}),
            ArtworkPage(2, {"large": "https://i.pximg.net/2.jpg"}),
        ),
    )


@pytest.mark.parametrize("rating", [Rating.R18, Rating.R18G])
def test_sensitive_ratings_are_blocked_by_default(rating):
    reason = R18ContentPolicy().rejection_reason(make_artwork(rating), False, False)
    assert reason is not None
    assert rating.display_name in reason
    assert "不能发送" in reason


@pytest.mark.parametrize(
    ("rating", "r18_enabled", "r18g_enabled", "allowed"),
    [
        (Rating.R18, True, False, True),
        (Rating.R18, False, True, False),
        (Rating.R18G, True, False, False),
        (Rating.R18G, False, True, True),
    ],
)
def test_r18_and_r18g_switches_are_independent(
    rating,
    r18_enabled,
    r18g_enabled,
    allowed,
):
    reason = R18ContentPolicy().rejection_reason(
        make_artwork(rating),
        r18_enabled,
        r18g_enabled,
    )
    assert (reason is None) is allowed


def test_unknown_rating_is_always_blocked():
    policy = R18ContentPolicy()
    assert policy.rejection_reason(make_artwork(Rating.UNKNOWN), False, False)
    assert policy.rejection_reason(make_artwork(Rating.UNKNOWN), True, True)


def test_formatting_includes_available_metadata_page_and_actual_quality():
    text = format_artwork_info(make_artwork(), 2, "medium")
    assert "作品：测试作品" in text
    assert "作品 ID：123" in text
    assert "作者：作者（ID 42，账号 artist）" in text
    assert "日期：2026-07-28 16:00:00" in text
    assert "页码：第 2/2 幅" in text
    assert "图片尺寸：medium（中图）" in text
    assert "原始分辨率：1600 x 900" in text
