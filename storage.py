"""JSON 文件存储：绑定关系与历史数据。

存放于 AstrBot 数据目录 data/plugin_data/<plugin_name>/ 下，
插件更新重装不会丢数据。
"""

import json
import os
import time
from pathlib import Path

DEFAULT_DATA = {"bindings": {}}
MAX_HISTORY = 5000


class Store:
    def __init__(self, path: Path, history_keep_days: int = 60) -> None:
        self.path = path
        self.history_keep_days = history_keep_days
        self.data = json.loads(json.dumps(DEFAULT_DATA))  # deep copy
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
                self.data = data
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------- 绑定 ----------

    def get_binding(self, umo: str) -> dict | None:
        return self.data["bindings"].get(umo)

    def set_binding(self, umo: str, binding: dict) -> None:
        self.data["bindings"][umo] = binding

    def del_binding(self, umo: str) -> bool:
        return self.data["bindings"].pop(umo, None) is not None

    # ---------- 历史 ----------

    @staticmethod
    def append_history(
        binding: dict, value: float, ts: float | None = None, keep_days: int = 60
    ) -> None:
        ts = ts if ts is not None else time.time()
        history = binding.setdefault("history", [])
        history.append({"t": ts, "v": value})
        cutoff = ts - 24 * 3600 * max(1, keep_days)
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]
        binding["history"] = [h for h in history if h["t"] >= cutoff]

    @staticmethod
    def latest_value(binding: dict) -> tuple[float, float] | None:
        """返回 (最新值, 时间戳)，无数据返回 None。"""
        history = binding.get("history") or []
        if not history:
            return None
        last = history[-1]
        return float(last["v"]), float(last["t"])

    @staticmethod
    def append_fee_history(
        binding: dict,
        fee: str,
        value: float,
        unit: str,
        ts: float | None = None,
        keep_days: int = 60,
    ) -> None:
        """保存指定费种的历史，避免度和元混在同一条曲线中。"""
        ts = ts if ts is not None else time.time()
        history = binding.setdefault("history_by_fee", {}).setdefault(fee, [])
        history.append({"t": ts, "v": value, "u": unit})
        cutoff = ts - 24 * 3600 * max(1, keep_days)
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]
        binding["history_by_fee"][fee] = [h for h in history if h["t"] >= cutoff]
