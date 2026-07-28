"""CI 中使用的最小 AstrBot API 替身。

真实兼容性另由本机 AstrBot 4.26.7 的只读导入检查覆盖；单元测试不应为了导入
领域模块而安装整个机器人框架。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
from pathlib import Path

import pytest

# GitHub Actions 的检出目录名称不应影响导入。AstrBot 安装后会把插件目录作为
# 包导入；单元测试在仓库根执行时，在这里构造同名包以复现该加载方式。
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
plugin_package = types.ModuleType("astrbot_plugin_pixiv_lookup")
plugin_package.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("astrbot_plugin_pixiv_lookup", plugin_package)


@pytest.fixture
def anyio_backend():
    """插件代码基于 asyncio；测试不额外尝试 Trio 后端。"""

    return "asyncio"


try:
    import astrbot  # noqa: F401
except ImportError:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_filter = types.ModuleType("astrbot.core.star.filter")
    command = types.ModuleType("astrbot.core.star.filter.command")

    class DummyConfig(dict):
        pass

    class DummyEvent:
        pass

    class DummyFilter:
        @staticmethod
        def command(name):
            def decorator(func):
                return func

            return decorator

    class DummyStar:
        def __init__(self, context, config=None):
            self.context = context

    class DummyStarTools:
        @staticmethod
        def get_data_dir(name):
            return Path(tempfile.gettempdir()) / name

    class GreedyStr(str):
        pass

    api.AstrBotConfig = DummyConfig
    api.logger = logging.getLogger("astrbot-test")
    event.AstrMessageEvent = DummyEvent
    event.filter = DummyFilter
    star.Context = object
    star.Star = DummyStar
    star.StarTools = DummyStarTools
    command.GreedyStr = GreedyStr

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.star": core_star,
            "astrbot.core.star.filter": core_filter,
            "astrbot.core.star.filter.command": command,
        },
    )
