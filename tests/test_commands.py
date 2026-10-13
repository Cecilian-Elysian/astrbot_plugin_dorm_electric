"""此前无覆盖的指令与后台通知路径（v1.2.1 Phase 4 补盲区）。

覆盖：
1. _notify_session_dead：24h 去重 + 发送失败不记账（下轮重试）
2. cmd_status：绑定/未绑定、最新余额渲染
3. cmd_credential：群拒、用法提示、更新落配置与 provider、凭证前缀剥离
4. cmd_query：未绑定 / 成功 / 失败三态
5. cmd_history：未绑定、按天快照渲染、非法 n 回默认
"""

import asyncio
import time

from astrbot_plugin_dorm_electric.providers.base import BalanceResult
from conftest import make_plugin

UMO = "qq:private:1"
GROUP = "qq:group:9"


class _Ev:
    def __init__(self, umo=UMO, private=True):
        self.unified_msg_origin = umo
        self._private = private

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return text


async def _collect(agen):
    return [item async for item in agen]


def _ok(value, unit="度"):
    return BalanceResult(
        ok=True, value=value, unit=unit, raw=f"余额 {value} {unit}", session_expired=False
    )


class _Provider:
    def __init__(self, results=None, fail=False):
        self._results = results or {}
        self._fail = fail
        self.cookie = None

    def update_cookie(self, cookie):
        self.cookie = cookie

    async def fetch(self, binding):
        if self._fail:
            return None
        aid = binding["params"].get("aid")
        return self._results.get(aid, _ok(50.0))


def _binding(aid="0030000000004301"):
    return {
        "room_label": "校本部/春雪楼2/8层/A-8-17",
        "fees": {"ac": {"provider": "hjnu", "params": {"aid": aid}}},
        "history_by_fee": {
            "ac": [
                {"t": time.time() - 86400, "v": 52.0, "u": "度"},
                {"t": time.time(), "v": 50.0, "u": "度"},
            ]
        },
    }


class _Store:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def save(self, force=False):
        self.saved += 1

    @staticmethod
    def append_fee_history(binding, fee, value, unit, ts=None, keep_days=60):
        from astrbot_plugin_dorm_electric.storage import Store

        Store.append_fee_history(binding, fee, value, unit, ts=ts, keep_days=keep_days)


# ================= _notify_session_dead =================


def test_dead_notify_sends_and_records():
    store = _Store({UMO: _binding()})
    plugin = make_plugin(store=store)
    sent = []

    async def _send(umo, text):
        sent.append(umo)
        return True

    plugin._send = _send
    binding = store.get_binding(UMO)
    asyncio.run(plugin._notify_session_dead(UMO, binding))
    assert sent == [UMO]
    assert binding["alert_state"]["dead_notified_at"] > 0
    assert store.saved >= 1


def test_dead_notify_dedupes_within_24h():
    store = _Store({UMO: _binding()})
    plugin = make_plugin(store=store)
    sent = []

    async def _send(umo, text):
        sent.append(umo)
        return True

    plugin._send = _send
    binding = store.get_binding(UMO)
    asyncio.run(plugin._notify_session_dead(UMO, binding))
    asyncio.run(plugin._notify_session_dead(UMO, binding))
    assert len(sent) == 1  # 24h 内只提醒一次


def test_dead_notify_failure_not_recorded():
    """发送失败不记账：否则被 24h 去重吞掉后彻底失声。"""
    store = _Store({UMO: _binding()})
    plugin = make_plugin(store=store)

    async def _send(umo, text):
        return False

    plugin._send = _send
    binding = store.get_binding(UMO)
    asyncio.run(plugin._notify_session_dead(UMO, binding))
    assert "dead_notified_at" not in binding.get("alert_state", {})


# ================= cmd_status =================


def test_status_unbound_shows_hint():
    plugin = make_plugin(config={"hjnu_cookie": "JSESSIONID=x"}, store=_Store())
    out = "".join(asyncio.run(_collect(plugin.cmd_status(_Ev()))))
    assert "未绑定" in out
    assert "已配置" in out


def test_status_bound_shows_label_and_latest():
    plugin = make_plugin(
        config={"hjnu_cookie": "JSESSIONID=x"}, store=_Store({UMO: _binding()})
    )
    out = "".join(asyncio.run(_collect(plugin.cmd_status(_Ev()))))
    assert "A-8-17" in out
    assert "50.00" in out
    assert "已关联" in out


# ================= cmd_credential =================


def test_credential_group_denied():
    plugin = make_plugin(store=_Store())
    out = "".join(asyncio.run(_collect(plugin.cmd_credential(_Ev(GROUP, False), "JSESSIONID=x"))))
    assert "私聊" in out


def test_credential_without_arg_shows_usage():
    plugin = make_plugin(store=_Store())
    out = "".join(asyncio.run(_collect(plugin.cmd_credential(_Ev(), None))))
    assert "用法" in out and "JSESSIONID" in out


def test_credential_updates_config_and_provider():
    store = _Store()
    provider = _Provider()
    plugin = make_plugin(config={"hjnu_cookie": ""}, store=store, hjnu=provider)
    saved = []
    plugin.config.save_config = lambda: saved.append(1)
    out = "".join(
        asyncio.run(_collect(plugin.cmd_credential(_Ev(), "JSESSIONID=NEWVAL")))
    )
    assert "凭证已保存" in out
    assert plugin.config["hjnu_cookie"] == "JSESSIONID=NEWVAL"
    assert provider.cookie == "JSESSIONID=NEWVAL"
    assert saved == [1]


def test_credential_strips_cookie_prefix_and_queries():
    store = _Store({UMO: _binding()})

    class _P(_Provider):
        async def fetch(self, binding):
            return _ok(50.0)

    plugin = make_plugin(config={"hjnu_cookie": "old"}, store=store, hjnu=_P())
    out = "".join(
        asyncio.run(_collect(plugin.cmd_credential(_Ev(), "Cookie: JSESSIONID=NEW")))
    )
    assert "凭证已更新" in out
    assert plugin.config["hjnu_cookie"] == "JSESSIONID=NEW"
    assert "50.00" in out


# ================= cmd_query =================


def test_query_unbound():
    plugin = make_plugin(store=_Store())
    out = "".join(asyncio.run(_collect(plugin.cmd_query(_Ev()))))
    assert "尚未绑定" in out


def test_query_success_saves_and_records():
    store = _Store({UMO: _binding()})
    plugin = make_plugin(store=store, hjnu=_Provider())
    out = "".join(asyncio.run(_collect(plugin.cmd_query(_Ev()))))
    assert "A-8-17" in out
    assert "50.00" in out
    assert store.get_binding(UMO)["history_by_fee"]["ac"][-1]["v"] == 50.0
    assert plugin._last_raw[UMO]["results"]


def test_query_failure_reports_error():
    store = _Store({UMO: _binding()})
    plugin = make_plugin(store=store, hjnu=_Provider(fail=True))
    out = "".join(asyncio.run(_collect(plugin.cmd_query(_Ev()))))
    assert "查询失败" in out


# ================= cmd_history =================


def test_history_unbound():
    plugin = make_plugin(store=_Store())
    out = "".join(asyncio.run(_collect(plugin.cmd_history(_Ev(), None))))
    assert "没有" in out or "尚未" in out


def test_history_renders_daily_snapshots():
    plugin = make_plugin(store=_Store({UMO: _binding()}))
    out = "".join(asyncio.run(_collect(plugin.cmd_history(_Ev(), "7"))))
    assert "A-8-17" in out
    assert "50.00" in out


def test_history_garbage_n_falls_back():
    plugin = make_plugin(store=_Store({UMO: _binding()}))
    out = "".join(asyncio.run(_collect(plugin.cmd_history(_Ev(), "abc"))))
    assert "A-8-17" in out  # 用默认天数照常渲染


def test_history_n_clamped_to_60():
    plugin = make_plugin(store=_Store({UMO: _binding()}))
    out = "".join(asyncio.run(_collect(plugin.cmd_history(_Ev(), "99999"))))
    assert "A-8-17" in out
