"""AI 对话工具与确认验证码机制的测试。

三组断言：
1. 验证码矩阵：没被用户亲口确认过绝不执行（模型无法伪造）
2. 群聊只读：绑定/解绑/确认全部拒绝，浏览与查询照常
3. 渲染断言：工具返回文本要真的能让 AI 照着做（v1.0.7/v1.0.8 的教训）

所有工具输出都断言不含 cookie 值——它们会被送进 LLM，等于公开。
"""

import asyncio
import time
from collections import deque

import pytest
from astrbot_plugin_dorm_electric.main import (
    CODE_LENGTH,
    CREDENTIAL_HINT,
    GROUP_WRITE_DENIED,
    DormElectricPlugin,
)
from astrbot_plugin_dorm_electric.providers.base import BalanceResult

UMO = "qq:private:1"
GROUP_UMO = "qq:group:1"
AC_AID = "0030000000004301"
ELEC_AID = "0030000000014501"
FEE_ITEMS = {AC_AID: "清美时雨春雪空调（校本部）", ELEC_AID: "宿舍电费（校本部）"}
LABEL = "校本部/春雪楼2/8层"

AREAS = [{"area": "1", "areaname": "1#"}, {"area": "1", "areaname": "校本部"}]
BUILDINGS = [
    {"building": "春雪楼2", "buildingid": 3},
    {"building": "春雪楼1", "buildingid": 2},
]
FLOORS = [{"floor": "7层", "floorid": 7}, {"floor": "8层", "floorid": 8}]
# 春雪楼2 的房间（8 层末位含 17，模拟主人自己的 A-8-17）；春雪楼1 是 B 字头
ROOMS_CS2_8 = [
    {"room": f"A-8-{i:02d}", "roomid": 640 + i} for i in (1, 2, 3, 4, 5)
] + [{"room": "A-8-17", "roomid": 646}]
ROOMS_CS2_7 = [{"room": f"A-7-{i:02d}", "roomid": 540 + i} for i in (1, 2, 3)]
ROOMS_CS1 = [{"room": f"B-7-{i:02d}", "roomid": 740 + i} for i in (1, 2, 3)]
DEFAULT_ROOMS = {
    ("春雪楼2", "8层"): ROOMS_CS2_8,
    ("春雪楼2", "7层"): ROOMS_CS2_7,
    ("春雪楼1", "7层"): ROOMS_CS1,
}


def _ok(value, unit="度"):
    return BalanceResult(ok=True, value=value, raw="ok", unit=unit)


class _FakeEvent:
    def __init__(self, umo=UMO, private=True, message=""):
        self.unified_msg_origin = umo
        self._private = private
        self.message_str = message
        self.sent: list[str] = []

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return text

    async def send(self, chain):
        self.sent.append(chain.get_plain_text())


class _FakeProvider:
    """rooms 传 dict 则按楼栋给不同房间；传 list 则所有楼栋同一份；None 用默认。"""

    def __init__(self, ac=94.66, elec=12.3, rooms=None):
        self._ac = ac
        self._elec = elec
        self._rooms = rooms
        self.calls: list[str] = []

    async def list_areas(self, aid):
        self.calls.append("areas")
        return AREAS

    async def list_buildings(self, aid, area):
        self.calls.append("buildings")
        return BUILDINGS

    async def list_floors(self, aid, area, building):
        self.calls.append(f"floors:{building['building']}")
        return FLOORS

    async def list_rooms(self, aid, area, building, floor):
        self.calls.append(f"rooms:{building['building']}:{floor['floor']}")
        if isinstance(self._rooms, dict):
            return self._rooms.get(building["building"], [])
        if isinstance(self._rooms, list):
            return self._rooms
        return DEFAULT_ROOMS.get((building["building"], floor["floor"]), [])

    async def fetch(self, binding):
        if binding["params"]["aid"] == ELEC_AID:
            return _ok(self._elec, "元")
        return _ok(self._ac, "度")


class _FakeStore:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def set_binding(self, umo, binding):
        self.data["bindings"][umo] = binding

    def del_binding(self, umo):
        return self.data["bindings"].pop(umo, None) is not None

    def save(self):
        self.saved += 1

    @staticmethod
    def append_fee_history(binding, fee, value, unit, ts=None, keep_days=60):
        binding.setdefault("history_by_fee", {}).setdefault(fee, []).append(
            {"t": ts if ts is not None else time.time(), "v": value, "u": unit}
        )


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def save_config(self):
        return None


def _params(room="A-8-17"):
    return {
        "aid": AC_AID,
        "area": {"areaname": "校本部", "area": "1"},
        "building": {"building": "春雪楼2", "buildingid": 3},
        "floor": {"floor": "8层", "floorid": 8},
        "room": {"room": room, "roomid": 646},
    }


def _binding(room="A-8-17", history=False):
    ac = _params(room)
    elec = dict(ac, aid=ELEC_AID)
    binding = {
        "provider": "hjnu",
        "room_label": f"{LABEL}/{room}",
        "params": ac,
        "fees": {
            "ac": {"provider": "hjnu", "params": ac},
            "elec": {"provider": "hjnu", "params": elec},
        },
    }
    if history:
        now = time.time()
        binding["history_by_fee"] = {
            "ac": [
                {"t": now - 86400 * 2, "v": 100.0, "u": "度"},
                {"t": now - 86400, "v": 97.0, "u": "度"},
                {"t": now, "v": 94.66, "u": "度"},
            ]
        }
    return binding


def _plugin(bindings=None, ac=94.66, rooms=None, **cfg) -> DormElectricPlugin:
    plugin = DormElectricPlugin.__new__(DormElectricPlugin)
    plugin.config = _FakeConfig(
        {"fee_items": FEE_ITEMS, "hjnu_cookie": "JSESSIONID=SECRET"}, **cfg
    )
    plugin.store = _FakeStore(bindings)
    plugin.hjnu = _FakeProvider(ac=ac, rooms=rooms)
    plugin._wizard = {}
    plugin._events = deque(maxlen=200)
    plugin._last_raw = {}
    plugin._bind_tokens = {}
    plugin._lookup_cache = {}
    plugin._last_room = {}
    plugin._alert_muted = {}
    plugin._pending_alerts = {}
    return plugin


def _call(plugin, coro):
    return asyncio.run(coro)


def _at_rooms(plugin, umo=UMO, event=None):
    """把向导推到房间列表（等价于 /电费 绑定 → 校区 2 → 楼栋 1 → 楼层 2）。"""

    async def _run():
        await plugin._select_start(umo)
        await plugin._select_area(umo, 2)
        await plugin._select_building(umo, 1)
        await plugin._select_floor(umo, 2)

    _call(plugin, _run())
    return event or _FakeEvent(umo)


def _code_of(plugin, umo=UMO) -> str:
    return plugin._bind_tokens[umo]["code"]


def _user_replies(plugin, umo=UMO) -> str:
    """模拟用户亲手把验证码发回来（走真实监听器）。"""
    code = _code_of(plugin, umo)
    _call(plugin, plugin.on_user_replied_code(_FakeEvent(umo, message=code)))
    return code


# ================= 验证码：同意必须来自用户本人 =================


def test_token_code_is_six_digits():
    plugin = _plugin()
    token = plugin._issue_token(UMO, "bind", label="A-8-17")
    assert len(token["code"]) == CODE_LENGTH
    assert token["code"].isdigit()


def test_confirm_rejected_until_user_replied_the_code():
    """AI 拿着码直接 confirm（用户从没回过）必须被拒。"""
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    text = _call(plugin, plugin._run_token(UMO, _code_of(plugin)))
    assert "还没有检测到用户回复验证码" in text
    assert plugin.store.get_binding(UMO) is None


def test_listener_marks_user_ok_on_bare_code():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _user_replies(plugin)
    assert plugin._bind_tokens[UMO]["user_ok"] is True


@pytest.mark.parametrize(
    "message",
    [
        "{code}度电够吗",
        "{code} 帮我确认一下",
        "确认 {code}",
        "{code}1",
    ],
)
def test_listener_ignores_code_embedded_in_a_sentence(message):
    """群里任何嵌在句子里的码（含带「确认」关键词）都不算同意。"""
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _call(
        plugin,
        plugin.on_user_replied_code(
            _FakeEvent(
                GROUP_UMO,
                private=False,
                message=message.format(code=_code_of(plugin)),
            )
        ),
    )
    assert plugin._bind_tokens[UMO]["user_ok"] is False


@pytest.mark.parametrize("message", ["{code}", " {code} ", "{code}。", "{code}！"])
def test_listener_accepts_code_wrapped_in_punctuation(message):
    """QQ 客户端常把纯数字包进全角标点，这几种仍算「亲手回复」。"""
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _call(
        plugin,
        plugin.on_user_replied_code(
            _FakeEvent(UMO, message=message.format(code=_code_of(plugin)))
        ),
    )
    assert plugin._bind_tokens[UMO]["user_ok"] is True


def test_listener_ignores_other_session():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _call(
        plugin,
        plugin.on_user_replied_code(_FakeEvent(GROUP_UMO, message=_code_of(plugin))),
    )
    assert plugin._bind_tokens[UMO]["user_ok"] is False


def test_confirm_rejects_wrong_code():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _user_replies(plugin)
    text = _call(plugin, plugin._run_token(UMO, "000000"))
    assert "验证码不匹配" in text
    assert plugin.store.get_binding(UMO) is None


def test_new_token_invalidates_previous_code():
    plugin = _plugin()
    plugin._issue_token(UMO, "unbind", label="A-8-17")
    old = _code_of(plugin)
    plugin._issue_token(UMO, "unbind", label="A-8-17")
    assert _code_of(plugin) != old


def test_token_expires_after_ttl():
    plugin = _plugin(ai_bind_code_ttl=30)
    plugin._issue_token(UMO, "bind", label="A-8-17")
    plugin._bind_tokens[UMO]["at"] = time.time() - 31
    plugin._purge_tokens()
    assert UMO not in plugin._bind_tokens
    text = _call(plugin, plugin._run_token(UMO, "123456"))
    assert "本会话没有待确认的操作" in text


def test_token_is_single_use():
    plugin = _plugin(bindings={UMO: _binding()})
    plugin._issue_token(UMO, "unbind", label="A-8-17")
    code = _user_replies(plugin)
    assert "已解绑" in _call(plugin, plugin._run_token(UMO, code))
    assert "本会话没有待确认的操作" in _call(plugin, plugin._run_token(UMO, code))


def test_pending_view_shows_room_and_countdown():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    text = plugin._pending_view(UMO)
    assert "待确认：绑定到 A-8-17" in text
    assert _code_of(plugin) in text
    assert "等你回复验证码" in text


def test_pending_view_reports_user_reply():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _user_replies(plugin)
    assert "已收到你的确认" in plugin._pending_view(UMO)


def test_confirm_without_pending_token_says_so():
    plugin = _plugin()
    assert plugin._pending_view(UMO) == "当前会话没有待确认的操作。"
    assert "本会话没有待确认的操作" in _call(plugin, plugin._run_token(UMO, "123456"))


# ================= 群聊只读 =================


def test_bind_room_refused_in_group():
    plugin = _plugin()
    event = _at_rooms(plugin, GROUP_UMO, _FakeEvent(GROUP_UMO, private=False))
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(event, 1))
    assert text == GROUP_WRITE_DENIED
    assert plugin._bind_tokens == {}


def test_unbind_refused_in_group():
    plugin = _plugin(bindings={GROUP_UMO: _binding()})
    event = _FakeEvent(GROUP_UMO, private=False)
    assert _call(plugin, plugin.tool_dorm_electric_unbind(event)) == GROUP_WRITE_DENIED
    assert plugin.store.get_binding(GROUP_UMO) is not None


def test_confirm_refused_in_group():
    plugin = _plugin()
    event = _FakeEvent(GROUP_UMO, private=False)
    assert _call(plugin, plugin.tool_dorm_electric_confirm(event, "123456")) == GROUP_WRITE_DENIED


def test_group_can_still_browse_and_pick_rooms():
    plugin = _plugin()
    event = _at_rooms(plugin, GROUP_UMO, _FakeEvent(GROUP_UMO, private=False))
    text = _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "共 6 间" in text
    assert "已选择" in _call(plugin, plugin.tool_dorm_electric_pick(event, 3))


def test_group_bind_command_refused():
    plugin = _plugin()
    event = _FakeEvent(GROUP_UMO, private=False)

    async def _run():
        return [r async for r in plugin.cmd_bind(event)]

    assert _call(plugin, _run())[0] == GROUP_WRITE_DENIED
    assert plugin.hjnu.calls == []  # 连学校接口都不该打


def test_group_unbind_command_refused():
    plugin = _plugin(bindings={GROUP_UMO: _binding()})
    event = _FakeEvent(GROUP_UMO, private=False)

    async def _run():
        return [r async for r in plugin.cmd_unbind(event)]

    assert _call(plugin, _run())[0] == GROUP_WRITE_DENIED
    assert plugin.store.get_binding(GROUP_UMO) is not None


def test_group_confirm_command_refused():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    event = _FakeEvent(GROUP_UMO, private=False)

    async def _run():
        return [r async for r in plugin.cmd_confirm(event, _code_of(plugin))]

    assert "私聊" in _call(plugin, _run())[0]


# ================= 绑定链路（私聊） =================


def test_bind_room_sends_code_directly_and_tells_ai_not_to_repeat():
    plugin = _plugin()
    event = _at_rooms(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    assert _code_of(plugin) in event.sent[0]  # 机器人直发，避免 LLM 复述出错
    assert "不要复述" in text
    assert plugin.store.get_binding(UMO) is None  # 还没真正绑定


def test_bind_room_reuses_code_for_same_room():
    plugin = _plugin()
    event = _at_rooms(plugin)
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    first = _code_of(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    assert _code_of(plugin) == first
    assert "仍是" in text
    assert len(event.sent) == 1  # 不重复骚扰用户


def test_bind_room_rejects_out_of_range_index():
    plugin = _plugin()
    event = _at_rooms(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(event, 99))
    assert text == "房间编号无效：本层共 6 间，有效编号 1-6"
    assert plugin._bind_tokens == {}


def test_bind_room_requires_room_list_first():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(_FakeEvent(), 1))
    assert "还没选到房间列表" in text


def test_rebinding_requires_unbind_first():
    plugin = _plugin(bindings={UMO: _binding("B-7-01")})
    event = _at_rooms(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    assert "改绑要先解绑" in text
    assert plugin._bind_tokens == {}


def test_confirm_binds_and_reports_balance():
    plugin = _plugin()
    event = _at_rooms(plugin)
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    code = _user_replies(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_confirm(event, code))
    assert "✅ 绑定成功：校本部/春雪楼2/8层/A-8-05" in text
    assert "空调费：94.66 度" in text
    assert "宿舍电费：12.30 元" in text
    assert plugin.store.get_binding(UMO) is not None
    assert UMO not in plugin._bind_tokens


def test_confirm_survives_intervening_browse():
    """AI 在等验证码期间又调了 browse，绑定仍要用 issue 时那个房间。"""
    plugin = _plugin()
    event = _at_rooms(plugin)
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    code = _user_replies(plugin)
    _call(plugin, plugin.tool_dorm_electric_browse(event))  # 向导被重置
    text = _call(plugin, plugin.tool_dorm_electric_confirm(event, code))
    assert "A-8-05" in text


# ================= hint 直连绑定（一句话定位房间，不走向导） =================


def test_bind_room_hint_skips_wizard_and_sends_code():
    plugin = _plugin()
    event = _FakeEvent()
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(event, hint="春雪楼2 8层 A-8-17")
    )
    assert "不要复述" in text
    assert "校本部/春雪楼2/8层/A-8-17" in event.sent[0]
    assert _code_of(plugin) in event.sent[0]
    token = plugin._bind_tokens[UMO]
    assert token["action"] == "bind"
    assert token["wizard"]["step"] == "bind"
    assert token["wizard"]["room"]["room"] == "A-8-17"
    assert token["wizard"]["building"]["building"] == "春雪楼2"
    assert plugin._step(UMO) == ""  # 向导全程没被碰到


def test_bind_room_hint_full_chain_binds_and_matches_elec():
    """「我住 A817」→ 发码 → 用户回码 → 绑定成功 + 电费自动关联。"""
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-17"))
    code = _user_replies(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_confirm(event, code))
    assert "✅ 绑定成功：校本部/春雪楼2/8层/A-8-17" in text
    binding = plugin.store.get_binding(UMO)
    assert "elec" in binding["fees"]
    assert UMO not in plugin._bind_tokens


def test_bind_room_hint_same_room_reminds_old_code():
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-17"))
    first = _code_of(plugin)
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(event, hint="春雪楼2 8层 A817")
    )
    assert _code_of(plugin) == first
    assert "仍是" in text
    assert len(event.sent) == 1  # 不重复骚扰用户


def test_bind_room_hint_other_room_reissues():
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-01"))
    old = _code_of(plugin)
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-17"))
    assert _code_of(plugin) != old


def test_bind_room_hint_ambiguous_asks_user():
    """两个楼栋同层同名房间：必须反问，绝不能默默发码。"""
    plugin = _plugin(
        rooms={
            "春雪楼2": [{"room": "A-8-17", "roomid": 900}],
            "春雪楼1": [{"room": "A-8-17", "roomid": 901}],
        }
    )
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(_FakeEvent(), hint="8层 A817")
    )
    assert "找到多个匹配的房间" in text
    assert UMO not in plugin._bind_tokens


def test_bind_room_hint_unknown_floor_asks_details():
    plugin = _plugin()
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(_FakeEvent(), hint="C-9-99")
    )
    assert "没有 9 层" in text
    assert UMO not in plugin._bind_tokens


def test_bind_room_hint_rebinding_requires_unbind():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(_FakeEvent(), hint="A-8-17")
    )
    assert "改绑要先解绑" in text
    assert UMO not in plugin._bind_tokens


def test_bind_room_hint_keeps_existing_wizard_progress():
    """用户逛了一半向导又直接报房间号：向导进度不该被 hint 破坏。"""
    plugin = _plugin()
    event = _FakeEvent()
    _at_rooms(plugin)  # 向导已走到房间层
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-17"))
    assert plugin._step(UMO) == "room"  # 仍在房间列表，没被写坏


def test_unbind_then_confirm_removes_binding():
    plugin = _plugin(bindings={UMO: _binding()})
    event = _FakeEvent()
    assert "不要复述" in _call(plugin, plugin.tool_dorm_electric_unbind(event))
    code = _user_replies(plugin)
    assert _call(plugin, plugin.tool_dorm_electric_confirm(event, code)) == (
        "✅ 已解绑并停止监控。\n要再绑定，直接说房间号即可，例如「春雪楼817」。"
    )
    assert plugin.store.get_binding(UMO) is None


def test_unbind_without_binding_says_nothing_to_do():
    plugin = _plugin()
    assert "无需解绑" in _call(plugin, plugin.tool_dorm_electric_unbind(_FakeEvent()))


def test_cmd_confirm_with_code_executes():
    """兜底指令：用户私聊手打 /电费 确认 <码> 也要能完成绑定。"""
    plugin = _plugin()
    event = _at_rooms(plugin)
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, 5))
    code = _code_of(plugin)

    async def _run():
        return [r async for r in plugin.cmd_confirm(_FakeEvent(), code)]

    assert "绑定成功" in _call(plugin, _run())[0]
    assert plugin.store.get_binding(UMO) is not None


# ================= 余额与配置 =================


def test_balance_without_binding_asks_which_room():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent()))
    assert "还没有绑定宿舍" in text
    assert "反问用户" in text
    assert "dorm_electric_query_room" in text


def test_balance_reports_thresholds_and_daily_report():
    plugin = _plugin(bindings={UMO: _binding()})
    text = _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent()))
    assert f"⚡ {LABEL}/A-8-17" in text
    assert "空调费：94.66 度" in text
    assert "宿舍电费：12.30 元" in text
    assert "预警线 10 / 紧急线 5" in text
    assert "每日播报 08:00" in text
    assert "轮询间隔 20 分钟" in text
    assert "空调费：✅ 高于预警线，状态正常。" in text
    assert "宿舍电费：✅ 高于预警线，状态正常。" in text
    # 每个费种只出现一行明细：曾经的 bug 是逐 kind 调 _format_fee_results，
    # 而它内部固定遍历 ac/elec，导致每种费种被打印两遍
    assert text.count("空调费：94.66 度") == 1
    assert text.count("宿舍电费：12.30 元") == 1


def test_balance_trend_keeps_unit_separated():
    plugin = _plugin(bindings={UMO: _binding(history=True)})
    text = _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent(), 3))
    assert "24h 用电 2.34 度" in text  # 不是 "2.34度"


def test_balance_flags_low_balance():
    plugin = _plugin(bindings={UMO: _binding()}, ac=3.0)
    text = _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent()))
    assert "低于紧急线 5" in text


def test_balance_does_not_write_history():
    """狂问会稀释「24h 用电 / 日均」，所以查询类工具绝不写历史。"""
    plugin = _plugin(bindings={UMO: _binding()})
    _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent()))
    _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent(), 3))
    assert plugin.store.get_binding(UMO).get("history_by_fee") is None


def test_balance_with_days_renders_trend():
    plugin = _plugin(bindings={UMO: _binding(history=True)})
    text = _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent(), 3))
    assert "最近 3 天每日余额" in text
    assert "24h 用电" in text
    assert "→" in text


# ================= 房间名反查 =================


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("A-8-17", ("A817", "8")),
        ("A817", ("A817", "")),
        ("A-817", ("A817", "")),
        ("春雪楼2 8层 A817", ("A817", "8")),
        ("我住 8 层 17 号 A817", ("A817", "8")),
        ("春雪楼2 8层 A-8-17", ("A817", "8")),
        ("随便说点什么", ("", "")),
    ],
)
def test_parse_room_hint(hint, expected):
    assert DormElectricPlugin._parse_room_hint(hint) == expected


def test_query_room_hit_returns_both_fees():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A817"))
    assert f"⚡ {LABEL}/A-8-17" in text
    assert "空调费：94.66 度" in text
    assert "宿舍电费：12.30 元" in text


def test_query_room_with_building_and_floor_hint():
    plugin = _plugin()
    text = _call(
        plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪楼2 8层 A-8-03")
    )
    assert "A-8-03" in text


def test_query_room_missing_asks_user_to_confirm_number():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A-8-99"))
    assert text == "没找到房间号包含 A899 的房间，请让用户确认一下房间号。"


def test_query_room_multiple_buildings_hit_asks_which():
    """两栋楼都有同名房间时必须反问，不能随便挑一间报余额。"""
    same = [{"room": "A-8-17", "roomid": 646}]
    plugin = _plugin(rooms=same)  # 所有楼栋同一份房间
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A817"))
    assert "找到多个匹配的房间" in text
    assert "请反问用户是哪一个" in text


def test_query_room_without_floor_respects_request_budget():
    """没给楼层时搜索有预算上限，不会把学校接口打爆。"""
    rooms = [{"room": f"A-8-{i:02d}", "roomid": i} for i in range(1, 61)]
    plugin = _plugin(rooms=rooms)
    _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A899"))
    assert len([c for c in plugin.hjnu.calls if c.startswith("rooms:")]) <= 12


def test_query_room_missing_floor_asks_user_to_confirm():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "Z-9-99"))
    assert "没有 9 层" in text
    assert "Z999" in text


def test_query_room_missing_building_floor_asks_for_details():
    """给不出楼栋楼层时，工具要让 AI 去问，而不是默默报一个错结果。"""
    plugin = _plugin(rooms=[])
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "Q777"))
    assert "没找到房间号包含 Q777 的房间" in text


def test_query_room_without_hint_explains_format():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "随便聊聊"))
    assert "没认出房间号" in text
    assert "A817" in text


def test_query_room_caches_repeat_question():
    plugin = _plugin()
    _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A817"))
    before = len(plugin.hjnu.calls)
    second = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A817"))
    assert "缓存命中" in second
    assert len(plugin.hjnu.calls) == before  # 没再打学校接口


def test_query_room_does_not_create_binding():
    plugin = _plugin()
    _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A817"))
    assert plugin.store.data["bindings"] == {}


# ================= 房间口令：真实说法解析 + 模糊反问（v1.1.2） =================


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("春雪楼817", ("817", "8")),
        ("817", ("817", "8")),
        ("汉江师范学院春雪楼817", ("817", "8")),
        ("春雪楼８１７", ("817", "8")),  # 全角数字 NFKC 归一
        ("8楼817", ("817", "8")),
        ("春雪楼 A-08-17", ("A0817", "8")),  # 前导零，解析层原样、匹配层去零
        ("0817", ("0817", "")),  # 首位 0 不可当楼层，交给软猜测
        ("A817", ("A817", "")),  # 原有路径不变
    ],
)
def test_parse_room_hint_real_world_phrases(hint, expected):
    assert DormElectricPlugin._parse_room_hint(hint) == expected


def test_query_room_plain_building_number_hits():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪楼817"))
    assert "⚡ 校本部/春雪楼2/8层/A-8-17" in text
    assert "空调费：94.66 度" in text
    assert "宿舍电费：12.30 元" in text


def test_query_room_bare_digits_hit():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "817"))
    assert "A-8-17" in text


def test_query_room_full_width_digits_hit():
    plugin = _plugin()
    text = _call(
        plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪楼８１７")
    )
    assert "A-8-17" in text


def test_query_room_leading_zero_variant_hits():
    plugin = _plugin()
    text = _call(
        plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪楼 A-08-17")
    )
    assert "A-8-17" in text


def test_soft_floor_guess_scans_guessed_floor_first():
    """「A817」没说楼层：8 层要排在扫描队首，而不是轮转到第二轮才扫到。"""
    plugin = _plugin()
    _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A817"))
    room_calls = [c for c in plugin.hjnu.calls if c.startswith("rooms:")]
    assert room_calls[:2] == [
        "rooms:春雪楼2:8层",
        "rooms:春雪楼1:8层",
    ]


def test_query_room_transposition_fuzzy_asks():
    """「A-8-71」（17 手滑打反）：列出近似候选让 AI 反问，而不是只回格式提示。"""
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪楼 A-8-71"))
    assert "没有完全叫「A871」" in text
    assert "春雪楼2/8层/A-8-17" in text
    assert "请反问用户是哪一个" in text


def test_query_room_wrong_letter_fuzzy_suggests():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪楼 B817"))
    assert "没有完全叫「B817」" in text
    assert "A-8-17" in text


def test_query_room_dissimilar_miss_keeps_format_hint():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "A-8-99"))
    assert text == "没找到房间号包含 A899 的房间，请让用户确认一下房间号。"


def test_bind_room_fuzzy_suggests_and_never_issues_code():
    """模糊候选绝不能直接发码：必须反问，等用户点名后用精确名重调。"""
    plugin = _plugin()
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(_FakeEvent(), hint="春雪楼 B817")
    )
    assert "没有完全叫「B817」" in text
    assert "A-8-17" in text
    assert UMO not in plugin._bind_tokens


def test_bind_room_fuzzy_then_exact_recall_issues_code():
    plugin = _plugin()
    event = _FakeEvent()
    first = _call(
        plugin, plugin.tool_dorm_electric_bind_room(event, hint="春雪楼 A8-71")
    )
    assert "没有完全叫" in first
    text = _call(
        plugin, plugin.tool_dorm_electric_bind_room(event, hint="春雪楼2 8层 A-8-17")
    )
    assert "不要复述" in text
    assert UMO in plugin._bind_tokens
    assert plugin._bind_tokens[UMO]["wizard"]["room"]["room"] == "A-8-17"


def test_bind_room_plain_building_number_full_chain():
    """「春雪楼817」→ 发码 → 回码 → 绑定成功。"""
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="春雪楼817"))
    code = _user_replies(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_confirm(event, code))
    assert "✅ 绑定成功：校本部/春雪楼2/8层/A-8-17" in text
    assert "elec" in plugin.store.get_binding(UMO)["fees"]


# ================= 会话房间记忆：碎片说法不再反复反问（v1.1.3） =================


def test_query_fragment_after_hit_suggests_last_room():
    """「春雪楼817」查过之后说「春雪」：提示记忆房间让 AI 重调，而不是要格式。"""
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_query_room(event, "春雪楼817"))
    text = _call(plugin, plugin.tool_dorm_electric_query_room(event, "春雪"))
    assert "没认出房间号" in text
    assert "定位过 校本部/春雪楼2/8层/A-8-17" in text
    assert "hint 传「A-8-17」" in text
    assert "不要反问" in text


def test_bind_fragment_after_query_binds_directly():
    """先查过 817，再说「绑定」：AI 按记忆提示重调一次就直接发码。"""
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_query_room(event, "春雪楼817"))
    first = _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="绑定"))
    assert "定位过 校本部/春雪楼2/8层/A-8-17" in first
    assert UMO not in plugin._bind_tokens  # 提示阶段不发码
    text = _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-17"))
    assert "不要复述" in text
    assert plugin._bind_tokens[UMO]["wizard"]["room"]["room"] == "A-8-17"


def test_room_memory_expires():
    plugin = _plugin()
    plugin._last_room[UMO] = {
        "params": _params(),
        "label": f"{LABEL}/A-8-17",
        "at": time.time() - 1801,
    }
    text = _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(), "春雪"))
    assert "定位过" not in text


def test_room_memory_not_poisoned_by_misses():
    """解析失败/模糊反问不许写入记忆。"""
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_query_room(event, "随便聊聊"))
    _call(plugin, plugin.tool_dorm_electric_query_room(event, "春雪楼 A-8-71"))
    assert UMO not in plugin._last_room


def test_room_memory_is_per_session():
    plugin = _plugin()
    _call(plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(UMO), "春雪楼817"))
    text = _call(
        plugin, plugin.tool_dorm_electric_query_room(_FakeEvent(GROUP_UMO), "春雪")
    )
    assert "定位过" not in text


def test_bind_hint_success_also_refreshes_memory():
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-01"))
    assert plugin._last_room[UMO]["label"].endswith("/A-8-01")


# ================= 指令：共用选择器的回归（重构不许改行为） =================


def _cmd(plugin, handler, *args, event=None):
    async def _run():
        return [r async for r in handler(event or _FakeEvent(), *args)]

    return _call(plugin, _run())[0]


def test_wizard_commands_still_render_each_layer():
    plugin = _plugin()
    assert "🏫 校区（清美时雨春雪空调（校本部））" in _cmd(plugin, plugin.cmd_bind)
    assert "🏢 楼栋列表" in _cmd(plugin, plugin.cmd_area, "2")
    assert "🧱 楼层列表" in _cmd(plugin, plugin.cmd_building, "1")
    assert "共 6 间" in _cmd(plugin, plugin.cmd_floor, "2")
    assert "🚪 房间列表（春雪楼2 / 8层）" in _cmd(plugin, plugin.cmd_room)
    # 选房后仍要提示确认，命令路径与工具路径共用同一段文案
    assert "发送 /电费 绑定 1 确认" in _cmd(plugin, plugin.cmd_room, "5")


@pytest.mark.parametrize(
    "handler,arg,text",
    [
        ("cmd_area", "99", "校区编号无效"),
        ("cmd_building", "99", "楼栋编号无效"),
        ("cmd_floor", "99", "楼层编号无效"),
        ("cmd_room", "99", "房间编号无效：本层共 6 间，有效编号 1-6"),
    ],
)
def test_wizard_commands_keep_error_text(handler, arg, text):
    plugin = _plugin()
    _at_rooms(plugin, event=_FakeEvent())
    assert _cmd(plugin, getattr(plugin, handler), arg) == text


def test_wizard_commands_reject_unknown_token():
    plugin = _plugin()
    _at_rooms(plugin, event=_FakeEvent())
    text = _cmd(plugin, plugin.cmd_room, "x2")
    assert text.startswith("无法识别的参数「x2」")


def test_bind_command_requires_step_one():
    plugin = _plugin()
    _at_rooms(plugin, event=_FakeEvent())
    assert "选好房间后" in _cmd(plugin, plugin.cmd_bind, "1")


def test_help_lists_confirm_and_group_rule():
    plugin = _plugin()
    text = _cmd(plugin, plugin.cmd_help)
    assert "/电费 确认 [验证码]" in text
    assert "群里只能查询余额" in text


# ================= 工具开关与隐私 =================


def test_ai_tools_can_be_disabled_by_config():
    plugin = _plugin(ai_tools_enabled=False)
    event = _FakeEvent()
    assert "已被插件配置关闭" in _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "已被插件配置关闭" in _call(plugin, plugin.tool_dorm_electric_balance(event))


def test_browse_walks_layers_and_suggests_next_tool():
    plugin = _plugin()
    event = _FakeEvent()
    first = _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "🏫 校区" in first
    assert "dorm_electric_pick(index=N)" in first
    _call(plugin, plugin.tool_dorm_electric_pick(event, 2))
    second = _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "🏢 楼栋列表" in second
    _call(plugin, plugin.tool_dorm_electric_pick(event, 1))
    third = _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "🧱 楼层列表" in third
    _call(plugin, plugin.tool_dorm_electric_pick(event, 2))
    fourth = _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "共 6 间" in fourth
    assert "dorm_electric_pick(index=房间编号)" in fourth


def test_pick_before_browse_tells_ai_to_browse_first():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_pick(_FakeEvent(), 1))
    assert "请先调用 dorm_electric_browse()" in text


def test_pick_uses_wizard_step_not_argument_position():
    """错位防护：当前层决定 index 的含义，模型传什么都错不了。"""
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_browse(event))
    _call(plugin, plugin.tool_dorm_electric_pick(event, 2))  # 选校区
    text = _call(plugin, plugin.tool_dorm_electric_pick(event, 1))  # 现在是楼栋 1
    assert "🧱 楼层列表" in text


def test_pick_rejects_index_out_of_range():
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert _call(plugin, plugin.tool_dorm_electric_pick(event, 99)) == "校区编号无效"


def test_pick_pages_room_list():
    rooms = [{"room": f"A-8-{i:02d}", "roomid": i} for i in range(1, 71)]
    plugin = _plugin(rooms=rooms)
    event = _at_rooms(plugin)
    assert "第 2/3 页" in _call(plugin, plugin.tool_dorm_electric_pick(event, page=2))


def test_pick_page_beyond_last_page_is_clamped():
    """v1.0.8 渲染实测教训：页码越界时不能出现「第 2/1 页」。"""
    plugin = _plugin()
    event = _at_rooms(plugin)  # 6 间房，只有 1 页
    text = _call(plugin, plugin.tool_dorm_electric_pick(event, page=2))
    assert "第 1/1 页" in text
    assert "第 2/1 页" not in text


def test_browse_at_room_step_lists_rooms():
    """选完楼层后再 browse 必须真的列房间，而不是重复「共 N 间」。"""
    plugin = _plugin()
    event = _at_rooms(plugin)
    text = _call(plugin, plugin.tool_dorm_electric_browse(event))
    assert "🚪 房间列表（春雪楼2 / 8层）" in text
    assert "1. A-8-01" in text
    assert "dorm_electric_pick(page=N) 翻页" in text


def test_browse_repeats_room_list_after_clamped_page():
    plugin = _plugin()
    event = _at_rooms(plugin)
    _call(plugin, plugin.tool_dorm_electric_pick(event, page=9))
    assert "第 1/1 页" in _call(plugin, plugin.tool_dorm_electric_browse(event))


@pytest.mark.parametrize(
    "call",
    [
        lambda p, e: p.tool_dorm_electric_browse(e),
        lambda p, e: p.tool_dorm_electric_pick(e, 2),
        lambda p, e: p.tool_dorm_electric_pick(e, 1),
        lambda p, e: p.tool_dorm_electric_pick(e, page=1),
        lambda p, e: p.tool_dorm_electric_balance(e),
        lambda p, e: p.tool_dorm_electric_query_room(e, "A817"),
        lambda p, e: p.tool_dorm_electric_bind_room(e, 1),
        lambda p, e: p.tool_dorm_electric_unbind(e),
        lambda p, e: p.tool_dorm_electric_confirm(e, "123456"),
    ],
)
def test_no_tool_output_ever_contains_cookie_value(call):
    """工具输出会进 LLM 上下文，等于公开——cookie 值绝不能出现。"""
    plugin = _plugin(bindings={UMO: _binding()})
    event = _at_rooms(plugin)
    assert "SECRET" not in _call(plugin, call(plugin, event))
    assert "SECRET" not in "".join(event.sent)


def test_credential_hint_never_echoes_value():
    plugin = _plugin()
    assert "SECRET" not in CREDENTIAL_HINT
    assert plugin.config["hjnu_cookie"] == "JSESSIONID=SECRET"

# ================= 预警静音（v1.1.4） =================


def test_mute_tool_sets_and_reports_status():
    plugin = _plugin()
    event = _FakeEvent()
    text = _call(plugin, plugin.tool_dorm_electric_mute_alerts(event, 24))
    assert "已静音余额预警 24 小时" in text
    assert plugin._alert_muted[UMO] > time.time()
    status = _call(plugin, plugin.tool_dorm_electric_mute_alerts(event, 0))
    assert "静音中" in status


def test_mute_tool_clear_and_double_clear():
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_mute_alerts(event, 24))
    text = _call(plugin, plugin.tool_dorm_electric_mute_alerts(event, -1))
    assert "已恢复余额预警" in text
    assert UMO not in plugin._alert_muted
    again = _call(plugin, plugin.tool_dorm_electric_mute_alerts(event, -1))
    assert "没有静音" in again


def test_mute_tool_caps_hours_at_168():
    plugin = _plugin()
    text = _call(plugin, plugin.tool_dorm_electric_mute_alerts(_FakeEvent(), 1000))
    assert "静音余额预警 168 小时" in text


def test_mute_tool_group_denied():
    plugin = _plugin()
    text = _call(
        plugin,
        plugin.tool_dorm_electric_mute_alerts(
            _FakeEvent(GROUP_UMO, private=False), 24
        ),
    )
    assert text == GROUP_WRITE_DENIED
    assert GROUP_UMO not in plugin._alert_muted


def test_muted_evaluation_enqueues_nothing_and_recovers():
    """静音期间轮询不产生预警；解除后同一条低余额立刻恢复预警（state 没被污染）。"""
    plugin = _plugin(bindings={UMO: _binding()})
    binding = plugin.store.get_binding(UMO)
    _call(plugin, plugin.tool_dorm_electric_mute_alerts(_FakeEvent(), 24))
    _call(plugin, plugin._evaluate_alerts(UMO, binding, 3.0, "ac"))
    assert plugin._pending_alerts.get(UMO) is None
    _call(plugin, plugin.tool_dorm_electric_mute_alerts(_FakeEvent(), -1))
    _call(plugin, plugin._evaluate_alerts(UMO, binding, 3.0, "ac"))
    assert plugin._pending_alerts[UMO]


def test_flush_alerts_carries_mute_hint():
    plugin = _plugin(bindings={UMO: _binding()})
    sent: list[tuple[str, str]] = []

    async def _capture(umo, text):
        sent.append((umo, text))
        return True

    plugin._send = _capture
    binding = plugin.store.get_binding(UMO)
    _call(plugin, plugin._evaluate_alerts(UMO, binding, 8.0, "ac"))
    _call(plugin, plugin._flush_alerts())
    assert len(sent) == 1
    assert "静音" in sent[0][1] and "/电费 静音" in sent[0][1]


def test_cmd_mute_flow():
    plugin = _plugin()
    event = _FakeEvent()

    async def _run():
        texts = []
        async for r in plugin.cmd_mute(event, "24"):
            texts.append(r)
        async for r in plugin.cmd_mute(event):
            texts.append(r)
        async for r in plugin.cmd_mute(event, "取消"):
            texts.append(r)
        async for r in plugin.cmd_mute(event, "abc"):
            texts.append(r)
        return texts

    texts = asyncio.run(_run())
    assert "已静音余额预警 24 小时" in texts[0]
    assert "预警静音中" in texts[1]
    assert "已恢复余额预警" in texts[2]
    assert "用法" in texts[3]


def test_cmd_mute_group_denied():
    plugin = _plugin()
    event = _FakeEvent(GROUP_UMO, private=False)

    async def _run():
        return [r async for r in plugin.cmd_mute(event, "24")]

    assert asyncio.run(_run()) == [GROUP_WRITE_DENIED]


def test_balance_shows_mute_line():
    plugin = _plugin(bindings={UMO: _binding()})
    _call(plugin, plugin.tool_dorm_electric_mute_alerts(_FakeEvent(), 24))
    text = _call(plugin, plugin.tool_dorm_electric_balance(_FakeEvent()))
    assert "预警静音中" in text


# ================= 确认码私聊放宽（v1.1.4） =================


@pytest.mark.parametrize(
    "template",
    ["确认 {code}", "验证码是{code}", "码：{code}。", "好，确认 {code}"],
)
def test_listener_accepts_keyword_forms_in_private(template):
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _call(
        plugin,
        plugin.on_user_replied_code(
            _FakeEvent(UMO, message=template.format(code=_code_of(plugin)))
        ),
    )
    assert plugin._bind_tokens[UMO]["user_ok"] is True


def test_listener_keyword_with_wrong_code_ignored():
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _call(
        plugin,
        plugin.on_user_replied_code(_FakeEvent(UMO, message="确认 000000")),
    )
    assert plugin._bind_tokens[UMO]["user_ok"] is False


def test_listener_keyword_with_trailing_digits_ignored():
    """「确认 481526123」这种数字粘连不算（防手滑多敲一位变误同意）。"""
    plugin = _plugin()
    plugin._issue_token(UMO, "bind", label="A-8-17")
    _call(
        plugin,
        plugin.on_user_replied_code(
            _FakeEvent(UMO, message=f"确认 {_code_of(plugin)}123")
        ),
    )
    assert plugin._bind_tokens[UMO]["user_ok"] is False


def test_keyword_reply_completes_bind_end_to_end():
    plugin = _plugin()
    event = _FakeEvent()
    _call(plugin, plugin.tool_dorm_electric_bind_room(event, hint="A-8-17"))
    _call(
        plugin,
        plugin.on_user_replied_code(
            _FakeEvent(UMO, message=f"验证码是 {_code_of(plugin)}")
        ),
    )
    text = _call(plugin, plugin.tool_dorm_electric_confirm(event, _code_of(plugin)))
    assert "✅ 绑定成功：校本部/春雪楼2/8层/A-8-17" in text
