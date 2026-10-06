"""测试环境准备：注入 astrbot 桩模块，使插件 main.py 可在宿主外导入测试。

桩只覆盖 main.py 顶层 import 所需的最小表面（装饰器挂元数据标记供
注册面冒烟测试使用），被测逻辑（providers/storage/静态工具方法）均为真实实现。
"""

import logging
import sys
import types
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class StubAstrBotConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def save_config(self):
        return None


def make_plugin(config=None, store=None, hjnu=None, context=None, scheduler=None):
    """统一测试脚手架：__new__ 绕过 __init__ 后补全全部实例属性。

    AGENTS.md 记录过两次「某测试文件的 _plugin() 工厂漏补新属性 →
    AttributeError」的坑——根因是每个测试文件手搓一份脚手架、加字段时
    漏改。以后 __init__ 加新字段，只改这里；各测试文件的 _plugin() 一律
    薄封装本工厂。
    """
    from astrbot_plugin_dorm_electric import main as _main

    plugin = _main.DormElectricPlugin.__new__(_main.DormElectricPlugin)
    plugin.context = context
    plugin.config = StubAstrBotConfig(config or {})
    plugin.store = store
    plugin.hjnu = hjnu
    plugin.scheduler = scheduler
    plugin._wizard = {}
    plugin._tasks = []
    plugin._events = deque(maxlen=200)
    plugin._pending_alerts = {}
    plugin._last_raw = {}
    plugin._bind_tokens = {}
    plugin._lookup_cache = {}
    plugin._last_room = {}
    plugin._alert_muted = {}
    return plugin


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    api.AstrBotConfig = StubAstrBotConfig
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
        def command(self, name, *args, **kwargs):
            # 挂元数据标记：注册面冒烟测试（test_registration.py）靠它断言
            # 「指令装饰器必须落在 cmd_* 函数上」这类结构性错误。
            def deco(func):
                func.__dorm_command__ = (name, set(kwargs.get("alias") or ()))
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

        # 事件级监听器在宿主里由元数据登记，测试只需透传装饰器本身。
        event_message_type = staticmethod(_passthrough)
        on_llm_request = staticmethod(_passthrough)

        @staticmethod
        def llm_tool(name=None, **kwargs):
            # 同样挂标记：冒烟测试断言每个工具 docstring 可被宿主解析（含 Args: 段）、
            # 参数都有默认值（宿主生成的 schema 没有 required）。
            def deco(func):
                func.__dorm_llm_tool__ = name or func.__name__
                return func

            return deco

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
