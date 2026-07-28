"""aiocqhttp/NapCat 的 OneBot 消息发送和撤回。"""

from __future__ import annotations

import base64
from typing import Any

from .exceptions import MessageSendError
from .models import DownloadedImage


def _image_segment(image: DownloadedImage) -> dict[str, Any]:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {"type": "image", "data": {"file": f"base64://{encoded}"}}


def _text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def extract_message_id(response: object) -> str | None:
    """兼容不同 OneBot 实现的直接与 data 嵌套返回结构。"""

    if not isinstance(response, dict):
        return None
    value = response.get("message_id")
    if value is None and isinstance(response.get("data"), dict):
        value = response["data"].get("message_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class OneBotMessageSender:
    """直接调用 OneBot 动作，以便取得后续撤回所需的 message_id。"""

    def __init__(self, nickname: str = "Pixiv ID 查询") -> None:
        self.nickname = nickname

    @staticmethod
    def _routing(event: Any) -> tuple[bool, str, dict[str, object]]:
        platform_name = str(event.get_platform_name()).casefold()
        if platform_name != "aiocqhttp":
            raise MessageSendError("当前平台不是受支持的 aiocqhttp")
        group_id = str(event.get_group_id() or "").strip()
        target_id = group_id or str(event.get_sender_id() or "").strip()
        if not target_id.isdigit():
            raise MessageSendError("无法确定 QQ 发送目标")
        extra: dict[str, object] = {}
        self_id = str(event.get_self_id() or "").strip()
        if self_id:
            extra["self_id"] = self_id
        return bool(group_id), target_id, extra

    async def send_artwork(
        self,
        event: Any,
        info_text: str,
        page_text: str,
        image: DownloadedImage,
        *,
        as_forward: bool,
    ) -> str:
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            raise MessageSendError("当前事件没有可用的 OneBot 客户端")
        is_group, target_id, routing = self._routing(event)
        try:
            if as_forward:
                uin = str(event.get_self_id() or "0")
                messages = [
                    {
                        "type": "node",
                        "data": {
                            "user_id": uin,
                            "nickname": self.nickname,
                            "content": [_text_segment(info_text)],
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "user_id": uin,
                            "nickname": self.nickname,
                            "content": [_text_segment(page_text), _image_segment(image)],
                        },
                    },
                ]
                action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
                target_key = "group_id" if is_group else "user_id"
                response = await bot.call_action(
                    action,
                    **{target_key: int(target_id), "messages": messages, **routing},
                )
            else:
                action = "send_group_msg" if is_group else "send_private_msg"
                target_key = "group_id" if is_group else "user_id"
                response = await bot.call_action(
                    action,
                    **{
                        target_key: int(target_id),
                        "message": [
                            _text_segment(info_text + "\n" + page_text),
                            _image_segment(image),
                        ],
                        **routing,
                    },
                )
        except Exception as exc:
            raise MessageSendError("OneBot 发送图片失败") from exc

        message_id = extract_message_id(response)
        if message_id is None:
            raise MessageSendError("OneBot 未返回 message_id，无法保证撤回")
        return message_id

    async def recall(self, bot: Any, message_id: str, self_id: str = "") -> None:
        kwargs: dict[str, object] = {
            "message_id": int(message_id) if message_id.isdigit() else message_id,
        }
        if self_id:
            kwargs["self_id"] = self_id
        try:
            await bot.call_action("delete_msg", **kwargs)
        except Exception as exc:
            raise MessageSendError("OneBot 撤回消息失败") from exc
