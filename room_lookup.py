"""房间名反查：从口语提示（「春雪楼817」「A-8-17」）定位学校侧房间参数。

从 main.py 平移而来。纯函数（提示解析、楼栋排序、楼层挑选、模糊反问文案）
+ RoomResolver（注入 provider/store/config/记忆 dict，串起带请求预算的扫描）。
"""

import asyncio
import difflib
import re
import time
import unicodedata

try:
    from .formatting import CREDENTIAL_HINT
    from .providers import QueryError, SessionExpiredError
except ImportError:  # 兜底：被以非包方式加载时
    from formatting import CREDENTIAL_HINT  # type: ignore
    from providers import QueryError, SessionExpiredError  # type: ignore

# 房间名反查：A-8-17 / A817 / 春雪楼2 8层 A817
HINT_ROOM_RE = re.compile(r"([A-Za-z]+)[-_ ]?(\d+)(?:[-_ ]?(\d+))?")
HINT_FLOOR_RE = re.compile(r"(\d+)\s*层")
HINT_DIGITS_RE = re.compile(r"\d{3,4}")

# 扫描预算：学校接口每层一次请求，全校扫一遍要几十次，必须设上限。
LOOKUP_BUILDING_BUDGET = 12
LOOKUP_ROOM_BUDGET = 12

# 会话房间记忆 30 分钟
LAST_ROOM_TTL = 1800


def remember_room(last_room: dict, umo: str, params: dict) -> None:
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
    last_room[umo] = {"params": params, "label": label, "at": time.time()}


def last_room_line(last_room: dict, umo: str) -> str:
    """未解析出房间时的追加提示：告诉 AI 本会话最近定位过哪个房间、怎么重调。"""
    last = last_room.get(umo)
    if not last or time.time() - float(last.get("at", 0)) > LAST_ROOM_TTL:
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


def parse_room_hint(hint: str) -> tuple[str, str]:
    """从口语里抽出房间 token（A817 / 817）与楼层号（8）。

    「A-8-17」自带楼层段；「A817」这类紧凑写法再从「8层」里捞楼层；
    「春雪楼817」「817」这类没有字母的说法走纯数字回退，首位数字当楼层。
    全角数字先 NFKC 归一。都没有就留空，由 resolve 按受限的逐层搜索去找。
    """
    text = unicodedata.normalize("NFKC", str(hint or ""))
    m = HINT_ROOM_RE.search(text)
    if not m:
        digits = HINT_DIGITS_RE.search(text)
        if not digits:
            return "", ""
        token = digits.group(0)
        first = token[0]
        return token, ("" if first == "0" else first)
    letters, second, third = m.groups()
    token = f"{letters}{second}{third or ''}"
    if third:
        return token, second.lstrip("0")
    floor = HINT_FLOOR_RE.search(text)
    if floor:
        return token, floor.group(1).lstrip("0")
    return token, ""


def order_buildings(
    buildings: list[dict], text: str, bound_name: str | None
) -> list[dict]:
    """扫描顺序：提示里点名的楼栋 > 本会话已绑定的楼栋 > 其余（截预算）。"""
    order: list[dict] = []
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
    if bound_name:
        same = next((b for b in buildings if b.get("building") == bound_name), None)
        if same and same not in order:
            order.append(same)
    order.extend(b for b in buildings if b not in order)
    return order[:LOOKUP_BUILDING_BUDGET]


def guess_floor_no(token: str) -> str:
    """「A817」这类 token 不带楼层：从去字母后的首个非零数字猜楼层（软优先）。"""
    digits_part = re.sub(r"^[A-Za-z]+", "", token)
    g = re.search(r"[1-9]", digits_part)
    return g.group(0) if g else ""


def pick_floor_pairs(
    floors_by_building: list[tuple[dict, list[dict]]],
    floor_no: str,
    guess: str = "",
) -> list[tuple[dict, dict]]:
    """挑出要查房间的 (楼栋, 楼层) 对，截断在预算内。

    给了楼层就只取匹配楼层；没给就先试猜测楼层（如「A817」猜 8），再按
    「先每栋楼第一层、再每栋楼第二层」轮转。软优先只影响扫描顺序不影响正确性，
    猜错就按原轮转顺序兜底。
    """
    pairs: list[tuple[dict, dict]] = []
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
        return pairs[:LOOKUP_ROOM_BUDGET]
    picked: set[tuple[str, str]] = set()

    def pick(b: dict, f: dict) -> None:
        key = (str(b.get("building")), str(f.get("floor")))
        if key not in picked and len(pairs) < LOOKUP_ROOM_BUDGET:
            picked.add(key)
            pairs.append((b, f))

    if guess:
        for building, floors in floors_by_building:
            floor = next(
                (
                    f
                    for f in floors
                    if str(f.get("floor", "")).replace("层", "").strip() == guess
                ),
                None,
            )
            if floor:
                pick(building, floor)
    depth = 0
    while len(pairs) < LOOKUP_ROOM_BUDGET:
        added = False
        for building, floors in floors_by_building:
            if len(floors) > depth:
                pick(building, floors[depth])
                added = True
                if len(pairs) >= LOOKUP_ROOM_BUDGET:
                    break
        if not added:
            break
        depth += 1
    return pairs[:LOOKUP_ROOM_BUDGET]


def norm_room_name(room: dict) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(room.get("room", ""))).lower()


def strict_match(
    results: list[tuple[dict, dict, list[dict]]], t: str
) -> list[tuple[dict, dict, dict]]:
    needle = re.sub(r"[^A-Za-z0-9]", "", str(t)).lower()
    out: list[tuple[dict, dict, dict]] = []
    for building, floor, rooms in results:
        for room in rooms:
            if needle and needle in norm_room_name(room):
                out.append((building, floor, room))
                break
    return out


def fuzzy_miss_text(
    token: str, scanned: list[tuple[str, dict, dict, dict]]
) -> str:
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


class RoomResolver:
    """按房间名反查学校侧房间参数。last_room 由宿主持有并跨调用共享。"""

    def __init__(self, hjnu, store, config: dict, last_room: dict):
        self._hjnu = hjnu
        self._store = store
        self._config = config
        self._last_room = last_room

    async def resolve(self, umo: str, hint: str) -> tuple[dict | None, str | None]:
        """按房间名反查学校侧的房间参数。返回 (ac_params, err)。

        搜索有请求预算上限（学校接口每层一次请求，全校扫一遍要几十次）：
        给了楼层就只在匹配楼层找；没给楼层就按「先每栋楼第一层、再每栋楼第二层」
        的顺序轮转，命中不了就反问用户补楼栋和楼层。
        """
        token, floor_no = parse_room_hint(hint)
        if not token:
            return None, (
                "没认出房间号。可以说「春雪楼817」「817」「A817」「A-8-17」或"
                "「春雪楼2 8层 A817」这类格式。"
                + last_room_line(self._last_room, umo)
            )
        items = self._config.get("fee_items", {}) or {}
        aids = list(items)
        if not aids:
            return None, "配置中没有任何缴费项目（fee_items）。"
        aid = aids[0]
        try:
            areas = await self._hjnu.list_areas(aid)
            area = next(
                (a for a in areas if a.get("areaname") == "校本部"),
                (areas or [None])[0],
            )
            if not area:
                return None, "学校没有返回校区列表。"
            buildings = await self._hjnu.list_buildings(aid, area)
        except SessionExpiredError:
            return None, CREDENTIAL_HINT
        except QueryError as e:
            return None, f"❌ {e}"
        if not buildings:
            return None, "学校没有返回楼栋列表。"
        text = str(hint)
        bound = self._store.get_binding(umo) if self._store else None
        bound_name = (bound or {}).get("params", {}).get("building", {}).get("building")
        order = order_buildings(buildings, text, bound_name)

        async def floors_of(building: dict) -> list[dict]:
            try:
                return await self._hjnu.list_floors(aid, area, building)
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
        # 「A817」这类 token 不带楼层：从去字母后的首个非零数字猜楼层，猜中的排最前
        # （软优先，只影响扫描顺序不影响正确性；猜错就按原轮转顺序兜底）。
        guess = "" if floor_no else guess_floor_no(token)
        pairs = pick_floor_pairs(floors_by_building, floor_no, guess)
        if not pairs:
            if floor_no:
                named = next(
                    (
                        b
                        for b in buildings
                        if b.get("building") and b["building"] in text
                    ),
                    None,
                )
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
                return building, floor, await self._hjnu.list_rooms(
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

        hits = strict_match(results, token)
        if not hits:
            # 「A-08-17」这类前导零写法：去掉数字段里的前导零再试一次
            alt = re.sub(r"(^|[^0-9])0+(\d)", r"\1\2", token)
            if alt != token:
                hits = strict_match(results, alt)
                if hits:
                    token = alt
        if not hits:
            scanned: list[tuple[str, dict, dict, dict]] = []
            for building, floor, rooms in results:
                for room in rooms:
                    scanned.append((norm_room_name(room), building, floor, room))
            return None, fuzzy_miss_text(token, scanned)
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
        remember_room(self._last_room, umo, params)
        return params, None
