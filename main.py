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

CREDENTIAL_HINT = (
    "🔐 学校系统凭证已失效或尚未配置。\n"
    "请重新获取 JSESSIONID 后发送：/电费 凭证 JSESSIONID=xxxx\n"
    "（获取方式：企业微信打开缴费查询页，用抓包工具复制请求头 Cookie；\n"
    "期间也可用 /电费 登记 <度数> 手动记录余额）"
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
    "1.0.5",
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
            history_keep_days=int(self._cfg("history_keep_days", 60)),
        )
        self.hjnu = self._build_provider()

        self.scheduler = AsyncIOScheduler()
        poll_min = max(0, int(self._cfg("poll_interval_minutes", 20)))
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
            timeout=int(self._cfg("request_timeout_seconds", 15)),
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
        results = {}
        for kind, entry in self._fee_bindings(binding).items():
            results[kind] = await self._fetch_entry(entry)
        return results

    @staticmethod
    def _fee_entry(kind: str, params: dict) -> dict:
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
            return {"provider": "hjnu", "params": {
                "aid": elec_aid, "area": area, "building": building,
                "floor": floor, "room": room,
            }}
        except QueryError as e:
            logger.warning(f"[{PLUGIN_NAME}] 自动匹配宿舍电费房间失败：{e}")
            return None

    async def _send(self, umo: str, text: str) -> None:
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 推送失败到 {umo}: {e!r}")

    def _provider_of(self, binding: dict):
        return self.hjnu

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
        bindings = self.store.data.get("bindings", {})
        for umo, binding in list(bindings.items()):
            for kind, entry in self._fee_bindings(binding).items():
                result = await self._fetch_entry(entry)
                if result is None:
                    continue
                if result.ok and result.value is not None:
                    self.store.append_fee_history(
                        binding,
                        kind,
                        result.value,
                        result.unit,
                        keep_days=int(self._cfg("history_keep_days", 60)),
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
        warn = float(self._cfg("threshold_warn", 10))
        critical = float(self._cfg("threshold_critical", 5))
        cooldown = float(self._cfg("alert_cooldown_hours", 24)) * 3600
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
        self.store.save()

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
            header = "🚨 余额预警" if has_critical else "⚠️ 余额预警"
            lines = [f"{header} | {label}"]
            for i in items:
                if i["level"] == 2:
                    lines.append(
                        f"  {self._fee_name(i['kind'])}：{i['value']:.2f} {i['unit']}（≤ 紧急线 {i['critical']:g}）"
                    )
                else:
                    lines.append(
                        f"  {self._fee_name(i['kind'])}：{i['value']:.2f} {i['unit']}（≤ 预警线 {i['warn']:g}）"
                    )
            if has_critical:
                lines.append("请立即充值。")
            else:
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
            binding["last_daily_date"] = today
            self.store.save()
            text = self._daily_text(binding)
            if text:
                await self._send(umo, text)
                self._record_event(umo, "info", f"每日播报已发送：{self._binding_label(binding)}")

    def _daily_text(self, binding: dict) -> str | None:
        label = self._binding_label(binding)
        histories = binding.get("history_by_fee") or {}
        if not histories:
            latest = self.store.latest_value(binding) if self.store else None
            if latest is None:
                return None
            return f"☀️ 每日电费播报 | {label}\n空调费：{latest[0]:.2f} 度"
        lines = [f"☀️ 每日电费播报 | {label}"]
        for kind in ("ac", "elec"):
            history = histories.get(kind) or []
            if not history:
                continue
            value = float(history[-1]["v"])
            lines.append(f"{self._fee_name(kind)}：{value:.2f} {history[-1].get('u', '度')}")
        return "\n".join(lines)

    @staticmethod
    def _history_stats(history: list[dict]) -> dict | None:
        """计算 24h 用电、日均、最低/最高。"""
        if len(history) < 1:
            return None
        values = [float(h["v"]) for h in history]
        ts = [float(h["t"]) for h in history]
        unit = history[-1].get("u", "度")
        now = time.time()
        min_v = min(values)
        max_v = max(values)

        # 24h 用电：找最接近 (now-24h) 时刻的样本差值
        ref = history[0]
        for h in history:
            if now - float(h["t"]) >= 24 * 3600:
                ref = h
            else:
                break
        usage_24h = float(ref["v"]) - values[-1]
        if usage_24h < 0:
            usage_24h = 0.0

        # 日均：按全历史跨度
        span_days = (ts[-1] - ts[0]) / 86400
        per_day = None
        if span_days >= 0.5 and len(history) >= 2:
            drop = values[0] - values[-1]
            if drop > 0:
                per_day = drop / span_days

        return {
            "usage_24h": usage_24h,
            "per_day": per_day if per_day is not None else 0.0,
            "min": min_v,
            "max": max_v,
            "unit": unit,
        }

    def _record_event(self, umo: str, kind: str, text: str) -> None:
        """记录一条事件到内存事件流。"""
        self._events.append(
            {"t": time.time(), "kind": kind, "text": text, "umo": umo}
        )

    @staticmethod
    def _usage_since(binding: dict, hours: float) -> float | None:
        """估算近 N 小时用电量：最早一条与最新一条的差值。"""
        history = binding.get("history") or []
        if len(history) < 2:
            return None
        now = time.time()
        old_ref = None
        for h in history:
            if now - float(h["t"]) >= hours * 3600:
                old_ref = h
            else:
                break
        if old_ref is None:
            old_ref = history[0]
        usage = float(old_ref["v"]) - float(history[-1]["v"])
        return usage if usage > 0 else 0.0

    @staticmethod
    def _estimate_days(binding: dict, value: float) -> tuple[float | None, float | None]:
        """按全部历史平均日用电估算可用天数。"""
        history = binding.get("history") or []
        if len(history) < 2:
            return None, None
        span_days = (float(history[-1]["t"]) - float(history[0]["t"])) / 86400
        if span_days < 0.5:
            return None, None
        drop = float(history[0]["v"]) - float(history[-1]["v"])
        if drop <= 0:
            return None, None
        per_day = drop / span_days
        return value / per_day, per_day

    # ================= 指令：帮助与状态 =================

    @electric.command("帮助", alias={"help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看指令帮助"""
        yield event.plain_result(
            "⚡ 宿舍电费监控指令：\n"
            "/电费 绑定 — 启动绑定宿舍向导（自动同时关联空调费 + 宿舍电费）\n"
            "/电费 校区/楼栋/楼层/房间 <编号> — 逐级选择宿舍\n"
            "/电费 绑定 1 — 确认绑定\n"
            "/电费 解绑 — 取消监控\n"
            "/电费 查询 — 同时查询空调费和宿舍电费\n"
            "/电费 凭证 <JSESSIONID=...> — 更新会话凭证（热更新，无需重启）\n"
            "/电费 历史 [n] — 查看最近 n 条余额记录（默认 7，最多 60）\n"
            "/电费 日志 [n] — 查看最近 n 条事件 + 最近一次原始返回（默认 20，最多 100）\n"
            "/电费 状态 — 查看绑定与运行状态\n"
            "预警线：10；紧急线：5（空调费单位为度，宿舍电费单位为元）"
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
                f"预警线：{self._cfg('threshold_warn', 10):g} / 紧急 "
                f"{self._cfg('threshold_critical', 5):g}（空调费为度，宿舍电费为元）"
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
            if not binding.get("history_by_fee"):
                latest = self.store.latest_value(binding)
                if latest:
                    ago = (time.time() - latest[1]) / 60
                    lines.append(f"空调费：{latest[0]:.2f} 度（{ago:.0f} 分钟前）")
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
        except QueryError as e:
            yield event.plain_result(f"❌ {e}")
            return
        wizard["floor"], wizard["rooms"], wizard["step"] = floor, rooms, "room"
        lines = ["🚪 房间列表（前 60 个）："]
        lines.extend(f"{i}. {r.get('room')}（{r.get('roomid')}）" for i, r in enumerate(rooms[:60], 1))
        lines.append("\n请选择房间：发送 /电费 房间 <编号>")
        yield event.plain_result("\n".join(lines))

    @electric.command("房间")
    async def cmd_room(self, event: AstrMessageEvent, room_no: str | None = None):
        """列出房间"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        rooms = wizard.get("rooms") or []
        if room_no is None or not rooms:
            yield event.plain_result("用法：/电费 房间 <编号>（先 /电费 楼层）")
            return
        try:
            room = rooms[int(room_no) - 1]
        except (ValueError, IndexError):
            yield event.plain_result("房间编号无效")
            return
        wizard["room"], wizard["step"] = room, "bind"
        lines = [
            (
                f"📍 已选择：{wizard['area'].get('areaname')}/"
                f"{wizard['building'].get('building')}/"
                f"{wizard['floor'].get('floor')}/{room.get('room')}"
            ),
            "",
            "确认绑定并同时查询空调费、宿舍电费？",
            "发送 /电费 绑定 1 确认。",
        ]
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
        fees = {"ac": self._fee_entry("ac", ac_params)}
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
        self.store.save()
        results = await self._fetch_fees(binding)
        self._last_raw[umo] = {k: v.raw for k, v in results.items() if v is not None}
        lines = [f"✅ 绑定成功：{label}"]
        lines.append(
            "已自动关联宿舍电费房间"
            if "elec" in fees
            else "⚠️ 未自动关联宿舍电费（请检查 room token 是否在电费 aid 下也存在）"
        )
        for kind, result in results.items():
            if result and result.ok and result.value is not None:
                self.store.append_fee_history(
                    binding,
                    kind,
                    result.value,
                    result.unit,
                    keep_days=int(self._cfg("history_keep_days", 60)),
                )
                lines.append(self._fee_text(kind, result))
            elif result and result.session_expired:
                lines.append(f"{self._fee_name(kind)}：凭证已失效")
            elif result:
                lines.append(f"{self._fee_name(kind)}：查询失败：{result.raw}")
        lines.append("预警线：10；紧急线：5。轮询与预警已启用。")
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
        results = await self._fetch_fees(binding)
        if not results:
            yield event.plain_result("❌ 查询失败（网络异常或数据源不可用）。")
            self._record_event(umo, "error", "查询失败：网络异常")
            return
        self._last_raw[umo] = {k: v.raw for k, v in results.items() if v is not None}
        lines = [f"⚡ {self._binding_label(binding)}"]
        ok_count = 0
        for kind, result in results.items():
            if result and result.ok and result.value is not None:
                self.store.append_fee_history(
                    binding,
                    kind,
                    result.value,
                    result.unit,
                    keep_days=int(self._cfg("history_keep_days", 60)),
                )
                lines.append(self._fee_text(kind, result))
                ok_count += 1
            elif result and result.session_expired:
                lines.append(f"{self._fee_name(kind)}：凭证已失效")
            elif result:
                lines.append(f"{self._fee_name(kind)}：查询失败：{result.raw}")
        self.store.save()
        self._record_event(
            umo,
            "query",
            f"查询成功 {ok_count}/{len(results)} 项（{self._binding_label(binding)}）",
        )
        yield event.plain_result("\n".join(lines))

    @electric.command("历史")
    async def cmd_history(self, event: AstrMessageEvent, n: str | None = None):
        """查看最近 n 条余额记录（默认 7，最多 60）"""
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        if not binding:
            yield event.plain_result("尚未绑定房间。发送 /电费 绑定 开始。")
            return
        try:
            count = int(n) if n else 7
        except ValueError:
            count = 7
        count = max(1, min(60, count))
        histories = binding.get("history_by_fee") or {}
        if not histories:
            yield event.plain_result("暂无历史数据。下次轮询后会自动记录。")
            return
        lines = [f"📈 {self._binding_label(binding)} 历史"]
        any_data = False
        for kind in ("ac", "elec"):
            history = histories.get(kind) or []
            if not history:
                continue
            any_data = True
            window = history[-count:]
            lines.append(f"\n【{self._fee_name(kind)}】共 {len(history)} 条，展示最近 {len(window)} 条：")
            for h in window:
                ts = float(h["t"])
                ago = (time.time() - ts) / 3600
                when = (
                    "刚刚"
                    if ago < 1 / 60
                    else f"{ago * 60:.0f} 分钟前"
                    if ago < 1
                    else f"{ago:.1f} 小时前"
                    if ago < 24
                    else f"{ago / 24:.1f} 天前"
                )
                lines.append(
                    f"  {when}  {float(h['v']):.2f} {h.get('u', '度')}"
                )
            stats = self._history_stats(history)
            if stats:
                lines.append(
                    f"  24h 用电：{stats['usage_24h']:.2f} {history[-1].get('u', '度')} | "
                    f"日均：{stats['per_day']:.2f} | "
                    f"最低：{stats['min']:.2f} / 最高：{stats['max']:.2f}"
                )
        if not any_data:
            yield event.plain_result("暂无历史数据。下次轮询后会自动记录。")
            return
        self._record_event(umo, "info", f"查看历史（n={count}）")
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
        events = list(self._events)[-count:]
        lines = [f"📋 事件流（最近 {len(events)} 条）："]
        if not events:
            lines.append("  （暂无事件）")
        else:
            for ev in events:
                ts = float(ev.get("t", 0))
                when = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
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
        """更新缴费系统会话凭证（JSESSIONID）"""
        if not credential:
            yield event.plain_result(
                "用法：/电费 凭证 JSESSIONID=xxxx\n"
                "获取方式：在企业微信打开缴电费页面，用抓包工具复制请求头中的 "
                "Cookie 值（整段粘贴即可）。"
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
            self._last_raw[umo] = {k: v.raw for k, v in results.items() if v is not None}
            lines = ["✅ 凭证已更新。"]
            lines.extend(self._format_fee_results(results))
            self._record_event(umo, "credential", "凭证已更新")
            yield event.plain_result("\n".join(lines))
        else:
            yield event.plain_result("✅ 凭证已保存。")
