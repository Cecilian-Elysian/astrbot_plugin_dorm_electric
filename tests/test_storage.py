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


def test_save_debounces_within_window(tmp_path, monkeypatch):
    p = tmp_path / "h.json"
    s = Store(p)
    clock = [1000.0]

    def fake_time():
        return clock[0]

    monkeypatch.setattr(time, "time", fake_time)
    s.data["bindings"]["u"] = {"room_label": "v1"}
    s.save()  # 首次（_last_saved_at=0）必然落盘
    assert p.exists()
    mtime1 = p.read_text(encoding="utf-8")
    s.data["bindings"]["u"] = {"room_label": "v2"}
    clock[0] += 5  # 窗口内
    s.save()
    assert p.read_text(encoding="utf-8") == mtime1  # 没写
    assert s._dirty is True
    clock[0] += 120  # 窗口外
    s.save()
    assert "v2" in p.read_text(encoding="utf-8")
    assert s._dirty is False


def test_save_force_bypasses_debounce(tmp_path, monkeypatch):
    p = tmp_path / "h.json"
    s = Store(p)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    s.data["bindings"]["u"] = {"room_label": "v1"}
    s.save()
    s.data["bindings"]["u"] = {"room_label": "v2"}
    clock[0] += 1  # 窗口内
    s.save(force=True)  # 用户动作必须立即持久化
    assert "v2" in p.read_text(encoding="utf-8")
    assert s._dirty is False


def test_flush_writes_pending_changes(tmp_path, monkeypatch):
    p = tmp_path / "h.json"
    s = Store(p)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    assert s.flush() is False  # 没东西可写
    s.data["bindings"]["u"] = {"room_label": "v"}
    s.save()
    s.data["bindings"]["u"] = {"room_label": "v2"}
    clock[0] += 1
    s.save()  # 被去抖
    assert s.flush() is True
    assert "v2" in p.read_text(encoding="utf-8")
    assert s.flush() is False


def test_save_atomic_no_tmp_leftover(tmp_path):
    p = tmp_path / "h.json"
    s = Store(p)
    s.data["bindings"]["u"] = {"room_label": "v"}
    s.save(force=True)
    assert not (tmp_path / "h.tmp").exists()
    assert p.exists()


def test_load_quarantine_renames_corrupt_file(tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{not json", encoding="utf-8")
    Store(p)
    renamed = list(tmp_path.glob("h.json.corrupt-*"))
    assert len(renamed) == 1
