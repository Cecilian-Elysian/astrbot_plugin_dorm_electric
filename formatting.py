"""纯格式化/统计函数：费种文案、余额渲染、分页、日末快照、历史统计。

从 main.py 平移而来（全部为无状态纯函数，零 astrbot 依赖）；
main.py 里以静态方法别名的形式引用（self._fee_name 等调用点不变）。
"""

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# 绑定向导里每页显示的房间数。房间多时用 /电费 房间 翻页、/电费 房间 p2 跳页，
# 选择时仍用全楼层绝对编号，避免超过一页的房间选不到。
ROOM_PAGE_SIZE = 30

# 凭证失效/未配置的统一友好提示。多处共用（查询、绑定、反查扫描途中过期都指向它）。
CREDENTIAL_HINT = (
    "🔐 学校系统凭证已失效或尚未配置。\n"
    "重新获取 JSESSIONID 后私聊发送：/电费 凭证 JSESSIONID=xxxx\n"
    "（获取方式：企业微信打开缴费查询页让 Cookie 入库，再运行仓库内 "
    "tools/extract_cookie.py 提取，详见 README）"
)


def fee_name(kind: str) -> str:
    return "空调费" if kind == "ac" else "宿舍电费"


def fee_text(kind: str, result) -> str:
    return f"{fee_name(kind)}：{result.value:.2f} {result.unit}"


def format_fee_results(
    results: dict[str, object], include_missing: bool = False
) -> list[str]:
    """把查询结果格式化成按费种分行的文本。

    include_missing=True 时把「连响应都没有」（网络/学校 5xx）的费种也列出来，
    /电费 检查 用它来避免明细整段空白、看不出是哪一路失败。
    """
    lines = []
    for kind in ("ac", "elec"):
        result = results.get(kind)
        if result is None:
            if include_missing:
                lines.append(
                    f"{fee_name(kind)}：❌ 未取到响应（网络异常或学校无响应）"
                )
            continue
        if result.ok and result.value is not None:
            lines.append(fee_text(kind, result))
        elif result.session_expired:
            lines.append(f"{fee_name(kind)}：凭证已失效")
        else:
            lines.append(f"{fee_name(kind)}：查询失败（{result.raw}）")
    return lines


def alert_hint(value: float, warn: float, critical: float) -> str:
    if value <= critical:
        return f"⚠️ 已低于紧急线 {critical:g}，建议马上充值。"
    if value <= warn:
        return f"⚠️ 已低于预警线 {warn:g}，建议尽快充值。"
    return "✅ 高于预警线，状态正常。"


def room_page(
    rooms: list, page: int, page_size: int = ROOM_PAGE_SIZE
) -> tuple[int, int, int]:
    """房间列表分页：返回 (起始下标, 结束下标, 总页数)，页码自动夹到有效范围。"""
    total = len(rooms)
    total_pages = max(1, -(-total // page_size))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(total_pages, page))
    start = (page - 1) * page_size
    return start, min(start + page_size, total), total_pages


def room_token(room_name: str) -> str | None:
    m = re.search(r"([A-Za-z]+)[-_](\d+)[-_](\d+)", room_name or "")
    return "".join(m.groups()) if m else None


def daily_snapshots(
    history: list[dict], tz: timezone, days: int
) -> list[tuple[Any, dict | None]]:
    """按本地日期取「日末快照」：每天时间戳最大的一条记录。

    返回从今天往前共 days 个自然日的 (date, 记录|None)，
    最新日期在前，当天无记录的日期为 None。
    """
    by_date: dict[Any, dict] = {}
    for h in history:
        d = datetime.fromtimestamp(float(h["t"]), tz).date()
        prev = by_date.get(d)
        if prev is None or float(h["t"]) >= float(prev["t"]):
            by_date[d] = h
    today = datetime.now(tz).date()
    snapshots: list[tuple[Any, dict | None]] = []
    for i in range(days):
        d = today - timedelta(days=i)
        snapshots.append((d, by_date.get(d)))
    return snapshots


def history_stats(history: list[dict]) -> dict | None:
    """计算 24h 用电、24h 充值、日均、最低/最高。

    余额序列中下降段计为用电、上升段计为充值，避免中途充值
    导致用电量被低估甚至算成 0。
    """
    if len(history) < 1:
        return None
    values = [float(h["v"]) for h in history]
    ts = [float(h["t"]) for h in history]
    unit = history[-1].get("u", "度")
    now = time.time()
    min_v = min(values)
    max_v = max(values)

    # 24h 窗口：起点为最接近 (now-24h) 时刻的样本
    start = 0
    for i, h in enumerate(history):
        if now - float(h["t"]) >= 24 * 3600:
            start = i
        else:
            break
    usage_24h = 0.0
    recharged_24h = 0.0
    for i in range(start + 1, len(history)):
        delta = values[i - 1] - values[i]
        if delta > 0:
            usage_24h += delta
        else:
            recharged_24h += -delta

    # 日均：按全历史跨度的下降段之和
    span_days = (ts[-1] - ts[0]) / 86400
    per_day = 0.0
    if span_days >= 0.5 and len(history) >= 2:
        total_usage = 0.0
        for i in range(1, len(history)):
            delta = values[i - 1] - values[i]
            if delta > 0:
                total_usage += delta
        per_day = total_usage / span_days

    return {
        "usage_24h": usage_24h,
        "recharged_24h": recharged_24h,
        "per_day": per_day,
        "min": min_v,
        "max": max_v,
        "unit": unit,
    }


def credential_state(results: dict) -> str:
    """由一次真实查询的结果判定凭证状态（供 /电费 检查 复用）。

    ⚠️ 与 /电费 状态 无关：那条指令显示的「凭证：已配置」只表示 hjnu_cookie
    写进了配置，不代表学校还认这个会话。判断凭证是否真正有效，以本函数
    基于真实查询结果的判定为准。

    判定顺序很重要：先看有没有取到余额（说明学校认这个会话），
    再看是不是 91001（学校明确拒绝），最后才是网络/学校抖动——
    反过来会把学校夜间故障误报成「凭证过期」，害用户白折腾一轮重新提取。
    """
    values = list(results.values())
    ok = [r for r in values if r is not None and r.ok and r.value is not None]
    expired = [r for r in values if r is not None and r.session_expired]
    if not values:
        return "⚠️ 绑定里没有任何费种参数，请重新 /电费 绑定"
    if ok:
        if expired:
            return "⚠️ 仅部分费种可用（详见下方明细）"
        return "✅ 有效（学校接口已接受本次查询）"
    if expired:
        return "⚠️ 学校已拒绝（retcode 91001 会话超时），需重新提取 JSESSIONID"
    if all(r is None for r in values):
        return "⚠️ 学校接口不可用（网络异常或学校无响应），凭证状态未知，请稍后 /电费 查询 重试"
    return "⚠️ 学校有响应但未取到余额（见下方明细与 /电费 日志 的原始返回）"
