"""手动登记数据源：用户通过指令自行上报余额。"""

from .base import BalanceResult, ElecProvider


class ManualProvider(ElecProvider):
    name = "manual"

    async def fetch(self, binding: dict) -> BalanceResult:
        value = binding.get("manual_value")
        if value is None:
            return BalanceResult(ok=False, value=None, raw="尚未登记余额")
        updated = binding.get("manual_updated")
        raw = "手动登记的余额"
        if updated:
            raw = f"手动登记于 {updated}"
        return BalanceResult(ok=True, value=float(value), raw=raw)
