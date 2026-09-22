"""汉江师范学院缴费系统（pay2.hjnu.edu.cn）数据源。

接口为企业微信内嵌 H5 的 JSON API：
- POST 表单（键值对，层级选项序列化为 JSON 字符串）
- 需要携带 JSESSIONID 会话 Cookie 与带 wxwork 标识的 UA
- 未认证时返回 {"retcode": "91001", "errmsg": "会话已超时…"}
"""

import asyncio
import json
import logging
import re

import httpx

from .base import BalanceResult, ElecProvider, QueryError

SESSION_EXPIRED_CODE = "91001"

logger = logging.getLogger("astrbot.plugin.dorm_electric")


class SessionExpiredError(QueryError):
    """会话凭证失效（retcode 91001）。"""


# 例：A-8-17房间当前剩余电量94.66度
BALANCE_RE = re.compile(r"剩余电量\s*([0-9]+(?:\.[0-9]+)?)\s*度")
ROOM_RE = re.compile(r"^\s*(\S+?)房间")


def parse_balance(raw_msg: str) -> tuple[float, str] | None:
    """从 errmsg 文本中解析 (余额度数, 房间名)，解析失败返回 None。"""
    m = BALANCE_RE.search(raw_msg or "")
    if not m:
        return None
    room = ""
    mr = ROOM_RE.search(raw_msg or "")
    if mr:
        room = mr.group(1)
    return float(m.group(1)), room


class HjnuProvider(ElecProvider):
    name = "hjnu"

    def __init__(
        self,
        base_url: str,
        query_path: str,
        cookie: str,
        user_agent: str,
        referer: str,
        timeout: int,
        proxy: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.query_path = query_path
        self.cookie = (cookie or "").strip()
        self.user_agent = user_agent
        self.referer = referer
        self.timeout = timeout
        self.proxy = (proxy or "").strip() or None
        self._client: httpx.AsyncClient | None = None

    def update_cookie(self, cookie: str) -> None:
        self.cookie = (cookie or "").strip()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.referer,
                "Origin": self.base_url,
            }
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                proxy=self.proxy,
                headers=headers,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self) -> dict:
        if not self.cookie:
            raise QueryError("未配置会话凭证。请先发送 /电费 凭证 更新 JSESSIONID。")
        return {"Cookie": self.cookie}

    @staticmethod
    def _j(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def _post_once(self, path: str, fields: dict) -> tuple[int, str, str]:
        """单次请求，返回 (status, text, err)；err 非空表示网络层失败。"""
        client = self._get_client()
        try:
            resp = await client.post(
                self.base_url + path,
                data=fields,
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            return 0, "", f"网络请求失败：{e!r}"
        try:
            text = resp.text
        except Exception:
            text = ""
        return resp.status_code, text, ""

    async def _post(self, path: str, fields: dict) -> dict:
        # 5xx（如学校服务器偶发 502）自动换新连接重试 2 次
        last_err = ""
        for attempt in range(3):
            status, text, net_err = await self._post_once(path, fields)
            if net_err:
                last_err = net_err
                logger.error("电费接口网络错误(%s)：%s", path, net_err)
            elif status == 200:
                try:
                    data = json.loads(text)
                except ValueError as e:
                    snippet = (text or "").strip()[:200]
                    logger.error("电费接口返回非 JSON(%s)：%s", path, snippet)
                    raise QueryError("接口返回的不是 JSON（可能凭证已被服务端作废）") from e
                if not isinstance(data, dict):
                    raise QueryError("接口返回了意外结构")
                return data
            else:
                snippet = (text or "").strip()[:200]
                last_err = f"接口返回 HTTP {status}"
                logger.error("电费接口 HTTP %s(%s)：%s", status, path, snippet)
                if status < 500:
                    break
            if attempt < 2:
                await asyncio.sleep(1.5)
                # 5xx/网络错误时丢弃旧连接，避免复用异常连接
                if self._client is not None and not self._client.is_closed:
                    await self._client.aclose()
                self._client = None
        raise QueryError(f"{last_err}（学校服务器暂时无响应，已自动重试仍失败，请稍后再试）")

    @staticmethod
    def _check(data: dict) -> None:
        retcode = str(data.get("retcode", ""))
        if retcode == SESSION_EXPIRED_CODE:
            raise SessionExpiredError(data.get("errmsg", "会话已超时"))
        if retcode != "0":
            raise QueryError(f"接口错误 retcode={retcode}: {data.get('errmsg', '')}")

    async def fetch(self, binding: dict) -> BalanceResult:
        """ElecProvider 接口：从绑定中取参数查询余额。"""
        return await self.query_room(binding.get("params") or {})

    async def query_room(self, params: dict) -> BalanceResult:
        """params: {aid, area, building, floor, room}，值为选项 dict。"""
        fields = {"aid": params["aid"]}
        for key in ("area", "building", "floor", "room"):
            if params.get(key):
                fields[key] = self._j(params[key])
        try:
            data = await self._post(self.query_path, fields)
            self._check(data)
        except SessionExpiredError as e:
            return BalanceResult(ok=False, value=None, raw=str(e), session_expired=True)
        except QueryError as e:
            return BalanceResult(ok=False, value=None, raw=str(e))
        raw_msg = str(data.get("errmsg", ""))
        parsed = parse_balance(raw_msg)
        if parsed is None:
            return BalanceResult(ok=False, value=None, raw=raw_msg or "接口未返回余额文本")
        value, room = parsed
        return BalanceResult(ok=True, value=value, raw=raw_msg, extra={"room": room})

    async def list_areas(self, aid: str) -> list[dict]:
        data = await self._post("/wechat/basicQuery/queryElecArea.html", {"aid": aid})
        self._check(data)
        return list(data.get("areatab") or [])

    async def list_buildings(self, aid: str, area: dict) -> list[dict]:
        data = await self._post(
            "/wechat/basicQuery/queryElecBuilding.html",
            {"aid": aid, "area": self._j(area)},
        )
        self._check(data)
        return list(data.get("buildingtab") or [])

    async def list_floors(self, aid: str, area: dict, building: dict) -> list[dict]:
        data = await self._post(
            "/wechat/basicQuery/queryElecFloor.html",
            {"aid": aid, "area": self._j(area), "building": self._j(building)},
        )
        self._check(data)
        return list(data.get("floortab") or [])

    async def list_rooms(self, aid: str, area: dict, building: dict, floor: dict) -> list[dict]:
        data = await self._post(
            "/wechat/basicQuery/queryElecRoom.html",
            {
                "aid": aid,
                "area": self._j(area),
                "building": self._j(building),
                "floor": self._j(floor),
            },
        )
        self._check(data)
        return list(data.get("roomtab") or [])
