"""WebUI 仪表盘后端端点（v1.1.7）的测试。

覆盖：
1. 注册守卫：宿主缺 register_web_api / 注册抛异常都不崩
2. overview：字段完整性、凭证只给布尔、静音状态透出
3. 安全红线：任何响应里都不出现 cookie 值 / JSESSIONID 字样
4. history：日末快照序列、days 夹取、未知会话报错
"""

import asyncio
import json
import sys
import time
import types
from collections import deque

from astrbot_plugin_dorm_electric.main import PLUGIN_NAME, DormElectricPlugin

SECRET = "JSESSIONID=TOPSECRET"
UMO = "qq:private:1"


class _FakeStore:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)


class _FakeContext:
    def __init__(self, fail=False):
        self.registered: list[tuple] = []
        self._fail = fail

    def register_web_api(self, path, handler, methods, desc):
        if self._fail:
            raise RuntimeError("boom")
        self.registered.append((path, handler, methods, desc))


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _binding(history=False, thresholds=None) -> dict:
    b = {
        "provider": "hjnu",
        "room_label": "校本部/春雪楼2/8层/A-8-17",
        "fees": {"ac": {"provider": "hjnu", "params": {}}},
    }
    if thresholds is not None:
        b["thresholds"] = thresholds
    if history:
        now = time.time()
        b["history_by_fee"] = {
            "ac": [
                {"t": now - 86400 * 2, "v": 100.0, "u": "度"},
                {"t": now - 86400, "v": 97.0, "u": "度"},
                {"t": now, "v": 94.66, "u": "度"},
            ],
            "elec": [{"t": now, "v": 12.3, "u": "元"}],
        }
    return b


def _plugin(bindings=None, context=None, **cfg) -> DormElectricPlugin:
    plugin = DormElectricPlugin.__new__(DormElectricPlugin)
    plugin.config = _FakeConfig({"hjnu_cookie": SECRET}, **cfg)
    plugin.store = _FakeStore(bindings)
    plugin.context = context if context is not None else _FakeContext()
    plugin._wizard = {}
    plugin._events = deque(maxlen=200)
    plugin._last_raw = {}
    plugin._bind_tokens = {}
    plugin._lookup_cache = {}
    plugin._last_room = {}
    plugin._alert_muted = {}
    plugin._pending_alerts = {}
    return plugin


def _call(plugin, coro):
    return asyncio.run(coro)


def _install_quart_stub(args: dict):
    mod = types.ModuleType("quart")

    class _Request:
        pass

    _Request.args = args
    mod.request = _Request()
    sys.modules["quart"] = mod
    return mod


def _uninstall_quart_stub():
    sys.modules.pop("quart", None)


# ================= 注册 =================


def test_register_dashboard_two_endpoints():
    ctx = _FakeContext()
    plugin = _plugin(context=ctx)
    plugin._register_dashboard()
    paths = [r[0] for r in ctx.registered]
    assert paths == [
        f"/{PLUGIN_NAME}/dashboard/overview",
        f"/{PLUGIN_NAME}/dashboard/history",
    ]
    assert ctx.registered[0][2] == ["GET"]


def test_register_dashboard_without_host_support():
    class _NoApi:
        pass

    plugin = _plugin(context=_NoApi())
    plugin._register_dashboard()  # 不应抛异常


def test_register_dashboard_with_host_error():
    plugin = _plugin(context=_FakeContext(fail=True))
    plugin._register_dashboard()  # 不应抛异常
    assert plugin.context.registered == []


# ================= overview =================


def test_overview_fields_and_no_credential_leak():
    plugin = _plugin(
        bindings={UMO: _binding(history=True)},
        poll_interval_minutes=20,
        daily_time="08:00",
    )
    resp = _call(plugin, plugin._web_overview())
    assert resp["success"] is True
    data = resp["data"]
    assert data["cookie_ok"] is True
    assert data["poll_interval_minutes"] == 20
    item = data["bindings"][0]
    assert item["umo"] == UMO
    assert item["label"].endswith("A-8-17")
    ac = item["fees"]["ac"]
    assert ac["latest_value"] == 94.66
    assert ac["warn"] == 10.0 and ac["critical"] == 5.0
    assert ac["per_day"] > 0
    raw = json.dumps(resp, ensure_ascii=False)
    assert SECRET not in raw
    assert "JSESSIONID" not in raw


def test_overview_legacy_thresholds_not_marked_custom():
    plugin = _plugin(bindings={UMO: _binding(history=True, thresholds={"warn": 20})})
    resp = _call(plugin, plugin._web_overview())
    fees = resp["data"]["bindings"][0]["fees"]
    assert fees["ac"]["custom"] is False
    assert fees["ac"]["warn"] == 20.0


def test_overview_per_fee_custom_marked():
    plugin = _plugin(
        bindings={UMO: _binding(history=True, thresholds={"elec": {"warn": 5, "critical": 2}})}
    )
    resp = _call(plugin, plugin._web_overview())
    fees = resp["data"]["bindings"][0]["fees"]
    assert fees["ac"]["custom"] is False and fees["ac"]["warn"] == 10.0
    assert fees["elec"]["custom"] is True and fees["elec"]["warn"] == 5.0


def test_overview_alert_mute_state_exposed():
    plugin = _plugin(bindings={UMO: _binding()})
    plugin._alert_muted[UMO] = time.time() + 3600
    resp = _call(plugin, plugin._web_overview())
    assert resp["data"]["bindings"][0]["alert_muted_until"] > time.time()


def test_overview_empty_store():
    plugin = _plugin()
    resp = _call(plugin, plugin._web_overview())
    assert resp["success"] is True
    assert resp["data"]["bindings"] == []
    assert resp["data"]["cookie_ok"] is True


def test_overview_no_cookie_configured():
    plugin = _plugin(bindings={UMO: _binding()}, hjnu_cookie="")
    resp = _call(plugin, plugin._web_overview())
    assert resp["data"]["cookie_ok"] is False


# ================= history =================


def test_history_daily_snapshots_with_quart_stub():
    _install_quart_stub({"umo": UMO, "days": "7"})
    try:
        plugin = _plugin(bindings={UMO: _binding(history=True)})
        resp = _call(plugin, plugin._web_history())
        assert resp["success"] is True
        data = resp["data"]
        assert data["label"].endswith("A-8-17")
        assert len(data["series"]["ac"]) == 7
        assert data["series"]["ac"][0]["date"] >= data["series"]["ac"][-1]["date"]
        assert data["series"]["ac"][0]["value"] == 94.66
        raw = json.dumps(resp, ensure_ascii=False)
        assert SECRET not in raw and "JSESSIONID" not in raw
    finally:
        _uninstall_quart_stub()


def test_history_days_clamped_to_60():
    _install_quart_stub({"umo": UMO, "days": "9999"})
    try:
        plugin = _plugin(bindings={UMO: _binding(history=True)})
        resp = _call(plugin, plugin._web_history())
        assert resp["success"] is True
        assert len(resp["data"]["series"]["ac"]) == 60
    finally:
        _uninstall_quart_stub()


def test_history_missing_days_defaults_14():
    _install_quart_stub({"umo": UMO})
    try:
        plugin = _plugin(bindings={UMO: _binding(history=True)})
        resp = _call(plugin, plugin._web_history())
        assert len(resp["data"]["series"]["ac"]) == 14
    finally:
        _uninstall_quart_stub()


def test_history_unknown_umo():
    _install_quart_stub({"umo": "nobody"})
    try:
        plugin = _plugin(bindings={UMO: _binding()})
        resp = _call(plugin, plugin._web_history())
        assert resp["success"] is False
        assert "未绑定" in resp["message"]
    finally:
        _uninstall_quart_stub()


def test_history_without_quart_falls_back_to_error_dict():
    plugin = _plugin(bindings={UMO: _binding()})
    resp = _call(plugin, plugin._web_history())
    assert resp["success"] is False
    assert "Web 框架不可用" in resp["message"]


def test_history_gap_days_are_none():
    """缺数据日占位 None（与 /电费 历史 的日末快照语义一致）。"""
    _install_quart_stub({"umo": UMO, "days": "3"})
    try:
        plugin = _plugin(bindings={UMO: _binding(history=True)})
        resp = _call(plugin, plugin._web_history())
        # elec 只有一条记录（今天），昨天/前天应为 None
        elec = resp["data"]["series"]["elec"]
        assert elec[0]["value"] == 12.3
        assert elec[1]["value"] is None
        assert elec[2]["value"] is None
    finally:
        _uninstall_quart_stub()
