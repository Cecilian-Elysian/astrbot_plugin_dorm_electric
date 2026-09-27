"""_daily_snapshots（按天取日末快照）的测试。"""

from datetime import datetime, timedelta, timezone

from astrbot_plugin_dorm_electric.main import DormElectricPlugin

daily_snapshots = DormElectricPlugin._daily_snapshots

TZ = timezone(timedelta(hours=8))


def _ts(day_offset: int, hour: int) -> float:
    """本地（UTC+8）「今天往前 day_offset 天」的 hour 点整的时间戳。"""
    d = datetime.now(TZ).date() - timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, tzinfo=TZ).timestamp()


def _h(entries):
    """entries: [(day_offset, hour, value)] → 时间升序历史样本。"""
    rows = [
        {"t": _ts(d, h), "v": v, "u": "度"} for d, h, v in entries
    ]
    rows.sort(key=lambda r: r["t"])
    return rows


def test_same_day_takes_latest_record():
    history = _h([(0, 9, 10.0), (0, 18, 8.0)])
    snaps = daily_snapshots(history, TZ, 1)
    assert len(snaps) == 1
    date, rec = snaps[0]
    assert date == datetime.now(TZ).date()
    assert rec["v"] == 8.0


def test_missing_days_are_none():
    history = _h([(0, 12, 8.0), (3, 12, 20.0)])
    snaps = daily_snapshots(history, TZ, 5)
    assert [rec for _d, rec in snaps] == [
        history[-1],
        None,
        None,
        history[0],
        None,
    ]


def test_dates_descend_from_today():
    history = _h([(0, 12, 9.0), (1, 12, 8.0), (2, 12, 7.0)])
    snaps = daily_snapshots(history, TZ, 3)
    today = datetime.now(TZ).date()
    assert [d for d, _rec in snaps] == [
        today,
        today - timedelta(days=1),
        today - timedelta(days=2),
    ]


def test_days_ignored_extra_history():
    """days 截断更早的日期，超出窗口的记录不出现。"""
    history = _h([(0, 12, 9.0), (10, 12, 1.0)])
    snaps = daily_snapshots(history, TZ, 3)
    assert len(snaps) == 3
    assert snaps[0][1]["v"] == 9.0
    assert all(rec is None for _d, rec in snaps[1:])


def test_empty_history_all_none():
    snaps = daily_snapshots([], TZ, 2)
    assert snaps == [
        (datetime.now(TZ).date(), None),
        (datetime.now(TZ).date() - timedelta(days=1), None),
    ]
