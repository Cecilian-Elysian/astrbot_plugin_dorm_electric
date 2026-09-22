"""数据源接口与通用数据结构。"""

from dataclasses import dataclass, field


@dataclass
class BalanceResult:
    """一次余额查询的结果。

    value 单位为“度”，由各数据源负责解析。
    raw 保存原始 errmsg 或错误说明，便于调试与展示。
    """

    ok: bool
    value: float | None
    raw: str
    session_expired: bool = False
    extra: dict = field(default_factory=dict)


class QueryError(Exception):
    """查询过程中发生的可预期错误（网络、凭证缺失、接口返回异常等）。"""


class ElecProvider:
    """余额数据源接口，所有数据源实现 fetch()。"""

    name = "base"

    async def fetch(self, binding: dict) -> BalanceResult:
        raise NotImplementedError
