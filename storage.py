"""JSON 文件存储：绑定关系与历史数据。

存放于 AstrBot 数据目录 data/plugin_data/<plugin_name>/ 下，
插件更新重装不会丢数据。
"""

import json
import logging
import os
import time
from pathlib import Path

DEFAULT_DATA = {"bindings": {}}
MAX_HISTORY = 5000
# save() 去抖窗口：指令/轮询密集时合并写盘，退出时 flush 兜底
SAVE_DEBOUNCE_SECONDS = 60.0

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, path: Path, history_keep_days: int = 60) -> None:
        self.path = path
        self.history_keep_days = history_keep_days
        self.data = json.loads(json.dumps(DEFAULT_DATA))  # deep copy
        self._last_saved_at = 0.0
        self._dirty = False
        self._quarantine_failed = False
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return  # 首次运行尚无存储文件，正常
        except (OSError, ValueError) as e:
            # 断电半截 JSON 等损坏也落在这里——改名保留现场再重建，
            # 避免无声清零后无从诊断。
            logger.error(
                "存储文件读取失败（%s: %s）：%s", type(e).__name__, e, self.path
            )
            self._quarantine_corrupt()
            return
        if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
            self.data = data
        else:
            logger.error(
                "存储文件结构异常（bindings 不是 dict），已按空数据起步：%s",
                self.path,
            )
            self._quarantine_corrupt()

    def _quarantine_corrupt(self) -> None:
        """把损坏的存储文件改名保留（.corrupt-<ts>），再从头建空数据。

        改名失败（文件被占用等）不阻断运行：内存里的数据才是权威，下次保存
        会覆盖那个坏文件。但要留痕——否则现场被覆盖后再也查不出数据怎么坏的，
        所以只在首次失败时报一次错。
        """
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.path.rename(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
        except OSError as e:
            if not self._quarantine_failed:
                self._quarantine_failed = True
                logger.error(
                    "损坏存储文件改名失败（下次保存会覆盖 %s，原始现场将丢失）：%s",
                    self.path,
                    e,
                )

    def save(self, force: bool = False) -> None:
        """原子写：先写临时文件 → flush+fsync → os.replace 覆盖。

        fsync 是为了断电安全：os.replace 只保证目录项原子，断电时可能留下
        「已 replace 但内容还是空的」的文件，那就会走 load() 的损坏分支。

        force=False 时做 60 秒去抖：每条指令、每轮轮询都会 save()，多绑定
        场景下是每半小时重写几 MB；数据全部在内存、进程退出时会强制落盘，
        丢最多 60 秒的采样点不影响任何用户可见行为（历史只用于统计）。
        """
        now = time.time()
        if not force and (now - self._last_saved_at) < SAVE_DEBOUNCE_SECONDS:
            self._dirty = True
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            # 覆盖不了就保住旧文件，下次重试；别删 tmp 以外的任何东西。
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._last_saved_at = now
        self._dirty = False
        self._quarantine_failed = False

    def flush(self) -> bool:
        """把被去抖攒下的改动落盘。返回是否真的写了。"""
        if not self._dirty:
            return False
        self.save(force=True)
        return True

    # ---------- 绑定 ----------

    def get_binding(self, umo: str) -> dict | None:
        return self.data["bindings"].get(umo)

    def set_binding(self, umo: str, binding: dict) -> None:
        self.data["bindings"][umo] = binding

    def del_binding(self, umo: str) -> bool:
        return self.data["bindings"].pop(umo, None) is not None

    # ---------- 历史 ----------

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
