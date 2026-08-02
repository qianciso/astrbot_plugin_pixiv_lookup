from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest
from astrbot_plugin_pixiv_lookup.exceptions import ImageDownloadError
from astrbot_plugin_pixiv_lookup.main import (
    DEFAULT_ARTIST_COMMAND,
    DEFAULT_COMMAND,
    TEMP_ARTIST_COMMAND,
    TEMP_ARTWORK_COMMAND,
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
    assert schema["command_name"]["default"] == "pi"
    assert schema["artist_command_name"]["default"] == "pa"
    assert schema["artist_max_results"]["default"] == 10
    assert schema["artist_max_results"]["slider"] == {
        "min": 1,
        "max": 20,
        "step": 1,
    }
    assert schema["r18_enabled"]["default"] is False
    assert schema["r18g_enabled"]["default"] is False
    assert schema["r18_recall_seconds"]["default"] == 120
    assert schema["r18_recall_seconds"]["slider"] == {"min": 5, "max": 120, "step": 5}
    assert schema["image_size"]["options"] == [
        "original",
        "large",
        "medium",
        "square_medium",
    ]
    assert schema["primary_image_proxy"]["options"] == ["i.pixiv.re", "i.pixiv.nl"]


def test_v11_version_metadata_and_readme_notice_are_synchronized():
    root = Path(__file__).resolve().parents[1]
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "version: v1.1.0" in metadata
    assert 'version = "1.1.0"' in project
    assert "## v1.1.0 更新内容" in readme[:1000]
    assert "/pa" in readme[:1000]


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
    plugin = PixivLookupPlugin(object(), {"command_name": "/pixiv"})
    plugin.file_logs = FakeLog()
    await plugin._configure_commands()
    assert [call[1] for call in calls] == [
        TEMP_ARTWORK_COMMAND,
        TEMP_ARTIST_COMMAND,
        "pixiv",
        "pa",
    ]
    assert len(plugin._command_records) == 2


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
        "occupied",
        "pi",
        "pa",
    ]
    assert len(plugin._command_records) == 2


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
        DEFAULT_COMMAND,
        DEFAULT_ARTIST_COMMAND,
    ]
    assert any(event[1] == "command_pair_conflict" for event in plugin.file_logs.events)


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
    def __init__(self, artwork, artist_works=None):
        self.artwork = artwork
        self.artist_works = artist_works
        self.closed = False
        self.artist_calls = []

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
    plugin._command_records = {artwork_handler, artist_handler}

    await plugin.terminate()

    assert recall.calls == [("shutdown",)]
    assert provider.closed and proxy.closed and logs.closed
    assert deleted == sorted([artwork_handler, artist_handler])
    assert plugin.provider is None
    assert plugin.image_proxy is None
    assert plugin.recall_manager is None
    assert plugin.file_logs is None
