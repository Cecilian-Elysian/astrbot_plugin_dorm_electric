"""幕后指令注入（on_llm_request 钩子）的测试。

断言矩阵：
1. 关键词命中才注入：电费话题 → 注入；普通闲聊 → 不动 system_prompt
2. 上下文也能救回：本轮没提、近几轮提过电费 → 照样注入
3. 双开关：ai_prompt_hint / ai_tools_enabled 任一关闭都不注入
4. 幂等与安全：提示文本固定追加一次；任何输出不含 cookie 值
"""

import asyncio
import sys

import pytest
from astrbot_plugin_dorm_electric import main as plugin_main
from astrbot_plugin_dorm_electric.main import (
    AI_ELECTRIC_HINT,
    DormElectricPlugin,
)

SECRET = "JSESSIONID=SECRET"


class _FakeReq:
    def __init__(self, system_prompt=None, prompt="", contexts=None):
        self.system_prompt = system_prompt
        self.prompt = prompt
        self.contexts = contexts if contexts is not None else []


class _FakeEvent:
    def __init__(self, message=""):
        self.unified_msg_origin = "qq:private:1"
        self.message_str = message


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _plugin(**cfg) -> DormElectricPlugin:
    plugin = DormElectricPlugin.__new__(DormElectricPlugin)
    plugin.config = _FakeConfig({"hjnu_cookie": SECRET}, **cfg)
    plugin.context = None
    return plugin


def _call(plugin, coro):
    return asyncio.run(coro)


def _injected(req: _FakeReq) -> bool:
    return isinstance(req.system_prompt, str) and AI_ELECTRIC_HINT in req.system_prompt


# ================= 关键词矩阵 =================


@pytest.mark.parametrize(
    "text",
    [
        "宿舍电费还剩多少",
        "空调费还有几度",
        "帮我看看余额",
        "这个月用了多少度",
        "还能用几天啊",
        "去充值一下",
        "低于 20 提醒我",
        "别再提醒了，好烦",
        "我想绑定春雪楼817",
        "早上别播报了",
        "水电费一起交吗",
    ],
)
def test_keyword_hits_inject(text):
    plugin = _plugin()
    req = _FakeReq(system_prompt="你是月亮。")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent(text), req))
    assert _injected(req)
    assert req.system_prompt.startswith("你是月亮。")


@pytest.mark.parametrize(
    "text",
    ["今天天气不错", "几点了", "讲个笑话", "你叫什么名字", "明天周几"],
)
def test_plain_chat_not_injected(text):
    plugin = _plugin()
    req = _FakeReq(system_prompt="你是月亮。")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent(text), req))
    assert req.system_prompt == "你是月亮。"


# ================= 上下文救回 =================


def test_followup_via_context_history():
    """本轮只说「那空调呢」，近几轮聊过电费 → 照样注入。"""
    plugin = _plugin()
    req = _FakeReq(
        system_prompt="你是月亮。",
        contexts=[
            {"role": "user", "content": "宿舍电费还剩多少"},
            {"role": "assistant", "content": "94.66 度"},
            {"role": "user", "content": "那空调呢"},
        ],
    )
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("那空调呢"), req))
    assert _injected(req)


def test_multimodal_context_content_list():
    plugin = _plugin()
    req = _FakeReq(
        system_prompt="",
        contexts=[
            {"role": "user", "content": [{"type": "text", "text": "查一下电费"}]},
        ],
    )
    _call(plugin, plugin.inject_electric_hint(_FakeEvent(""), req))
    assert _injected(req)


def test_prompt_field_counts_even_without_message_str():
    plugin = _plugin()
    req = _FakeReq(system_prompt="", prompt="电费多少")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent(""), req))
    assert _injected(req)


def test_malformed_contexts_do_not_crash():
    plugin = _plugin()
    req = _FakeReq(system_prompt="x", contexts=[None, "str", {"role": "user"}, 42])
    _call(plugin, plugin.inject_electric_hint(_FakeEvent(""), req))
    assert not _injected(req)


def test_all_text_sources_empty_is_safe():
    plugin = _plugin()
    req = _FakeReq(system_prompt="x", contexts=None)
    _call(plugin, plugin.inject_electric_hint(_FakeEvent(""), req))
    assert req.system_prompt == "x"


# ================= 开关与幂等 =================


def test_ai_prompt_hint_off():
    plugin = _plugin(ai_prompt_hint=False)
    req = _FakeReq(system_prompt="你是月亮。")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("电费还剩多少"), req))
    assert req.system_prompt == "你是月亮。"


def test_ai_tools_enabled_off():
    plugin = _plugin(ai_tools_enabled=False)
    req = _FakeReq(system_prompt="你是月亮。")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("电费还剩多少"), req))
    assert req.system_prompt == "你是月亮。"


def test_injection_is_idempotent():
    plugin = _plugin()
    req = _FakeReq(system_prompt="你是月亮。")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("电费还剩多少"), req))
    once = req.system_prompt
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("空调费呢"), req))
    assert req.system_prompt == once


def test_none_system_prompt_becomes_hint_only():
    plugin = _plugin()
    req = _FakeReq(system_prompt=None)
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("电费还剩多少"), req))
    assert req.system_prompt == AI_ELECTRIC_HINT


def test_no_credential_leak_in_hint():
    plugin = _plugin()
    req = _FakeReq(system_prompt="")
    _call(plugin, plugin.inject_electric_hint(_FakeEvent("电费还剩多少"), req))
    assert SECRET not in (req.system_prompt or "")
    assert "JSESSIONID=" not in (req.system_prompt or "")


# ================= 宿主兼容 =================


def test_hook_fallback_without_host_support():
    """宿主没有 on_llm_request 属性时，装饰器退化为 no-op，插件照常加载。"""
    holder = plugin_main.filter
    cls = type(holder)
    saved = getattr(cls, "on_llm_request", None)
    had = saved is not None
    if had:
        del cls.on_llm_request
    try:
        deco = plugin_main._llm_request_hook()

        def _func():
            return 1

        assert deco(_func) is _func
    finally:
        if had:
            cls.on_llm_request = saved


def test_keyword_tuple_has_no_duplicates():
    assert len(plugin_main.AI_HINT_KEYWORDS) == len(set(plugin_main.AI_HINT_KEYWORDS))


def test_stub_module_importable_without_astrbot_host():
    """conftest 桩里必须有 on_llm_request，否则 import main 直接 AttributeError。"""
    mod = sys.modules.get("astrbot.api.event")
    assert mod is not None and hasattr(mod.filter, "on_llm_request")
