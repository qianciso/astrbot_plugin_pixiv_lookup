"""插件独立日志的轮转、清理和安全写入。"""

from __future__ import annotations

import asyncio
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class PluginLogManager:
    """维护不包含凭据、图片地址或图片内容的插件日志。"""

    def __init__(self, data_dir: Path, retention_days: int) -> None:
        self.log_dir = data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, min(int(retention_days), 30))
        self.logger = logging.getLogger(f"astrbot_plugin_pixiv_lookup.file.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self._handler = TimedRotatingFileHandler(
            self.log_dir / "plugin.log",
            when="midnight",
            backupCount=self.retention_days,
            encoding="utf-8",
            utc=False,
        )
        self._handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s"),
        )
        self.logger.addHandler(self._handler)
        self._cleanup_task: asyncio.Task[None] | None = None

    def info(self, event: str, **fields: object) -> None:
        self.logger.info(self._format(event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.logger.warning(self._format(event, fields))

    @staticmethod
    def _format(event: str, fields: dict[str, object]) -> str:
        # 调用方只传递作品 ID、页码、状态码等稳定字段；这里再次压平换行防止日志注入。
        parts = [f"event={event}"]
        for key, value in sorted(fields.items()):
            safe = " ".join(str(value).split())[:160]
            parts.append(f"{key}={safe}")
        return " ".join(parts)

    def cleanup_once(self, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - self.retention_days * 86400
        removed = 0
        for path in self.log_dir.glob("plugin.log.*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                self.warning("log_cleanup_failed", file=path.name)
        return removed

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(86400)
                removed = await asyncio.to_thread(self.cleanup_once)
                if removed:
                    self.info("log_cleanup", removed=removed)
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="pixiv_lookup_log_cleanup",
            )

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        self.logger.removeHandler(self._handler)
        self._handler.close()
