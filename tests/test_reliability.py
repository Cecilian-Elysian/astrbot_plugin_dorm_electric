"""可靠性回归测试：重试矩阵、预警合并发送、逐绑定隔离、terminate 清理。

- 学校服务器夜间不稳（502/chunked 空页/抖动）是 AGENTS.md 两次事故的根源，
  v1.0.1/v1.0.6 的修复长期零回归防线——这里用 httpx.MockTransport 按脚本回放。
- v1.2.0 审查发现的「预警先记账后发送 → 推送瞬断丢预警」「共享 client 被重试
  路径中途关闭」「terminate 先关 client 后取消任务」都有对应断言。
"""

import asyncio
import json

import httpx
import pytest
from astrbot_plugin_dorm_electric.main import CREDENTIAL_HINT
from astrbot_plugin_dorm_electric.providers import http_json
from astrbot_plugin_dorm_electric.providers.base import BalanceResult
from astrbot_plugin_dorm_electric.providers.http_json import (
    HjnuProvider,
    QueryError,
    SessionExpiredError,
)
from conftest import make_plugin

OK_BALANCE = {"retcode": "0", "errmsg": "A-8-17房间剩余电量94.66度"}
EXPIRED = {"retcode": "91001", "errmsg": "会话已超时"}
ROOM_PARAMS = {
    "aid": "a1",
    "area": {"areaname": "校本部"},
    "building": {"building": "春雪楼2"},
    "floor": {"floor": "8层"},
    "room": {"room": "A-8-17", "roomid": 646},
}


def _provider(script, monkeypatch):
    """按脚本回放响应；script 元素： (status, text) / "nonjson" / "neterr"。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = script[len(calls)] if len(calls) < len(script) else script[-1]
        calls.append(str(request.url.path))
        if action == "neterr":
            raise httpx.ConnectError("boom", request=request)
        if action == "nonjson":
            return httpx.Response(200, text="<html>chunked 空页</html>")
        status, text = action
        return httpx.Response(status, text=text)

    provider = HjnuProvider(
        base_url="http://school.test",
        query_path="/q",
        cookie="JSESSIONID=x",
        user_agent="ua",
        referer="http://school.test/",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )

    async def _nosleep(_):
        return None

    monkeypatch.setattr(http_json.asyncio, "sleep", _nosleep)
    return provider, calls


# ================= 重试矩阵 =================


def test_5xx_retry_then_success(monkeypatch):
    provider, calls = _provider(
        [(502, ""), (502, ""), (200, json.dumps(OK_BALANCE))], monkeypatch
    )
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.ok and result.value == 94.66 and result.unit == "度"
    assert len(calls) == 3


def test_all_5xx_raises_query_error(monkeypatch):
    provider, calls = _provider([(502, ""), (502, ""), (502, "")], monkeypatch)
    with pytest.raises(QueryError):
        asyncio.run(provider.list_areas("a1"))
    assert len(calls) == 3


def test_all_5xx_query_room_reports_failed_result(monkeypatch):
    """query_room 自身吞 QueryError 成 BalanceResult：raw 必须带「已自动重试」。"""
    provider, _ = _provider([(502, ""), (502, ""), (502, "")], monkeypatch)
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert not result.ok and "已自动重试" in result.raw


def test_200_non_json_retries_then_raises(monkeypatch):
    provider, calls = _provider(["nonjson", "nonjson", "nonjson"], monkeypatch)
    with pytest.raises(QueryError):
        asyncio.run(provider.list_areas("a1"))
    assert len(calls) == 3


def test_4xx_fails_fast_without_retry(monkeypatch):
    provider, calls = _provider([(404, "")], monkeypatch)
    with pytest.raises(QueryError):
        asyncio.run(provider.list_areas("a1"))
    assert len(calls) == 1


def test_network_error_retries(monkeypatch):
    provider, calls = _provider(["neterr", "neterr", "neterr"], monkeypatch)
    with pytest.raises(QueryError):
        asyncio.run(provider.list_areas("a1"))
    assert len(calls) == 3


def test_91001_maps_to_session_expired(monkeypatch):
    provider, calls = _provider([(200, json.dumps(EXPIRED))], monkeypatch)
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.session_expired and not result.ok
    assert len(calls) == 1


def test_5xx_rebuild_does_not_break_concurrent_peer(monkeypatch):
    """同实例并发两路查询：一路 502 换新连接，另一路不受影响。"""
    provider, calls = _provider(
        [(502, ""), (200, json.dumps(OK_BALANCE)), (200, json.dumps(OK_BALANCE))],
        monkeypatch,
    )

    async def _run():
        return await asyncio.gather(
            provider.query_room(ROOM_PARAMS),
            provider.query_room(ROOM_PARAMS),
        )

    a, b = asyncio.run(_run())
    assert a.ok and b.ok
    assert len(calls) == 3


# ================= 扫描途中过期 → 凭证提示（不是「没找到房间」） =================


class _ExpireMidScan:
    async def list_areas(self, aid):
        return [{"area": "1", "areaname": "校本部"}]

    async def list_buildings(self, aid, area):
        return [{"building": "春雪楼2", "buildingid": 1}]

    async def list_floors(self, aid, area, building):
        raise SessionExpiredError("91001")


def test_midscan_expiry_reports_credential_hint():
    plugin = make_plugin(config={"fee_items": {"aid": "x"}}, hjnu=_ExpireMidScan())
    params, err = asyncio.run(plugin._resolve_room("qq:private:1", "817"))
    assert params is None
    assert err == CREDENTIAL_HINT


# ================= 预警合并发送：成功才记账 =================


class _OneBindingStore:
    def __init__(self, label="校本部/春雪楼2/8层/A-8-17"):
        self._binding = {"room_label": label} if label else None

    def get_binding(self, umo):
        return self._binding


def _alert(kind, level, value):
    return {
        "kind": kind,
        "level": level,
        "value": value,
        "unit": "度" if kind == "ac" else "元",
        "warn": 10.0,
        "critical": 5.0,
        "at": 0,
    }


def test_flush_alerts_merges_two_fees_into_one_message(monkeypatch):
    plugin = make_plugin(store=_OneBindingStore())
    plugin._pending_alerts["qq:private:1"] = [
        _alert("ac", 2, 3.0),
        _alert("elec", 1, 8.0),
    ]
    sent = []

    async def fake_send(umo, text):
        sent.append((umo, text))
        return True

    monkeypatch.setattr(plugin, "_send", fake_send)
    asyncio.run(plugin._flush_alerts())
    assert len(sent) == 1
    umo, text = sent[0]
    assert umo == "qq:private:1"
    assert "🚨" in text and "空调费" in text and "宿舍电费" in text
    assert not plugin._pending_alerts
    assert any(e["kind"] == "alert" for e in plugin._events)


def test_flush_alerts_send_failure_keeps_pending(monkeypatch):
    """推送瞬断不能丢预警：pending 保留到下轮，也不记 alert 事件。"""
    plugin = make_plugin(store=_OneBindingStore())
    plugin._pending_alerts["qq:private:1"] = [_alert("ac", 1, 3.0)]

    async def fake_send(umo, text):
        return False

    monkeypatch.setattr(plugin, "_send", fake_send)
    asyncio.run(plugin._flush_alerts())
    assert plugin._pending_alerts["qq:private:1"]
    assert not any(e["kind"] == "alert" for e in plugin._events)


def test_flush_alerts_empty_entries_cleaned():
    plugin = make_plugin(store=_OneBindingStore())
    plugin._pending_alerts["qq:private:1"] = []
    asyncio.run(plugin._flush_alerts())
    assert "qq:private:1" not in plugin._pending_alerts


# ================= 轮询隔离与 terminate =================


class _CountingProvider:
    def __init__(self):
        self.fetches = 0

    async def fetch(self, binding):
        self.fetches += 1
        return BalanceResult(ok=True, value=99.0, raw="剩余99度", unit="度")


class _MultiBindingStore:
    def __init__(self, umos):
        self.data = {
            "bindings": {
                u: {
                    "room_label": u,
                    "fees": {"ac": {"provider": "hjnu", "params": {"aid": "a"}}},
                }
                for u in umos
            }
        }
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def append_fee_history(self, *a, **k):  # 与真实 Store 同名，直接借用
        from astrbot_plugin_dorm_electric.storage import Store

        Store.append_fee_history(*a, **k)

    def save(self):
        self.saved += 1


def test_poll_isolates_failing_binding(monkeypatch):
    """第一个绑定评估炸掉，第二个绑定照常轮询、预警照常 flush。"""
    plugin = make_plugin(
        config={}, store=_MultiBindingStore(["u1", "u2"]), hjnu=_CountingProvider()
    )
    calls = {"n": 0}

    async def boom(self_, umo, binding, value, kind="ac", unit="度"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dirty data")
        plugin._pending_alerts.setdefault(umo, []).append(_alert(kind, 1, value))

    monkeypatch.setattr(type(plugin), "_evaluate_alerts", boom)
    sent = []

    async def fake_send(umo, text):
        sent.append(text)
        return True

    monkeypatch.setattr(plugin, "_send", fake_send)
    asyncio.run(plugin._poll_all())
    assert plugin.hjnu.fetches == 2
    assert len(sent) == 1


def test_poll_save_failure_still_flushes_and_does_not_raise(monkeypatch):
    class _BrokenStore(_MultiBindingStore):
        def save(self):
            raise OSError("disk full")

    plugin = make_plugin(
        config={}, store=_BrokenStore(["u1"]), hjnu=_CountingProvider()
    )
    plugin._pending_alerts["u1"] = [_alert("ac", 1, 3.0)]
    sent = []

    async def fake_send(umo, text):
        sent.append(text)
        return True

    monkeypatch.setattr(plugin, "_send", fake_send)
    asyncio.run(plugin._poll_all())  # 不抛
    assert sent  # flush 仍执行


def test_terminate_cancels_tasks_before_closing_resources():
    order = []

    class _FakeCloseable:
        async def close(self):
            order.append("close")

    from astrbot_plugin_dorm_electric.storage import Store

    class _TmpStore(Store):
        def save(self):
            order.append("save")

    import tempfile
    from pathlib import Path

    plugin = make_plugin(
        store=_TmpStore(Path(tempfile.mkdtemp()) / "history.json"),
        hjnu=_FakeCloseable(),
    )
    plugin._wizard["u"] = {"step": "room"}
    plugin._last_raw["u"] = {"ac": "raw"}
    plugin._events.append({"t": 0, "kind": "poll", "text": "x", "umo": "u"})
    plugin._bind_tokens["u"] = {}
    plugin._lookup_cache["u"] = (0.0, "t")
    plugin._last_room["u"] = {}
    plugin._alert_muted["u"] = 1.0
    plugin._pending_alerts["u"] = []

    async def _hang():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("task_done")
            raise

    async def _run():
        task = asyncio.create_task(_hang())
        plugin._tasks.append(task)
        await asyncio.sleep(0)  # 让任务真正起跑（pending 状态下 cancel 不会进 body）
        await plugin.terminate()

    asyncio.run(_run())
    assert order == ["task_done", "close", "save"]
    for attr in (
        "_wizard",
        "_last_raw",
        "_events",
        "_bind_tokens",
        "_lookup_cache",
        "_last_room",
        "_alert_muted",
        "_pending_alerts",
    ):
        assert not getattr(plugin, attr), f"terminate 未清空 {attr}"


def test_terminate_tolerates_missing_resources():
    plugin = make_plugin()  # store/hjnu/scheduler 全 None，不应抛
    asyncio.run(plugin.terminate())
