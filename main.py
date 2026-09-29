"""宿舍电费余额监控预警插件。

- /电费 指令组：绑定宿舍向导、查询、状态、凭证、历史、日志
- 定时轮询余额 → 低余额/紧急预警（含冷却），预警按会话合并为单条消息
- 轮询同时保活学校缴费系统会话凭证
- 每日定时播报：当前余额（两种费种）
- 数据源：hjnu（学校缴费系统自动查询）
"""

import asyncio
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from .providers import HjnuProvider, QueryError, SessionExpiredError
    from .storage import Store
except ImportError:  # 兜底：被以非包方式加载时
    from providers import HjnuProvider, QueryError, SessionExpiredError  # type: ignore
    from storage import Store  # type: ignore

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:

    def get_astrbot_data_path() -> str:
        return "data"


PLUGIN_NAME = "astrbot_plugin_dorm_electric"

# 绑定向导里每页显示的房间数。房间多时用 /电费 房间 翻页、/电费 房间 p2 跳页，
# 选择时仍用全楼层绝对编号，避免超过一页的房间选不到。
ROOM_PAGE_SIZE = 30

CREDENTIAL_HINT = (
    "🔐 学校系统凭证已失效或尚未配置。\n"
    "重新获取 JSESSIONID 后私聊发送：/电费 凭证 JSESSIONID=xxxx\n"
    "（获取方式：企业微信打开缴费查询页让 Cookie 入库，再运行仓库内 "
    "tools/extract_cookie.py 提取，详见 README）"
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/107.0.5304.110 Safari/537.36 Language/zh ColorScheme/Light "
    "wxwork/5.0.11 (MicroMessenger/6.2) WindowsWechat MailPlugin_Electron WeMail "
    "embeddisk wwmver/3.26.511.637 noMediaCs/true"
)


@filter.command_group("电费")
def electric():
    """宿舍电费余额监控预警。"""


@register(
    PLUGIN_NAME,
    "Cecilian",
    "宿舍电费余额监控预警：低余额预警、每日播报、双费种同时查询",
    "1.0.8",
)
class DormElectricPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store: Store | None = None
        self.hjnu: HjnuProvider | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self._wizard: dict[str, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self._events: deque = deque(maxlen=200)
        self._pending_alerts: dict[str, list[dict]] = {}
        self._last_raw: dict[str, dict[str, Any]] = {}

    # ================= 生命周期 =================

    async def initialize(self):
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.store = Store(
            data_dir / "history.json",
            history_keep_days=self._cfg_int("history_keep_days", 60),
        )
        self.hjnu = self._build_provider()

        self.scheduler = AsyncIOScheduler()
        poll_min = self._cfg_int("poll_interval_minutes", 20)
        if poll_min:
            self.scheduler.add_job(
                self._poll_all,
                IntervalTrigger(minutes=max(5, poll_min)),
                id="poll",
                max_instances=1,
                coalesce=True,
            )
        if self._cfg("daily_report", True):
            hour, minute = self._parse_daily_time(self._cfg("daily_time", "08:00"))
            tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
            self.scheduler.add_job(
                self._daily_all,
                CronTrigger(hour=hour, minute=minute, timezone=tz),
                id="daily",
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.start()
        logger.info(f"[{PLUGIN_NAME}] 插件已初始化，轮询={poll_min}min")

        self._tasks.append(asyncio.create_task(self._startup_poll()))

    async def terminate(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if self.hjnu:
            await self.hjnu.close()
        if self.store:
            try:
                self.store.save()
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 保存数据失败：{e}")
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._pending_alerts.clear()

    # ================= 工具方法 =================

    def _cfg(self, key: str, default=None):
        value = default
        try:
            value = self.config.get(key, default)
        except Exception:
            pass
        if value is None or value == "":
            return default
        return value

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(float(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _build_provider(self) -> HjnuProvider:
        return HjnuProvider(
            base_url=str(self._cfg("hjnu_base", "http://pay2.hjnu.edu.cn")),
            query_path=str(
                self._cfg(
                    "hjnu_query_path",
                    "/wechat/basicQuery/queryElecRoomInfo.html",
                )
            ),
            cookie=str(self.config.get("hjnu_cookie", "") or ""),
            user_agent=str(self._cfg("hjnu_user_agent", DEFAULT_UA)),
            referer=str(
                self._cfg(
                    "hjnu_referer",
                    "http://pay2.hjnu.edu.cn/wechat/elecpay/queryelec.html",
                )
            ),
            timeout=self._cfg_int("request_timeout_seconds", 15),
            proxy=str(self._cfg("http_proxy", "")),
        )

    @staticmethod
    def _parse_daily_time(text: str) -> tuple[int, int]:
        try:
            parts = str(text).split(":")
            return int(parts[0]) % 24, int(parts[1]) % 60
        except (ValueError, IndexError):
            return 8, 0

    @staticmethod
    def _resolve_tz(name: str) -> timezone:
        """解析时区配置为标准库 timezone 对象，零外部依赖。

        当前仅识别 Asia/Shanghai（CST，UTC+8）。其他字符串回退为 UTC。
        """
        n = str(name or "").strip()
        if "Shanghai" in n or n in ("CST", "CST-8", "+08", "+08:00"):
            return timezone(timedelta(hours=8))
        try:
            offset = int(n)
            return timezone(timedelta(hours=offset))
        except ValueError:
            return timezone.utc

    def _binding_label(self, binding: dict) -> str:
        return binding.get("room_label") or binding.get("params", {}).get("room", {}).get(
            "room", "未知房间"
        )

    @staticmethod
    def _fee_name(kind: str) -> str:
        return "空调费" if kind == "ac" else "宿舍电费"

    def _fee_bindings(self, binding: dict) -> dict:
        fees = binding.get("fees")
        if isinstance(fees, dict) and fees:
            return fees
        params = binding.get("params")
        if params:
            return {"ac": {"provider": "hjnu", "params": params}}
        return {}

    @staticmethod
    def _fee_params(entry: dict) -> dict:
        return entry.get("params") or {}

    async def _fetch_entry(self, entry: dict):
        if not self.hjnu:
            return None
        try:
            return await self.hjnu.fetch({"params": self._fee_params(entry)})
        except QueryError as e:
            logger.warning(f"[{PLUGIN_NAME}] 查询失败：{e}")
            return None
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 查询异常：{e!r}")
            return None

    async def _fetch_fees(self, binding: dict) -> dict[str, object]:
        entries = self._fee_bindings(binding)
        keys = list(entries)
        values = await asyncio.gather(*(self._fetch_entry(entries[k]) for k in keys))
        return dict(zip(keys, values))

    def _remember_raw(self, umo: str, results: dict[str, object]) -> None:
        """保存最近一次查询的原始返回，供 /电费 日志 展示（按会话隔离）。"""
        raw = {k: v.raw for k, v in results.items() if v is not None}
        if raw:
            self._last_raw[umo] = raw

    async def _query_and_record(self, umo: str, binding: dict) -> tuple[list[str], int]:
        """查询全部费种并记录历史与原始返回；返回 (按费种格式化的行, 成功数)。"""
        results = await self._fetch_fees(binding)
        self._remember_raw(umo, results)
        keep_days = self._cfg_int("history_keep_days", 60)
        lines = []
        ok_count = 0
        for kind, result in results.items():
            if result and result.ok and result.value is not None:
                self.store.append_fee_history(
                    binding, kind, result.value, result.unit, keep_days=keep_days
                )
                lines.append(self._fee_text(kind, result))
                ok_count += 1
            elif result and result.session_expired:
                lines.append(f"{self._fee_name(kind)}：凭证已失效")
            elif result:
                lines.append(f"{self._fee_name(kind)}：查询失败：{result.raw}")
        return lines, ok_count

    @staticmethod
    def _fee_entry(params: dict) -> dict:
        return {"provider": "hjnu", "params": params}

    def _format_fee_results(self, results: dict[str, object]) -> list[str]:
        lines = []
        for kind in ("ac", "elec"):
            result = results.get(kind)
            if result is None:
                continue
            if result.ok and result.value is not None:
                lines.append(self._fee_text(kind, result))
            elif result.session_expired:
                lines.append(f"{self._fee_name(kind)}：凭证已失效")
            else:
                lines.append(f"{self._fee_name(kind)}：查询失败（{result.raw}）")
        return lines
    @staticmethod
    def _fee_text(kind: str, result) -> str:
        return f"{DormElectricPlugin._fee_name(kind)}：{result.value:.2f} {result.unit}"

    @staticmethod
    def _room_page(
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

    @staticmethod
    def _room_token(room_name: str) -> str | None:
        m = re.search(r"([A-Za-z]+)[-_](\d+)[-_](\d+)", room_name or "")
        return "".join(m.groups()) if m else None

    async def _match_elec_fee(self, ac_params: dict) -> dict | None:
        """按楼栋、楼层和房间编号自动寻找宿舍电费对应房间。"""
        if not self.hjnu:
            return None
        items = self.config.get("fee_items", {}) or {}
        aids = list(items)
        elec_aid = next((aid for aid in aids if aid != ac_params.get("aid")), None)
        if not elec_aid:
            return None
        room_name = str(ac_params.get("room", {}).get("room", ""))
        token = self._room_token(room_name)
        if not token:
            return None
        building_name = str(ac_params.get("building", {}).get("building", ""))
        base_building = re.sub(r"\d+$", "", building_name)
        floor_name = str(ac_params.get("floor", {}).get("floor", ""))
        try:
            areas = await self.hjnu.list_areas(elec_aid)
            if not areas:
                return None
            area = next((a for a in areas if a.get("areaname") == "校本部"), areas[0])
            buildings = await self.hjnu.list_buildings(elec_aid, area)
            building = next(
                (b for b in buildings if b.get("building") == base_building), None
            )
            if not building:
                building = next(
                    (b for b in buildings if base_building in str(b.get("building", ""))),
                    None,
                )
            if not building:
                return None
            floors = await self.hjnu.list_floors(elec_aid, area, building)
            floor = next((f for f in floors if f.get("floor") == floor_name), None)
            if not floor:
                return None
            rooms = await self.hjnu.list_rooms(elec_aid, area, building, floor)
            room = next(
                (r for r in rooms if token.lower() in re.sub(r"[^A-Za-z0-9]", "", str(r.get("room", ""))).lower()),
                None,
            )
            if not room:
                return None
            return self._fee_entry({
                "aid": elec_aid, "area": area, "building": building,
                "floor": floor, "room": room,
            })
        except QueryError as e:
            logger.warning(f"[{PLUGIN_NAME}] 自动匹配宿舍电费房间失败：{e}")
            return None

    async def _send(self, umo: str, text: str) -> bool:
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
            return True
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 推送失败到 {umo}: {e!r}")
            return False

    async def _startup_poll(self):
        await asyncio.sleep(8)
        try:
            await self._poll_all()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 启动轮询失败：{e!r}")

    # ================= 后台任务 =================

    async def _poll_all(self):
        """轮询所有 hjnu 绑定：更新历史、评估预警；顺带保活会话。"""
        if not self.store:
            return
        keep_days = self._cfg_int("history_keep_days", 60)
        bindings = self.store.data.get("bindings", {})
        for umo, binding in list(bindings.items()):
            results = await self._fetch_fees(binding)
            self._remember_raw(umo, results)
            for kind, result in results.items():
                if result is None:
                    continue
                if result.ok and result.value is not None:
                    self.store.append_fee_history(
                        binding,
                        kind,
                        result.value,
                        result.unit,
                        keep_days=keep_days,
                    )
                    await self._evaluate_alerts(
                        umo, binding, result.value, kind, result.unit
                    )
                elif result.session_expired:
                    await self._notify_session_dead(umo, binding)
            self.store.save()
            self._record_event(
                umo, "poll", f"轮询：{self._binding_label(binding)}"
            )
        await self._flush_alerts()

    async def _evaluate_alerts(
        self, umo: str, binding: dict, value: float, kind: str = "ac", unit: str = "度"
    ):
        """评估预警：仅更新 state 与 pending_alerts，由 _flush_alerts 统一发送。"""
        warn = self._cfg_float("threshold_warn", 10)
        critical = self._cfg_float("threshold_critical", 5)
        cooldown = self._cfg_float("alert_cooldown_hours", 24) * 3600
        if critical > warn:
            warn, critical = critical, warn
        if value <= critical:
            level = 2
        elif value <= warn:
            level = 1
        else:
            level = 0

        alert_state = binding.setdefault("alert_state", {})
        if "level" in alert_state:
            alert_state = {"ac": alert_state}
            binding["alert_state"] = alert_state
        state = alert_state.setdefault(kind, {})
        prev = int(state.get("level", 0))
        now = time.time()

        if level == 0:
            state["level"] = 0
            if prev > 0 and self._cfg_bool("notify_recovery", False):
                self._pending_alerts.setdefault(umo, []).append(
                    {
                        "kind": kind,
                        "level": 0,
                        "value": value,
                        "unit": unit,
                        "warn": warn,
                        "critical": critical,
                        "at": now,
                    }
                )
            return
        last_at = float(state.get("last_alert_at", {}).get(str(level), 0) or 0)
        need = level != prev or (now - last_at) >= cooldown
        if not need:
            return

        self._pending_alerts.setdefault(umo, []).append(
            {
                "kind": kind,
                "level": level,
                "value": value,
                "unit": unit,
                "warn": warn,
                "critical": critical,
                "at": now,
            }
        )
        state["level"] = level
        state.setdefault("last_alert_at", {})[str(level)] = now

    async def _flush_alerts(self) -> None:
        """将本轮所有 pending 预警合并为单条消息发送。"""
        if not self._pending_alerts:
            return
        for umo, items in self._pending_alerts.items():
            if not items:
                continue
            binding = self.store.get_binding(umo) if self.store else None
            label = self._binding_label(binding) if binding else "未知房间"
            has_critical = any(i["level"] == 2 for i in items)
            has_recovery = all(i["level"] == 0 for i in items)
            if has_critical:
                header = "🚨 余额预警"
            elif has_recovery:
                header = "✅ 余额恢复"
            else:
                header = "⚠️ 余额预警"
            lines = [f"{header} | {label}"]
            for i in items:
                if i["level"] == 2:
                    lines.append(
                        f"  {self._fee_name(i['kind'])}：{i['value']:.2f} {i['unit']}（≤ 紧急线 {i['critical']:g}）"
                    )
                elif i["level"] == 1:
                    lines.append(
                        f"  {self._fee_name(i['kind'])}：{i['value']:.2f} {i['unit']}（≤ 预警线 {i['warn']:g}）"
                    )
                else:
                    lines.append(
                        f"  {self._fee_name(i['kind'])}：已恢复至 {i['value']:.2f} {i['unit']}"
                    )
            if has_critical:
                lines.append("请立即充值。")
            elif not has_recovery:
                lines.append("建议尽快充值。")
            await self._send(umo, "\n".join(lines))
            self._record_event(
                umo,
                "alert",
                f"{header}（{len(items)} 项）",
            )
        self._pending_alerts.clear()

    async def _notify_session_dead(self, umo: str, binding: dict):
        state = binding.setdefault("alert_state", {})
        now = time.time()
        last = float(state.get("dead_notified_at", 0) or 0)
        if now - last < 24 * 3600:
            return
        state["dead_notified_at"] = now
        self.store.save()
        await self._send(
            umo,
            "🔐 电费查询凭证已失效，暂时无法自动查询余额。\n"
            "重新获取 JSESSIONID 后发送：/电费 凭证 JSESSIONID=xxxx\n"
            "（获取方式见 README，或联系管理员）",
        )

    async def _daily_all(self):
        if not self.store:
            return
        tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
        today = datetime.now(tz).date().isoformat()
        bindings = self.store.data.get("bindings", {})
        for umo, binding in list(bindings.items()):
            if binding.get("last_daily_date") == today:
                continue
            text = self._daily_text(binding)
            if not text:
                continue
            if await self._send(umo, text):
                binding["last_daily_date"] = today
                self.store.save()
                self._record_event(umo, "info", f"每日播报已发送：{self._binding_label(binding)}")

    def _daily_text(self, binding: dict) -> str | None:
        label = self._binding_label(binding)
        histories = binding.get("history_by_fee") or {}
        lines = [f"☀️ 每日电费播报 | {label}"]
        for kind in ("ac", "elec"):
            history = histories.get(kind) or []
            if not history:
                continue
            value = float(history[-1]["v"])
            lines.append(f"{self._fee_name(kind)}：{value:.2f} {history[-1].get('u', '度')}")
        return "\n".join(lines) if len(lines) > 1 else None

    @staticmethod
    def _daily_snapshots(
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

    @staticmethod
    def _history_stats(history: list[dict]) -> dict | None:
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

    def _record_event(self, umo: str, kind: str, text: str) -> None:
        """记录一条事件到内存事件流。"""
        self._events.append(
            {"t": time.time(), "kind": kind, "text": text, "umo": umo}
        )

    # ================= 指令：帮助与状态 =================

    @electric.command("帮助", alias={"help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看指令帮助"""
        warn = self._cfg_float("threshold_warn", 10)
        critical = self._cfg_float("threshold_critical", 5)
        yield event.plain_result(
            "⚡ 宿舍电费监控指令：\n"
            "/电费 绑定 — 启动绑定宿舍向导（自动同时关联空调费 + 宿舍电费）\n"
            "/电费 校区/楼栋/楼层 <编号> — 逐级选择宿舍\n"
            "/电费 房间 — 浏览房间列表（无参翻页，p<页码> 跳页）\n"
            "/电费 房间 <编号> — 按全楼层绝对编号选择房间\n"
            "/电费 绑定 1 — 确认绑定\n"
            "/电费 解绑 — 取消监控\n"
            "/电费 查询 — 同时查询空调费和宿舍电费\n"
            "/电费 凭证 <JSESSIONID=...> — 更新会话凭证（仅限私聊，热更新）\n"
            "/电费 历史 [n] — 查看最近 n 天每日余额（默认 7 天，最多 60 天）\n"
            "/电费 日志 [n] — 查看最近 n 条事件 + 最近一次原始返回（默认 20，最多 100）\n"
            "/电费 状态 — 查看绑定与运行状态\n"
            f"预警线：{warn:g}；紧急线：{critical:g}（空调费单位为度，宿舍电费单位为元）"
        )

    @electric.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前绑定与插件运行状态"""
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        cookie_ok = bool(str(self.config.get("hjnu_cookie", "") or "").strip())
        poll_min = self._cfg("poll_interval_minutes", 20)
        lines = [
            "📊 电费插件状态",
            f"凭证：{'已配置' if cookie_ok else '❌ 未配置（/电费 凭证）'}",
            (
                f"轮询间隔：{poll_min} 分钟 | 每日播报：{self._cfg('daily_time', '08:00')}"
                f"（{self._cfg('daily_timezone', 'Asia/Shanghai')}）"
            ),
            (
                f"预警线：{self._cfg_float('threshold_warn', 10):g} / 紧急 "
                f"{self._cfg_float('threshold_critical', 5):g}（空调费为度，宿舍电费为元）"
            ),
        ]
        if not binding:
            lines.append("绑定：❌ 未绑定（/电费 绑定 开始）")
        else:
            lines.append(f"绑定：✅ {self._binding_label(binding)}")
            fees = self._fee_bindings(binding)
            if fees:
                lines.append("已关联：" + "、".join(self._fee_name(kind) for kind in fees))
            for kind, history in (binding.get("history_by_fee") or {}).items():
                if history:
                    latest = history[-1]
                    ago = (time.time() - float(latest["t"])) / 60
                    lines.append(
                        f"{self._fee_name(kind)}：{float(latest['v']):.2f} "
                        f"{latest.get('u', '度')}（{ago:.0f} 分钟前）"
                    )
        yield event.plain_result("\n".join(lines))

    # ================= 指令：绑定向导 =================

    @electric.command("校区")
    async def cmd_area(self, event: AstrMessageEvent, area_id: str | None = None):
        """选择缴费项目的校区"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        items = self.config.get("fee_items", {}) or {}
        if not items:
            yield event.plain_result("配置中没有任何缴费项目（fee_items）。")
            return
        aid = wizard.get("aid")
        areas = wizard.get("areas") or []
        if not aid or not areas or area_id is None:
            yield event.plain_result("用法：/电费 校区 <编号>（先 /电费 绑定 启动向导）")
            return
        try:
            area = areas[int(area_id) - 1]
        except (ValueError, IndexError):
            yield event.plain_result("校区编号无效")
            return
        try:
            buildings = await self.hjnu.list_buildings(aid, area)
        except SessionExpiredError:
            yield event.plain_result(CREDENTIAL_HINT)
            return
        except QueryError as e:
            yield event.plain_result(f"❌ {e}")
            return
        wizard["area"], wizard["buildings"], wizard["step"] = area, buildings, "building"
        lines = ["🏢 楼栋列表："]
        lines.extend(f"{i}. {b.get('building')}（{b.get('buildingid')}）" for i, b in enumerate(buildings, 1))
        lines.append("\n请选择楼栋：发送 /电费 楼栋 <编号>")
        yield event.plain_result("\n".join(lines))

    @electric.command("楼栋")
    async def cmd_building(self, event: AstrMessageEvent, building_id: str | None = None):
        """选择楼栋"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        buildings = wizard.get("buildings") or []
        if building_id is None or not buildings:
            yield event.plain_result("用法：/电费 楼栋 <编号>（先 /电费 校区）")
            return
        try:
            building = buildings[int(building_id) - 1]
        except (ValueError, IndexError):
            yield event.plain_result("楼栋编号无效")
            return
        try:
            floors = await self.hjnu.list_floors(wizard["aid"], wizard["area"], building)
        except SessionExpiredError:
            yield event.plain_result(CREDENTIAL_HINT)
            return
        except QueryError as e:
            yield event.plain_result(f"❌ {e}")
            return
        wizard["building"], wizard["floors"], wizard["step"] = building, floors, "floor"
        lines = ["🧱 楼层列表："]
        lines.extend(f"{i}. {f.get('floor')}（{f.get('floorid')}）" for i, f in enumerate(floors, 1))
        lines.append("\n请选择楼层：发送 /电费 楼层 <编号>")
        yield event.plain_result("\n".join(lines))

    @electric.command("楼层")
    async def cmd_floor(self, event: AstrMessageEvent, floor_id: str | None = None):
        """选择楼层"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        floors = wizard.get("floors") or []
        if floor_id is None or not floors:
            yield event.plain_result("用法：/电费 楼层 <编号>（先 /电费 楼栋）")
            return
        try:
            floor = floors[int(floor_id) - 1]
        except (ValueError, IndexError):
            yield event.plain_result("楼层编号无效")
            return
        try:
            rooms = await self.hjnu.list_rooms(
                wizard["aid"], wizard["area"], wizard["building"], floor
            )
        except SessionExpiredError:
            yield event.plain_result(CREDENTIAL_HINT)
            return
        except QueryError as e:
            yield event.plain_result(f"❌ {e}")
            return
        wizard["floor"], wizard["rooms"], wizard["step"] = floor, rooms, "room"
        # 0 = 还没显示过任何页，下一次 /电费 房间 无参才展示第 1 页
        wizard["room_page"] = 0
        total = len(rooms)
        _, _, total_pages = self._room_page(rooms, 1)
        lines = [
            f"🧱 {floor.get('floor')}：共 {total} 间"
            + (f"，分 {total_pages} 页显示" if total_pages > 1 else "")
        ]
        lines.append("\n查看房间列表：发送 /电费 房间（无参数即为第 1 页）")
        yield event.plain_result("\n".join(lines))

    @electric.command("房间")
    async def cmd_room(self, event: AstrMessageEvent, room_no: str | None = None):
        """浏览房间列表（无参翻页、p<页码>跳页）或按绝对编号选择房间"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        rooms = wizard.get("rooms") or []
        if not rooms:
            yield event.plain_result("用法：/电费 房间 [编号]（先 /电费 楼层）")
            return
        # AstrBot 可能把纯数字参数转成 int，统一按字符串处理
        token = str(room_no).strip() if room_no is not None else ""

        if token[:1] in ("p", "P") and token[1:].isdigit():
            page = int(token[1:])
            wrapped = False
        elif token.isdigit():
            index = int(token) - 1
            if not 0 <= index < len(rooms):
                yield event.plain_result(
                    f"房间编号无效：本层共 {len(rooms)} 间，有效编号 1-{len(rooms)}"
                )
                return
            room = rooms[index]
            wizard["room"], wizard["step"] = room, "bind"
            yield event.plain_result(
                "\n".join(
                    [
                        (
                            f"📍 已选择：{wizard['area'].get('areaname')}/"
                            f"{wizard['building'].get('building')}/"
                            f"{wizard['floor'].get('floor')}/{room.get('room')}"
                        ),
                        "",
                        "确认绑定并同时查询空调费、宿舍电费？",
                        "发送 /电费 绑定 1 确认。",
                    ]
                )
            )
            return
        elif token:
            yield event.plain_result(
                f"无法识别的参数「{token}」。\n"
                "用法：/电费 房间（翻页）、/电费 房间 p<页码>（跳页）、"
                "/电费 房间 <编号>（选择，绝对编号）"
            )
            return
        else:
            _, _, total_pages = self._room_page(rooms, 1)
            if total_pages == 1:
                page, wrapped = 1, False
            else:
                page = int(wizard.get("room_page") or 0) + 1
                wrapped = page > total_pages
                if wrapped:
                    page = 1

        start, end, total_pages = self._room_page(rooms, page)
        wizard["room_page"] = page
        where = f"{wizard.get('building', {}).get('building')} / {wizard.get('floor', {}).get('floor')}"
        lines = [
            f"🚪 房间列表（{where}）",
            f"第 {page}/{total_pages} 页 · 本页第 {start + 1}-{end} 间（全楼层共 {len(rooms)} 间）",
        ]
        if wrapped:
            lines.append("（已到末页，回到第 1 页）")
        lines.append("")
        lines.extend(
            f"{i}. {r.get('room')}（{r.get('roomid')}）"
            for i, r in enumerate(rooms[start:end], start + 1)
        )
        lines.append("")
        if total_pages > 1:
            lines.append(
                "翻页：/电费 房间　　跳页：/电费 房间 p<页码>　　选择：/电费 房间 <编号>"
            )
        else:
            lines.append("选择：/电费 房间 <编号>")
        yield event.plain_result("\n".join(lines))

    @electric.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent, confirm: str | None = None):
        """启动绑定宿舍向导（无参）或确认绑定（带参 1）。"""
        umo = event.unified_msg_origin
        items = self.config.get("fee_items", {}) or {}
        if not items:
            yield event.plain_result("配置中没有任何缴费项目（fee_items）。")
            return

        if confirm is None:
            # 启动向导：取 fee_items 第 1 项 aid 作主线；老的 wizard 状态若不在新流程 step 列表中则重置
            valid_steps = {"area", "building", "floor", "room", "bind"}
            existing = self._wizard.get(umo) or {}
            if existing.get("step") not in valid_steps:
                self._wizard.pop(umo, None)
            aid = next(iter(items))
            try:
                areas = await self.hjnu.list_areas(aid)
            except SessionExpiredError:
                yield event.plain_result(CREDENTIAL_HINT)
                self._record_event(umo, "error", "绑定向导启动失败：凭证已失效")
                return
            except QueryError as e:
                yield event.plain_result(f"❌ {e}")
                self._record_event(umo, "error", f"绑定向导启动失败：{e}")
                return
            self._wizard[umo] = {"aid": aid, "areas": areas, "step": "area"}
            lines = ["🏫 校区（" + str(items.get(aid, aid)) + "）："]
            lines.extend(
                f"{i}. {a.get('areaname')}（{a.get('area')}）"
                for i, a in enumerate(areas, 1)
            )
            lines.append("\n下一步：/电费 校区 <编号>")
            self._record_event(
                umo, "info", f"绑定向导启动，主 aid={items.get(aid, aid)}"
            )
            yield event.plain_result("\n".join(lines))
            return

        wizard = self._wizard.get(umo) or {}
        if (
            str(confirm) != "1"
            or wizard.get("step") != "bind"
            or not wizard.get("room")
        ):
            yield event.plain_result(
                "请先 /电费 绑定 启动向导，选好房间后再 /电费 绑定 1 确认"
            )
            return
        room = wizard["room"]
        area, building = wizard["area"], wizard["building"]
        floor = wizard["floor"]
        label = (
            f"{area.get('areaname')}/{building.get('building')}/"
            f"{floor.get('floor')}/{room.get('room')}"
        )
        ac_params = {
            "aid": wizard["aid"],
            "area": area,
            "building": building,
            "floor": floor,
            "room": room,
        }
        fees = {"ac": self._fee_entry(ac_params)}
        elec = await self._match_elec_fee(ac_params)
        if elec:
            fees["elec"] = elec
        binding = {
            "provider": "hjnu",
            "room_label": label,
            "params": ac_params,
            "fees": fees,
        }
        self.store.set_binding(umo, binding)
        fee_lines, _ = await self._query_and_record(umo, binding)
        lines = [f"✅ 绑定成功：{label}"]
        lines.append(
            "已自动关联宿舍电费房间"
            if "elec" in fees
            else "⚠️ 未自动关联宿舍电费（请检查 room token 是否在电费 aid 下也存在）"
        )
        lines.extend(fee_lines)
        lines.append(
            f"预警线：{self._cfg_float('threshold_warn', 10):g}；"
            f"紧急线：{self._cfg_float('threshold_critical', 5):g}。轮询与预警已启用。"
        )
        self.store.save()
        self._record_event(
            umo,
            "info",
            f"绑定成功：{label}（{'ac+elec' if 'elec' in fees else '仅 ac'}）",
        )
        yield event.plain_result("\n".join(lines))

    @electric.command("解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """取消本会话的电费监控"""
        umo = event.unified_msg_origin
        if self.store.del_binding(umo):
            self.store.save()
            self._wizard.pop(umo, None)
            self._last_raw.pop(umo, None)
            yield event.plain_result("✅ 已解绑并停止监控。")
        else:
            yield event.plain_result("当前会话没有绑定。")

    # ================= 指令：查询 / 历史 / 日志 =================

    @electric.command("查询")
    async def cmd_query(self, event: AstrMessageEvent):
        """立即查询绑定的房间余额"""
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        if not binding:
            yield event.plain_result("尚未绑定房间。发送 /电费 绑定 开始。")
            return
        fee_lines, ok_count = await self._query_and_record(umo, binding)
        total = len(self._fee_bindings(binding))
        if not fee_lines:
            yield event.plain_result(
                f"⚡ {self._binding_label(binding)}\n❌ 查询失败（网络异常或数据源不可用）。"
            )
            self._record_event(umo, "error", "查询失败：网络异常")
            return
        self.store.save()
        self._record_event(
            umo,
            "query",
            f"查询成功 {ok_count}/{total} 项（{self._binding_label(binding)}）",
        )
        yield event.plain_result(
            f"⚡ {self._binding_label(binding)}\n" + "\n".join(fee_lines)
        )

    @electric.command("历史")
    async def cmd_history(self, event: AstrMessageEvent, n: str | None = None):
        """查看最近 n 天每日余额快照（默认 7 天，最多 60 天）"""
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        if not binding:
            yield event.plain_result("尚未绑定房间。发送 /电费 绑定 开始。")
            return
        try:
            days = int(n) if n else 7
        except ValueError:
            days = 7
        days = max(1, min(60, days))
        tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
        histories = binding.get("history_by_fee") or {}
        if not histories:
            yield event.plain_result("暂无历史数据。下次轮询后会自动记录。")
            return
        lines = [f"📈 {self._binding_label(binding)} 历史（近 {days} 天）"]
        any_data = False
        for kind in ("ac", "elec"):
            history = histories.get(kind) or []
            if not history:
                continue
            any_data = True
            # _daily_snapshots 最新在前；按时间正序逐日成行后倒序展示，
            # 与更早最近的可用日末快照求差：下降=用电、上升=充值
            snaps = self._daily_snapshots(history, tz, days)
            today = snaps[0][0]
            chron = list(reversed(snaps))
            row_lines: list[str] = []
            prev: dict | None = None
            for date, rec in chron:
                label = date.strftime("%m-%d") + ("（今天）" if date == today else "")
                if rec is None:
                    row_lines.append(f"  {label}  无记录")
                    continue
                row = f"  {label}  {float(rec['v']):.2f} {rec.get('u', '度')}"
                if prev is not None:
                    delta = float(prev["v"]) - float(rec["v"])
                    if delta > 0:
                        row += f"（-{delta:.2f}）"
                    elif delta < 0:
                        row += f"（充值 +{-delta:.2f}）"
                    else:
                        row += "（持平）"
                row_lines.append(row)
                prev = rec
            lines.append(f"\n【{self._fee_name(kind)}】每日余额：")
            lines.extend(reversed(row_lines))
            stats = self._history_stats(history)
            if stats:
                extra = ""
                if stats["recharged_24h"] > 0:
                    extra = f" | 检测到充值 +{stats['recharged_24h']:.2f}"
                lines.append(
                    f"  24h 用电：{stats['usage_24h']:.2f} {history[-1].get('u', '度')} | "
                    f"日均：{stats['per_day']:.2f} | "
                    f"最低：{stats['min']:.2f} / 最高：{stats['max']:.2f}{extra}"
                )
        if not any_data:
            yield event.plain_result("暂无历史数据。下次轮询后会自动记录。")
            return
        self._record_event(umo, "info", f"查看历史（近 {days} 天）")
        yield event.plain_result("\n".join(lines))

    @electric.command("日志")
    async def cmd_log(self, event: AstrMessageEvent, n: str | None = None):
        """查看最近 n 条事件 + 最近一次原始返回（默认 20，最多 100）"""
        umo = event.unified_msg_origin
        try:
            count = int(n) if n else 20
        except ValueError:
            count = 20
        count = max(1, min(100, count))
        tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
        events = [ev for ev in self._events if ev.get("umo") == umo][-count:]
        lines = [f"📋 事件流（最近 {len(events)} 条）："]
        if not events:
            lines.append("  （暂无事件）")
        else:
            for ev in events:
                ts = float(ev.get("t", 0))
                when = datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")
                kind = ev.get("kind", "?")
                text = ev.get("text", "")
                lines.append(f"  {when}  [{kind}] {text}")
        raw = self._last_raw.get(umo) or {}
        if raw:
            lines.append("\n🔍 最近一次原始返回（按费种）：")
            for kind, txt in raw.items():
                lines.append(f"  [{self._fee_name(kind)}] {txt}")
        else:
            lines.append("\n🔍 最近一次原始返回：（无，先发 /电费 查询 触发一次）")
        yield event.plain_result("\n".join(lines))

    # ================= 指令：凭证 =================

    @electric.command("凭证")
    async def cmd_credential(self, event: AstrMessageEvent, credential: str | None = None):
        """更新缴费系统会话凭证（JSESSIONID，仅限私聊）"""
        if not event.is_private_chat():
            yield event.plain_result(
                "🔒 凭证是全局会话密钥，请私聊机器人发送 /电费 凭证 更新。"
            )
            return
        if not credential:
            yield event.plain_result(
                "用法：/电费 凭证 JSESSIONID=xxxx\n"
                "获取方式：在企业微信打开缴电费页面让 Cookie 入库，"
                "再用 README 提供的本地解密脚本提取（无需抓包）。"
            )
            return
        text = credential.strip()
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        self.config["hjnu_cookie"] = text
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存配置失败：{e!r}")
        if self.hjnu:
            self.hjnu.update_cookie(text)
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        if binding:
            results = await self._fetch_fees(binding)
            self._remember_raw(umo, results)
            lines = ["✅ 凭证已更新。"]
            lines.extend(self._format_fee_results(results))
            self._record_event(umo, "credential", "凭证已更新")
            yield event.plain_result("\n".join(lines))
        else:
            yield event.plain_result("✅ 凭证已保存。")
