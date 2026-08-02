"""R18 消息定时撤回管理。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .messaging import OneBotMessageSender


@dataclass(slots=True)
class PendingRecall:
    key: str
    bot: Any
    message_id: str
    self_id: str
    task: asyncio.Task[None] | None = None


class RecallManager:
    """管理撤回任务，并在正常卸载时提前撤回所有敏感消息。"""

    def __init__(
        self,
        sender: OneBotMessageSender,
        log_info: Callable[..., None],
        log_warning: Callable[..., None],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sender = sender
        self.log_info = log_info
        self.log_warning = log_warning
        self._sleep = sleep
        self._pending: dict[str, PendingRecall] = {}
        self._closing = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def schedule(
        self,
        bot: Any,
        message_id: str,
        self_id: str,
        delay_seconds: int,
    ) -> None:
        if self._closing:
            return
        record = PendingRecall(
            key=uuid.uuid4().hex,
            bot=bot,
            message_id=message_id,
            self_id=self_id,
        )
        record.task = asyncio.create_task(
            self._run(record, max(5, min(int(delay_seconds), 120))),
            name=f"pixiv_lookup_recall_{message_id}",
        )
        self._pending[record.key] = record

    async def _run(self, record: PendingRecall, delay: int) -> None:
        try:
            await self._sleep(delay)
            await self.sender.recall(record.bot, record.message_id, record.self_id)
            self.log_info("r18_recall_success", message_id=record.message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log_warning(
                "r18_recall_failed",
                message_id=record.message_id,
                error_type=type(exc).__name__,
            )
        finally:
            self._pending.pop(record.key, None)

    async def shutdown(self) -> None:
        self._closing = True
        records = list(self._pending.values())
        self._pending.clear()
        tasks = [record.task for record in records if record.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # 正常热重载、停用或卸载时不能遗留尚未到期的 R18 消息。
        for record in records:
            try:
                await self.sender.recall(record.bot, record.message_id, record.self_id)
                self.log_info("r18_recall_on_shutdown", message_id=record.message_id)
            except Exception as exc:
                self.log_warning(
                    "r18_recall_shutdown_failed",
                    message_id=record.message_id,
                    error_type=type(exc).__name__,
                )
