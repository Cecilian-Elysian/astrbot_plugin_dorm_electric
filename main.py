"""宿舍电费余额监控预警插件。

- /电费 指令组：绑定宿舍向导、查询、状态、凭证、历史、日志、自检、确认验证码
- 定时轮询余额 → 低余额/紧急预警（含冷却，分费种独立预警线），预警按会话合并
- 轮询同时保活学校缴费系统会话凭证；每日定时播报（含 24h 充值检测）
- 10 个 LLM 工具：绑定/查询/预警线等全部可对话操作；写操作需用户回复验证码
- on_llm_request 钩子：聊到电费相关话题时在幕后提示模型使用本插件工具
- WebUI 仪表盘：插件详情页内嵌余额卡片与折线图（只读）
- 数据源：hjnu（学校缴费系统自动查询）
"""

import asyncio
import difflib
import re
import secrets
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

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

# 待确认项的验证码位数与默认有效期（秒），可被 ai_bind_code_ttl 覆盖。
CODE_LENGTH = 6

# 预警静音上限（小时）：「静音」只影响提醒推送，随时可逆，不需要验证码。
ALERT_MUTE_MAX_HOURS = 168

# 预警线 kind 参数的别名 → 内部 kind。指令（/电费 预警线 空调 15）与 AI 工具
# （set_alert_threshold 的 kind）共用同一份，避免两处各自维护后漂移。
THRESHOLD_KIND_MAP: dict[str, str] = {
    "ac": "ac", "all": "all",
    "空调": "ac", "空调费": "ac",
    "elec": "elec", "电": "elec", "电费": "elec", "宿舍电费": "elec",
    "宿舍电": "elec", "宿舍": "elec",
}

# 群聊是只读的：绑定/解绑会改「群」这份绑定，会影响到群里所有人。
GROUP_WRITE_DENIED = (
    "群聊里不能绑定或解绑（会影响到群里所有人）。\n"
    "请私聊机器人发送 /电费 绑定，我一步步带你弄；"
    "群里可以随时问我查电费余额。"
)

# 用户回复验证码时的严格格式：整条消息里除了标点只剩验证码。
# 这样群里有人问「481526 度电够吗」不会被误判成已同意。
CODE_ONLY_PATTERN = r"[\s，,。.!！?？:：]*{code}[\s，,。.!！?？:：]*"

# 私聊放宽：「确认 481526」「验证码是481526」也算亲手回复。
# 码是 6 位随机数且按会话隔离，私聊里没有误判对象；群聊仍只用上面的严格格式。
CODE_KEYWORD_PATTERN = r"(?:确认|验证码|码)\s*[是码:：,，\s]*{code}(?!\d)"

# —— AI 触达（幕后指令注入）——
# 用户消息或近几轮上下文命中这些词时，才把 AI_ELECTRIC_HINT 追加进 system_prompt。
# 宁宽勿漏：误命中（如聊手机充电）最多多花一轮 token，模型不会乱调工具；
# 漏命中则 AI 可能想不起来用插件。按真实用户说法随时可增删。
AI_HINT_KEYWORDS: tuple[str, ...] = (
    "电费", "空调费", "水电", "电量", "用电", "多少度", "几度电", "度电",
    "余额", "缴费", "充值", "欠费", "还能用", "撑几天", "够用",
    "宿舍电", "绑定宿舍", "解绑", "静音", "播报", "低于", "提醒", "春雪楼",
)

# 追加给模型的幕后指令（固定文本，利于前缀缓存；严禁回显任何凭证值）。
AI_ELECTRIC_HINT = (
    "【宿舍电费插件】机器人接有宿舍电费监控插件。用户话题涉及电费、空调费、"
    "余额、用电量、充值缴费，或想绑定/改绑/解绑宿舍监控、调整提醒与播报时："
    "必须调用 dorm_electric_* 系列工具获取真实数据，严禁编造或估算余额数字。"
    "查任意宿舍的余额用 dorm_electric_query_room（直接传用户原话）；"
    "查本会话绑定宿舍的余额与趋势用 dorm_electric_balance；"
    "绑定/改绑用 dorm_electric_bind_room（传房间原话，用户回复验证码后再调 "
    "dorm_electric_confirm）。/电费 指令只是兜底，不要主动让用户记指令。"
)


def _llm_request_hook():
    """取 on_llm_request 装饰器；宿主版本过旧没有该钩子时退化为 no-op。

    插件必须在缺钩子的宿主上照常加载，所以这里不抛 AttributeError。
    """
    deco = getattr(filter, "on_llm_request", None)
    if deco is None:
        return lambda func: func
    return deco()

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
    "宿舍电费余额监控预警：低余额预警、每日播报、双费种同时查询、支持 AI 对话绑定与 WebUI 仪表盘",
    "1.1.7",
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
        # AI 写操作待确认项：umo → {code, action, at, user_ok, ...}
        self._bind_tokens: dict[str, dict] = {}
        # 房间名反查结果缓存：f"{umo}|{token}" → (时间戳, 文案)
        self._lookup_cache: dict[str, tuple[float, str]] = {}
        # 会话内最近定位成功的房间：umo → {"params":…, "label":…, "at":…}
        # 用户说「绑定」「春雪」这类碎片时，提示 AI 用记忆里的房间重调，不再反复反问
        self._last_room: dict[str, dict] = {}
        # 预警静音：umo → 静音截止时间戳。只影响提醒推送（内存态，重启清空）
        self._alert_muted: dict[str, float] = {}

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
        self._register_dashboard()

        self._tasks.append(asyncio.create_task(self._startup_poll()))

    async def terminate(self):
        # 顺序重要：先停任务再关资源。旧顺序（先关 client 后取消任务）会让
        # 运行中的轮询在 close 的 await 间隙经 _get_client 重建一个再无人
        # 关闭的新 client（热重载泄漏），且尾部历史改动不落盘。
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.hjnu:
            await self.hjnu.close()
        if self.store:
            try:
                self.store.save()
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 保存数据失败：{e}")
        self._pending_alerts.clear()
        self._bind_tokens.clear()
        self._lookup_cache.clear()
        self._last_room.clear()
        self._alert_muted.clear()
        self._wizard.clear()
        self._last_raw.clear()
        self._events.clear()

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
        return dict(zip(keys, values, strict=True))

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

    def _format_fee_results(
        self, results: dict[str, object], include_missing: bool = False
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
                        f"{self._fee_name(kind)}：❌ 未取到响应（网络异常或学校无响应）"
                    )
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
            try:
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
                self._record_event(
                    umo, "poll", f"轮询：{self._binding_label(binding)}"
                )
            except Exception as e:
                # 单个绑定出问题（如磁盘满导致 save 抛 OSError）只废它自己，
                # 其余绑定照常轮询、预警照常 flush。
                logger.error(
                    f"[{PLUGIN_NAME}] 轮询 {umo} 失败：{e!r}", exc_info=True
                )
        try:
            self.store.save()
        except OSError as e:
            logger.error(f"[{PLUGIN_NAME}] 轮询后保存数据失败：{e}")
        await self._flush_alerts()

    def _effective_thresholds(
        self, binding: dict | None, kind: str = "ac"
    ) -> tuple[float, float]:
        """生效预警线（按费种）。

        优先级：会话 per-fee 自定义 > 全局 per-fee 配置（threshold_warn_ac 等）
        > 全局旧配置（threshold_warn/critical，两费种共用，老用户零感知）。
        会话存储兼容两代格式：新 {"ac": {warn, critical}, "elec": {…}} 只存
        自定义过的费种；旧 {"warn":…, "critical":…} 视为两费种共用。
        """
        warn = (
            self._cfg_float(f"threshold_warn_{kind}", 0)
            or self._cfg_float("threshold_warn", 10)
        )
        critical = (
            self._cfg_float(f"threshold_critical_{kind}", 0)
            or self._cfg_float("threshold_critical", 5)
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

    def _threshold_lines(self, binding: dict | None) -> list[str]:
        """两费种生效预警线展示行（balance / 预警线状态 / 配置摘要共用）。"""
        lines = []
        for kind in ("ac", "elec"):
            unit = "度" if kind == "ac" else "元"
            w, c = self._effective_thresholds(binding, kind)
            lines.append(
                f"{self._fee_name(kind)}：预警 {w:g} {unit} / 紧急 {c:g} {unit}"
            )
        return lines

    @staticmethod
    def _set_session_threshold(
        binding: dict, kind: str, warn: float, critical: float
    ) -> None:
        """写一条会话 per-fee 自定义预警线（新格式，只动指定费种）。"""
        binding.setdefault("thresholds", {})[kind] = {
            "warn": warn,
            "critical": critical,
        }

    def _daily_muted_until(self, binding: dict | None) -> float:
        try:
            return float((binding or {}).get("daily_muted_until") or 0)
        except (TypeError, ValueError):
            return 0.0

    async def _evaluate_alerts(
        self, umo: str, binding: dict, value: float, kind: str = "ac", unit: str = "度"
    ):
        """评估预警：仅更新 state 与 pending_alerts，由 _flush_alerts 统一发送。"""
        if float(self._alert_muted.get(umo, 0) or 0) > time.time():
            return
        warn, critical = self._effective_thresholds(binding, kind)
        cooldown = self._cfg_float("alert_cooldown_hours", 24) * 3600
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
        last_map = state.get("last_alert_at")
        if not isinstance(last_map, dict):
            # 脏数据防御：last_alert_at 被外写坏时按无冷却处理，别让每轮轮询都炸
            last_map = {}
        last_at = float(last_map.get(str(level), 0) or 0)
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
        """将本轮所有 pending 预警合并为单条消息发送。

        只清发送成功的会话：发送失败（QQ 推送瞬断等）的保留到下一轮轮询重发，
        否则 alert_state 已记账、冷却期会把它吞掉，预警静默丢失 24 小时。
        """
        for umo, items in list(self._pending_alerts.items()):
            if not items:
                self._pending_alerts.pop(umo, None)
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
            if not has_recovery:
                lines.append("（说「静音 24小时」或发 /电费 静音 可暂停提醒）")
            if not await self._send(umo, "\n".join(lines)):
                continue
            self._pending_alerts.pop(umo, None)
            self._record_event(
                umo,
                "alert",
                f"{header}（{len(items)} 项）",
            )

    async def _notify_session_dead(self, umo: str, binding: dict):
        state = binding.setdefault("alert_state", {})
        now = time.time()
        last = float(state.get("dead_notified_at", 0) or 0)
        if now - last < 24 * 3600:
            return
        if not await self._send(
            umo,
            "🔐 电费查询凭证已失效，暂时无法自动查询余额。\n"
            "重新获取 JSESSIONID 后发送：/电费 凭证 JSESSIONID=xxxx\n"
            "（获取方式见 README，或联系管理员）",
        ):
            # 发送失败不记账：下轮轮询重试，避免被 24h 去重吞掉后彻底失声
            return
        state["dead_notified_at"] = now
        self.store.save()

    async def _daily_all(self):
        if not self.store:
            return
        tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
        today = datetime.now(tz).date().isoformat()
        bindings = self.store.data.get("bindings", {})
        for umo, binding in list(bindings.items()):
            if self._daily_muted_until(binding) > time.time():
                continue
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
            unit = history[-1].get("u", "度")
            line = f"{self._fee_name(kind)}：{value:.2f} {unit}"
            stats = self._history_stats(history)
            if stats and stats.get("recharged_24h", 0) > 0:
                line += f"（24h 检测到充值 +{stats['recharged_24h']:.2f} {unit}）"
            lines.append(line)
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

    # ================= 绑定向导：选择器与渲染器 =================
    # /电费 指令与 LLM 工具共用同一套选择/渲染逻辑，避免两处实现漂移。
    # 工具侧「该选哪一层」由 wizard.step 决定，模型传错层级也错不了——
    # 这正是 /电费 校区/楼栋/楼层/房间「参数选上一层」那个老坑的解法。

    WIZARD_STEPS = ("area", "building", "floor", "room", "bind")

    def _wizard_state(self, umo: str) -> dict:
        return self._wizard.get(umo) or {}

    def _step(self, umo: str) -> str:
        step = self._wizard.get(umo, {}).get("step")
        return step if step in self.WIZARD_STEPS else ""

    @staticmethod
    def _as_index(raw, total: int, err_text: str) -> tuple[int, str | None]:
        """把指令参数/模型入参转成 1-based 下标，非法时返回 err_text。"""
        try:
            index = int(str(raw).strip())
        except (TypeError, ValueError):
            return 0, err_text
        if not 1 <= index <= total:
            return 0, err_text
        return index, None

    async def _select_start(self, umo: str) -> tuple[dict | None, str | None]:
        """启动向导：取主 aid（fee_items 第 1 项）并加载校区列表。"""
        items = self.config.get("fee_items", {}) or {}
        if not items:
            return None, "配置中没有任何缴费项目（fee_items）。"
        existing = self._wizard.get(umo) or {}
        if existing.get("step") not in self.WIZARD_STEPS:
            self._wizard.pop(umo, None)
        aid = next(iter(items))
        try:
            areas = await self.hjnu.list_areas(aid)
        except SessionExpiredError:
            self._record_event(umo, "error", "绑定向导启动失败：凭证已失效")
            return None, CREDENTIAL_HINT
        except QueryError as e:
            self._record_event(umo, "error", f"绑定向导启动失败：{e}")
            return None, f"❌ {e}"
        self._wizard[umo] = {"aid": aid, "areas": areas, "step": "area"}
        self._record_event(umo, "info", f"绑定向导启动，主 aid={items.get(aid, aid)}")
        return self._wizard[umo], None

    async def _select_area(self, umo: str, raw) -> tuple[dict | None, str | None]:
        """选校区并加载楼栋列表。"""
        wizard = self._wizard.get(umo) or {}
        aid, areas = wizard.get("aid"), wizard.get("areas") or []
        if not aid or not areas:
            return None, "用法：/电费 校区 <编号>（先 /电费 绑定 启动向导）"
        index, err = self._as_index(raw, len(areas), "校区编号无效")
        if err:
            return None, err
        area = areas[index - 1]
        try:
            buildings = await self.hjnu.list_buildings(aid, area)
        except SessionExpiredError:
            return None, CREDENTIAL_HINT
        except QueryError as e:
            return None, f"❌ {e}"
        wizard["area"], wizard["buildings"], wizard["step"] = area, buildings, "building"
        self._wizard[umo] = wizard
        return wizard, None

    async def _select_building(self, umo: str, raw) -> tuple[dict | None, str | None]:
        """选楼栋并加载楼层列表。"""
        wizard = self._wizard.get(umo) or {}
        buildings = wizard.get("buildings") or []
        if not wizard.get("area") or not buildings:
            return None, "用法：/电费 楼栋 <编号>（先 /电费 校区）"
        index, err = self._as_index(raw, len(buildings), "楼栋编号无效")
        if err:
            return None, err
        building = buildings[index - 1]
        try:
            floors = await self.hjnu.list_floors(wizard["aid"], wizard["area"], building)
        except SessionExpiredError:
            return None, CREDENTIAL_HINT
        except QueryError as e:
            return None, f"❌ {e}"
        wizard["building"], wizard["floors"], wizard["step"] = (
            building,
            floors,
            "floor",
        )
        self._wizard[umo] = wizard
        return wizard, None

    async def _select_floor(self, umo: str, raw) -> tuple[dict | None, str | None]:
        """选楼层并加载该层房间列表（room_page 归零，等用户/模型第一次翻页）。"""
        wizard = self._wizard.get(umo) or {}
        floors = wizard.get("floors") or []
        if not wizard.get("building") or not floors:
            return None, "用法：/电费 楼层 <编号>（先 /电费 楼栋）"
        index, err = self._as_index(raw, len(floors), "楼层编号无效")
        if err:
            return None, err
        floor = floors[index - 1]
        try:
            rooms = await self.hjnu.list_rooms(
                wizard["aid"], wizard["area"], wizard["building"], floor
            )
        except SessionExpiredError:
            return None, CREDENTIAL_HINT
        except QueryError as e:
            return None, f"❌ {e}"
        wizard["floor"], wizard["rooms"], wizard["step"] = floor, rooms, "room"
        # 0 = 还没显示过任何页，下一次无参查看才展示第 1 页
        wizard["room_page"] = 0
        self._wizard[umo] = wizard
        return wizard, None

    def _render_areas(self, umo: str) -> str:
        wizard = self._wizard.get(umo) or {}
        items = self.config.get("fee_items", {}) or {}
        aid = wizard.get("aid")
        lines = ["🏫 校区（" + str(items.get(aid, aid)) + "）："]
        lines.extend(
            f"{i}. {a.get('areaname')}（{a.get('area')}）"
            for i, a in enumerate(wizard.get("areas") or [], 1)
        )
        lines.append("\n下一步：/电费 校区 <编号>")
        return "\n".join(lines)

    def _render_buildings(self, umo: str) -> str:
        wizard = self._wizard.get(umo) or {}
        lines = ["🏢 楼栋列表："]
        lines.extend(
            f"{i}. {b.get('building')}（{b.get('buildingid')}）"
            for i, b in enumerate(wizard.get("buildings") or [], 1)
        )
        lines.append("\n请选择楼栋：发送 /电费 楼栋 <编号>")
        return "\n".join(lines)

    def _render_floors(self, umo: str) -> str:
        wizard = self._wizard.get(umo) or {}
        lines = ["🧱 楼层列表："]
        lines.extend(
            f"{i}. {f.get('floor')}（{f.get('floorid')}）"
            for i, f in enumerate(wizard.get("floors") or [], 1)
        )
        lines.append("\n请选择楼层：发送 /电费 楼层 <编号>")
        return "\n".join(lines)

    def _render_floor_rooms(self, umo: str) -> str:
        """刚选完楼层时的摘要（不列房间，房间多时一屏放不下）。"""
        wizard = self._wizard.get(umo) or {}
        rooms = wizard.get("rooms") or []
        total = len(rooms)
        _, _, total_pages = self._room_page(rooms, 1)
        lines = [
            f"🧱 {wizard.get('floor', {}).get('floor')}：共 {total} 间"
            + (f"，分 {total_pages} 页显示" if total_pages > 1 else "")
        ]
        lines.append("\n查看房间列表：发送 /电费 房间（无参数即为第 1 页）")
        return "\n".join(lines)

    def _render_rooms(self, umo: str, page: int, wrapped: bool = False) -> str:
        wizard = self._wizard.get(umo) or {}
        rooms = wizard.get("rooms") or []
        start, end, total_pages = self._room_page(rooms, page)
        page = start // max(1, ROOM_PAGE_SIZE) + 1  # 越界页码夹回真实页，避免「第 2/1 页」
        wizard["room_page"] = page
        self._wizard[umo] = wizard
        where = (
            f"{wizard.get('building', {}).get('building')} / "
            f"{wizard.get('floor', {}).get('floor')}"
        )
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
        return "\n".join(lines)

    def _render_room_picked(self, umo: str) -> str:
        wizard = self._wizard.get(umo) or {}
        return "\n".join(
            [
                (
                    f"📍 已选择：{wizard.get('area', {}).get('areaname')}/"
                    f"{wizard.get('building', {}).get('building')}/"
                    f"{wizard.get('floor', {}).get('floor')}/"
                    f"{wizard.get('room', {}).get('room')}"
                ),
                "",
                "确认绑定并同时查询空调费、宿舍电费？",
                "发送 /电费 绑定 1 确认。",
            ]
        )

    def _room_listing(self, umo: str, token) -> str:
        """房间列表的三种语义：无参翻页（末页回绕）、p<页码> 跳页、纯数字选房。

        房间号用全楼层绝对编号，与分页显示的序号一致，所以房间再多也选得到。
        """
        wizard = self._wizard.get(umo) or {}
        rooms = wizard.get("rooms") or []
        if not rooms:
            return "用法：/电费 房间 [编号]（先 /电费 楼层）"
        self._wizard[umo] = wizard
        token = str(token).strip() if token is not None else ""
        if token[:1] in ("p", "P") and token[1:].isdigit():
            page, wrapped = int(token[1:]), False
        elif token.isdigit():
            index, err = self._as_index(
                token,
                len(rooms),
                f"房间编号无效：本层共 {len(rooms)} 间，有效编号 1-{len(rooms)}",
            )
            if err:
                return err
            wizard["room"], wizard["step"] = rooms[index - 1], "bind"
            return self._render_room_picked(umo)
        elif token:
            return (
                f"无法识别的参数「{token}」。\n"
                "用法：/电费 房间（翻页）、/电费 房间 p<页码>（跳页）、"
                "/电费 房间 <编号>（选择，绝对编号）"
            )
        else:
            _, _, total_pages = self._room_page(rooms, 1)
            if total_pages == 1:
                page, wrapped = 1, False
            else:
                page = int(wizard.get("room_page") or 0) + 1
                wrapped = page > total_pages
                if wrapped:
                    page = 1
        return self._render_rooms(umo, page, wrapped)

    def _room_no_of(self, umo: str) -> int | None:
        """当前已选房间在该层的绝对编号，供 AI 调 bind_room 用。"""
        wizard = self._wizard.get(umo) or {}
        room = wizard.get("room")
        if not room:
            return None
        for i, r in enumerate(wizard.get("rooms") or [], 1):
            if r is room or r.get("room") == room.get("room"):
                return i
        return None

    def _unbind(self, umo: str) -> str:
        if self.store and self.store.del_binding(umo):
            self.store.save()
            self._wizard.pop(umo, None)
            self._last_raw.pop(umo, None)
            return "✅ 已解绑并停止监控。\n要再绑定，直接说房间号即可，例如「春雪楼817」。"
        return "当前会话没有绑定。"

    async def _complete_bind(self, umo: str) -> str:
        """完成绑定：写绑定、关联宿舍电费、查一次余额、落库。"""
        wizard = self._wizard.get(umo) or {}
        room = wizard.get("room")
        if wizard.get("step") != "bind" or not room:
            return "请先 /电费 绑定 启动向导，选好房间后再 /电费 绑定 1 确认"
        area, building, floor = wizard["area"], wizard["building"], wizard["floor"]
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
        return "\n".join(lines)

    # ================= AI 对话：确认验证码 =================

    _ACTION_LABEL: ClassVar[dict[str, str]] = {
        "bind": "绑定",
        "unbind": "解绑",
        "rebind": "改绑",
    }


    def _ai_enabled(self) -> bool:
        return self._cfg_bool("ai_tools_enabled", True)

    def _token_ttl(self) -> int:
        return max(30, self._cfg_int("ai_bind_code_ttl", 300))

    def _purge_tokens(self) -> None:
        now = time.time()
        for umo, token in list(self._bind_tokens.items()):
            if now - float(token.get("at", 0)) >= self._token_ttl():
                self._bind_tokens.pop(umo, None)

    def _issue_token(self, umo: str, action: str, **payload) -> dict:
        """生成待确认项；同一会话只保留最新一个，旧码立即作废。"""
        self._purge_tokens()
        token = {
            "code": f"{secrets.randbelow(900000) + 100000:0{CODE_LENGTH}d}",
            "action": action,
            "at": time.time(),
            "user_ok": False,
        }
        token.update(payload)
        self._bind_tokens[umo] = token
        self._record_event(
            umo,
            "ai",
            f"已生成{self._ACTION_LABEL.get(action, action)}确认码"
            f"（{token['code']}，{token.get('label', '')}）",
        )
        return token

    def _pending_view(self, umo: str) -> str:
        """未带码时的待确认项概览（只对发起它的会话可见）。"""
        self._purge_tokens()
        token = self._bind_tokens.get(umo)
        if not token:
            return "当前会话没有待确认的操作。"
        left = max(0, int(self._token_ttl() - (time.time() - token["at"])))
        action = str(token.get("action", "bind"))
        if action == "bind":
            what = f"绑定到 {token['label']}"
        elif action == "rebind":
            prev = token.get("prev_label")
            what = f"改绑到 {token['label']}" + (f"（原 {prev}）" if prev else "")
        else:
            what = f"解绑 {token['label']}"
        return (
            f"⏳ 待确认：{what}\n"
            f"验证码：{token['code']}（剩余 {left // 60} 分 {left % 60} 秒，"
            f"{'已收到你的确认' if token['user_ok'] else '等你回复验证码'}）\n"
            f"回复这个 {CODE_LENGTH} 位数字即可完成，或发 /电费 确认 {token['code']}"
        )

    def _take_token(self, umo: str, code: str) -> tuple[dict | None, str | None]:
        """校验并消费待确认项。user_ok 必须由用户亲手回复验证码才置位。"""
        self._purge_tokens()
        token = self._bind_tokens.get(umo)
        if not token:
            return None, (
                "本会话没有待确认的操作。需要用户先确认绑定/解绑，"
                "请重新调用 dorm_electric_bind_room 或 dorm_electric_unbind。"
            )
        if str(code or "").strip() != token["code"]:
            return None, (
                "验证码不匹配（可能是复述有误）。"
                "请让用户重新发起一次绑定/解绑，会生成新的验证码。"
            )
        if not token.get("user_ok"):
            return None, (
                "还没有检测到用户回复验证码，暂不能执行。请让用户把刚才的 "
                f"{CODE_LENGTH} 位验证码原样发过来（私聊直接发数字即可），"
                "收到后你再调用本工具。"
            )
        self._bind_tokens.pop(umo, None)
        return token, None

    async def _run_token(self, umo: str, code: str) -> str:
        """执行待确认项：解绑或完成绑定。"""
        token, err = self._take_token(umo, code)
        if err:
            return err
        if token["action"] == "unbind":
            text = self._unbind(umo)
            self._record_event(umo, "ai", f"用户确认解绑：{text}")
            return text
        if token["action"] == "rebind":
            self._wizard.setdefault(umo, {}).update(token.get("wizard") or {})
            text = await self._complete_bind(umo)
            prev = str(token.get("prev_label", ""))
            if prev and text.startswith("✅ 绑定成功："):
                text = f"✅ 已改绑（原 {prev}）：" + text[len("✅ 绑定成功："):]
            self._record_event(
                umo, "ai", f"用户确认改绑：{prev} → {token.get('label', '')}"
            )
            return text
        # 把 issue 时快照的层级写回向导：期间 AI 可能又调了 browse/pick
        self._wizard.setdefault(umo, {}).update(token.get("wizard") or {})
        text = await self._complete_bind(umo)
        self._record_event(umo, "ai", "用户确认绑定")
        return text

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_user_replied_code(self, event: AstrMessageEvent):
        """记录「用户亲手回复了确认验证码」。

        放在事件级而不是 on_llm_request：这样标记与 LLM 是否被唤醒、模型是否
        支持 function calling 完全无关，纯数字消息被别的插件先吃掉也不影响。
        判定很严——整条消息里除标点外只剩验证码，所以群里问
        「481526 度电够吗」不会被误判成已同意。不发消息、不阻断事件。
        """
        umo = event.unified_msg_origin
        token = self._bind_tokens.get(umo)
        if not token or token.get("user_ok"):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        code = re.escape(token["code"])
        # 私聊放宽：带「确认/验证码/码」关键词且数字与待确认码一致也算亲手回复。
        # 码是 6 位随机数、按会话隔离，私聊没有误判对象；群聊维持严格格式。
        bare_ok = bool(re.fullmatch(CODE_ONLY_PATTERN.format(code=code), text))
        keyword_ok = event.is_private_chat() and bool(
            re.search(CODE_KEYWORD_PATTERN.format(code=code), text)
        )
        if not (bare_ok or keyword_ok):
            return
        token["user_ok"] = True
        self._record_event(
            umo,
            "ai",
            f"用户已回复{self._ACTION_LABEL.get(token['action'], token['action'])}确认码",
        )

    # ================= AI 触达：幕后指令注入 =================
    # 弱模型（flash 级）选不选工具全靠自觉；聊到电费时在 system_prompt 末尾
    # 明确指示「必须调 dorm_electric_* 工具、严禁编数字」，是让 AI「反应过来」
    # 最有效的一招。只在关键词命中时注入：平时零 token 开销，提示文本固定
    # 也有利于服务商侧的前缀缓存。

    @staticmethod
    def _collect_hint_texts(event: AstrMessageEvent, req) -> list[str]:
        """关键词扫描的文本源：本轮消息 + 本轮 prompt + 近几轮历史。"""
        texts: list[str] = []
        msg = str(getattr(event, "message_str", "") or "")
        if msg:
            texts.append(msg)
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str) and prompt:
            texts.append(prompt)
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            for item in contexts[-6:]:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, str) and content:
                    texts.append(content)
                elif isinstance(content, list):
                    # 多模态 content：[{"type": "text", "text": …}, …]
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
        return texts

    def _hit_electric_topic(self, event: AstrMessageEvent, req) -> bool:
        return any(
            kw in text
            for text in self._collect_hint_texts(event, req)
            for kw in AI_HINT_KEYWORDS
        )

    @_llm_request_hook()
    async def inject_electric_hint(self, event: AstrMessageEvent, req) -> None:
        """关键词命中的对话，在 system_prompt 末尾追加电费工具提示（幂等）。"""
        try:
            if not self._ai_enabled() or not self._cfg_bool("ai_prompt_hint", True):
                return
            if not self._hit_electric_topic(event, req):
                return
            prompt = getattr(req, "system_prompt", None)
            if not isinstance(prompt, str):
                prompt = ""
            if AI_ELECTRIC_HINT in prompt:
                return
            req.system_prompt = (prompt.rstrip() + "\n" + AI_ELECTRIC_HINT).strip()
        except Exception as e:  # 注入失败绝不能影响正常对话
            logger.warning(f"[{PLUGIN_NAME}] 电费提示注入失败：{e!r}")

    # ================= AI 对话：绑定向导工具 =================
    # 工具返回值语义（AstrBot v4.27 astr_agent_tool_exec._execute_local）：
    #   return str            → 文本作为工具结果喂给 LLM
    #   yield plain_result()  → 框架把消息直接发到聊天窗口，LLM 收不到
    # 所以查询类一律 return；生成验证码的两个工具用 event.send() 直发，
    # 再 return 一句给 LLM 的指令，避免它复述 6 位数字出错。

    @filter.llm_tool(name="dorm_electric_browse")
    async def tool_dorm_electric_browse(self, event: AstrMessageEvent) -> str:
        """查看宿舍电费的学校结构：当前该选校区、楼栋、楼层还是房间。

        第一次调用会从「校区」开始；之后按已选到的层级继续往下。
        只读，群里也能用。
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        umo = event.unified_msg_origin
        step = self._step(umo)
        if not step:
            _, err = await self._select_start(umo)
            if err:
                return err
            return self._render_areas(umo) + "\n\n（下一步：确定校区后调用 dorm_electric_pick(index=N)）"
        if step == "bind":
            return (
                self._render_room_picked(umo)
                + f"\n\n（下一步：调用 dorm_electric_bind_room(room_no={self._room_no_of(umo)}）"
                " 发起绑定，用户回复验证码后再调用 dorm_electric_confirm）"
            )
        if step == "room":
            return (
                self._room_listing(umo, None)
                + "\n\n（下一步：调用 dorm_electric_pick(index=房间编号) 选房，"
                "或 dorm_electric_pick(page=N) 翻页）"
            )
        body = {
            "area": self._render_areas,
            "building": self._render_buildings,
            "floor": self._render_floors,
        }[step](umo)
        hint = {
            "area": "（下一步：调用 dorm_electric_pick(index=校区编号)）",
            "building": "（下一步：调用 dorm_electric_pick(index=楼栋编号)）",
            "floor": "（下一步：调用 dorm_electric_pick(index=楼层编号)）",
        }[step]
        return f"{body}\n\n{hint}"

    @filter.llm_tool(name="dorm_electric_pick")
    async def tool_dorm_electric_pick(
        self,
        event: AstrMessageEvent,
        index: int = 0,
        page: int = 0,
    ) -> str:
        """在最近一次 dorm_electric_browse 列出的选项里选一项，往下一层走。

        Args:
            index(int): 要选的序号，取自 browse 结果里带编号的那一行
            page(int): 当前层是房间列表时要翻到第几页，0 表示不翻页
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        umo = event.unified_msg_origin
        step = self._step(umo)
        if not step:
            return "还没有开始浏览流程，请先调用 dorm_electric_browse() 查看可选的校区。"
        if step == "bind":
            return (
                self._render_room_picked(umo)
                + f"\n\n（下一步：调用 dorm_electric_bind_room(room_no={self._room_no_of(umo)}）"
                " 发起绑定）"
            )
        if step == "area":
            _, err = await self._select_area(umo, index)
            return err or (
                self._render_buildings(umo)
                + "\n\n（下一步：调用 dorm_electric_pick(index=楼栋编号)）"
            )
        if step == "building":
            _, err = await self._select_building(umo, index)
            return err or (
                self._render_floors(umo)
                + "\n\n（下一步：调用 dorm_electric_pick(index=楼层编号)）"
            )
        if step == "floor":
            _, err = await self._select_floor(umo, index)
            return err or (
                self._render_floor_rooms(umo)
                + "\n\n（下一步：调用 dorm_electric_browse() 查看该层房间，"
                "或 dorm_electric_pick(index=房间编号) 直接选）"
            )
        # step == "room"：page 给就翻页，否则 index 当全楼层绝对编号选房
        if page:
            return self._room_listing(umo, f"p{int(page)}")
        return self._room_listing(umo, index)

    @filter.llm_tool(name="dorm_electric_bind_room")
    async def tool_dorm_electric_bind_room(
        self, event: AstrMessageEvent, room_no: int = 0, hint: str = ""
    ) -> str:
        """选定房间并向用户发一个绑定确认码（此时还没真正绑定）。

        用户想绑定、换宿舍或改绑监控房间时调用本工具。用户说过房间信息
        （例如「春雪楼2 8层 A817」「春雪楼817」）时，把原话传给
        hint 直接发起，完全不需要 browse/pick；几轮之前说过的也算——对话里能找到
        房间就传，不要重新问。用户只说了「绑定」这类碎片时，工具会提示本会话
        最近定位过的房间，照它给的 hint 重调即可。楼栋名以工具返回为准，不要臆造。
        已绑定时传一个不同的房间就是「改绑」：同样只发一个确认码，确认后
        原房间的历史与预警状态清零、开始监控新房间。传回同一个房间会被拒绝。
        只有完全说不出房间信息时，才用 browse/pick 逐级选到房间列表，
        再把列表里带编号那行的编号传给 room_no。
        凭证（JSESSIONID）这类密钥永远不要向用户索要、不要转述或保存：
        用户自己贴出来时，让他私下发送 /电费 凭证 JSESSIONID=… 更新。

        Args:
            room_no(int): 仅当已用 browse/pick 走到房间列表时，列表里带编号那行的编号
            hint(string): 用户提到的房间原话，如「春雪楼817」「春雪楼2 8层 A817」「A-8-17」；传了它就不需要 room_no
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        if not event.is_private_chat():
            return GROUP_WRITE_DENIED
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo) if self.store else None
        prev_label = self._binding_label(binding) if binding else ""
        wizard = self._wizard_state(umo)
        snapshot: dict
        label: str
        index = 0
        if str(hint or "").strip():
            params, err = await self._resolve_room(umo, hint)
            if err:
                return err
            assert params
            snapshot = {**params, "step": "bind"}
            label = "/".join(
                str(params[scope].get(key, ""))
                for scope, key in (
                    ("area", "areaname"),
                    ("building", "building"),
                    ("floor", "floor"),
                    ("room", "room"),
                )
            )
        else:
            # step 为 room 是刚看到列表；为 bind 是已选好房间但还没确认
            if self._step(umo) not in ("room", "bind") or not wizard.get("rooms"):
                return (
                    "还没选到房间列表，用户也没说房间号：优先在对话里问出房间"
                    "（如「春雪楼2 8层 A817」）后用 hint 直接发起，"
                    "或调用 dorm_electric_browse() 逐级选到房间。"
                    + self._last_room_line(umo)
                )
            rooms = wizard["rooms"]
            index, err = self._as_index(
                room_no, len(rooms), f"房间编号无效：本层共 {len(rooms)} 间，有效编号 1-{len(rooms)}"
            )
            if err:
                return err
            self._room_listing(umo, index)
            w = self._wizard_state(umo)
            snapshot = {
                "aid": w.get("aid"),
                "area": w.get("area"),
                "building": w.get("building"),
                "floor": w.get("floor"),
                "room": w.get("room"),
                "step": "bind",
            }
            label = str(w.get("room", {}).get("room", ""))
        room_id = (snapshot.get("room") or {}).get("roomid")
        pending = self._bind_tokens.get(umo)
        if binding:
            bound_id = (binding.get("params", {}).get("room") or {}).get("roomid")
            if room_id is not None and bound_id == room_id:
                return (
                    f"当前已绑定 {prev_label}（就是这个房间），无需重复绑定或改绑。"
                )
            if (
                pending
                and pending.get("action") == "rebind"
                and ((pending.get("wizard") or {}).get("room") or {}).get("roomid")
                == room_id
            ):
                return f"改绑到 {label} 的验证码仍是 {pending['code']}，请让用户回复这个验证码。"
            token = self._issue_token(
                umo,
                "rebind",
                room_no=index,
                label=label,
                wizard=snapshot,
                prev_label=prev_label,
            )
            await event.send(
                MessageChain().message(
                    f"🔄 待改绑：{prev_label}\n→ {label}\n"
                    f"确认码：{token['code']}（{self._token_ttl() // 60} 分钟内有效，一次性）\n"
                    f"确认后我会改为监控新房间（原房间的历史与预警状态清零）。"
                    f"回复 {CODE_LENGTH} 位确认码即可。"
                )
            )
            return (
                f"已向用户发出从 {prev_label} 改绑到 {label} 的确认码（{token['code']}）。"
                "不要复述这串数字，等用户回复后你调用 dorm_electric_confirm(code=用户回复的码)。"
            )
        if pending and pending.get("action") == "bind":
            old_rid = ((pending.get("wizard") or {}).get("room") or {}).get("roomid")
            if old_rid is not None and old_rid == room_id:
                return f"{label} 的绑定验证码仍是 {pending['code']}，请让用户回复这个验证码。"
        token = self._issue_token(
            umo, "bind", room_no=index, label=label, wizard=snapshot
        )
        await event.send(
            MessageChain().message(
                f"📍 待绑定：{label}\n"
                f"确认码：{token['code']}（{self._token_ttl() // 60} 分钟内有效，一次性）\n"
                f"请把 {CODE_LENGTH} 位确认码原样发给我，收到后我立刻完成绑定并报当前余额。"
            )
        )
        return (
            f"已向用户直接发出 {label} 的确认码（{token['code']}）。"
            "不要复述这串数字，等用户回复后你调用 dorm_electric_confirm(code=用户回复的码)。"
        )


    @filter.llm_tool(name="dorm_electric_unbind")
    async def tool_dorm_electric_unbind(self, event: AstrMessageEvent) -> str:
        """向用户发一个解绑确认验证码（此时还没解绑）。"""
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        if not event.is_private_chat():
            return GROUP_WRITE_DENIED
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo) if self.store else None
        if not binding:
            return "当前会话没有绑定，无需解绑。"
        label = self._binding_label(binding)
        pending = self._bind_tokens.get(umo)
        if pending and pending.get("action") == "unbind":
            return f"解绑 {label} 的验证码仍是 {pending['code']}，请让用户回复这个验证码。"
        token = self._issue_token(umo, "unbind", label=label)
        await event.send(
            MessageChain().message(
                f"🗑️ 待解绑：{label}\n"
                f"确认码：{token['code']}（{self._token_ttl() // 60} 分钟内有效，一次性）\n"
                f"确认后我会停止监控这个房间。回复 {CODE_LENGTH} 位确认码即可。"
            )
        )
        return (
            f"已向用户直接发出解绑 {label} 的确认码（{token['code']}）。"
            "不要复述这串数字，等用户回复后你调用 dorm_electric_confirm(code=用户回复的码)。"
        )

    @filter.llm_tool(name="dorm_electric_confirm")
    async def tool_dorm_electric_confirm(
        self, event: AstrMessageEvent, code: str = ""
    ) -> str:
        """提交用户回复的确认码，完成绑定或解绑。

        Args:
            code(string): 用户刚刚回复的确认码，必须是用户亲手发的那串数字
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        if not event.is_private_chat():
            return GROUP_WRITE_DENIED
        return await self._run_token(event.unified_msg_origin, code)

    # ================= AI 对话：查询工具 =================

    def _config_brief(self) -> str:
        """给 AI 的一句话配置摘要：分费种预警线、播报时间、轮询间隔。"""
        poll = self._cfg("poll_interval_minutes", 20)
        return (
            f"预警线 {'；'.join(self._threshold_lines(None))}；"
            f"每日播报 {self._cfg('daily_time', '08:00')}"
            f"（{self._cfg('daily_timezone', 'Asia/Shanghai')}）；"
            f"轮询间隔 {poll} 分钟"
            + ("（已关闭）" if not poll else "")
        )

    @staticmethod
    def _alert_hint(value: float, warn: float, critical: float) -> str:
        if value <= critical:
            return f"⚠️ 已低于紧急线 {critical:g}，建议马上充值。"
        if value <= warn:
            return f"⚠️ 已低于预警线 {warn:g}，建议尽快充值。"
        return "✅ 高于预警线，状态正常。"

    @filter.llm_tool(name="dorm_electric_balance")
    async def tool_dorm_electric_balance(
        self, event: AstrMessageEvent, days: int = 0
    ) -> str:
        """查本会话绑定宿舍的当前电费余额（空调费 + 宿舍电费），顺带告知预警线与播报设置。

        只要用户问起本会话宿舍的电费/空调费，例如「电费还剩多少」「空调费还有多少」
        「这个月用了多少」「还能用几天」「余额够不够」，都调用本工具；不要凭印象回答。

        Args:
            days(int): 顺便看最近几天的每日余额，0 表示只看当前余额，最大 60
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo) if self.store else None
        if not binding:
            return (
                "本会话还没有绑定宿舍。请像平常聊天一样反问用户要查哪一间宿舍"
                "（房间号或「楼栋+楼层+房间」都行），拿到后用 dorm_electric_query_room 当场查；"
                "如果他希望每天被提醒余额，再引导他私聊完成绑定。"
            )
        results = await self._fetch_fees(binding)
        self._remember_raw(umo, results)
        lines = [f"⚡ {self._binding_label(binding)}"]
        lines.extend(self._format_fee_results(results, include_missing=True))
        for kind in ("ac", "elec"):
            result = results.get(kind)
            warn, critical = self._effective_thresholds(binding, kind)
            if result is not None and result.ok and result.value is not None:
                lines.append(
                    f"  {self._fee_name(kind)}："
                    f"{self._alert_hint(result.value, warn, critical)}"
                )
                stats = self._history_stats(
                    (binding.get("history_by_fee") or {}).get(kind) or []
                )
                if stats and stats["per_day"] > 0:
                    left = result.value / stats["per_day"]
                    est = f"{left:.0f}" if left < 999 else "999+"
                    lines.append(
                        f"  {self._fee_name(kind)}：按最近日均 "
                        f"{stats['per_day']:.2f}，约还能用 {est} 天"
                    )

        lines.append(self._config_brief())
        if binding.get("thresholds"):
            lines.append(
                f"（本会话预警线已自定义：{'；'.join(self._threshold_lines(binding))}；"
                "说「恢复默认预警线」可还原）"
            )
        daily_until = self._daily_muted_until(binding)
        if daily_until > time.time():
            tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
            when = datetime.fromtimestamp(daily_until, tz).strftime("%m-%d %H:%M")
            lines.append(f"🔕 每日播报已静音至 {when}")
        mute_line = self._mute_line(umo)
        if mute_line:
            lines.append(mute_line)
        if days:
            lines.extend(self._trend_lines(binding, int(days)))
        return "\n".join(lines)

    def _mute_until_text(self, umo: str) -> str:
        """静音截止时间（本地时区文案）；未静音返回空串。"""
        until = float(self._alert_muted.get(umo, 0) or 0)
        if until <= time.time():
            return ""
        tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
        return datetime.fromtimestamp(until, tz).strftime("%m-%d %H:%M")

    def _mute_line(self, umo: str) -> str:
        """静音状态行（静音中才输出），供 balance / 自检复用。"""
        when = self._mute_until_text(umo)
        return f"🔕 预警静音中：{when} 前不再推送余额预警" if when else ""

    def _mute_set(self, umo: str, hours: float) -> str:
        hours = max(1.0, min(float(hours), float(ALERT_MUTE_MAX_HOURS)))
        self._alert_muted[umo] = time.time() + hours * 3600
        self._record_event(umo, "info", f"预警静音 {hours:g} 小时")
        return (
            f"🔕 已静音余额预警 {hours:g} 小时（至 {self._mute_until_text(umo)}）。"
            "期间余额再低也不会提醒，查询、绑定、每日播报照常；"
            "随时发 /电费 静音 0 恢复。"
        )

    def _mute_clear(self, umo: str) -> str:
        if self._alert_muted.pop(umo, None) is None:
            return "当前没有静音中的预警。"
        self._record_event(umo, "info", "解除预警静音")
        return "🔔 已恢复余额预警。"

    @filter.llm_tool(name="dorm_electric_mute_alerts")
    async def tool_dorm_electric_mute_alerts(
        self, event: AstrMessageEvent, hours: float = 0, scope: str = "alerts"
    ) -> str:
        """暂停或恢复本会话的余额提醒（预警推送和/或每日播报），或查看静音状态。

        用户说「别再提醒了」「静音 24 小时」「烦死了别报了」→ scope=all 或默认；
        只嫌早上播报吵 → scope=daily。静音不需要验证码（只影响提醒、随时可逆），
        但写操作仅限私聊。

        Args:
            hours(number): 大于 0 = 静音 N 小时（上限 168）；0 = 只查当前状态；-1 = 解除静音
            scope(string): 作用范围："alerts"=只静音余额预警（默认）；"daily"=只静音每日播报；"all"=两者都静音
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        umo = event.unified_msg_origin
        s = str(scope or "alerts").strip().lower()
        scope_map = {"alerts": "alerts", "预警": "alerts", "alert": "alerts",
                     "daily": "daily", "播报": "daily",
                     "all": "all", "全部": "all", "都": "all"}
        s = scope_map.get(s, "")
        if not s:
            return "scope 只支持 alerts（预警）/ daily（每日播报）/ all（全部）。"
        write = hours > 0 or hours < 0
        if write and not event.is_private_chat():
            return GROUP_WRITE_DENIED

        async def _apply_alerts() -> str:
            if hours > 0:
                return self._mute_set(umo, hours)
            if hours < 0:
                return self._mute_clear(umo)
            when = self._mute_until_text(umo)
            if when:
                return f"🔕 预警静音中，至 {when}。期间查询、绑定、每日播报照常。"
            return "🔔 预警未静音，余额低于预警线会照常推送。"

        async def _apply_daily() -> str:
            binding = self.store.get_binding(umo) if self.store else None
            if not binding:
                return "本会话还没有绑定宿舍，没有每日播报可静音。"
            until = self._daily_muted_until(binding)
            tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
            if hours > 0:
                span = max(1.0, min(float(hours), float(ALERT_MUTE_MAX_HOURS)))
                binding["daily_muted_until"] = time.time() + span * 3600
                self.store.save()
                self._record_event(umo, "info", f"每日播报静音 {span:g} 小时")
                when = datetime.fromtimestamp(binding["daily_muted_until"], tz)
                return f"🔕 已静音每日播报 {span:g} 小时（至 {when:%m-%d %H:%M}），预警照常。"
            if hours < 0:
                binding.pop("daily_muted_until", None)
                self.store.save()
                self._record_event(umo, "info", "恢复每日播报")
                return "🔔 已恢复每日播报。"
            if until > time.time():
                when = datetime.fromtimestamp(until, tz).strftime("%m-%d %H:%M")
                return f"🔕 每日播报静音中，至 {when}。"
            return "🔔 每日播报正常。"

        if s == "alerts":
            return await _apply_alerts()
        if s == "daily":
            return await _apply_daily()
        parts = [await _apply_alerts(), await _apply_daily()]
        return "\n".join(parts)

    @filter.llm_tool(name="dorm_electric_set_alert_threshold")
    async def tool_dorm_electric_set_alert_threshold(
        self,
        event: AstrMessageEvent,
        warn: float = 0,
        critical: float = 0,
        kind: str = "all",
    ) -> str:
        """设置本会话的余额预警线（只影响这个会话的提醒，不改全局配置、不影响其他会话）。

        用户说「低于 20 就提醒我」「空调费低于 5 立刻告诉我」这类话时调用。
        空调费按度计、宿舍电费按元计，消耗速度不同，可以只给其中一个设置（kind 传 ac 或 elec）。
        写操作仅限私聊。预警线只对该会话已绑定的房间生效，未绑定无法设置。

        Args:
            warn(number): 预警线：余额低于它时发预警提醒；0 = 只看当前生效值；-1 = 恢复全局默认
            critical(number): 紧急线（低于它时发紧急提醒），可不传，默认取预警线的一半
            kind(string): 作用费种："ac"=只设空调费；"elec"=只设宿舍电费；"all"=两者都设（默认）；「空调」「电」这类中文也可以
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        umo = event.unified_msg_origin
        k = THRESHOLD_KIND_MAP.get(str(kind or "all").strip().lower(), "")
        if not k:
            return "kind 只支持 ac（空调费）/ elec（宿舍电费）/ all（两者）。"
        binding = self.store.get_binding(umo) if self.store else None
        if warn == 0:
            if binding and binding.get("thresholds"):
                custom = "；".join(self._threshold_lines(binding))
                return (
                    f"本会话预警线（自定义）：{custom}。"
                    f"全局默认：{'；'.join(self._threshold_lines(None))}。"
                )
            return f"当前预警线（全局默认）：{'；'.join(self._threshold_lines(None))}。"
        if not event.is_private_chat():
            return GROUP_WRITE_DENIED
        if not binding:
            return "本会话还没有绑定宿舍，无法单独设置预警线；先帮用户完成绑定。"
        if warn < 0:
            binding.pop("thresholds", None)
            self.store.save()
            gw, gc = self._effective_thresholds(None)
            self._record_event(umo, "info", "恢复全局预警线")
            return (
                f"已恢复全局默认预警线：预警 {gw:g} / 紧急 {gc:g}"
                "（两费种回到全局配置）。"
            )
        try:
            w = float(warn)
        except (TypeError, ValueError):
            return "预警线需要是一个数字（比如 20）。"
        if w <= 0:
            return "预警线需要是一个正数（比如 20）。"
        try:
            c = float(critical)
        except (TypeError, ValueError):
            c = 0.0
        if c <= 0 or c > w:
            c = w / 2
        kinds = ("ac", "elec") if k == "all" else (k,)
        for one in kinds:
            self._set_session_threshold(binding, one, w, c)
        self.store.save()
        scope = "空调费和宿舍电费" if k == "all" else self._fee_name(k)
        unit = "，单位分别为度、元" if k == "all" else (
            "，单位度" if k == "ac" else "，单位元"
        )
        self._record_event(
            umo, "info",
            f"自定义预警线 {scope} {w:g}/{c:g}",
        )
        return (
            f"已设置本会话预警线（{scope}{unit}）：余额 ≤ {w:g} 时提醒、"
            f"≤ {c:g} 时紧急提醒。下一个轮询周期（约 20 分钟内）开始按新线判断；"
            "说「恢复默认预警线」可还原。"
        )

    def _trend_lines(self, binding: dict, days: int) -> list[str]:
        """按天余额趋势（只读历史，不写）。"""
        days = max(1, min(60, days))
        tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
        lines = [f"\n📈 最近 {days} 天每日余额："]
        for kind in ("ac", "elec"):
            history = (binding.get("history_by_fee") or {}).get(kind) or []
            if not history:
                continue
            snaps = self._daily_snapshots(history, tz, days)
            chron = list(reversed(snaps))
            cells = []
            for date, rec in chron:
                if rec is None:
                    continue
                cells.append(f"{date.strftime('%m-%d')} {float(rec['v']):.2f}")
            if cells:
                lines.append(f"  {self._fee_name(kind)}：" + " → ".join(cells))
            stats = self._history_stats(history)
            if stats:
                extra = (
                    f"，检测到充值 +{stats['recharged_24h']:.2f}"
                    if stats["recharged_24h"] > 0
                    else ""
                )
                lines.append(
                    f"  {self._fee_name(kind)}：24h 用电 {stats['usage_24h']:.2f} "
                    f"{history[-1].get('u', '度')} | 日均 {stats['per_day']:.2f} | "
                    f"最低 {stats['min']:.2f} / 最高 {stats['max']:.2f}{extra}"
                )
        if len(lines) == 1:
            lines.append("  （暂无历史数据，等轮询几轮就有了）")
        return lines

    # 房间名反查：A-8-17 / A817 / 春雪楼2 8层 A817
    HINT_ROOM_RE = re.compile(r"([A-Za-z]+)[-_ ]?(\d+)(?:[-_ ]?(\d+))?")
    HINT_FLOOR_RE = re.compile(r"(\d+)\s*层")
    HINT_DIGITS_RE = re.compile(r"\d{3,4}")
    LOOKUP_BUILDING_BUDGET = 12
    LOOKUP_ROOM_BUDGET = 12
    LAST_ROOM_TTL = 1800  # 会话房间记忆 30 分钟

    def _remember_room(self, umo: str, params: dict) -> None:
        """定位成功后记下本会话的房间，供「绑定」「春雪」这类碎片说法复用。"""
        label = "/".join(
            str(params[scope].get(key, ""))
            for scope, key in (
                ("area", "areaname"),
                ("building", "building"),
                ("floor", "floor"),
                ("room", "room"),
            )
        )
        self._last_room[umo] = {"params": params, "label": label, "at": time.time()}

    def _last_room_line(self, umo: str) -> str:
        """未解析出房间时的追加提示：告诉 AI 本会话最近定位过哪个房间、怎么重调。"""
        last = self._last_room.get(umo)
        if not last or time.time() - float(last.get("at", 0)) > self.LAST_ROOM_TTL:
            return ""
        room_name = (last.get("params", {}).get("room") or {}).get("room", "")
        if not room_name:
            return ""
        minutes = max(1, int((time.time() - float(last["at"])) // 60))
        return (
            f"\n\n本会话 {minutes} 分钟前定位过 {last['label']}。"
            f"如果用户指的就是它（比如用户刚说「绑定」「查电费」），"
            f"直接用 hint 传「{room_name}」重新调用本工具，不要反问。"
        )

    @classmethod
    def _parse_room_hint(cls, hint: str) -> tuple[str, str]:
        """从口语里抽出房间 token（A817 / 817）与楼层号（8）。

        「A-8-17」自带楼层段；「A817」这类紧凑写法再从「8层」里捞楼层；
        「春雪楼817」「817」这类没有字母的说法走纯数字回退，首位数字当楼层。
        全角数字先 NFKC 归一。都没有就留空，由 _resolve_room 按受限的逐层搜索去找。
        """
        text = unicodedata.normalize("NFKC", str(hint or ""))
        m = cls.HINT_ROOM_RE.search(text)
        if not m:
            digits = cls.HINT_DIGITS_RE.search(text)
            if not digits:
                return "", ""
            token = digits.group(0)
            first = token[0]
            return token, ("" if first == "0" else first)
        letters, second, third = m.groups()
        token = f"{letters}{second}{third or ''}"
        if third:
            return token, second.lstrip("0")
        floor = cls.HINT_FLOOR_RE.search(text)
        if floor:
            return token, floor.group(1).lstrip("0")
        return token, ""


    async def _resolve_room(self, umo: str, hint: str) -> tuple[dict | None, str | None]:
        """按房间名反查学校侧的房间参数。返回 (ac_params, err)。

        搜索有请求预算上限（学校接口每层一次请求，全校扫一遍要几十次）：
        给了楼层就只在匹配楼层找；没给楼层就按「先每栋楼第一层、再每栋楼第二层」
        的顺序轮转，命中不了就反问用户补楼栋和楼层。
        """
        token, floor_no = self._parse_room_hint(hint)
        if not token:
            return None, (
                "没认出房间号。可以说「春雪楼817」「817」「A817」「A-8-17」或"
                "「春雪楼2 8层 A817」这类格式。" + self._last_room_line(umo)
            )
        items = self.config.get("fee_items", {}) or {}
        aids = list(items)
        if not aids:
            return None, "配置中没有任何缴费项目（fee_items）。"
        aid = aids[0]
        try:
            areas = await self.hjnu.list_areas(aid)
            area = next(
                (a for a in areas if a.get("areaname") == "校本部"),
                (areas or [None])[0],
            )
            if not area:
                return None, "学校没有返回校区列表。"
            buildings = await self.hjnu.list_buildings(aid, area)
        except SessionExpiredError:
            return None, CREDENTIAL_HINT
        except QueryError as e:
            return None, f"❌ {e}"
        if not buildings:
            return None, "学校没有返回楼栋列表。"
        text = str(hint)
        order: list[dict] = []
        # 优先级：提示里点名的楼栋 > 本会话已绑定的楼栋 > 其余
        named = next(
            (b for b in buildings if b.get("building") and b["building"] in text), None
        )
        if not named:
            # 「春雪楼817」点不出完整楼名「春雪楼2」：楼栋名去掉数字后再匹配一次
            named = next(
                (
                    b
                    for b in buildings
                    if b.get("building") and re.sub(r"\d", "", str(b["building"])) in text
                ),
                None,
            )
        if named:
            order.append(named)
        bound = self.store.get_binding(umo) if self.store else None
        bound_name = (bound or {}).get("params", {}).get("building", {}).get("building")
        if bound_name:
            same = next((b for b in buildings if b.get("building") == bound_name), None)
            if same and same not in order:
                order.append(same)
        order.extend(b for b in buildings if b not in order)
        order = order[: self.LOOKUP_BUILDING_BUDGET]

        async def floors_of(building: dict) -> list[dict]:
            try:
                return await self.hjnu.list_floors(aid, area, building)
            except SessionExpiredError:
                raise
            except QueryError:
                return []

        try:
            floors_by_building = list(
                zip(order, await asyncio.gather(*(floors_of(b) for b in order)), strict=False)
            )
        except SessionExpiredError:
            # 扫描途中会话过期：报「凭证过期」而不是「没找到房间」，避免误导
            return None, CREDENTIAL_HINT
        pairs: list[tuple[dict, dict]] = []
        # 「A817」这类 token 不带楼层：从去字母后的首个非零数字猜楼层，猜中的排最前
        # （软优先，只影响扫描顺序不影响正确性；猜错就按原轮转顺序兜底）。
        guess_floor = ""
        if not floor_no:
            digits_part = re.sub(r"^[A-Za-z]+", "", token)
            g = re.search(r"[1-9]", digits_part)
            guess_floor = g.group(0) if g else ""
        if floor_no:
            for building, floors in floors_by_building:
                floor = next(
                    (
                        f
                        for f in floors
                        if str(f.get("floor", "")).replace("层", "").strip() == floor_no
                    ),
                    None,
                )
                if floor:
                    pairs.append((building, floor))
        else:
            picked: set[tuple[str, str]] = set()

            def pick(b: dict, f: dict) -> None:
                key = (str(b.get("building")), str(f.get("floor")))
                if key not in picked and len(pairs) < self.LOOKUP_ROOM_BUDGET:
                    picked.add(key)
                    pairs.append((b, f))

            if guess_floor:
                for building, floors in floors_by_building:
                    floor = next(
                        (
                            f
                            for f in floors
                            if str(f.get("floor", "")).replace("层", "").strip()
                            == guess_floor
                        ),
                        None,
                    )
                    if floor:
                        pick(building, floor)
            depth = 0
            while len(pairs) < self.LOOKUP_ROOM_BUDGET:
                added = False
                for building, floors in floors_by_building:
                    if len(floors) > depth:
                        pick(building, floors[depth])
                        added = True
                        if len(pairs) >= self.LOOKUP_ROOM_BUDGET:
                            break
                if not added:
                    break
                depth += 1
        pairs = pairs[: self.LOOKUP_ROOM_BUDGET]
        if not pairs:
            if floor_no:
                scope = f"{named['building']} " if named else "学校那边"
                return None, (
                    f"{scope}没有 {floor_no} 层（关键词 {token}）。"
                    "请让用户确认楼栋和楼层，例如「春雪楼2 8层 A817」。"
                )
            return None, (
                f"在学校里没找到关键词 {token} 对应的房间。请让用户补全楼栋和楼层，"
                "例如「春雪楼2 8层 A817」。"
            )


        async def rooms_of(pair: tuple[dict, dict]) -> tuple[dict, dict, list[dict]]:
            building, floor = pair
            try:
                return building, floor, await self.hjnu.list_rooms(
                    aid, area, building, floor
                )
            except SessionExpiredError:
                raise
            except QueryError:
                return building, floor, []

        try:
            results = await asyncio.gather(*(rooms_of(p) for p in pairs))
        except SessionExpiredError:
            return None, CREDENTIAL_HINT

        def norm_name(room: dict) -> str:
            return re.sub(r"[^A-Za-z0-9]", "", str(room.get("room", ""))).lower()

        def strict_match(t: str) -> list[tuple[dict, dict, dict]]:
            needle = re.sub(r"[^A-Za-z0-9]", "", str(t)).lower()
            out: list[tuple[dict, dict, dict]] = []
            for building, floor, rooms in results:
                for room in rooms:
                    if needle and needle in norm_name(room):
                        out.append((building, floor, room))
                        break
            return out

        hits = strict_match(token)
        if not hits:
            # 「A-08-17」这类前导零写法：去掉数字段里的前导零再试一次
            alt = re.sub(r"(^|[^0-9])0+(\d)", r"\1\2", token)
            if alt != token:
                hits = strict_match(alt)
                if hits:
                    token = alt
        if not hits:
            scanned: list[tuple[str, dict, dict, dict]] = []
            for building, floor, rooms in results:
                for room in rooms:
                    scanned.append((norm_name(room), building, floor, room))
            return None, self._fuzzy_miss_text(token, scanned)
        if len(hits) > 1:
            options = "、".join(
                f"{b.get('building')}/{f.get('floor')}/{r.get('room')}"
                for b, f, r in hits[:8]
            )
            return None, f"找到多个匹配的房间：{options}。请反问用户是哪一个。"
        building, floor, room = hits[0]
        params = {
            "aid": aid,
            "area": area,
            "building": building,
            "floor": floor,
            "room": room,
        }
        self._remember_room(umo, params)
        return params, None

    @staticmethod
    def _fuzzy_miss_text(token: str, scanned: list[tuple[str, dict, dict, dict]]) -> str:
        """严格匹配落空时的回复：有近似房间就列出来让 AI 反问，没有才回格式引导。

        只在本次已扫描到的房间里算相似度（difflib，零额外请求）；永远不直接
        采用猜测结果——候选交给用户确认后，AI 必须用完整房间名重新调用。
        """
        if scanned:
            by_name: dict[str, tuple[dict, dict, dict]] = {}
            for name, building, floor, room in scanned:
                by_name.setdefault(name, (building, floor, room))
            close = difflib.get_close_matches(
                re.sub(r"[^A-Za-z0-9]", "", str(token)).lower(),
                list(by_name),
                n=5,
                cutoff=0.6,
            )
            if close:
                options = "、".join(
                    f"{by_name[n][0].get('building')}/"
                    f"{by_name[n][1].get('floor')}/{by_name[n][2].get('room')}"
                    for n in close
                )
                return (
                    f"学校里没有完全叫「{token}」的房间。最接近的是：{options}。"
                    "请反问用户是哪一个，确认后用完整房间名重新调用。"
                )
        return f"没找到房间号包含 {token} 的房间，请让用户确认一下房间号。"


    async def _query_room_balance(self, umo: str, hint: str) -> str:
        params, err = await self._resolve_room(umo, hint)
        if err:
            return err
        assert params
        label = (
            f"{params['area'].get('areaname')}/{params['building'].get('building')}/"
            f"{params['floor'].get('floor')}/{params['room'].get('room')}"
        )
        fees = {"ac": self._fee_entry(params)}
        elec = await self._match_elec_fee(params)
        if elec:
            fees["elec"] = elec
        # 只读：构造临时 binding 走 _fetch_fees，不落库、不写历史
        results = await self._fetch_fees(
            {"provider": "hjnu", "room_label": label, "params": params, "fees": fees}
        )
        self._remember_raw(umo, results)
        lines = [f"⚡ {label}"]
        lines.extend(self._format_fee_results(results, include_missing=True))
        if "elec" not in fees:
            lines.append("（该房间没有对应的宿舍电费项目，只查到空调费）")
        return "\n".join(lines)

    @filter.llm_tool(name="dorm_electric_query_room")
    async def tool_dorm_electric_query_room(
        self, event: AstrMessageEvent, room_hint: str = ""
    ) -> str:
        """按房间名/编号查任意宿舍的当前电费余额，不需要绑定。

        用户想查「某间宿舍」的电费时（无论本会话是否已绑定），把他说的话直接传进来，
        例如「春雪楼817 电费多少」「帮我看看 5-302」。用户只说了模糊片段（如「春雪」）时，
        工具会提示本会话最近定位过的房间，照它给的 hint 重调即可；楼栋名以工具返回为准，
        不要臆造。

        Args:
            room_hint(string): 用户的原话直接传，例如 春雪楼817、817、A-8-17、A817、春雪楼2 8层 A817
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        umo = event.unified_msg_origin
        # 缓存键用归一化的用户原话，而不是解析出的房间 token：不同楼栋的同号
        # 房间（春雪楼A817 / 清美楼A817）token 都是 A817，按 token 建键会让
        # 60 秒内的第二个查询命中另一栋楼的余额。按原话建键，同房间换个说法
        # 只是多查一次，不会错房。
        cache_key = f"{umo}|{str(room_hint or '').strip().lower()}"
        ttl = max(0, self._cfg_int("ai_lookup_cache_seconds", 60))
        cached = self._lookup_cache.get(cache_key)
        if cached and ttl and time.time() - cached[0] < ttl:
            return f"{cached[1]}\n（{int(time.time() - cached[0])} 秒前的结果，缓存命中）"
        text = await self._query_room_balance(umo, room_hint)
        if ttl and "⚡" in text:
            if len(self._lookup_cache) >= 200:
                # 顺手清掉最老的一批，避免长期运行无限增长
                for key, _ in sorted(
                    self._lookup_cache.items(), key=lambda kv: kv[1][0]
                )[:50]:
                    self._lookup_cache.pop(key, None)
            self._lookup_cache[cache_key] = (time.time(), text)
        return text


    # ================= WebUI 仪表盘（只读） =================
    # AstrBot 插件页面机制：context.register_web_api 注册带 WebUI 登录鉴权的
    # REST 端点；pages/dashboard/ 下的前端经 window.AstrBotPluginPage Bridge
    # 调用（apiGet("dashboard/overview") → /插件名/dashboard/overview）。
    # 首版只读：解绑/改配置仍走聊天，验证码同意链不在网页上复制。
    # 任何响应都不得包含 cookie 值（有测试断言）。

    def _register_dashboard(self):
        reg = getattr(self.context, "register_web_api", None)
        if not callable(reg):
            logger.info(f"[{PLUGIN_NAME}] 宿主不支持 register_web_api，仪表盘端点未注册")
            return
        base = f"/{PLUGIN_NAME}/dashboard"
        try:
            reg(f"{base}/overview", self._web_overview, ["GET"], "电费仪表盘概览")
            reg(f"{base}/history", self._web_history, ["GET"], "电费仪表盘历史")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 仪表盘端点注册失败：{e!r}")

    def _web_binding_item(self, umo: str, binding: dict) -> dict:
        """单个绑定的仪表盘数据（纯数据，无任何凭证字段）。"""
        fees = {}
        for kind in ("ac", "elec"):
            history = (binding.get("history_by_fee") or {}).get(kind) or []
            latest = history[-1] if history else None
            try:
                latest_value = float(latest["v"]) if latest else None
            except (TypeError, ValueError, KeyError):
                latest_value = None
            stats = self._history_stats(history)
            warn, critical = self._effective_thresholds(binding, kind)
            fees[kind] = {
                "name": self._fee_name(kind),
                "unit": (latest or {}).get("u", "度" if kind == "ac" else "元"),
                "latest_value": latest_value,
                "latest_time": float(latest["t"]) if latest else None,
                "warn": warn,
                "critical": critical,
                "custom": bool(
                    isinstance(binding.get("thresholds"), dict)
                    and isinstance(binding["thresholds"].get(kind), dict)
                ),
                "per_day": stats["per_day"] if stats else 0.0,
                "recharged_24h": stats["recharged_24h"] if stats else 0.0,
                "points": len(history),
            }
        return {
            "umo": umo,
            "label": self._binding_label(binding),
            "fees": fees,
            "alert_muted_until": float(self._alert_muted.get(umo, 0) or 0),
            "daily_muted_until": self._daily_muted_until(binding),
        }

    async def _web_overview(self):
        """仪表盘概览：全部绑定 + 全局状态。"""
        try:
            bindings = (self.store.data.get("bindings", {}) if self.store else {}) or {}
            cookie = str(self.config.get("hjnu_cookie", "") or "")
            return {
                "success": True,
                "data": {
                    "bindings": [
                        self._web_binding_item(umo, b) for umo, b in bindings.items()
                    ],
                    "cookie_ok": bool(cookie.strip()),
                    "poll_interval_minutes": self._cfg_int("poll_interval_minutes", 20),
                    "daily_time": str(self._cfg("daily_time", "08:00")),
                    "daily_report": self._cfg_bool("daily_report", True),
                    "server_time": time.time(),
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def _web_history(self):
        """仪表盘历史：指定会话两个费种的日末快照序列（days 上限 60）。"""
        try:
            try:
                from quart import request
            except ImportError:
                return {"success": False, "message": "宿主 Web 框架不可用"}
            umo = str(request.args.get("umo", ""))
            days = max(1, min(60, int(request.args.get("days", 14))))
            binding = self.store.get_binding(umo) if self.store else None
            if not binding:
                return {"success": False, "message": "会话不存在或未绑定"}
            tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
            series = {}
            for kind in ("ac", "elec"):
                history = (binding.get("history_by_fee") or {}).get(kind) or []
                snaps = self._daily_snapshots(history, tz, days)
                series[kind] = [
                    {
                        "date": str(d),
                        "value": (float(rec["v"]) if rec else None),
                    }
                    for d, rec in snaps
                ]
            return {
                "success": True,
                "data": {
                    "label": self._binding_label(binding),
                    "days": days,
                    "series": series,
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ================= 指令：帮助与状态 =================

    @electric.command("帮助", alias={"help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看指令帮助"""
        warn = self._cfg_float("threshold_warn", 10)
        critical = self._cfg_float("threshold_critical", 5)
        yield event.plain_result(
            "⚡ 宿舍电费监控指令：\n"
            "日常直接跟机器人聊天就行（问余额、报房间号查电费、让他帮你绑定），"
            "下面这些是给 AI 兜底和进阶用的：\n"
            "/电费 绑定 — 启动绑定宿舍向导（自动同时关联空调费 + 宿舍电费，仅私聊）\n"
            "/电费 校区/楼栋/楼层 <编号> — 逐级选择宿舍\n"
            "/电费 房间 — 浏览房间列表（无参翻页，p<页码> 跳页）\n"
            "/电费 房间 <编号> — 按全楼层绝对编号选择房间\n"
            "/电费 绑定 1 — 确认绑定\n"
            "/电费 确认 [验证码] — 提交 AI 给的确认码（不带参数则查看待确认项）\n"
            "/电费 静音 [小时] — 暂停余额预警（0 或 取消=恢复；仅私聊）\n"
            "/电费 预警线 [n] — 本会话自定义预警线（取消=恢复全局；仅私聊）\n"
            "/电费 解绑 — 取消监控（仅私聊）\n"
            "/电费 查询 — 同时查询空调费和宿舍电费\n"
            "/电费 凭证 <JSESSIONID=...> — 更新会话凭证（仅限私聊，热更新）\n"
            "/电费 历史 [n] — 查看最近 n 天每日余额（默认 7 天，最多 60 天）\n"
            "/电费 日志 [n] — 查看最近 n 条事件 + 最近一次原始返回（默认 20，最多 100）\n"
            "/电费 状态 — 查看绑定与运行状态\n"
            "/电费 检查 — 自检：凭证是否生效 + 绑定是否正确 + 余额能否查到\n"
            f"预警线：{warn:g}；紧急线：{critical:g}（空调费单位为度，宿舍电费单位为元）\n"
            "群里只能查询余额，报房间号即可；绑定/解绑请私聊"
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

    # ================= 指令：自检 =================

    @staticmethod
    def _credential_state(results: dict) -> str:
        """由一次真实查询的结果判定凭证状态（供 /电费 检查 与 /电费 状态 复用）。

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

    async def _check_report(self, umo: str) -> str:
        """自检报告全文：凭证三态 + 绑定摘要 + 实时查询 + 本会话事件尾部。

        纯函数：cmd_check 指令与 dorm_electric_check 工具共用，不要在它上面
        挂 @electric.command——指令注册必须落在会 yield 消息的 cmd_* 上。
        """
        binding = self.store.get_binding(umo) if self.store else None
        label = self._binding_label(binding) if binding else ""

        if not str(self.config.get("hjnu_cookie", "") or "").strip():
            bound_line = (
                f"绑定：✅ {label}" if binding else "绑定：❌ 未绑定（/电费 绑定 开始）"
            )
            return (
                "🔎 电费自检\n"
                "凭证：❌ 未配置（私聊发送 /电费 凭证 JSESSIONID=xxxx）\n"
                f"{bound_line}"
            )

        if not binding:
            return (
                "🔎 电费自检\n"
                "凭证：✅ 已配置（尚未验证，绑定后可验证）\n"
                "绑定：❌ 未绑定（/电费 绑定 开始）"
            )

        results = await self._fetch_fees(binding)
        self._remember_raw(umo, results)
        state = self._credential_state(results)
        fees = self._fee_bindings(binding)
        lines = [
            f"🔎 电费自检 {label}",
            f"凭证：{state}",
            "绑定：✅ "
            + label
            + "（已关联："
            + "、".join(self._fee_name(kind) for kind in fees)
            + "）",
        ]
        mute_line = self._mute_line(umo)
        if mute_line:
            lines.append(mute_line)
        lines.append("实时查询：")
        lines.extend(
            "  " + line
            for line in self._format_fee_results(results, include_missing=True)
        )
        events = [ev for ev in self._events if ev.get("umo") == umo][-5:]
        lines.append("最近事件（新→旧）：")
        if not events:
            lines.append("  （暂无事件）")
        else:
            tz = self._resolve_tz(self._cfg("daily_timezone", "Asia/Shanghai"))
            for ev in reversed(events):
                when = datetime.fromtimestamp(float(ev.get("t", 0)), tz).strftime(
                    "%H:%M:%S"
                )
                lines.append(
                    f"  {when}  [{ev.get('kind', '?')}] {ev.get('text', '')}"
                )
        self._record_event(umo, "info", f"自检：{state.split('（')[0]}")
        return "\n".join(lines)

    @filter.llm_tool(name="dorm_electric_check")
    async def tool_dorm_electric_check(self, event: AstrMessageEvent) -> str:
        """一次性自检：凭证状态 + 绑定摘要 + 两个费种实时查询 + 本会话最近事件。只读，群里也能用。

        用户说「凭证还有效吗」「怎么没提醒我」「电费是不是查不了/挂了」「检查一下电费查询」
        这类话时调用。
        凭证（JSESSIONID）永远不要向用户索要、不要转述或保存：用户自己贴出来时，
        让他私下发送 /电费 凭证 JSESSIONID=… 更新。
        """
        if not self._ai_enabled():
            return "电费 AI 工具已被插件配置关闭。"
        return await self._check_report(event.unified_msg_origin)

    @electric.command("检查", alias={"自检"})
    async def cmd_check(self, event: AstrMessageEvent):
        """一次性自检：凭证是否生效、绑定是否正确、余额能否查到"""
        yield event.plain_result(await self._check_report(event.unified_msg_origin))

    # ================= 指令：绑定向导 =================

    @electric.command("校区")
    async def cmd_area(self, event: AstrMessageEvent, area_id: str | None = None):
        """选择缴费项目的校区"""
        umo = event.unified_msg_origin
        if not (self.config.get("fee_items", {}) or {}):
            yield event.plain_result("配置中没有任何缴费项目（fee_items）。")
            return
        if area_id is None:
            yield event.plain_result("用法：/电费 校区 <编号>（先 /电费 绑定 启动向导）")
            return
        _, err = await self._select_area(umo, area_id)
        yield event.plain_result(err or self._render_buildings(umo))

    @electric.command("楼栋")
    async def cmd_building(self, event: AstrMessageEvent, building_id: str | None = None):
        """选择楼栋"""
        umo = event.unified_msg_origin
        if building_id is None or not (self._wizard_state(umo).get("buildings") or []):
            yield event.plain_result("用法：/电费 楼栋 <编号>（先 /电费 校区）")
            return
        _, err = await self._select_building(umo, building_id)
        yield event.plain_result(err or self._render_floors(umo))

    @electric.command("楼层")
    async def cmd_floor(self, event: AstrMessageEvent, floor_id: str | None = None):
        """选择楼层"""
        umo = event.unified_msg_origin
        if floor_id is None or not (self._wizard_state(umo).get("floors") or []):
            yield event.plain_result("用法：/电费 楼层 <编号>（先 /电费 楼栋）")
            return
        _, err = await self._select_floor(umo, floor_id)
        yield event.plain_result(err or self._render_floor_rooms(umo))

    @electric.command("房间")
    async def cmd_room(self, event: AstrMessageEvent, room_no: str | None = None):
        """浏览房间列表（无参翻页、p<页码>跳页）或按绝对编号选择房间"""
        yield event.plain_result(
            self._room_listing(event.unified_msg_origin, room_no)
        )

    @electric.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent, confirm: str | None = None):
        """启动绑定宿舍向导（无参）或确认绑定（带参 1）。"""
        if not event.is_private_chat():
            yield event.plain_result(GROUP_WRITE_DENIED)
            return
        umo = event.unified_msg_origin
        if not (self.config.get("fee_items", {}) or {}):
            yield event.plain_result("配置中没有任何缴费项目（fee_items）。")
            return
        if confirm is None:
            _, err = await self._select_start(umo)
            yield event.plain_result(err or self._render_areas(umo))
            return
        if str(confirm) != "1":
            yield event.plain_result(
                "请先 /电费 绑定 启动向导，选好房间后再 /电费 绑定 1 确认"
            )
            return
        yield event.plain_result(await self._complete_bind(umo))

    @electric.command("确认")
    async def cmd_confirm(self, event: AstrMessageEvent, code: str | None = None):
        """提交 AI 给的确认验证码（带参=执行，不带参=看待确认项）。"""
        if not event.is_private_chat():
            yield event.plain_result(
                "🔒 验证码只对发起它的私聊会话有效，请在私聊里发送 /电费 确认。"
            )
            return
        umo = event.unified_msg_origin
        if not code:
            yield event.plain_result(self._pending_view(umo))
            return
        # 用户亲手敲下这条指令本身就是同意
        token = self._bind_tokens.get(umo)
        if token and str(code).strip() == token.get("code"):
            token["user_ok"] = True
        yield event.plain_result(await self._run_token(umo, str(code).strip()))

    @electric.command("静音")
    async def cmd_mute(self, event: AstrMessageEvent, arg: str | None = None):
        """暂停/恢复余额预警推送（仅私聊）：无参看状态，0/取消=恢复，N=静音 N 小时"""
        if not event.is_private_chat():
            yield event.plain_result(GROUP_WRITE_DENIED)
            return
        umo = event.unified_msg_origin
        if arg is None:
            when = self._mute_until_text(umo)
            yield event.plain_result(
                f"🔕 预警静音中，至 {when}。\n用法：/电费 静音 <小时>（0 或 取消=恢复）"
                if when
                else "🔔 未静音。\n用法：/电费 静音 <小时>（0 或 取消=恢复，上限 168）"
            )
            return
        s = str(arg).strip()
        if s in {"0", "取消", "解除"}:
            yield event.plain_result(self._mute_clear(umo))
            return
        try:
            hours = float(s)
        except ValueError:
            yield event.plain_result(
                "用法：/电费 静音 <小时>（0 或 取消=恢复，上限 168）"
            )
            return
        yield event.plain_result(self._mute_set(umo, hours))

    @electric.command("预警线")
    async def cmd_threshold(self, event: AstrMessageEvent, arg: str | None = None):
        """查看/设置本会话预警线（仅私聊）：无参看当前，取消=恢复全局，数字=设置"""
        if not event.is_private_chat():
            yield event.plain_result(GROUP_WRITE_DENIED)
            return
        umo = event.unified_msg_origin
        binding = self.store.get_binding(umo) if self.store else None
        usage = (
            "用法：/电费 预警线 <预警线>（空调费和宿舍电费同时设，紧急线自动取一半）\n"
            "只设一种：/电费 预警线 空调 <预警线> 或 /电费 预警线 电 <预警线>"
            "（空调费单位度、宿舍电费单位元；取消=恢复全局）"
        )
        if arg is None:
            custom = "（自定义）" if binding and binding.get("thresholds") else "（全局默认）"
            yield event.plain_result(
                f"当前预警线{custom}：\n  "
                + "\n  ".join(self._threshold_lines(binding))
                + "\n全局默认：\n  " + "\n  ".join(self._threshold_lines(None))
                + f"\n{usage}"
            )
            return
        s = str(arg).strip()
        if s in {"取消", "恢复", "解除"}:
            if not binding:
                yield event.plain_result("当前会话没有绑定。")
                return
            binding.pop("thresholds", None)
            self.store.save()
            gw, gc = self._effective_thresholds(None)
            self._record_event(umo, "info", "恢复全局预警线")
            yield event.plain_result(f"已恢复全局默认预警线：预警 {gw:g} / 紧急 {gc:g}。")
            return
        # 单费种前缀：「空调 20」「电 20」「ac 20」「elec 20」（别名表与 AI 工具共用）
        kind = "all"
        parts = s.split()
        if len(parts) == 2 and THRESHOLD_KIND_MAP.get(parts[0].lower()):
            kind, s = THRESHOLD_KIND_MAP[parts[0].lower()], parts[1]
        try:
            w = float(s)
        except ValueError:
            yield event.plain_result(usage)
            return
        if w <= 0:
            yield event.plain_result("预警线需要是一个正数（比如 20）。")
            return
        if not binding:
            yield event.plain_result("本会话还没有绑定宿舍，先 /电费 绑定。")
            return
        c = w / 2
        kinds = ("ac", "elec") if kind == "all" else (kind,)
        for one in kinds:
            self._set_session_threshold(binding, one, w, c)
        self.store.save()
        scope = "空调费和宿舍电费" if kind == "all" else self._fee_name(kind)
        self._record_event(umo, "info", f"自定义预警线 {scope} {w:g}/{c:g}")
        yield event.plain_result(
            f"已设置本会话预警线（{scope}）：余额 ≤ {w:g} 时提醒、≤ {c:g} 时紧急提醒。\n"
            "下一个轮询周期开始按新线判断；/电费 预警线 取消 可还原。"
        )

    @electric.command("解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """取消本会话的电费监控"""
        if not event.is_private_chat():
            yield event.plain_result(GROUP_WRITE_DENIED)
            return
        yield event.plain_result(self._unbind(event.unified_msg_origin))


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
