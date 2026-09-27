"""parse_balance 与 _room_token 的解析测试。"""

from astrbot_plugin_dorm_electric.main import DormElectricPlugin
from astrbot_plugin_dorm_electric.providers.http_json import parse_balance

room_token = DormElectricPlugin._room_token


def test_parse_balance_kwh():
    value, room, unit = parse_balance("A-8-17房间当前剩余电量94.66度")
    assert value == 94.66
    assert room == "A-8-17"
    assert unit == "度"


def test_parse_balance_yuan_full_width_colon():
    value, room, unit = parse_balance("春雪楼A817房间余额：23.45元")
    assert value == 23.45
    assert room == "春雪楼A817"
    assert unit == "元"


def test_parse_balance_half_width_colon():
    value, room, unit = parse_balance("余额:12.00元")
    assert value == 12.0
    assert room == ""
    assert unit == "元"


def test_parse_balance_no_match():
    assert parse_balance("接口错误：未知返回") is None


def test_parse_balance_none_or_empty():
    assert parse_balance(None) is None
    assert parse_balance("") is None


def test_parse_balance_integer_value():
    value, _, unit = parse_balance("A-8-17房间当前剩余电量100度")
    assert value == 100.0
    assert unit == "度"


def test_room_token_standard():
    assert room_token("A-8-17") == "A817"


def test_room_token_underscore():
    assert room_token("B_3_09") == "B309"


def test_room_token_prefixed():
    assert room_token("春雪楼2 C-5-12") == "C512"


def test_room_token_no_letters():
    assert room_token("8-17") is None


def test_room_token_empty():
    assert room_token("") is None
    assert room_token(None) is None
