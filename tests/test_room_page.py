"""_room_page（房间列表分页）与 cmd_room 参数语义的测试。"""

import asyncio

import pytest
from astrbot_plugin_dorm_electric.main import ROOM_PAGE_SIZE, DormElectricPlugin

room_page = DormElectricPlugin._room_page


def _rooms(n: int) -> list[dict]:
    """造 n 间假房间，房间号形如 A-1-01（与学校一致的 A 字头命名）。"""
    return [{"room": f"A-1-{i:02d}", "roomid": 100 + i} for i in range(1, n + 1)]


def test_empty_rooms_yields_single_empty_page():
    assert room_page([], 1) == (0, 0, 1)


def test_single_page_when_fewer_than_page_size():
    rooms = _rooms(ROOM_PAGE_SIZE)
    assert room_page(rooms, 1) == (0, ROOM_PAGE_SIZE, 1)


def test_exactly_page_size_plus_one_makes_two_pages():
    rooms = _rooms(ROOM_PAGE_SIZE + 1)
    assert room_page(rooms, 1) == (0, ROOM_PAGE_SIZE, 2)
    assert room_page(rooms, 2) == (ROOM_PAGE_SIZE, ROOM_PAGE_SIZE + 1, 2)


def test_last_page_is_partial():
    rooms = _rooms(128)
    start, end, total_pages = room_page(rooms, 5)
    assert (start, end, total_pages) == (120, 128, 5)


def test_page_number_is_clamped():
    rooms = _rooms(128)
    # 越界页码夹到最后一页 / 第一页
    assert room_page(rooms, 99) == room_page(rooms, 5)
    assert room_page(rooms, 0) == room_page(rooms, -3) == room_page(rooms, 1)


@pytest.mark.parametrize("bad", [None, "x", "", []])
def test_non_numeric_page_falls_back_to_first(bad):
    rooms = _rooms(128)
    assert room_page(rooms, bad) == (0, ROOM_PAGE_SIZE, 5)


def test_custom_page_size():
    rooms = _rooms(128)
    start, end, total_pages = room_page(rooms, 2, 64)
    assert (start, end, total_pages) == (64, 128, 2)


@pytest.mark.parametrize("total", [1, 7, 30, 31, 59, 60, 61, 128, 200])
def test_pagination_covers_every_room_exactly_once(total):
    """所有页切片拼起来必须等于完整列表，且互不重叠。"""
    rooms = _rooms(total)
    _, _, total_pages = room_page(rooms, 1)
    seen: list[dict] = []
    for p in range(1, total_pages + 1):
        start, end, _ = room_page(rooms, p)
        seen.extend(rooms[start:end])
    assert seen == rooms


# ---------- cmd_room 端到端（走真实渲染，才能抓到页码方向类 bug） ----------

UMO = "qq:12345"


class _FakeEvent:
    unified_msg_origin = UMO

    def plain_result(self, text):
        return text


def _plugin(rooms: list[dict], room_page=0) -> DormElectricPlugin:
    plugin = DormElectricPlugin.__new__(DormElectricPlugin)
    plugin._wizard = {
        UMO: {
            "step": "room",
            "area": {"areaname": "校本部"},
            "building": {"building": "春雪楼2"},
            "floor": {"floor": "8层"},
            "rooms": rooms,
            "room_page": room_page,
        }
    }
    return plugin


async def _call_async(plugin: DormElectricPlugin, *args) -> str:
    out = [r async for r in plugin.cmd_room(_FakeEvent(), *args)]
    return out[0]


def _call(plugin: DormElectricPlugin, *args) -> str:
    """cmd_room 是 async 生成器；这里同步驱动，避免引入 pytest-asyncio 依赖。"""
    return asyncio.run(_call_async(plugin, *args))


def test_first_page_call_shows_page_one():
    """cmd_floor 把 room_page 置 0，首次 /电费 房间 必须给第 1 页而不是第 2 页。"""
    plugin = _plugin(_rooms(128))
    text = _call(plugin)
    assert "第 1/5 页" in text
    assert "本页第 1-30 间" in text
    assert "1. A-1-01（101）" in text
    assert "30. A-1-30（130）" in text
    assert "31. A-1-31" not in text


def test_no_arg_pages_forward_then_wraps():
    plugin = _plugin(_rooms(128))
    assert "第 1/5 页" in _call(plugin)
    for expected in (2, 3, 4, 5):
        assert f"第 {expected}/5 页" in _call(plugin)
    wrapped = _call(plugin)
    assert "第 1/5 页" in wrapped
    assert "已到末页" in wrapped


def test_page_jump_token():
    plugin = _plugin(_rooms(128))
    text = _call(plugin, "p4")
    assert "第 4/5 页" in text
    assert "本页第 91-120 间" in text
    assert "91. A-1-91（191）" in text
    # 大写 P 也认
    assert "第 3/5 页" in _call(plugin, "P3")


def test_absolute_index_selects_across_pages():
    """绝对编号可以选中列表里看不到的房间（正是 60 上限问题的修复点）。"""
    plugin = _plugin(_rooms(128))
    text = _call(plugin, "75")
    assert "已选择：校本部/春雪楼2/8层/A-1-75" in text
    assert plugin._wizard[UMO]["step"] == "bind"
    assert plugin._wizard[UMO]["room"]["room"] == "A-1-75"


def test_int_index_accepted():
    """AstrBot 会把纯数字参数转成 int，int 也要能选中。"""
    plugin = _plugin(_rooms(128))
    assert "已选择：校本部/春雪楼2/8层/A-1-75" in _call(plugin, 75)


@pytest.mark.parametrize("bad", ["0", "129", "9999"])
def test_out_of_range_index_rejected(bad):
    plugin = _plugin(_rooms(128))
    text = _call(plugin, bad)
    assert "房间编号无效：本层共 128 间，有效编号 1-128" in text
    assert plugin._wizard[UMO]["step"] == "room"


@pytest.mark.parametrize("bad", ["abc", "第1页", "1-5", "-1", "0x10"])
def test_unparsable_argument_explains_usage(bad):
    plugin = _plugin(_rooms(128))
    text = _call(plugin, bad)
    assert "无法识别的参数" in text
    assert "p<页码>" in text


def test_no_rooms_yields_usage_hint():
    plugin = DormElectricPlugin.__new__(DormElectricPlugin)
    plugin._wizard = {UMO: {}}
    assert "先 /电费 楼层" in _call(plugin)


def test_single_page_room_never_wraps():
    """房间数不足一页时，无参调用应停在第 1 页，且不提示翻页/回绕。"""
    plugin = _plugin(_rooms(12))
    for _ in range(3):
        text = _call(plugin)
        assert "第 1/1 页" in text
        assert "已到末页" not in text
        assert "翻页" not in text
        assert "选择：/电费 房间 <编号>" in text


def test_page_reset_when_floor_changes():
    """cmd_floor 重置 room_page=0 后，换楼层应从第 1 页重新开始。"""
    plugin = _plugin(_rooms(128))
    _call(plugin)
    _call(plugin)
    assert plugin._wizard[UMO]["room_page"] == 2
    plugin._wizard[UMO]["room_page"] = 0  # 等价于 cmd_floor 选完楼层后的重置
    assert "第 1/5 页" in _call(plugin)

