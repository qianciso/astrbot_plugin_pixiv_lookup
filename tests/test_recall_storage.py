from __future__ import annotations

import asyncio
import os
import time

import pytest

from astrbot_plugin_pixiv_lookup.recall import RecallManager
from astrbot_plugin_pixiv_lookup.storage import PluginLogManager


class FakeSender:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def recall(self, bot, message_id, self_id=""):
        self.calls.append((bot, message_id, self_id))
        if self.fail:
            raise RuntimeError("delete failed")


@pytest.mark.anyio
async def test_recall_runs_after_configured_120_seconds():
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    sender = FakeSender()
    info = []
    manager = RecallManager(
        sender,
        lambda event, **kw: info.append(event),
        lambda *a, **k: None,
        sleep=fake_sleep,
    )
    manager.schedule("bot", "10", "20", 120)
    task = next(iter(manager._pending.values())).task
    assert task is not None
    await task

    assert delays == [120]
    assert sender.calls == [("bot", "10", "20")]
    assert "r18_recall_success" in info
    assert manager.pending_count == 0


@pytest.mark.anyio
async def test_shutdown_immediately_recalls_pending_messages():
    blocker = asyncio.Event()

    async def blocked_sleep(delay):
        await blocker.wait()

    sender = FakeSender()
    manager = RecallManager(
        sender,
        lambda *a, **k: None,
        lambda *a, **k: None,
        sleep=blocked_sleep,
    )
    manager.schedule("bot", "11", "22", 120)
    await manager.shutdown()

    assert sender.calls == [("bot", "11", "22")]
    assert manager.pending_count == 0


@pytest.mark.anyio
async def test_shutdown_logs_recall_failure():
    blocker = asyncio.Event()

    async def blocked_sleep(delay):
        await blocker.wait()

    warnings = []
    manager = RecallManager(
        FakeSender(fail=True),
        lambda *a, **k: None,
        lambda event, **kw: warnings.append((event, kw)),
        sleep=blocked_sleep,
    )
    manager.schedule("bot", "12", "22", 120)
    await manager.shutdown()
    assert warnings[0][0] == "r18_recall_shutdown_failed"


@pytest.mark.anyio
async def test_log_cleanup_rotation_and_close(tmp_path):
    manager = PluginLogManager(tmp_path, retention_days=1)
    old = manager.log_dir / "plugin.log.2020-01-01"
    fresh = manager.log_dir / "plugin.log.2099-01-01"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    old_time = time.time() - 3 * 86400
    os.utime(old, (old_time, old_time))

    assert manager.cleanup_once() == 1
    assert not old.exists()
    assert fresh.exists()
    manager.info("safe_event", value="line one\nline two")
    manager.start()
    await manager.close()
    text = (manager.log_dir / "plugin.log").read_text(encoding="utf-8")
    assert "value=line one line two" in text
