"""宿舍电费余额监控预警插件。

- /电费 指令组：向导式绑定房间、查询、手动登记、更新凭证等
- 定时轮询余额 → 低余额/紧急预警（含冷却），轮询同时保活会话凭证
- 每日定时播报：当前余额、近 24h 用电、预计可用天数
- 双数据源：hjnu（学校缴费系统自动查询）/ manual（手动登记兜底）
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from .providers import HjnuProvider, ManualProvider, QueryError, SessionExpiredError
    from .storage import Store
except ImportError:  # 兜底：被以非包方式加载时
    from providers import (  # type: ignore
        HjnuProvider,
        ManualProvider,
        QueryError,
        SessionExpiredError,
    )
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
    "宿舍电费余额监控预警：低余额预警、每日播报、可用天数预估",
    "1.0.0",
)
class DormElectricPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store: Store | None = None
        self.hjnu: HjnuProvider | None = None
        self.manual = ManualProvider()
        self.scheduler: AsyncIOScheduler | None = None
        self._wizard: dict[str, dict] = {}
        self._tasks: list[asyncio.Task] = []

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

    async def _send(self, umo: str, text: str) -> None:
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 推送失败到 {umo}: {e!r}")

    def _provider_of(self, binding: dict):
        name = binding.get("provider", "hjnu")
        return self.hjnu if name == "hjnu" else self.manual

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
            if binding.get("provider") == "manual":
                # 手动登记：无网络查询，直接基于登记值评估预警
                mv = binding.get("manual_value")
                if mv is not None:
                    await self._evaluate_alerts(umo, binding, float(mv))
                continue
            if binding.get("provider") != "hjnu":
                continue
            result = await self._safe_fetch(binding)
            if result is None:
                continue
            if result.ok and result.value is not None:
                self.store.append_history(
                    binding, result.value, keep_days=int(self._cfg("history_keep_days", 60))
                )
                binding.setdefault("alert_state", {})["session_dead"] = False
                await self._evaluate_alerts(umo, binding, result.value)
            elif result.session_expired:
                await self._notify_session_dead(umo, binding)
            self.store.save()

    async def _safe_fetch(self, binding: dict):
        provider = self._provider_of(binding)
        if provider is None:
            return None
        try:
            return await provider.fetch(binding)
        except QueryError as e:
            logger.warning(f"[{PLUGIN_NAME}] 查询失败：{e}")
            return None
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 查询异常：{e!r}")
            return None

    async def _evaluate_alerts(self, umo: str, binding: dict, value: float):
        warn = float(self._cfg("threshold_warn", 20))
        critical = float(self._cfg("threshold_critical", 10))
        cooldown = float(self._cfg("alert_cooldown_hours", 24)) * 3600
        if critical < warn:
            warn, critical = critical, warn
        if value <= critical:
            level = 2
        elif value <= warn:
            level = 1
        else:
            level = 0

        state = binding.setdefault("alert_state", {})
        prev = int(state.get("level", 0))
        now = time.time()
        label = self._binding_label(binding)

        if level == 0:
            state["level"] = 0
            return
        last_at = float(state.get("last_alert_at", {}).get(str(level), 0) or 0)
        need = level != prev or (now - last_at) >= cooldown
        if not need:
            return

        if level == 2:
            text = (
                f"🚨 电费紧急预警 | {label}\n"
                f"当前剩余：{value:.2f} 度（≤ 紧急线 {critical:g} 度）\n"
                "余额可能即将耗尽，请立即充值！"
            )
        else:
            text = (
                f"⚠️ 电费低余额预警 | {label}\n"
                f"当前剩余：{value:.2f} 度（≤ 预警线 {warn:g} 度）\n"
                "建议尽快充值。"
            )
        await self._send(umo, text)
        state["level"] = level
        state.setdefault("last_alert_at", {})[str(level)] = now
        self.store.save()

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

    def _daily_text(self, binding: dict) -> str | None:
        label = self._binding_label(binding)
        if binding.get("provider") == "manual":
            value = binding.get("manual_value")
            if value is None:
                return None
            return (
                f"☀️ 每日电费播报 | {label}\n"
                f"手动登记余额：{float(value):.2f} 度\n"
                "提示：发送 /电费 登记 <最新读数> 更新。"
            )
        latest = self.store.latest_value(binding)
        if latest is None:
            return None
        value, _ = latest
        usage_24h = self._usage_since(binding, hours=24)
        days_left, per_day = self._estimate_days(binding, value)
        lines = [
            f"☀️ 每日电费播报 | {label}",
            f"当前剩余：{value:.2f} 度",
        ]
        if usage_24h is not None:
            lines.append(f"近24h用电：{usage_24h:.2f} 度")
        if days_left is not None:
            extra = f"（日均 {per_day:.2f} 度）" if per_day else ""
            lines.append(f"预计可用：{days_left:.0f} 天{extra}")
        else:
            lines.append("预计可用：暂无足够用电数据")
        return "\n".join(lines)

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
            "/电费 项目 — 查看缴费项目\n"
            "/电费 校区 <项目编号> — 选择校区\n"
            "/电费 楼栋 <校区编号> — 选择楼栋\n"
            "/电费 楼层 <楼栋编号> — 选择楼层\n"
            "/电费 房间 <楼层编号> — 列出房间\n"
            "/电费 绑定 <房间编号> — 绑定并开始监控\n"
            "/电费 查询 — 立即查询余额\n"
            "/电费 登记 <度数> — 手动登记余额（无需凭证）\n"
            "/电费 凭证 <JSESSIONID=...> — 更新会话凭证\n"
            "/电费 状态 — 查看绑定与运行状态\n"
            "/电费 解绑 — 取消监控\n"
            "/电费 测试 — 测试查询并显示原始返回"
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
                f"预警线：低 {self._cfg('threshold_warn', 20):g} 度 / 紧急 "
                f"{self._cfg('threshold_critical', 10):g} 度"
            ),
        ]
        if not binding:
            lines.append("绑定：❌ 未绑定（/电费 项目 开始绑定）")
        else:
            latest = self.store.latest_value(binding)
            lines.append(
                f"绑定：✅ {self._binding_label(binding)}（模式 {binding.get('provider')}）"
            )
            if latest:
                ago = (time.time() - latest[1]) / 60
                lines.append(f"最新：{latest[0]:.2f} 度（{ago:.0f} 分钟前）")
        yield event.plain_result("\n".join(lines))

    # ================= 指令：绑定向导 =================

    @electric.command("项目")
    async def cmd_items(self, event: AstrMessageEvent):
        """列出可用的缴费项目"""
        items = self.config.get("fee_items", {}) or {}
        if not items:
            yield event.plain_result("配置中没有任何缴费项目（fee_items）。")
            return
        lines = ["📋 缴费项目："]
        for i, (aid, label) in enumerate(items.items(), 1):
            lines.append(f"{i}. {label}（{aid}）")
        lines.append("\n发送 /电费 校区 <编号> 继续")
        umo = event.unified_msg_origin
        aids = list(items.keys())
        self._wizard[umo] = {"aid": aids[0], "step": "area"}
        yield event.plain_result("\n".join(lines))

    @electric.command("校区")
    async def cmd_area(self, event: AstrMessageEvent, area_id: str | None = None):
        """选择缴费项目的校区"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        items = self.config.get("fee_items", {}) or {}
        if not items:
            yield event.plain_result("配置中没有任何缴费项目（fee_items）。")
            return
        if area_id is None:
            aid = wizard.get("aid") or next(iter(items.keys()))
        else:
            try:
                aid = list(items.keys())[int(area_id) - 1]
            except (ValueError, IndexError):
                yield event.plain_result("编号无效，请先 /电费 项目 查看列表")
                return
        try:
            areas = await self.hjnu.list_areas(aid)
        except SessionExpiredError:
            yield event.plain_result(CREDENTIAL_HINT)
        except QueryError as e:
            yield event.plain_result(f"❌ {e}")
            return
        self._wizard[umo] = {"aid": aid, "areas": areas, "step": "building"}
        lines = [f"🏫 校区列表（项目 {items.get(aid, aid)}）："]
        for i, area in enumerate(areas, 1):
            lines.append(f"{i}. {area.get('areaname')}（{area.get('area')}）")
        lines.append("\n发送 /电费 楼栋 <编号> 继续")
        yield event.plain_result("\n".join(lines))

    @electric.command("楼栋")
    async def cmd_building(self, event: AstrMessageEvent, building_id: str | None = None):
        """选择楼栋"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        areas = wizard.get("areas") or []
        if building_id is None or not areas:
            yield event.plain_result("用法：/电费 楼栋 <校区编号>（先 /电费 校区）")
            return
        try:
            area = areas[int(building_id) - 1]
        except (ValueError, IndexError):
            yield event.plain_result("校区编号无效")
            return
        try:
            buildings = await self.hjnu.list_buildings(wizard["aid"], area)
        except SessionExpiredError:
            yield event.plain_result(CREDENTIAL_HINT)
        except QueryError as e:
            yield event.plain_result(f"❌ {e}")
            return
        wizard["area"] = area
        wizard["buildings"] = buildings
        wizard["step"] = "floor"
        lines = ["🏢 楼栋列表："]
        for i, b in enumerate(buildings, 1):
            lines.append(f"{i}. {b.get('building')}（{b.get('buildingid')}）")
        lines.append("\n发送 /电费 楼层 <编号> 继续")
        yield event.plain_result("\n".join(lines))

    @electric.command("楼层")
    async def cmd_floor(self, event: AstrMessageEvent, floor_id: str | None = None):
        """选择楼层"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        buildings = wizard.get("buildings") or []
        if floor_id is None or not buildings:
            yield event.plain_result("用法：/电费 楼层 <楼栋编号>（先 /电费 楼栋）")
            return
        try:
            building = buildings[int(floor_id) - 1]
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
        wizard["building"] = building
        wizard["floors"] = floors
        wizard["step"] = "room"
        lines = ["🧱 楼层列表："]
        for i, f in enumerate(floors, 1):
            lines.append(f"{i}. {f.get('floor')}（{f.get('floorid')}）")
        lines.append("\n发送 /电费 房间 <编号> 继续")
        yield event.plain_result("\n".join(lines))

    @electric.command("房间")
    async def cmd_room(self, event: AstrMessageEvent, room_no: str | None = None):
        """列出房间"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        floors = wizard.get("floors") or []
        if room_no is None or not floors:
            yield event.plain_result("用法：/电费 房间 <楼层编号>（先 /电费 楼层）")
            return
        try:
            floor = floors[int(room_no) - 1]
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
        wizard["floor"] = floor
        wizard["rooms"] = rooms
        wizard["step"] = "bind"
        lines = ["🚪 房间列表（前 60 个）："]
        for i, r in enumerate(rooms[:60], 1):
            lines.append(f"{i}. {r.get('room')}（{r.get('roomid')}）")
        if len(rooms) > 60:
            lines.append(f"…共 {len(rooms)} 个，可用 /电费 房间 <楼层编号> 重查")
        lines.append("\n发送 /电费 绑定 <编号> 完成绑定")
        yield event.plain_result("\n".join(lines))

    @electric.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent, room_id: str | None = None):
        """绑定房间并开始监控"""
        umo = event.unified_msg_origin
        wizard = self._wizard.get(umo) or {}
        rooms = wizard.get("rooms") or []
        if room_id is None or not rooms:
            yield event.plain_result("用法：/电费 绑定 <房间编号>（先 /电费 房间）")
            return
        try:
            room = rooms[int(room_id) - 1]
        except (ValueError, IndexError):
            yield event.plain_result("房间编号无效")
            return
        area, building = wizard["area"], wizard["building"]
        floor = wizard["floor"]
        label = (
            f"{area.get('areaname')}/{building.get('building')}/"
            f"{floor.get('floor')}/{room.get('room')}"
        )
        binding = {
            "provider": "hjnu",
            "room_label": label,
            "params": {
                "aid": wizard["aid"],
                "area": area,
                "building": building,
                "floor": floor,
                "room": room,
            },
        }
        self.store.set_binding(umo, binding)
        self.store.save()
        result = await self._safe_fetch(binding)
        if result and result.ok:
            self.store.append_history(
                binding, result.value, keep_days=int(self._cfg("history_keep_days", 60))
            )
            self.store.save()
            yield event.plain_result(
                f"✅ 绑定成功：{label}\n当前剩余电量：{result.value:.2f} 度\n轮询与预警已启用。"
            )
        elif result and result.session_expired:
            yield event.plain_result(
                f"✅ 绑定成功：{label}\n⚠️ 但凭证已失效，查询失败。请 /电费 凭证 更新后自动恢复。"
            )
        else:
            raw = result.raw if result else "未知错误"
            yield event.plain_result(f"✅ 绑定成功：{label}\n⚠️ 首次查询失败：{raw}")

    @electric.command("解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """取消本会话的电费监控"""
        umo = event.unified_msg_origin
        if self.store.del_binding(umo):
            self.store.save()
            self._wizard.pop(umo, None)
            yield event.plain_result("✅ 已解绑并停止监控。")
        else:
            yield event.plain_result("当前会话没有绑定。")

    # ================= 指令：查询与登记 =================

    @electric.command("查询")
    async def cmd_query(self, event: AstrMessageEvent):
        """立即查询绑定的房间余额"""
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        if not binding:
            yield event.plain_result("尚未绑定房间。发送 /电费 项目 开始绑定。")
            return
        result = await self._safe_fetch(binding)
        if result is None:
            yield event.plain_result("❌ 查询失败（网络异常或数据源不可用）。")
            return
        if result.ok and result.value is not None:
            if binding.get("provider") == "hjnu":
                self.store.append_history(
                    binding, result.value, keep_days=int(self._cfg("history_keep_days", 60))
                )
                self.store.save()
            days_left, per_day = self._estimate_days(binding, result.value)
            text = f"⚡ {self._binding_label(binding)}\n当前剩余电量：{result.value:.2f} 度"
            if days_left is not None:
                text += f"\n预计可用：{days_left:.0f} 天（日均 {per_day:.2f} 度）"
            yield event.plain_result(text)
        elif result.session_expired:
            yield event.plain_result("🔐 凭证已失效。请发送 /电费 凭证 JSESSIONID=xxxx 更新。")
        else:
            yield event.plain_result(f"❌ 查询失败：{result.raw}")

    @electric.command("测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试查询并显示接口原始返回"""
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo)
        if not binding:
            yield event.plain_result("尚未绑定房间。")
            return
        provider = self._provider_of(binding)
        try:
            result = await provider.fetch(binding)
        except QueryError as e:
            yield event.plain_result(f"❌ 测试失败：{e}")
            return
        params = binding.get("params", {})
        yield event.plain_result(
            f"数据源：{binding.get('provider')}\n参数：{params.get('aid')} "
            f"{params.get('room', {}).get('room', '')}\n"
            f"结果：ok={result.ok} value={result.value}\n原始：{result.raw}"
        )

    @electric.command("登记")
    async def cmd_manual(self, event: AstrMessageEvent, value: str | None = None):
        """手动登记余额（无需学校凭证）"""
        umo = event.unified_msg_origin
        if value is None:
            binding = self.store.get_binding(umo)
            if binding and binding.get("manual_value") is not None:
                yield event.plain_result(
                    f"当前登记余额：{binding['manual_value']:g} 度\n更新：/电费 登记 <度数>"
                )
            else:
                yield event.plain_result("用法：/电费 登记 <度数>，例如 /电费 登记 95.36")
            return
        try:
            number = float(value)
        except ValueError:
            yield event.plain_result("度数格式不对，例如：/电费 登记 95.36")
            return
        binding = self.store.get_binding(umo) or {
            "provider": "manual",
            "room_label": event.get_sender_name(),
        }
        binding["provider"] = "manual"
        binding["manual_value"] = number
        binding["manual_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.store.set_binding(umo, binding)
        self.store.save()
        yield event.plain_result(
            f"✅ 已登记 {number:g} 度。\n低余额预警将基于此数值触发；建议每次查看电费后更新。"
        )

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
        binding = self.store.get_binding(event.unified_msg_origin)
        if binding and binding.get("provider") == "hjnu":
            result = await self._safe_fetch(binding)
            if result and result.ok:
                yield event.plain_result(f"✅ 凭证已更新，查询成功：{result.value:.2f} 度")
            else:
                raw = result.raw if result else "未知错误"
                yield event.plain_result(f"⚠️ 凭证已保存，但查询失败：{raw}")
        else:
            yield event.plain_result("✅ 凭证已保存。")
