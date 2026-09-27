"""Store 的历史追加、截断、清理与绑定 CRUD 测试。"""

import time

from astrbot_plugin_dorm_electric.storage import MAX_HISTORY, Store

DAY = 24 * 3600


def test_append_fee_history_basic():
    b = {}
    now = time.time()
    Store.append_fee_history(b, "ac", 12.5, "度", ts=now)
    hist = b["history_by_fee"]["ac"]
    assert len(hist) == 1
    assert hist[0] == {"t": now, "v": 12.5, "u": "度"}


def test_append_fee_history_cutoff_old_entries():
    b = {}
    now = time.time()
    Store.append_fee_history(b, "ac", 20.0, "度", ts=now - 100 * DAY)
    assert len(b["history_by_fee"]["ac"]) == 1  # 相对自身 ts 未越界，先保留
    Store.append_fee_history(b, "ac", 9.0, "度", ts=now)  # 新样本触发 60 天清理
    hist = b["history_by_fee"]["ac"]
    assert len(hist) == 1
    assert hist[0]["v"] == 9.0


def test_append_fee_history_max_truncation():
    b = {}
    now = time.time()
    hist = b.setdefault("history_by_fee", {}).setdefault("ac", [])
    hist.extend({"t": now - i, "v": 1.0, "u": "度"} for i in range(MAX_HISTORY))
    Store.append_fee_history(b, "ac", 2.0, "度", ts=now)
    assert len(hist) == MAX_HISTORY
    assert hist[-1]["v"] == 2.0


def test_fees_isolated():
    b = {}
    now = time.time()
    Store.append_fee_history(b, "ac", 1.0, "度", ts=now)
    Store.append_fee_history(b, "elec", 2.0, "元", ts=now)
    assert set(b["history_by_fee"]) == {"ac", "elec"}
    assert b["history_by_fee"]["ac"][0]["u"] == "度"
    assert b["history_by_fee"]["elec"][0]["u"] == "元"


def test_binding_crud(tmp_path):
    s = Store(tmp_path / "h.json")
    assert s.get_binding("u") is None
    s.set_binding("u", {"room_label": "A-8-17"})
    assert s.get_binding("u")["room_label"] == "A-8-17"
    assert s.del_binding("u") is True
    assert s.del_binding("u") is False
    assert s.get_binding("u") is None


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "h.json"
    s = Store(p)
    s.data["bindings"]["umo1"] = {"room_label": "X", "history_by_fee": {"ac": []}}
    s.save()
    s2 = Store(p)
    assert s2.data["bindings"]["umo1"]["room_label"] == "X"


def test_load_missing_file(tmp_path):
    s = Store(tmp_path / "none.json")
    assert s.data == {"bindings": {}}


def test_load_corrupt_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{not json", encoding="utf-8")
    s = Store(p)
    assert s.data == {"bindings": {}}
