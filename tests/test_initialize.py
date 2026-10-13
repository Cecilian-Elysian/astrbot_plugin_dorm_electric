"""initialize() 装配链路测试：存储落位、调度任务、仪表盘注册、退出清理。

此前 initialize 从未被测试覆盖（v1.2.1 审查指出的盲区）：它串起
Store/HjnuProvider/AsyncIOScheduler/仪表盘注册/启动轮询五件事，
任何一环配置读取错了都只会在服务器上第一次启动时炸。

注意：initialize 创建的调度任务/启动轮询都绑在当时的 asyncio loop 上，
断言与 terminate 必须在同一个 loop 里做（拆成两次 asyncio.run 会撞
「Event loop is closed」），所以全部走 _lifecycle 驱动。
"""

import asyncio
from pathlib import Path

import pytest
from astrbot_plugin_dorm_electric.main import PLUGIN_NAME, DormElectricPlugin
from conftest import make_plugin


class _FakeContext:
    def __init__(self):
        self.registered: list[tuple] = []

    def register_web_api(self, path, handler, methods, desc):
        self.registered.append((path, handler, methods, desc))


@pytest.fixture()
def data_path(tmp_path, monkeypatch):
    """把 get_astrbot_data_path 指到临时目录，避免测试写真实 data/。"""
    import astrbot_plugin_dorm_electric.main as main_mod

    monkeypatch.setattr(main_mod, "get_astrbot_data_path", lambda: str(tmp_path))
    return tmp_path


def _lifecycle(plugin, check):
    """initialize → 同 loop 内断言 → terminate。"""

    async def _main():
        await plugin.initialize()
        try:
            check(plugin)
        finally:
            await plugin.terminate()

    asyncio.run(_main())


def test_initialize_wires_store_provider_jobs_and_dashboard(data_path):
    ctx = _FakeContext()

    def check(plugin: DormElectricPlugin):
        # 存储落位在 plugin_data 下，可写
        assert plugin.store is not None
        assert plugin.store.path.parent.name == PLUGIN_NAME
        assert plugin.hjnu is not None
        # 两个调度任务都在
        jobs = {job.id for job in plugin.scheduler.get_jobs()}
        assert jobs == {"poll", "daily"}
        # 仪表盘两个端点已注册
        paths = [r[0] for r in ctx.registered]
        assert f"/{PLUGIN_NAME}/dashboard/overview" in paths
        assert f"/{PLUGIN_NAME}/dashboard/history" in paths
        # 启动轮询任务已挂
        assert len(plugin._tasks) == 1

    plugin = make_plugin(
        config={"hjnu_cookie": "JSESSIONID=x", "poll_interval_minutes": 20},
        context=ctx,
    )
    _lifecycle(plugin, check)


def test_initialize_respects_disabled_poll_and_daily(data_path):
    def check(plugin: DormElectricPlugin):
        jobs = {job.id for job in plugin.scheduler.get_jobs()}
        assert jobs == set()  # 全关时不排任何任务

    plugin = make_plugin(
        config={"poll_interval_minutes": 0, "daily_report": False},
        context=_FakeContext(),
    )
    _lifecycle(plugin, check)


def test_initialize_uses_history_keep_days_for_store(data_path):
    def check(plugin: DormElectricPlugin):
        assert plugin.store.history_keep_days == 30

    plugin = make_plugin(config={"history_keep_days": 30}, context=_FakeContext())
    _lifecycle(plugin, check)


def test_initialize_then_terminate_is_clean(data_path):
    """initialize → terminate 全链路不抛（热重载场景，AstrBot 会反复调）。"""

    def check(plugin: DormElectricPlugin):
        assert plugin._tasks  # 启动轮询任务在挂

    plugin = make_plugin(context=_FakeContext())
    _lifecycle(plugin, check)
    assert plugin._tasks == []
    assert not list(Path(data_path).rglob("*.tmp"))
