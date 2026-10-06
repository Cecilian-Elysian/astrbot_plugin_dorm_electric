"""预警线与播报免打扰的生效值计算（按费种）。

从 main.py 平移而来；config 读取以 cfg_float 可调用对象注入，保持纯函数。
main.py 里以薄封装方法引用（self._effective_thresholds 等调用点不变）。
"""

try:
    from .formatting import fee_name
except ImportError:  # 兜底：被以非包方式加载时
    from formatting import fee_name  # type: ignore


def effective_thresholds(
    cfg_float, binding: dict | None, kind: str = "ac"
) -> tuple[float, float]:
    """生效预警线（按费种）。

    优先级：会话 per-fee 自定义 > 全局 per-fee 配置（threshold_warn_ac 等）
    > 全局旧配置（threshold_warn/critical，两费种共用，老用户零感知）。
    会话存储兼容两代格式：新 {"ac": {warn, critical}, "elec": {…}} 只存
    自定义过的费种；旧 {"warn":…, "critical":…} 视为两费种共用。
    """
    warn = cfg_float(f"threshold_warn_{kind}", 0) or cfg_float("threshold_warn", 10)
    critical = cfg_float(f"threshold_critical_{kind}", 0) or cfg_float(
        "threshold_critical", 5
    )
    if critical > warn:
        warn, critical = critical, warn
    t = (binding or {}).get("thresholds") or {}
    if not isinstance(t, dict):
        t = {}
    ft = t.get(kind)
    if not isinstance(ft, dict):
        # legacy：整份 flat dict 就是两费种共用的自定义值
        ft = t if ("warn" in t or "critical" in t) else {}
    try:
        w = float(ft.get("warn") or 0)
    except (TypeError, ValueError):
        w = 0.0
    try:
        c = float(ft.get("critical") or 0)
    except (TypeError, ValueError):
        c = 0.0
    if w > 0:
        warn = w
        critical = c if 0 < c <= warn else w / 2
    return warn, critical


def threshold_lines(cfg_float, binding: dict | None) -> list[str]:
    """两费种生效预警线展示行（balance / 预警线状态 / 配置摘要共用）。"""
    lines = []
    for kind in ("ac", "elec"):
        unit = "度" if kind == "ac" else "元"
        w, c = effective_thresholds(cfg_float, binding, kind)
        lines.append(
            f"{fee_name(kind)}：预警 {w:g} {unit} / 紧急 {c:g} {unit}"
        )
    return lines


def set_session_threshold(
    binding: dict, kind: str, warn: float, critical: float
) -> None:
    """写一条会话 per-fee 自定义预警线（新格式，只动指定费种）。"""
    binding.setdefault("thresholds", {})[kind] = {
        "warn": warn,
        "critical": critical,
    }


def daily_muted_until(binding: dict | None) -> float:
    try:
        return float((binding or {}).get("daily_muted_until") or 0)
    except (TypeError, ValueError):
        return 0.0
