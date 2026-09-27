"""_history_stats（含充值识别）与时区/时间解析的测试。"""

import time
from datetime import timedelta, timezone

from astrbot_plugin_dorm_electric.main import DormElectricPlugin

resolve_tz = DormElectricPlugin._resolve_tz
parse_daily_time = DormElectricPlugin._parse_daily_time
history_stats = DormElectricPlugin._history_stats


def _h(values, ages_hours, unit="度"):
    """按「距现在多少小时前」构造时间升序（最老在前）的历史样本。"""
    now = time.time()
    return [
        {"t": now - age * 3600, "v": v, "u": unit}
        for age, v in zip(ages_hours, values)
    ]


def test_stats_empty():
    assert history_stats([]) is None


def test_stats_single_sample():
    stats = history_stats(_h([10.0], [1]))
    assert stats["usage_24h"] == 0.0
    assert stats["recharged_24h"] == 0.0
    assert stats["min"] == stats["max"] == 10.0
    assert stats["per_day"] == 0.0


def test_stats_monotonic_decline():
    stats = history_stats(_h([10.0, 8.0, 5.0], [3, 2, 1]))
    assert stats["usage_24h"] == 5.0
    assert stats["recharged_24h"] == 0.0
    assert stats["min"] == 5.0
    assert stats["max"] == 10.0


def test_stats_recharge_not_counted_as_negative_usage():
    """老逻辑会把充值后的下降算成 0；新逻辑分段统计。"""
    stats = history_stats(_h([10.0, 8.0, 20.0, 18.0], [4, 3, 2, 1]))
    assert stats["usage_24h"] == 4.0
    assert stats["recharged_24h"] == 12.0


def test_stats_24h_window_excludes_older_samples():
    """窗口起点为最接近 24h 前的样本，更老的样本不计入 24h 用电。"""
    stats = history_stats(_h([50.0, 40.0, 30.0, 25.0], [30, 25, 3, 2]))
    assert stats["usage_24h"] == 15.0  # 40→30→25
    assert stats["recharged_24h"] == 0.0


def test_stats_per_day_over_span():
    stats = history_stats(_h([30.0, 20.0, 10.0], [120, 24, 1]))
    span_days = 119 / 24
    assert stats["per_day"] == (20.0 / span_days)


def test_stats_per_day_short_span_zero():
    stats = history_stats(_h([10.0, 8.0], [2, 1]))
    assert stats["per_day"] == 0.0


def test_stats_recharge_in_per_day():
    """日均用电按下降段之和计算，充值段不计入。"""
    stats = history_stats(_h([10.0, 5.0, 15.0, 10.0], [96, 72, 48, 1]))
    span_days = 95 / 24
    assert stats["per_day"] == (10.0 / span_days)


def test_stats_unit():
    stats = history_stats(_h([3.0, 2.0], [2, 1], unit="元"))
    assert stats["unit"] == "元"


def test_resolve_tz_shanghai():
    assert resolve_tz("Asia/Shanghai") == timezone(timedelta(hours=8))


def test_resolve_tz_offset_alias():
    assert resolve_tz("+08") == timezone(timedelta(hours=8))
    assert resolve_tz("CST") == timezone(timedelta(hours=8))


def test_resolve_tz_fallback_utc():
    assert resolve_tz("Mars/Olympus") == timezone.utc
    assert resolve_tz("") == timezone.utc
    assert resolve_tz(None) == timezone.utc


def test_parse_daily_time_valid():
    assert parse_daily_time("08:30") == (8, 30)
    assert parse_daily_time("00:00") == (0, 0)


def test_parse_daily_time_overflow_wraps():
    assert parse_daily_time("25:70") == (1, 10)


def test_parse_daily_time_invalid_defaults():
    assert parse_daily_time("abc") == (8, 0)
    assert parse_daily_time("") == (8, 0)
