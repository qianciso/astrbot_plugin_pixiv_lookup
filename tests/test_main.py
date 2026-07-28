from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from astrbot_plugin_pixiv_lookup.main import (
    PixivLookupPlugin,
    normalize_command_name,
    parse_command_args,
)
from astrbot_plugin_pixiv_lookup.models import Artwork, ArtworkPage, DownloadedImage, Rating


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


def test_normalize_command_accepts_optional_slash_and_rejects_spaces():
    assert normalize_command_name("pi") == "pi"
    assert normalize_command_name("/pixiv") == "pixiv"
    with pytest.raises(ValueError):
        normalize_command_name("pixiv search")


def test_config_schema_has_required_defaults_and_ranges():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["command_name"]["default"] == "pi"
    assert schema["r18_enabled"]["default"] is False
    assert schema["r18_recall_seconds"]["default"] == 120
    assert schema["r18_recall_seconds"]["slider"] == {"min": 5, "max": 120, "step": 5}
    assert schema["image_size"]["options"] == [
        "original",
        "large",
        "medium",
        "square_medium",
    ]
    assert schema["primary_image_proxy"]["options"] == ["i.pixiv.re", "i.pixiv.nl"]


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
    await plugin._configure_command()
    assert calls[0][1:] == ("pixiv", [])
    assert plugin._command_record_created


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
    await plugin._configure_command()
    assert calls == ["occupied", "pi"]
    assert plugin._command_record_created


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


class FakeProvider:
    def __init__(self, artwork):
        self.artwork = artwork
        self.closed = False

    async def get_artwork(self, illust_id):
        return self.artwork

    async def close(self):
        self.closed = True


class FakeProxy:
    def __init__(self):
        self.closed = False

    async def fetch(self, page, quality):
        return DownloadedImage(b"image", "image/jpeg", quality, "i.pixiv.re")

    async def close(self):
        self.closed = True


class FakeSender:
    def __init__(self):
        self.calls = []

    async def send_artwork(self, event, info, page, image, *, as_forward):
        self.calls.append((page, as_forward))
        return "88"


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
    plugin._command_record_created = True

    await plugin.terminate()

    assert recall.calls == [("shutdown",)]
    assert provider.closed and proxy.closed and logs.closed
    assert deleted == [plugin._handler_full_name()]
    assert plugin.provider is None
    assert plugin.image_proxy is None
    assert plugin.recall_manager is None
    assert plugin.file_logs is None
