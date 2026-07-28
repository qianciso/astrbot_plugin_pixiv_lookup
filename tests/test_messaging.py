from __future__ import annotations

import base64

import pytest

from astrbot_plugin_pixiv_lookup.exceptions import MessageSendError
from astrbot_plugin_pixiv_lookup.messaging import OneBotMessageSender, extract_message_id
from astrbot_plugin_pixiv_lookup.models import DownloadedImage


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
