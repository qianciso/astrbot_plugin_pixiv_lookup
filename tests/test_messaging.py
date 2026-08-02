from __future__ import annotations

import base64

import pytest
from astrbot_plugin_pixiv_lookup.exceptions import (
    BatchMessageSendError,
    MessageSendError,
)
from astrbot_plugin_pixiv_lookup.messaging import (
    OneBotMessageSender,
    extract_message_id,
    split_artwork_items,
)
from astrbot_plugin_pixiv_lookup.models import ArtworkMessageItem, DownloadedImage


class FakeBot:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = {"message_id": 987} if response is None else response
        self.error = error
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.error:
            raise self.error
        return self.response


class FakeEvent:
    def __init__(self, *, group_id="10001", sender_id="20002", platform="aiocqhttp"):
        self.bot = FakeBot()
        self.group_id = group_id
        self.sender_id = sender_id
        self.platform = platform

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return "30003"


def image() -> DownloadedImage:
    return DownloadedImage(b"image-bytes", "image/jpeg", "large", "i.pixiv.re")


def batch_item(data=b"image-bytes", label="作品") -> ArtworkMessageItem:
    image_value = DownloadedImage(data, "image/jpeg", "large", "i.pixiv.re")
    return ArtworkMessageItem(label, "第 1/1 幅", image_value)


@pytest.mark.parametrize(
    ("response", "expected"),
    [({"message_id": 1}, "1"), ({"data": {"message_id": "2"}}, "2"), ({}, None)],
)
def test_extract_message_id(response, expected):
    assert extract_message_id(response) == expected


@pytest.mark.anyio
async def test_group_forward_payload_and_message_id():
    event = FakeEvent()
    sender = OneBotMessageSender()
    message_id = await sender.send_artwork(event, "信息", "第 1/2 幅", image(), as_forward=True)

    assert message_id == "987"
    action, payload = event.bot.calls[0]
    assert action == "send_group_forward_msg"
    assert payload["group_id"] == 10001
    assert payload["self_id"] == "30003"
    assert len(payload["messages"]) == 2
    encoded = payload["messages"][1]["data"]["content"][1]["data"]["file"]
    assert encoded == "base64://" + base64.b64encode(b"image-bytes").decode("ascii")


@pytest.mark.anyio
async def test_private_normal_payload():
    event = FakeEvent(group_id="")
    sender = OneBotMessageSender()
    await sender.send_artwork(event, "信息", "第 1/1 幅", image(), as_forward=False)

    action, payload = event.bot.calls[0]
    assert action == "send_private_msg"
    assert payload["user_id"] == 20002
    assert payload["message"][0]["data"]["text"] == "信息\n第 1/1 幅"


@pytest.mark.anyio
async def test_group_normal_payload():
    event = FakeEvent()
    await OneBotMessageSender().send_artwork(
        event,
        "信息",
        "第 1/1 幅",
        image(),
        as_forward=False,
    )
    action, payload = event.bot.calls[0]
    assert action == "send_group_msg"
    assert payload["group_id"] == 10001


@pytest.mark.anyio
async def test_private_forward_uses_napcat_action():
    event = FakeEvent(group_id="")
    await OneBotMessageSender().send_artwork(
        event,
        "信息",
        "第 1/1 幅",
        image(),
        as_forward=True,
    )
    assert event.bot.calls[0][0] == "send_private_forward_msg"


@pytest.mark.anyio
async def test_send_rejects_wrong_platform_and_missing_message_id():
    with pytest.raises(MessageSendError):
        await OneBotMessageSender().send_artwork(
            FakeEvent(platform="webchat"),
            "信息",
            "页码",
            image(),
            as_forward=False,
        )

    event = FakeEvent()
    event.bot.response = {"status": "ok", "data": {}}
    with pytest.raises(MessageSendError, match="message_id"):
        await OneBotMessageSender().send_artwork(
            event,
            "信息",
            "页码",
            image(),
            as_forward=False,
        )


@pytest.mark.anyio
async def test_send_and_recall_wrap_onebot_failures():
    event = FakeEvent()
    event.bot.error = RuntimeError("transport down")
    with pytest.raises(MessageSendError):
        await OneBotMessageSender().send_artwork(
            event,
            "信息",
            "页码",
            image(),
            as_forward=False,
        )
    with pytest.raises(MessageSendError):
        await OneBotMessageSender().recall(event.bot, "987", "30003")


@pytest.mark.anyio
async def test_recall_uses_delete_msg_and_numeric_id():
    bot = FakeBot()
    await OneBotMessageSender().recall(bot, "987", "30003")
    assert bot.calls == [("delete_msg", {"message_id": 987, "self_id": "30003"})]


def test_batch_items_are_split_by_raw_image_size():
    chunks = split_artwork_items(
        [batch_item(b"12"), batch_item(b"34"), batch_item(b"5")],
        max_bytes=3,
    )
    assert [len(chunk) for chunk in chunks] == [1, 2]


@pytest.mark.anyio
async def test_batch_forward_and_normal_payloads():
    sender = OneBotMessageSender()
    event = FakeEvent()
    ids = await sender.send_artworks(
        event,
        "画师信息",
        [batch_item(label="作品一"), batch_item(label="作品二")],
        as_forward=True,
    )
    assert ids == ("987",)
    action, payload = event.bot.calls[0]
    assert action == "send_group_forward_msg"
    assert len(payload["messages"]) == 3
    assert payload["messages"][0]["data"]["content"][0]["data"]["text"] == "画师信息"
    assert "作品二" in payload["messages"][2]["data"]["content"][0]["data"]["text"]

    private_event = FakeEvent(group_id="")
    await sender.send_artworks(
        private_event,
        "画师信息",
        [batch_item()],
        as_forward=False,
    )
    action, payload = private_event.bot.calls[0]
    assert action == "send_private_msg"
    assert payload["message"][0]["data"]["text"] == "画师信息"
    assert payload["message"][2]["type"] == "image"


@pytest.mark.anyio
async def test_batch_partial_failure_preserves_previous_message_ids(monkeypatch):
    class SequenceBot:
        def __init__(self):
            self.calls = []
            self.responses = [{"message_id": 1}, RuntimeError("transport down")]

        async def call_action(self, action, **kwargs):
            self.calls.append((action, kwargs))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr("astrbot_plugin_pixiv_lookup.messaging.MAX_BATCH_MESSAGE_BYTES", 3)
    event = FakeEvent()
    event.bot = SequenceBot()
    with pytest.raises(BatchMessageSendError) as error:
        await OneBotMessageSender().send_artworks(
            event,
            "画师信息",
            [batch_item(b"12"), batch_item(b"34")],
            as_forward=True,
        )
    assert error.value.message_ids == ("1",)
    assert len(event.bot.calls) == 2
