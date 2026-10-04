"""/电费 检查（自检）指令的凭证状态判定与端到端渲染测试。

渲染断言是必须的：v1.0.7 的按天列表方向、v1.0.8 的页码初值都是只有真跑一遍
渲染才抓到的 bug，纯函数级断言看不见。
"""

import asyncio
import time
from collections import deque

import pytest
from astrbot_plugin_dorm_electric.main import DormElectricPlugin
from astrbot_plugin_dorm_electric.providers.base import BalanceResult

UMO = "qq:12345"
LABEL = "校本部/春雪楼2/8层/A-8-17"

credential_state = DormElectricPlugin._credential_state


def _ok(value, unit="度", raw="A-8-17房间剩余电量94.66度"):
    return BalanceResult(ok=True, value=value, raw=raw, unit=unit)


def _expired():
    return BalanceResult(ok=False, value=None, raw="会话已超时", session_expired=True)


def _failed(raw="接口返回 HTTP 502"):
    return BalanceResult(ok=False, value=None, raw=raw)


# ---------- _credential_state 判定矩阵 ----------

def test_state_valid_when_school_accepted():
    assert credential_state({"ac": _ok(94.66), "elec": _ok(12.3, "元")}) == (
        "✅ 有效（学校接口已接受本次查询）"
    )


def test_state_expired_reports_91001():
    assert "91001" in credential_state({"ac": _expired(), "elec": _expired()})


def test_network_failure_is_not_reported_as_expired():
    """学校 5xx / 断网时结果为 None，绝不能误报成凭证过期。"""
    state = credential_state({"ac": None, "elec": None})
    assert "学校接口不可用" in state
    assert "91001" not in state
    assert "凭证状态未知" in state


def test_partial_success_reports_partial():
    state = credential_state({"ac": _ok(94.66), "elec": _expired()})
    assert "部分费种可用" in state


def test_other_error_reports_responded_but_no_balance():
    state = credential_state({"ac": _failed("余额 0 元"), "elec": _failed()})
    assert "未取到余额" in state


def test_empty_results_report_missing_fee_params():
    assert "没有任何费种参数" in credential_state({})


def test_ok_takes_precedence_over_expired():
    """先看有没有取到余额：学校认了会话就不该被另一路 91001 盖掉。"""
    assert credential_state({"ac": _ok(1.0), "elec": None}).startswith("✅")


# ---------- 端到端渲染 ----------

class _FakeEvent:
    unified_msg_origin = UMO

    def plain_result(self, text):
        return text


class _FakeProvider:
    def __init__(self, results: dict):
        self._results = results
        self.calls = 0

    async def fetch(self, binding):
        self.calls += 1
        return self._results.get(binding["params"]["aid"])


class _FakeStore:
    def __init__(self, binding):
        self._binding = binding

    def get_binding(self, umo):
        return self._binding


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def save_config(self):
        return None


def _binding():
    ac_params = {
        "aid": "0030000000004301",
        "area": {"areaname": "校本部"},
        "building": {"building": "春雪楼2"},
        "floor": {"floor": "8层"},
        "room": {"room": "A-8-17", "roomid": 646},
    }
    elec_params = dict(ac_params, aid="0030000000014501")
    return {
        "provider": "hjnu",
        "room_label": LABEL,
        "params": ac_params,
        "fees": {
            "ac": {"provider": "hjnu", "params": ac_params},
            "elec": {"provider": "hjnu", "params": elec_params},
        },
    }


def _plugin(results, cookie="JSESSIONID=abc", events=0, binding=True):
    plugin = DormElectricPlugin.__new__(DormElectricPlugin)
    plugin.config = _FakeConfig({"hjnu_cookie": cookie})
    plugin.store = _FakeStore(_binding() if binding else None)
    plugin.hjnu = _FakeProvider(results)
    plugin._events = deque(
        [
            {
                "t": time.time() - 60 * (i + 1),
                "kind": "poll",
                "text": f"第 {i} 次",
                "umo": UMO,
            }
            for i in range(events)
        ],
        maxlen=200,
    )
    plugin._last_raw = {}
    plugin._alert_muted = {}
    return plugin


def _call(plugin) -> str:
    async def _run():
        out = [r async for r in plugin.cmd_check(_FakeEvent())]
        return out[0]

    return asyncio.run(_run())


def test_check_renders_four_sections_and_does_not_touch_history():
    """自检不写历史：否则用户多敲几次会稀释 24h 用电 / 日均统计。"""
    plugin = _plugin(
        {"0030000000004301": _ok(94.66), "0030000000014501": _ok(12.3, "元", "余额：12.30元")},
        events=2,
    )
    binding = plugin.store.get_binding(UMO)
    text = _call(plugin)
    assert "🔎 电费自检" in text
    assert f"🔎 电费自检 {LABEL}" in text
    assert "凭证：✅ 有效" in text
    assert "已关联：空调费、宿舍电费" in text
    assert "  空调费：94.66 度" in text
    assert "  宿舍电费：12.30 元" in text
    assert "最近事件（新→旧）" in text
    assert "第 1 次" in text and "第 0 次" in text
    assert binding.get("history_by_fee") is None
    # 原始返回照旧刷新，供 /电费 日志 查看
    assert set(plugin._last_raw[UMO]) == {"ac", "elec"}


def test_check_events_are_newest_first():
    plugin = _plugin({"0030000000004301": _ok(1.0)}, events=3)
    text = _call(plugin)
    assert text.index("第 2 次") < text.index("第 1 次") < text.index("第 0 次")


def test_check_reports_91001_instead_of_credential_unverified():
    plugin = _plugin({"0030000000004301": _expired(), "0030000000014501": _expired()})
    text = _call(plugin)
    assert "凭证：⚠️ 学校已拒绝（retcode 91001 会话超时）" in text
    assert "空调费：凭证已失效" in text


def test_check_reports_network_failure_separately():
    plugin = _plugin({}, cookie="JSESSIONID=abc")
    # 两种 aid 都查不到 → provider 抛错被 _fetch_entry 吞掉 → 结果 None
    text = _call(plugin)
    assert "学校接口不可用" in text
    # 明细不能整段空白，要逐个费种说明为什么没有响应
    assert "空调费：❌ 未取到响应" in text
    assert "宿舍电费：❌ 未取到响应" in text


def test_check_without_cookie_short_circuits():
    plugin = _plugin({"0030000000004301": _ok(1.0)}, cookie="  ")
    text = _call(plugin)
    assert "凭证：❌ 未配置" in text
    assert "绑定：✅ 校本部/春雪楼2/8层/A-8-17" in text
    assert plugin.hjnu.calls == 0


def test_check_without_binding_short_circuits():
    plugin = _plugin({"0030000000004301": _ok(1.0)}, binding=False)
    text = _call(plugin)
    assert "凭证：✅ 已配置（尚未验证，绑定后可验证）" in text
    assert "绑定：❌ 未绑定" in text
    assert plugin.hjnu.calls == 0


def test_check_never_echoes_cookie_value():
    """凭证是全局密钥，任何分支都不能把值打到消息里。"""
    for cookie, results in (
        ("JSESSIONID=SECRETVALUE", {"0030000000004301": _ok(1.0)}),
        ("JSESSIONID=SECRETVALUE", {}),
        ("", {}),
    ):
        plugin = _plugin(results, cookie=cookie)
        assert "SECRETVALUE" not in _call(plugin)


def test_check_records_one_event_without_duplicate_flood():
    plugin = _plugin({"0030000000004301": _ok(1.0)})
    _call(plugin)
    _call(plugin)
    checks = [ev for ev in plugin._events if "自检" in ev["text"]]
    assert len(checks) == 2
    assert checks[0]["text"] == "自检：✅ 有效"
    assert checks[0]["umo"] == UMO


@pytest.mark.parametrize("n_events", [0, 1, 9])
def test_check_event_tail_is_capped_at_five(n_events):
    plugin = _plugin({"0030000000004301": _ok(1.0)}, events=n_events)
    text = _call(plugin)
    body = text.split("最近事件（新→旧）：")[1]
    assert body.count("[poll]") == min(n_events, 5)
    if n_events == 0:
        assert "（暂无事件）" in body


def test_check_shows_mute_line():
    plugin = _plugin({"0030000000004301": _ok(1.0)})
    plugin._alert_muted[UMO] = time.time() + 3600
    assert "预警静音中" in _call(plugin)
