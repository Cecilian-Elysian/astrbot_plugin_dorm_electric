"""测试环境准备：注入 astrbot 桩模块，使插件 main.py 可在宿主外导入测试。

桩只覆盖 main.py 顶层 import 所需的最小表面（装饰器为透传），
被测逻辑（providers/storage/静态工具方法）均为真实实现。
"""

import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class AstrBotConfig(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

        def save_config(self):
            return None

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("astrbot.stub")

    api_event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self):
            self.chain: list[str] = []

        def message(self, text):
            self.chain.append(text)
            return self

        def get_plain_text(self):
            return "".join(str(c) for c in self.chain)

    class _CmdGroup:
        def command(self, *args, **kwargs):
            def deco(func):
                return func

            return deco

    class _EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    def _passthrough(*args, **kwargs):
        def deco(func):
            return func

        return deco

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def command_group(*args, **kwargs):
            def deco(func):
                return _CmdGroup()

            return deco

        # 事件级监听器与 LLM 工具注册器在宿主里由元数据登记，
        # 测试只需透传装饰器本身，桩不做 docstring 解析。
        event_message_type = staticmethod(_passthrough)
        llm_tool = staticmethod(_passthrough)
        on_llm_request = staticmethod(_passthrough)

    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.MessageChain = MessageChain
    api_event.filter = _Filter()

    api_star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register

    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: "data"

    astrbot.api = api
    astrbot.api.event = api_event
    astrbot.api.star = api_star
    astrbot.core = core
    core.utils = utils
    utils.astrbot_path = path_mod

    for name, mod in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
        ("astrbot.core", core),
        ("astrbot.core.utils", utils),
        ("astrbot.core.utils.astrbot_path", path_mod),
    ):
        sys.modules[name] = mod


_install_stubs()
