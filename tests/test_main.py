from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest
from astrbot_plugin_pixiv_lookup.exceptions import (
    BatchMessageSendError,
    ImageDownloadError,
    ProviderError,
)
from astrbot_plugin_pixiv_lookup.main import (
    DEFAULT_ARTIST_COMMAND,
    DEFAULT_COMMAND,
    TEMP_ARTIST_COMMAND,
    TEMP_ARTWORK_COMMAND,
    TEMP_TAG_COMMAND,
    PixivLookupPlugin,
    normalize_command_name,
    parse_artist_command_args,
    parse_command_args,
)
from astrbot_plugin_pixiv_lookup.models import (
    ArtistArtworkEntry,
    ArtistProfile,
    ArtistWorks,
    Artwork,
    ArtworkPage,
    DownloadedImage,
    Rating,
)
from astrbot_plugin_pixiv_lookup.tag_search import TagSearchEntry, TagSearchPage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("123", (123, 1)), ("123 2", (123, 2)), ("  123   3 ", (123, 3))],
)
def test_parse_command_args(raw, expected):
    assert parse_command_args(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "-1", "0", "1 0", "1 two", "1 2 3"])
def test_parse_command_args_rejects_invalid_input(raw):
    with pytest.raises(ValueError):
        parse_command_args(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42", (42, 1, "latest")),
        ("42 5", (42, 5, "latest")),
        ("42 5 1", (42, 5, "latest")),
        ("42 5 0", (42, 5, "pick")),
        ("42 latest", (42, 1, "latest")),
        ("42 latest 5", (42, 5, "latest")),
        ("42 pick 5", (42, 5, "pick")),
        ("42 13 0", (42, 13, "pick")),
        ("42 pick 13", (42, 13, "pick")),
    ],
)
def test_parse_artist_command_args_supports_numeric_and_keyword_modes(raw, expected):
    query = parse_artist_command_args(raw)
    assert (query.artist_id, query.number, query.mode) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "0", "42 0", "42 11", "42 2 2", "42 pick", "42 x 2", "42 1 0 extra"],
)
def test_parse_artist_command_args_rejects_invalid_input(raw):
    with pytest.raises(ValueError):
        parse_artist_command_args(raw)


def test_artist_latest_limit_is_configurable_but_never_exceeds_twenty():
    query = parse_artist_command_args("42 latest 13", latest_limit=20)
    assert (query.number, query.mode) == (13, "latest")

    with pytest.raises(ValueError, match="1-20"):
        parse_artist_command_args("42 21 1", latest_limit=99)


def test_normalize_command_accepts_optional_slash_and_rejects_spaces():
    assert normalize_command_name("pi") == "pi"
    assert normalize_command_name("/pixiv") == "pixiv"
    with pytest.raises(ValueError):
        normalize_command_name("pixiv search")


def test_config_schema_has_required_defaults_and_ranges():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(schema) == [
        "global_settings",
        "pixiv_connection",
        "artwork_query",
        "artist_query",
        "tag_query",
    ]
    global_items = schema["global_settings"]["items"]
    connection_items = schema["pixiv_connection"]["items"]
    artwork_items = schema["artwork_query"]["items"]
    artist_items = schema["artist_query"]["items"]
    tag_items = schema["tag_query"]["items"]
    assert all(group["type"] == "object" for group in schema.values())
    assert list(global_items) == [
        "r18_enabled",
        "r18g_enabled",
        "r18_recall_seconds",
        "image_size",
        "send_as_forward",
        "primary_image_proxy",
        "request_timeout",
        "log_retention_days",
    ]
    assert list(artist_items) == ["artist_command_name", "artist_max_results"]
    assert list(tag_items) == [
        "tag_command_name",
        "tag_search_target",
        "tag_allow_ai",
        "tag_sort",
        "tag_popular_fallback_enabled",
        "tag_popular_bookmark_threshold",
        "tag_translate_enabled",
    ]
    assert connection_items["pixiv_refresh_token"]["default"] == ""
    assert artwork_items["command_name"]["default"] == "pi"
    assert artist_items["artist_command_name"]["default"] == "pa"
    assert tag_items["tag_command_name"]["default"] == "pt"
    assert tag_items["tag_search_target"]["default"] == "partial_match_for_tags"
    assert tag_items["tag_allow_ai"]["default"] is False
    assert tag_items["tag_sort"]["default"] == "date_desc"
    assert tag_items["tag_popular_fallback_enabled"]["default"] is True
    assert tag_items["tag_popular_bookmark_threshold"]["default"] == 500
    assert tag_items["tag_translate_enabled"]["default"] is True
    assert artist_items["artist_max_results"]["default"] == 10
    assert artist_items["artist_max_results"]["slider"] == {
        "min": 1,
        "max": 20,
        "step": 1,
    }
    assert global_items["r18_enabled"]["default"] is False
    assert global_items["r18g_enabled"]["default"] is False
    assert global_items["r18_recall_seconds"]["default"] == 120
    assert global_items["r18_recall_seconds"]["slider"] == {
        "min": 5,
        "max": 120,
        "step": 5,
    }
    assert global_items["image_size"]["options"] == [
        "original",
        "large",
        "medium",
        "square_medium",
    ]
    assert global_items["primary_image_proxy"]["options"] == [
        "i.pixiv.re",
        "i.pixiv.nl",
    ]


def test_grouped_config_is_read_and_legacy_flat_config_is_migrated():
    grouped = PixivLookupPlugin(
        object(),
        {
            "global_settings": {"r18_enabled": True, "image_size": "medium"},
            "artist_query": {"artist_command_name": "artist", "artist_max_results": 17},
            "tag_query": {"tag_sort": "date_asc"},
        },
    )
    assert grouped._cfg_bool("r18_enabled", False) is True
    assert grouped._cfg_str("image_size") == "medium"
    assert grouped._configured_command("artist_command_name", "pa") == "artist"
    assert grouped._cfg_int("artist_max_results", 10, 1, 20) == 17
    assert grouped._cfg_str("tag_sort") == "date_asc"

    class SavingConfig(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.save_calls = 0

        def save_config(self):
            self.save_calls += 1

    legacy = SavingConfig(
        {
            "r18_enabled": True,
            "artist_command_name": "artist",
            "tag_sort": "date_asc",
        },
    )
    migrated = PixivLookupPlugin(object(), legacy)

    assert legacy.save_calls == 1
    assert "r18_enabled" not in legacy
    assert legacy["global_settings"]["r18_enabled"] is True
    assert legacy["artist_query"]["artist_command_name"] == "artist"
    assert legacy["tag_query"]["tag_sort"] == "date_asc"
    assert migrated._cfg_bool("r18_enabled", False) is True


def test_v12_version_metadata_and_readme_notice_are_synchronized():
    root = Path(__file__).resolve().parents[1]
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "version: v1.2.0" in metadata
    assert 'version = "1.2.0"' in project
    assert "## v1.2.0 更新内容（2026-08-08）" in readme[:1200]
    assert "Pixiv 查询" in metadata
    assert "/pa" in readme[:1000]
    assert "/pt" in readme[:1600]


class FakeLog:
    def __init__(self):
        self.events = []

    def info(self, event, **fields):
        self.events.append(("info", event, fields))

    def warning(self, event, **fields):
        self.events.append(("warning", event, fields))


def install_command_module(monkeypatch, rename):
    module = types.ModuleType("astrbot.core.star.command_management")
    module.rename_command = rename
    monkeypatch.setitem(sys.modules, "astrbot.core.star.command_management", module)


@pytest.mark.anyio
async def test_command_name_is_synchronized(monkeypatch):
    calls = []

    async def rename(handler, command, aliases):
        calls.append((handler, command, aliases))

    install_command_module(monkeypatch, rename)
    plugin = PixivLookupPlugin(
        object(),
        {"command_name": "/pixiv", "tag_command_name": "/tags"},
    )
    plugin.file_logs = FakeLog()
    await plugin._configure_commands()
    assert [call[1] for call in calls] == [
        TEMP_ARTWORK_COMMAND,
        TEMP_ARTIST_COMMAND,
        TEMP_TAG_COMMAND,
        "pixiv",
        "pa",
        "tags",
    ]
    assert len(plugin._command_records) == 3


@pytest.mark.anyio
async def test_command_conflict_falls_back_to_pi(monkeypatch):
    calls = []

    async def rename(handler, command, aliases):
        calls.append(command)
        if command == "occupied":
            raise ValueError("conflict")

    install_command_module(monkeypatch, rename)
    plugin = PixivLookupPlugin(object(), {"command_name": "occupied"})
    plugin.file_logs = FakeLog()
    await plugin._configure_commands()
    assert calls == [
        TEMP_ARTWORK_COMMAND,
        TEMP_ARTIST_COMMAND,
        TEMP_TAG_COMMAND,
        "occupied",
        "pi",
        "pa",
        "pt",
    ]
    assert len(plugin._command_records) == 3


@pytest.mark.anyio
async def test_two_configured_commands_cannot_have_the_same_name(monkeypatch):
    calls = []

    async def rename(handler, command, aliases):
        calls.append(command)

    install_command_module(monkeypatch, rename)
    plugin = PixivLookupPlugin(
        object(),
        {"command_name": "same", "artist_command_name": "/same"},
    )
    plugin.file_logs = FakeLog()
    await plugin._configure_commands()
    assert calls == [
        TEMP_ARTWORK_COMMAND,
        TEMP_ARTIST_COMMAND,
        TEMP_TAG_COMMAND,
        DEFAULT_COMMAND,
        DEFAULT_ARTIST_COMMAND,
        "pt",
    ]
    assert any(event[1] == "command_group_conflict" for event in plugin.file_logs.events)


@pytest.mark.anyio
async def test_phelp_is_reserved_from_query_command_renaming(monkeypatch):
    calls = []

    async def rename(handler, command, aliases):
        calls.append(command)

    install_command_module(monkeypatch, rename)
    plugin = PixivLookupPlugin(object(), {"command_name": "phelp"})
    plugin.file_logs = FakeLog()

    await plugin._configure_commands()

    assert calls == [
        TEMP_ARTWORK_COMMAND,
        TEMP_ARTIST_COMMAND,
        TEMP_TAG_COMMAND,
        "pi",
        "pa",
        "pt",
    ]
    assert plugin._active_commands["artwork"] == "pi"
    assert any(
        event[1] == "command_reserved_conflict"
        for event in plugin.file_logs.events
    )


def make_artwork(rating=Rating.SAFE):
    return Artwork(
        illust_id=123,
        title="作品",
        author_name="作者",
        author_id=1,
        author_account="author",
        create_date="",
        artwork_type="illust",
        caption="",
        tags=(),
        rating=rating,
        width=100,
        height=100,
        pages=(
            ArtworkPage(1, {"large": "https://i.pximg.net/1.jpg"}),
            ArtworkPage(2, {"large": "https://i.pximg.net/2.jpg"}),
        ),
    )


def make_artist_works(*artworks, exhausted=True):
    return ArtistWorks(
        profile=ArtistProfile(42, "画师", "artist", len(artworks)),
        entries=tuple(
            ArtistArtworkEntry(index, artwork.illust_id, artwork)
            for index, artwork in enumerate(artworks, 1)
        ),
        exhausted=exhausted,
    )


class FakeProvider:
    def __init__(self, artwork, artist_works=None, tag_page=None):
        self.artwork = artwork
        self.artist_works = artist_works
        self.tag_page = tag_page
        self.closed = False
        self.artist_calls = []
        self.tag_calls = []

    async def get_artwork(self, illust_id):
        return self.artwork

    async def get_artist_artworks(self, artist_id, limit, start_position=1):
        self.artist_calls.append((artist_id, limit, start_position))
        if self.artist_works is None:
            return None
        entries = tuple(
            entry
            for entry in self.artist_works.entries
            if entry.position >= start_position
        )[:limit]
        return replace(self.artist_works, entries=entries)

    async def search_tag_page(self, word, **kwargs):
        self.tag_calls.append((word, kwargs))
        return self.tag_page

    async def close(self):
        self.closed = True


class FakeProxy:
    def __init__(self):
        self.closed = False
        self.calls = []

    async def fetch(self, page, quality):
        self.calls.append((page.index, quality))
        return DownloadedImage(b"image", "image/jpeg", quality, "i.pixiv.re")

    async def close(self):
        self.closed = True


class FakeSender:
    def __init__(self):
        self.calls = []
        self.batch_calls = []

    async def send_artwork(self, event, info, page, image, *, as_forward):
        self.calls.append((page, as_forward))
        return "88"

    async def send_artworks(self, event, header, items, *, as_forward):
        self.batch_calls.append((header, items, as_forward))
        return (str(90 + len(self.batch_calls)),)

    async def recall(self, bot, message_id, self_id=""):
        self.calls.append(("recall", message_id, self_id))


class FakeRecall:
    def __init__(self):
        self.calls = []

    def schedule(self, *args):
        self.calls.append(args)

    async def shutdown(self):
        self.calls.append(("shutdown",))


class FakeEvent:
    bot = object()

    def __init__(self):
        self.stopped = False

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def get_self_id(self):
        return "42"


@pytest.mark.anyio
async def test_phelp_lists_all_commands_formats_and_active_custom_names():
    plugin = PixivLookupPlugin(object(), {"artist_max_results": 15})
    plugin._active_commands.update(
        {
            "artwork": "pixiv",
            "artist": "artist",
            "tag": "tag",
        },
    )
    event = FakeEvent()

    results = [item async for item in plugin.pixiv_help(event)]

    assert event.stopped
    assert len(results) == 1
    help_text = results[0]
    assert "Pixiv 查询 v1.2.0 指令帮助" in help_text
    assert "/pixiv <作品ID> [图片页码]" in help_text
    assert "/artist <画师ID> [数量或排名] [1|0]" in help_text
    assert "当前上限为 15" in help_text
    assert "/tag <标签...> [数量或排名] [1|0] [搜索页]" in help_text
    assert "每个搜索页最多 60 项" in help_text
    assert '/tag "black hair"' in help_text
    assert "/phelp" in help_text
    assert "/pixiv <作品ID> <图片页码>" in help_text


@pytest.mark.anyio
async def test_page_out_of_range_returns_actual_total():
    plugin = PixivLookupPlugin(object(), {})
    plugin.provider = FakeProvider(make_artwork())
    plugin.image_proxy = FakeProxy()
    event = FakeEvent()
    results = [item async for item in plugin.pixiv_lookup(event, "123 3")]
    assert results == ["该作品共有 2 幅，页码 3 超出范围。"]


@pytest.mark.anyio
async def test_r18_default_block_and_enabled_120_second_recall():
    event = FakeEvent()
    plugin = PixivLookupPlugin(object(), {})
    plugin.provider = FakeProvider(make_artwork(Rating.R18))
    plugin.image_proxy = FakeProxy()
    blocked = [item async for item in plugin.pixiv_lookup(event, "123")]
    assert blocked and "不能发送" in blocked[0]

    plugin.config = {
        "r18_enabled": True,
        "r18_recall_seconds": 120,
        "send_as_forward": True,
        "image_size": "large",
    }
    plugin.sender = FakeSender()
    plugin.recall_manager = FakeRecall()
    sent = [item async for item in plugin.pixiv_lookup(event, "123 2")]
    assert sent == []
    assert plugin.sender.calls == [("Pixiv 123：第 2/2 幅", True)]
    assert plugin.recall_manager.calls[0][1:] == ("88", "42", 120)


@pytest.mark.anyio
async def test_pi_r18g_is_controlled_only_by_r18g_switch():
    event = FakeEvent()
    plugin = PixivLookupPlugin(object(), {"r18_enabled": True, "r18g_enabled": False})
    plugin.provider = FakeProvider(make_artwork(Rating.R18G))
    plugin.image_proxy = FakeProxy()
    blocked = [item async for item in plugin.pixiv_lookup(event, "123")]
    assert blocked and "R18G 开关未开启" in blocked[0]

    plugin.config = {
        "r18_enabled": False,
        "r18g_enabled": True,
        "r18_recall_seconds": 120,
    }
    plugin.sender = FakeSender()
    plugin.recall_manager = FakeRecall()
    sent = [item async for item in plugin.pixiv_lookup(event, "123")]
    assert sent == []
    assert plugin.recall_manager.calls[0][1:] == ("88", "42", 120)


@pytest.mark.anyio
async def test_artist_latest_keeps_shortage_and_blocked_items_without_replacement():
    safe = replace(make_artwork(), illust_id=101)
    r18 = replace(make_artwork(Rating.R18), illust_id=102)
    plugin = PixivLookupPlugin(object(), {})
    plugin.provider = FakeProvider(safe, make_artist_works(safe, r18))
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()
    plugin.recall_manager = FakeRecall()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 3")]

    assert len(plugin.sender.batch_calls) == 1
    assert len(plugin.sender.batch_calls[0][1]) == 1
    assert "全年龄作品" in plugin.sender.batch_calls[0][0]
    assert results and "请求 3 个作品，当前只有 2 个" in results[0]
    assert "102（R-18）" in results[0]


@pytest.mark.anyio
@pytest.mark.parametrize(("query", "query_text"), [("42", "最新 1 个"), ("42 1 0", "第 1 个最新")])
async def test_single_blocked_artist_work_still_returns_artist_profile(query, query_text):
    r18 = replace(make_artwork(Rating.R18), illust_id=150)
    plugin = PixivLookupPlugin(object(), {"r18_enabled": False})
    plugin.provider = FakeProvider(r18, make_artist_works(r18))
    proxy = FakeProxy()
    plugin.image_proxy = proxy
    plugin.sender = FakeSender()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), query)]

    assert len(results) == 1
    assert "画师：画师（ID 42，账号 artist）" in results[0]
    assert query_text in results[0]
    assert "因分级设置跳过：150（R-18）" in results[0]
    assert proxy.calls == []
    assert plugin.sender.batch_calls == []


@pytest.mark.anyio
async def test_all_blocked_artist_works_still_return_artist_profile():
    r18 = replace(make_artwork(Rating.R18), illust_id=160)
    r18g = replace(make_artwork(Rating.R18G), illust_id=161)
    plugin = PixivLookupPlugin(
        object(),
        {"r18_enabled": False, "r18g_enabled": False},
    )
    plugin.provider = FakeProvider(r18, make_artist_works(r18, r18g))
    proxy = FakeProxy()
    plugin.image_proxy = proxy
    plugin.sender = FakeSender()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 2")]

    assert len(results) == 1
    assert "画师：画师（ID 42，账号 artist）" in results[0]
    assert "查询：最新 2 个插画/动图作品" in results[0]
    assert "160（R-18）" in results[0]
    assert "161（R-18G）" in results[0]
    assert proxy.calls == []
    assert plugin.sender.batch_calls == []


@pytest.mark.anyio
async def test_artist_pick_r18g_uses_independent_switch_and_recalls_batch():
    safe = replace(make_artwork(), illust_id=201)
    r18g = replace(make_artwork(Rating.R18G), illust_id=202)
    plugin = PixivLookupPlugin(
        object(),
        {"r18_enabled": False, "r18g_enabled": True, "r18_recall_seconds": 120},
    )
    plugin.provider = FakeProvider(safe, make_artist_works(safe, r18g))
    proxy = FakeProxy()
    plugin.image_proxy = proxy
    plugin.sender = FakeSender()
    plugin.recall_manager = FakeRecall()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 2 0")]

    assert results == []
    assert proxy.calls == [(1, "large")]
    header, items, as_forward = plugin.sender.batch_calls[0]
    assert "敏感作品" in header and as_forward is True
    assert "第 2 个最新作品" in items[0].page_text
    assert "第 1/2 幅" in items[0].page_text
    assert plugin.recall_manager.calls[0][1:] == ("91", "42", 120)


@pytest.mark.anyio
async def test_artist_allowed_safe_and_sensitive_items_use_separate_messages():
    safe = replace(make_artwork(), illust_id=210)
    r18 = replace(make_artwork(Rating.R18), illust_id=211)
    plugin = PixivLookupPlugin(object(), {"r18_enabled": True})
    plugin.provider = FakeProvider(safe, make_artist_works(safe, r18))
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()
    plugin.recall_manager = FakeRecall()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 2")]

    assert results == []
    assert len(plugin.sender.batch_calls) == 2
    assert "全年龄作品" in plugin.sender.batch_calls[0][0]
    assert "敏感作品" in plugin.sender.batch_calls[1][0]
    assert plugin.recall_manager.calls[0][1:] == ("92", "42", 120)


@pytest.mark.anyio
async def test_artist_pick_out_of_range_reports_visible_count_without_sending():
    safe = replace(make_artwork(), illust_id=220)
    plugin = PixivLookupPlugin(object(), {})
    plugin.provider = FakeProvider(safe, make_artist_works(safe))
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 pick 3")]

    assert results == ["该画师当前只有 1 个可见的插画/动图作品，无法返回第 3 个最新作品。"]
    assert plugin.sender.batch_calls == []


@pytest.mark.anyio
async def test_artist_pick_position_is_not_limited_by_latest_maximum():
    artwork = replace(make_artwork(), illust_id=313)
    works = ArtistWorks(
        profile=ArtistProfile(42, "画师", "artist", 20),
        entries=(ArtistArtworkEntry(13, artwork.illust_id, artwork),),
        exhausted=False,
    )
    provider = FakeProvider(artwork, works)
    plugin = PixivLookupPlugin(object(), {"artist_max_results": 10})
    plugin.provider = provider
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 13 0")]

    assert results == []
    assert provider.artist_calls == [(42, 1, 13)]
    assert "第 13 个最新作品" in plugin.sender.batch_calls[0][1][0].page_text


@pytest.mark.anyio
async def test_artist_latest_uses_configured_maximum():
    plugin = PixivLookupPlugin(object(), {"artist_max_results": 15})

    rejected = [item async for item in plugin.artist_lookup(FakeEvent(), "42 16 1")]

    assert rejected and "1-15" in rejected[0]


@pytest.mark.anyio
async def test_tag_lookup_uses_aliases_search_page_and_sends_first_artwork_page():
    artwork = replace(make_artwork(), illust_id=701)
    page = TagSearchPage(
        entries=(TagSearchEntry(1, artwork.illust_id, artwork),),
        page=2,
        exhausted=True,
        used_search_word="黒髪",
    )
    provider = FakeProvider(artwork, tag_page=page)
    plugin = PixivLookupPlugin(object(), {"tag_translate_enabled": True})
    plugin.provider = provider
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()

    results = [item async for item in plugin.tag_lookup(FakeEvent(), "黑发 1 2")]

    assert len(results) == 1
    assert "未填写模式参数" in results[0]
    assert provider.tag_calls == [
        (
            "黒髪 500users入り",
            {
                "page": 2,
                "search_target": "partial_match_for_tags",
                "sort": "date_desc",
                "allow_ai": False,
                "excluded_tags": (),
            },
        ),
    ]
    assert plugin.image_proxy.calls == [(1, "large")]
    assert "实际搜索词：黒髪" in plugin.sender.batch_calls[0][0]
    assert "搜索页：第 2 页" in plugin.sender.batch_calls[0][0]


@pytest.mark.anyio
async def test_tag_lookup_all_blocked_returns_tag_summary_without_image():
    artwork = replace(make_artwork(Rating.R18), illust_id=702)
    page = TagSearchPage((TagSearchEntry(1, 702, artwork),), 1, True, "黒髪")
    plugin = PixivLookupPlugin(object(), {"r18_enabled": False})
    plugin.provider = FakeProvider(artwork, tag_page=page)
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()

    results = [item async for item in plugin.tag_lookup(FakeEvent(), "黑发")]

    assert len(results) == 1
    assert "标签：黑发" in results[0]
    assert "实际搜索词：黒髪" in results[0]
    assert "标签查询提示" in results[0]
    assert "因分级设置跳过：702（R-18）" in results[0]
    assert plugin.image_proxy.calls == []
    assert plugin.sender.batch_calls == []


@pytest.mark.anyio
async def test_tag_sensitive_batch_failure_without_message_id_reports_unknown_state():
    artwork = replace(make_artwork(Rating.R18), illust_id=706)
    page = TagSearchPage((TagSearchEntry(1, 706, artwork),), 1, True, "兔耳")

    class FailedSender(FakeSender):
        async def send_artworks(self, event, header, items, *, as_forward):
            raise BatchMessageSendError("OneBot 批量发送图片失败") from TimeoutError

    plugin = PixivLookupPlugin(object(), {"r18_enabled": True})
    plugin.provider = FakeProvider(artwork, tag_page=page)
    plugin.image_proxy = FakeProxy()
    plugin.sender = FailedSender()
    plugin.recall_manager = FakeRecall()

    results = [item async for item in plugin.tag_lookup(FakeEvent(), "兔耳")]

    assert len(results) == 1
    assert "敏感作品发送失败或状态未知" in results[0]
    assert "未取得消息 ID，无法安排自动撤回" in results[0]
    assert "仅部分发送成功" not in results[0]
    assert plugin.recall_manager.calls == []


def test_batch_failure_with_confirmed_message_id_reports_partial_unknown_state():
    text = PixivLookupPlugin._batch_send_failure_text(
        "敏感作品",
        ("123",),
        sensitive=True,
    )

    assert "仅部分确认发送成功" in text
    assert "未能自动撤回" in text


@pytest.mark.anyio
async def test_tag_popular_sort_falls_back_to_users_tag():
    artwork = replace(make_artwork(), illust_id=703)
    page = TagSearchPage((TagSearchEntry(1, 703, artwork),), 1, True, "黒髪 500users入り")

    class PopularProvider(FakeProvider):
        async def search_tag_page(self, word, **kwargs):
            self.tag_calls.append((word, kwargs))
            if kwargs["sort"] == "popular_desc":
                raise ProviderError("需要会员")
            return page

    provider = PopularProvider(artwork)
    plugin = PixivLookupPlugin(
        object(),
        {
            "tag_sort": "popular_desc",
            "tag_popular_fallback_enabled": True,
            "tag_popular_bookmark_threshold": 500,
        },
    )
    plugin.provider = provider
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()

    results = [item async for item in plugin.tag_lookup(FakeEvent(), "黑发")]

    assert len(provider.tag_calls) == 2
    assert provider.tag_calls[0][0] == "黒髪 500users入り"
    assert provider.tag_calls[0][1]["sort"] == "popular_desc"
    assert provider.tag_calls[1][0] == "黒髪 500users入り"
    assert provider.tag_calls[1][1]["sort"] == "date_desc"
    assert results and "官方热门排序不可用" in results[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("config", "raw_query", "expected_word"),
    [
        (
            {
                "tag_popular_fallback_enabled": True,
                "tag_popular_bookmark_threshold": 1000,
            },
            "黑发",
            "黒髪 1000users入り",
        ),
        (
            {
                "tag_popular_fallback_enabled": True,
                "tag_popular_bookmark_threshold": 500,
            },
            "黑发 1000users入り",
            "黒髪 1000users入り",
        ),
        (
            {"tag_popular_fallback_enabled": False},
            "黑发",
            "黒髪",
        ),
    ],
)
async def test_tag_users_tag_is_appended_as_a_separate_search_tag(
    config,
    raw_query,
    expected_word,
):
    artwork = replace(make_artwork(), illust_id=704)
    page = TagSearchPage(
        (TagSearchEntry(1, 704, artwork),),
        1,
        True,
        expected_word,
    )
    provider = FakeProvider(artwork, tag_page=page)
    plugin = PixivLookupPlugin(object(), config)
    plugin.provider = provider
    plugin.image_proxy = FakeProxy()
    plugin.sender = FakeSender()

    results = [item async for item in plugin.tag_lookup(FakeEvent(), raw_query)]

    assert results == []
    assert provider.tag_calls[0][0] == expected_word
    assert f"实际搜索词：{expected_word}" in plugin.sender.batch_calls[0][0]


@pytest.mark.anyio
async def test_artist_metadata_and_download_failures_do_not_hide_successful_items():
    failed_download = replace(
        make_artwork(),
        illust_id=301,
        pages=(ArtworkPage(1, {"large": "https://i.pximg.net/fail.jpg"}),),
    )
    safe = replace(make_artwork(), illust_id=302)
    works = ArtistWorks(
        profile=ArtistProfile(42, "画师", "artist"),
        entries=(
            ArtistArtworkEntry(1, 300, None),
            ArtistArtworkEntry(2, 301, failed_download),
            ArtistArtworkEntry(3, 302, safe),
        ),
        exhausted=True,
    )

    class PartialProxy(FakeProxy):
        async def fetch(self, page, quality):
            if page.urls["large"].endswith("fail.jpg"):
                raise ImageDownloadError("failed")
            return await super().fetch(page, quality)

    plugin = PixivLookupPlugin(object(), {})
    plugin.provider = FakeProvider(safe, works)
    plugin.image_proxy = PartialProxy()
    plugin.sender = FakeSender()
    plugin.recall_manager = FakeRecall()

    results = [item async for item in plugin.artist_lookup(FakeEvent(), "42 latest 3")]

    assert len(plugin.sender.batch_calls[0][1]) == 1
    assert "元数据异常：300" in results[0]
    assert "图片下载失败：301" in results[0]


@pytest.mark.anyio
async def test_terminate_closes_resources_and_removes_command_record(monkeypatch):
    deleted = []

    class FakeDatabase:
        async def delete_command_configs(self, names):
            deleted.extend(names)

    class ClosableLog(FakeLog):
        def __init__(self):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True

    core = sys.modules["astrbot.core"]
    monkeypatch.setattr(core, "db_helper", FakeDatabase(), raising=False)
    provider = FakeProvider(make_artwork())
    proxy = FakeProxy()
    recall = FakeRecall()
    logs = ClosableLog()
    plugin = PixivLookupPlugin(object(), {})
    plugin.provider = provider
    plugin.image_proxy = proxy
    plugin.recall_manager = recall
    plugin.file_logs = logs
    artwork_handler = plugin._handler_full_name(PixivLookupPlugin.pixiv_lookup)
    artist_handler = plugin._handler_full_name(PixivLookupPlugin.artist_lookup)
    tag_handler = plugin._handler_full_name(PixivLookupPlugin.tag_lookup)
    plugin._command_records = {artwork_handler, artist_handler, tag_handler}

    await plugin.terminate()

    assert recall.calls == [("shutdown",)]
    assert provider.closed and proxy.closed and logs.closed
    assert deleted == sorted([artwork_handler, artist_handler, tag_handler])
    assert plugin.provider is None
    assert plugin.image_proxy is None
    assert plugin.recall_manager is None
    assert plugin.file_logs is None
