"""v1.2.1 正确性修复的回归防线（Phase 1）。

这批修复的共同主题：**别把「学校挂了/凭证没了」说成「用户房间不存在」，
也别让一个坏会话连累其他会话**。每条都对应一个真实可触发的场景：

- 学校 5xx/超时 → 房间扫描全灭 → 曾报「没有 8 层 / 没找到房间」
- 每日播报循环无隔离 → 一个绑定数据损坏 → 全员当天播报消失
- 解绑后 pending 队列残留 → 收到「⚠️ 余额预警 | 未知房间」
- _match_elec_fee 吞掉 SessionExpiredError → 凭证过期被说成「room token 有问题」
- 时区认不出 Asia/Beijing → 每日播报与日志时间差 8 小时
- per-fee 紧急线=0 的回退与配置提示不符
"""

import asyncio
import time

import pytest
from astrbot_plugin_dorm_electric.providers.base import BalanceResult
from astrbot_plugin_dorm_electric.providers.http_json import (
    QueryError,
    SessionExpiredError,
)
from astrbot_plugin_dorm_electric.room_lookup import RoomResolver
from conftest import make_plugin

UMO = "qq:private:1"
UMO2 = "qq:private:2"
AC_AID = "0030000000004301"
ELEC_AID = "0030000000014501"
FEE_ITEMS = {AC_AID: "空调费（校本部）", ELEC_AID: "宿舍电费（校本部）"}

AREAS = [{"area": "1", "areaname": "1#"}, {"area": "1", "areaname": "校本部"}]
BUILDINGS = [
    {"building": "春雪楼2", "buildingid": 3},
    {"building": "春雪楼1", "buildingid": 2},
]
FLOORS = [{"floor": "7层", "floorid": 7}, {"floor": "8层", "floorid": 8}]
ROOMS_CS2_8 = [{"room": f"A-8-{i:02d}", "roomid": 640 + i} for i in (1, 2, 3, 4, 5)] + [
    {"room": "A-8-17", "roomid": 646}
]
ROOMS_CS1_8 = [{"room": f"B-8-{i:02d}", "roomid": 740 + i} for i in (1, 2, 3)]
ROOMS_BY_BUILDING = {"春雪楼2": ROOMS_CS2_8, "春雪楼1": ROOMS_CS1_8}


def _ok(value, unit="度"):
    return BalanceResult(ok=True, value=value, raw="ok", unit=unit)


class _FlakyProvider:
    """floors/rooms 可注入为异常对象；其余走正常数据。"""

    def __init__(self, floors=None, rooms=None, rooms_expire=False, elec_expired=False):
        self._floors = floors
        self._rooms = rooms
        self._rooms_expire = rooms_expire
        self._elec_expired = elec_expired

    async def list_areas(self, aid):
        if aid == ELEC_AID and self._elec_expired:
            raise SessionExpiredError("会话已超时")
        return AREAS

    async def list_buildings(self, aid, area):
        return BUILDINGS

    async def list_floors(self, aid, area, building):
        if isinstance(self._floors, BaseException):
            raise self._floors
        return self._floors if self._floors is not None else FLOORS

    async def list_rooms(self, aid, area, building, floor):
        if self._rooms_expire:
            raise SessionExpiredError("会话已超时")
        if isinstance(self._rooms, BaseException):
            raise self._rooms
        if self._rooms is not None:
            return self._rooms
        return ROOMS_BY_BUILDING[building["building"]]

    async def fetch(self, binding):
        if binding["params"]["aid"] == ELEC_AID:
            if self._elec_expired:
                raise SessionExpiredError("会话已超时")
            return _ok(12.3, "元")
        return _ok(94.66, "度")


class _FakeStore:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def set_binding(self, umo, binding):
        self.data["bindings"][umo] = binding

    def del_binding(self, umo):
        return self.data["bindings"].pop(umo, None) is not None

    def save(self, force=False):
        self.saved += 1

    @staticmethod
    def append_fee_history(binding, fee, value, unit, ts=None, keep_days=60):
        binding.setdefault("history_by_fee", {}).setdefault(fee, []).append(
            {"t": ts if ts is not None else time.time(), "v": value, "u": unit}
        )


def _resolver(provider, store=None):
    return RoomResolver(provider, store or _FakeStore(), {"fee_items": FEE_ITEMS}, {})


def _resolve(provider, hint="春雪楼817", store=None):
    return asyncio.run(_resolver(provider, store).resolve(UMO, hint))


# ================= 1.1 扫描全失败 ≠ 没找到房间 =================


def test_all_floors_failed_reports_school_error_not_missing_floor():
    err, err_text = _resolve(_FlakyProvider(floors=QueryError("接口返回 HTTP 502")))
    assert err is None
    assert "学校接口异常" in err_text
    assert "502" in err_text
    # 旧文案才是坑：不能出现「没有 8 层」/「没找到房间」
    assert "没有 8 层" not in err_text
    assert "没找到房间" not in err_text


def test_all_rooms_failed_reports_school_error_not_missing_room():
    err_text = _resolve(_FlakyProvider(rooms=QueryError("网络请求失败")), "A817")[1]
    assert "学校接口异常" in err_text
    assert "没找到房间号包含" not in err_text


def test_partial_floor_failure_still_reports_not_found():
    """部分楼栋扫失败不影响结论：该楼真没有 8 层时仍要如实说没有。"""

    class _HalfDown(_FlakyProvider):
        async def list_floors(self, aid, area, building):
            if building["building"] == "春雪楼1":
                raise QueryError("接口返回 HTTP 502")
            return FLOORS

    err_text = _resolve(_HalfDown(), "春雪楼1 9层 A917")[1]
    assert "学校接口异常" not in err_text
    assert "没有 9 层" in err_text


def test_session_expired_during_scan_still_wins():
    from astrbot_plugin_dorm_electric.main import CREDENTIAL_HINT

    err_text = _resolve(_FlakyProvider(rooms_expire=True), "春雪楼817")[1]
    assert err_text == CREDENTIAL_HINT


def test_happy_path_unaffected():
    params, err = _resolve(_FlakyProvider(), "春雪楼817")
    assert err is None
    assert params["room"]["room"] == "A-8-17"


# ================= 1.2 每日播报逐绑定隔离 =================


def _hist(value):
    return {"t": time.time(), "v": value, "u": "度"}


def _daily_store():
    return _FakeStore(
        {
            UMO: {"room_label": "校本部/春雪楼2/8层/A-8-17", "history_by_fee": {"ac": [_hist(5.0)]}},
            UMO2: {"room_label": "校本部/春雪楼2/8层/A-8-01", "history_by_fee": {"ac": [_hist(6.0)]}},
        }
    )


def test_daily_report_isolates_broken_binding(caplog):
    sent: list[tuple[str, str]] = []

    async def _send(umo, text):
        sent.append((umo, text))
        return True

    store = _daily_store()
    # 外写坏的历史（history_by_fee 里混进非 dict 元素），_daily_text 会在
    # 取时间戳时抛 TypeError —— 旧实现整个循环中断，当天全员都没播报。
    store.data["bindings"][UMO] = {
        "room_label": "校本部/春雪楼2/8层/A-8-17",
        "history_by_fee": {"ac": ["oops"]},
    }
    plugin = make_plugin(store=store)
    plugin._send = _send
    with caplog.at_level("ERROR"):
        asyncio.run(plugin._daily_all())
    # 坏的那个绑定被 try/except 吃掉，另一个照常收到
    assert [umo for umo, _ in sent] == [UMO2]
    assert any("每日播报" in r.getMessage() for r in caplog.records)


def test_daily_report_marks_date_only_after_success(monkeypatch):
    sent: list[str] = []

    async def _send(umo, text):
        sent.append(text)
        return True

    store = _daily_store()
    plugin = make_plugin(store=store)
    plugin._send = _send
    asyncio.run(plugin._daily_all())
    assert store.data["bindings"][UMO]["last_daily_date"]
    assert store.data["bindings"][UMO2]["last_daily_date"]

    sent.clear()
    asyncio.run(plugin._daily_all())
    assert sent == []  # 当天只播一次


# ================= 1.3 解绑后不再收到「未知房间」预警 =================


def test_unbind_clears_pending_alerts():
    store = _FakeStore({UMO: {"room_label": "校本部/春雪楼2/8层/A-8-17"}})
    plugin = make_plugin(store=store)
    plugin._pending_alerts[UMO] = [{"kind": "ac", "level": 1, "value": 3.0}]
    assert "已解绑" in plugin._unbind(UMO)
    assert UMO not in plugin._pending_alerts


def test_flush_alerts_drops_queue_when_binding_gone():
    sent: list[tuple[str, str]] = []

    async def _send(umo, text):
        sent.append((umo, text))
        return True

    store = _FakeStore({})  # 绑定已被外部删掉
    plugin = make_plugin(store=store)
    plugin._send = _send
    plugin._pending_alerts[UMO] = [
        {"kind": "ac", "level": 1, "value": 3.0, "unit": "度", "warn": 10, "critical": 5, "at": time.time()}
    ]
    asyncio.run(plugin._flush_alerts())
    assert sent == []
    assert UMO not in plugin._pending_alerts


def test_flush_alerts_still_sends_with_binding():
    sent: list[tuple[str, str]] = []

    async def _send(umo, text):
        sent.append((umo, text))
        return True

    binding = {"room_label": "校本部/春雪楼2/8层/A-8-17"}
    store = _FakeStore({UMO: binding})
    plugin = make_plugin(store=store)
    plugin._send = _send
    plugin._pending_alerts[UMO] = [
        {"kind": "ac", "level": 1, "value": 3.0, "unit": "度", "warn": 10, "critical": 5, "at": time.time()}
    ]
    asyncio.run(plugin._flush_alerts())
    assert len(sent) == 1 and "A-8-17" in sent[0][1]


# ================= 1.4 凭证过期不甩锅 room token =================


def _wizard_store():
    store = _FakeStore()
    return store


def _bind_setup(provider):
    store = _wizard_store()
    plugin = make_plugin(config={"fee_items": FEE_ITEMS}, store=store, hjnu=provider)
    plugin._wizard[UMO] = {
        "step": "bind",
        "aid": AC_AID,
        "area": AREAS[1],
        "building": BUILDINGS[0],
        "floor": FLOORS[1],
        "room": ROOMS_CS2_8[-1],
    }
    return plugin, store


def test_match_elec_fee_propagates_session_expired():
    plugin, _store = _bind_setup(_FlakyProvider(elec_expired=True))
    with pytest.raises(SessionExpiredError):
        asyncio.run(plugin._match_elec_fee({"aid": AC_AID, "room": {"room": "A-8-17"}}))


def test_match_elec_fee_still_swallows_plain_query_error():
    """普通网络错误照旧降级为「未自动关联」，只是不给用户看堆栈。"""

    class _BadElec(_FlakyProvider):
        async def list_areas(self, aid):
            raise QueryError("接口返回 HTTP 502")

    plugin, _store = _bind_setup(_BadElec())
    assert asyncio.run(plugin._match_elec_fee({"aid": AC_AID, "room": {"room": "A-8-17"}})) is None


def test_complete_bind_reports_credential_not_room_token():
    plugin, store = _bind_setup(_FlakyProvider(elec_expired=True))
    out = asyncio.run(plugin._complete_bind(UMO))
    assert "凭证失效" in out
    assert "room token" not in out
    # 绑定仍然落库（ac 费种可用），只是没关联宿舍电费
    assert store.get_binding(UMO) is not None


def test_query_room_reports_credential_not_missing_elec_fee():
    store = _FakeStore()
    plugin = make_plugin(config={"fee_items": FEE_ITEMS}, store=store, hjnu=_FlakyProvider(elec_expired=True))
    out = asyncio.run(plugin._query_room_balance(UMO, "春雪楼817"))
    assert "凭证失效" in out
    assert "没有对应的宿舍电费项目" not in out


# ================= 1.6 per-fee 紧急线回退 =================


def _cfg(mapping):
    def _get(key, default=0):
        v = mapping.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    return _get


def test_per_fee_critical_falls_back_to_half_of_warn():
    from astrbot_plugin_dorm_electric.thresholds import effective_thresholds

    cfg = _cfg({"threshold_warn_ac": 20, "threshold_critical_ac": 0})
    warn, critical = effective_thresholds(cfg, None, "ac")
    assert (warn, critical) == (20, 10)  # 与配置提示「取预警线一半」一致


def test_per_fee_critical_explicit_wins():
    from astrbot_plugin_dorm_electric.thresholds import effective_thresholds

    cfg = _cfg({"threshold_warn_ac": 20, "threshold_critical_ac": 3})
    assert effective_thresholds(cfg, None, "ac") == (20, 3)


def test_global_only_config_keeps_legacy_behavior():
    from astrbot_plugin_dorm_electric.thresholds import effective_thresholds

    cfg = _cfg({"threshold_warn": 20, "threshold_critical": 5})
    assert effective_thresholds(cfg, None, "ac") == (20, 5)


def test_session_threshold_still_overrides_everything():
    from astrbot_plugin_dorm_electric.thresholds import effective_thresholds

    cfg = _cfg({"threshold_warn_ac": 20, "threshold_critical_ac": 3})
    binding = {"thresholds": {"ac": {"warn": 8, "critical": 2}}}
    assert effective_thresholds(cfg, binding, "ac") == (8, 2)
