"""注册面冒烟测试：宿主集成层的结构性守卫。

conftest 的桩把装饰器透传 + 各测试直接调用方法，宿主真实的注册语义
（指令登记、docstring→schema 解析）在纯单测环境里是盲区——v1.1.5 曾把
@electric.command 挂在 _check_report 而不是 cmd_check 上，312 例全绿也
没发现。本文件在桩层挂元数据标记，用不变式拦截这一类错误。
"""

import asyncio
import inspect

from astrbot_plugin_dorm_electric import main

_PLUGIN_CLS = main.DormElectricPlugin


def _members():
    return vars(_PLUGIN_CLS).items()


def test_every_command_decorator_sits_on_cmd_named_function():
    for name, member in _members():
        if hasattr(member, "__dorm_command__"):
            assert name.startswith("cmd_"), (
                f"@electric.command 挂在了非 cmd_* 函数 {name} 上——"
                "宿主会把 event 当参数传进去，指令静默失效"
                "（参见 v1.1.5 的 /电费 检查 事故）"
            )


def test_every_cmd_function_carries_command_metadata():
    for name, member in _members():
        if name.startswith("cmd_") and asyncio.iscoroutinefunction(
            getattr(member, "__func__", member)
        ):
            assert hasattr(member, "__dorm_command__"), (
                f"cmd_* 函数 {name} 缺少 @electric.command 注册——"
                "它在宿主里永远不会被触发"
            )


def test_command_names_and_aliases_nonempty():
    for name, member in _members():
        meta = getattr(member, "__dorm_command__", None)
        if meta:
            cmd_name, alias = meta
            assert cmd_name and str(cmd_name).strip(), f"{name} 的指令名为空"
            assert all(a and str(a).strip() for a in alias), f"{name} 存在空别名"


def test_every_llm_tool_has_args_section():
    for name, member in _members():
        if hasattr(member, "__dorm_llm_tool__"):
            doc = inspect.getdoc(getattr(member, "__func__", member)) or ""
            params = [
                p
                for p in inspect.signature(
                    getattr(member, "__func__", member)
                ).parameters
                if p not in ("self", "event")
            ]
            if params:
                assert "Args:" in doc, (
                    f"LLM 工具 {name} 有参数但 docstring 缺 Args: 段——"
                    "宿主只从 Args: 段解析 schema，参数不会暴露给模型"
                )


def test_every_llm_tool_param_has_default():
    for name, member in _members():
        if hasattr(member, "__dorm_llm_tool__"):
            for pname, p in (
                inspect.signature(getattr(member, "__func__", member))
                .parameters.items()
            ):
                if pname in ("self", "event"):
                    continue
                assert p.default is not inspect.Parameter.empty, (
                    f"LLM 工具 {name} 的参数 {pname} 缺默认值——宿主生成的 "
                    "schema 没有 required，模型裸调时会 TypeError"
                )


def test_llm_tool_names_use_plugin_prefix():
    for name, member in _members():
        tool = getattr(member, "__dorm_llm_tool__", None)
        if tool:
            assert str(tool).startswith("dorm_electric_"), (
                f"工具 {name} 的注册名 {tool} 不在 dorm_electric_ 前缀内"
            )
