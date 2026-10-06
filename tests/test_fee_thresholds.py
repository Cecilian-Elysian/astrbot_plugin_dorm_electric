"""分费种预警线（v1.1.7）的测试。

覆盖：
1. 生效线三层回退：会话 per-fee > 全局 per-fee 配置 > 全局旧配置
2. 存量数据兼容：旧 flat {"warn","critical"} 视为两费种共用；新旧混合不崩
3. 工具与指令的 kind 矩阵：all/ac/elec/中文、状态查看、恢复全局、群聊闸门
4. _evaluate_alerts 按费种各自触发
5. 每日播报的 24h 充值检测行
"""

import asyncio
import time

from astrbot_plugin_dorm_electric.main import DormElectricPlugin
from conftest import make_plugin

UMO = "qq:private:1"
GROUP_UMO = "qq:group:1"
LABEL = "校本部/春雪楼2/8层/A-8-17"


class _FakeEvent:
    def __init__(self, umo=UMO, private=True):
        self.unified_msg_origin = umo
        self._private = private
        self.sent: list[str] = []

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return text

    async def send(self, chain):
        self.sent.append(str(chain))


class _FakeStore:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def set_binding(self, umo, binding):
        self.data["bindings"][umo] = binding

    def save(self):
        self.saved += 1


def _binding(thresholds=None) -> dict:
    return {
        "provider": "hjnu",
        "room_label": LABEL,
        "fees": {"ac": {"provider": "hjnu", "params": {"aid": "a"}}},
    } | ({"thresholds": thresholds} if thresholds is not None else {})


def _plugin(bindings=None, **cfg) -> DormElectricPlugin:
    return make_plugin(
        config={"hjnu_cookie": "JSESSIONID=x", **cfg},
        store=_FakeStore(bindings),
        hjnu=None,
    )


def _call(plugin, coro):
    return asyncio.run(coro)


# ================= 生效线：三层回退 =================


def test_defaults_both_fees_from_legacy_global():
    plugin = _plugin()
    assert plugin._effective_thresholds(None, "ac") == (10.0, 5.0)
    assert plugin._effective_thresholds(None, "elec") == (10.0, 5.0)


def test_global_per_fee_overrides_legacy_global():
    plugin = _plugin(threshold_warn_ac=15, threshold_critical_ac=3)
    assert plugin._effective_thresholds(None, "ac") == (15.0, 3.0)
    assert plugin._effective_thresholds(None, "elec") == (10.0, 5.0)


def test_global_per_fee_zero_falls_back():
    plugin = _plugin(threshold_warn_ac=0, threshold_critical_ac=0)
    assert plugin._effective_thresholds(None, "ac") == (10.0, 5.0)


def test_global_swap_when_critical_greater():
    plugin = _plugin(threshold_warn=5, threshold_critical=10)
    assert plugin._effective_thresholds(None, "ac") == (10.0, 5.0)


def test_legacy_flat_thresholds_apply_to_both_fees():
    plugin = _plugin(bindings={UMO: _binding({"warn": 20, "critical": 8})})
    assert plugin._effective_thresholds(plugin.store.get_binding(UMO), "ac") == (20.0, 8.0)
    assert plugin._effective_thresholds(plugin.store.get_binding(UMO), "elec") == (20.0, 8.0)


def test_new_per_fee_thresholds_are_independent():
    plugin = _plugin(
        bindings={UMO: _binding({"ac": {"warn": 30, "critical": 12}})}
    )
    b = plugin.store.get_binding(UMO)
    assert plugin._effective_thresholds(b, "ac") == (30.0, 12.0)
    assert plugin._effective_thresholds(b, "elec") == (10.0, 5.0)


def test_mixed_legacy_and_new_dict_does_not_crash():
    """旧 flat + 新 ac 并存（升级路径）：ac 用新值，elec 回落旧 flat 值。"""
    plugin = _plugin(
        bindings={
            UMO: _binding(
                {"warn": 20, "critical": 8, "ac": {"warn": 30, "critical": 15}}
            )
        }
    )
    b = plugin.store.get_binding(UMO)
    assert plugin._effective_thresholds(b, "ac") == (30.0, 15.0)
    assert plugin._effective_thresholds(b, "elec") == (20.0, 8.0)


def test_invalid_critical_falls_back_to_half():
    plugin = _plugin(
        bindings={UMO: _binding({"ac": {"warn": 30, "critical": 99}})}
    )
    assert plugin._effective_thresholds(plugin.store.get_binding(UMO), "ac") == (30.0, 15.0)


def test_threshold_lines_text():
    plugin = _plugin()
    lines = plugin._threshold_lines(None)
    assert lines[0] == "空调费：预警 10 度 / 紧急 5 度"
    assert lines[1] == "宿舍电费：预警 10 元 / 紧急 5 元"


# ================= 工具：kind 矩阵 =================


def _tool(plugin, event, *args, **kwargs):
    return _call(
        plugin,
        plugin.tool_dorm_electric_set_alert_threshold(event, *args, **kwargs),
    )


def test_tool_default_all_sets_both_fees():
    plugin = _plugin(bindings={UMO: _binding()})
    _tool(plugin, _FakeEvent(), 20)
    t = plugin.store.get_binding(UMO)["thresholds"]
    assert t["ac"] == {"warn": 20.0, "critical": 10.0}
    assert t["elec"] == {"warn": 20.0, "critical": 10.0}


def test_tool_single_kind_only_touches_that_fee():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _tool(plugin, _FakeEvent(), 5, kind="elec")
    t = plugin.store.get_binding(UMO)["thresholds"]
    assert "ac" not in t
    assert t["elec"]["warn"] == 5.0
    assert "宿舍电费" in text
    # 生效线：ac 走全局，elec 走自定义
    b = plugin.store.get_binding(UMO)
    assert plugin._effective_thresholds(b, "ac") == (10.0, 5.0)
    assert plugin._effective_thresholds(b, "elec") == (5.0, 2.5)


def test_tool_chinese_kind_alias():
    plugin = _plugin(bindings={UMO: _binding()})
    _tool(plugin, _FakeEvent(), 8, kind="空调")
    assert "ac" in plugin.store.get_binding(UMO)["thresholds"]


def test_tool_invalid_kind_rejected():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _tool(plugin, _FakeEvent(), 8, kind="水费")
    assert "kind 只支持" in text
    assert "thresholds" not in plugin.store.get_binding(UMO)


def test_tool_status_view_shows_both_fees():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _tool(plugin, _FakeEvent(), 0)
    assert "全局默认" in text
    assert "空调费：预警 10 度" in text
    assert "宿舍电费：预警 10 元" in text
    _tool(plugin, _FakeEvent(), 20, kind="ac")
    text = _tool(plugin, _FakeEvent(), 0)
    assert "自定义" in text and "空调费：预警 20 度" in text


def test_tool_restore_clears_all_fees():
    plugin = _plugin(bindings={UMO: _binding()})
    _tool(plugin, _FakeEvent(), 20)
    text = _tool(plugin, _FakeEvent(), -1)
    assert "thresholds" not in plugin.store.get_binding(UMO)
    assert "已恢复全局默认预警线" in text


def test_tool_group_write_denied_but_read_ok():
    plugin = _plugin(bindings={GROUP_UMO: _binding()})
    text = _tool(plugin, _FakeEvent(GROUP_UMO, private=False), 20)
    assert "群聊里不能绑定或解绑" in text
    text = _tool(plugin, _FakeEvent(GROUP_UMO, private=False), 0)
    assert "全局默认" in text


def test_tool_requires_binding_for_write():
    plugin = _plugin()
    text = _tool(plugin, _FakeEvent(), 20)
    assert "还没有绑定宿舍" in text


# ================= 指令流 =================


def _run_cmd(plugin, event, arg=None):
    async def _gen():
        async for r in plugin.cmd_threshold(event, arg):
            return r

    return _call(plugin, _gen())


def test_cmd_view_shows_both_fees():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _run_cmd(plugin, _FakeEvent())
    assert "空调费：预警 10 度 / 紧急 5 度" in text
    assert "宿舍电费：预警 10 元 / 紧急 5 元" in text
    assert "空调 <预警线>" in text


def test_cmd_number_sets_both():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _run_cmd(plugin, _FakeEvent(), "20")
    t = plugin.store.get_binding(UMO)["thresholds"]
    assert t["ac"]["warn"] == 20.0 and t["elec"]["warn"] == 20.0
    assert "空调费和宿舍电费" in text


def test_cmd_single_fee_prefix():
    plugin = _plugin(bindings={UMO: _binding()})
    _run_cmd(plugin, _FakeEvent(), "空调 15")
    t = plugin.store.get_binding(UMO)["thresholds"]
    assert t["ac"]["warn"] == 15.0 and "elec" not in t


def test_cmd_cancel():
    plugin = _plugin(bindings={UMO: _binding({"warn": 20})})
    text = _run_cmd(plugin, _FakeEvent(), "取消")
    assert "thresholds" not in plugin.store.get_binding(UMO)
    assert "已恢复全局默认预警线" in text


def test_cmd_group_denied():
    plugin = _plugin(bindings={GROUP_UMO: _binding()})
    text = _run_cmd(plugin, _FakeEvent(GROUP_UMO, private=False), "20")
    assert "群聊里不能绑定或解绑" in text


# ================= 预警评估按费种 =================


def test_evaluate_uses_per_fee_thresholds():
    plugin = _plugin(
        bindings={UMO: _binding({"ac": {"warn": 1, "critical": 0.5}})},
        alert_cooldown_hours=24,
    )
    b = plugin.store.get_binding(UMO)
    _call(plugin, plugin._evaluate_alerts(UMO, b, 5.0, "ac", "度"))
    # ac 的线是 1：5 度不预警
    assert UMO not in plugin._pending_alerts
    _call(plugin, plugin._evaluate_alerts(UMO, b, 5.0, "elec", "元"))
    # elec 走全局 10：5 元触发预警
    assert UMO in plugin._pending_alerts


# ================= 每日播报充值检测 =================


def _history(points):
    now = time.time()
    return [{"t": now - h * 3600, "v": v, "u": u} for h, v, u in points]


def test_daily_text_contains_recharge_detection():
    plugin = _plugin(bindings={UMO: _binding()})
    b = plugin.store.get_binding(UMO)
    b["history_by_fee"] = {
        "elec": _history([(3, 100.0, "元"), (2, 95.0, "元"), (1, 102.0, "元")]),
    }
    text = plugin._daily_text(b)
    assert "宿舍电费：102.00 元（24h 检测到充值 +7.00 元）" in text


def test_daily_text_no_recharge_no_line():
    plugin = _plugin(bindings={UMO: _binding()})
    b = plugin.store.get_binding(UMO)
    b["history_by_fee"] = {
        "ac": _history([(3, 100.0, "度"), (2, 97.0, "度"), (1, 94.66, "度")]),
    }
    text = plugin._daily_text(b)
    assert "空调费：94.66 度" in text
    assert "充值" not in text
