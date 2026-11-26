"""
网盘回收站定时清理的调度合同测试。

只验证任务注册形态与账号枚举隔离，不启动后台线程、不访问网络。
"""

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytz

from app.application.configuration import SchedulerRuntimeConfig
from app.application.netdisk.contract import NetdiskAccountView
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform
from app.scheduler import catalog as scheduler_catalog
from app.scheduler import chain as scheduler_chain
from app.scheduler.facade import Scheduler
from app.scheduler.registry import ExecutionRegistry


class _SchedulerStub:
    """
    记录 Scheduler 注册结果的最小调度器替身。
    """

    def __init__(self) -> None:
        """
        初始化空作业表。
        """
        self.jobs = {}

    def add_job(self, func, *, trigger, id, **kwargs) -> None:
        """
        按作业 ID 保存最近一次注册参数。
        """
        self.jobs[id] = {"func": func, "trigger": trigger, **kwargs}


def _scheduler() -> Scheduler:
    """
    构造不启动后台线程的 Scheduler。
    """
    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = _SchedulerStub()
    scheduler._jobs = {}
    scheduler._lock = threading.RLock()
    scheduler._lifecycle_state = "running"
    scheduler._registry = ExecutionRegistry(scheduler._lock)
    return scheduler


def _config(**changes) -> SchedulerRuntimeConfig:
    """
    构造回收站调度测试所需的最小配置快照。
    """
    config = SchedulerRuntimeConfig(
        dev=False,
        timezone="Asia/Shanghai",
        scheduler_workers=1,
        db_backup_enable=False,
        db_backup_cron="",
        cookiecloud_interval=None,
        mediaserver_sync_interval=None,
        subscribe_search=False,
        subscribe_search_interval=24,
        subscribe_mode="rss",
        subscribe_rss_interval=30,
        data_cleanup_enable=False,
        sitedata_refresh_interval=None,
        memory_gc_interval=None,
        ai_agent_enable=False,
        ai_agent_job_interval=None,
        usage_statistic_share=False,
        site_link=None,
        auto_update=False,
        auto_update_resource=False,
    )
    return replace(config, **changes)


def test_recycle_bin_job_registered_hourly_after_five_minutes(monkeypatch) -> None:
    """
    回收站清理任务每小时运行一次，启动后延迟 5 分钟首跑。
    """
    scheduler = _scheduler()
    scheduler._services = Mock()
    background_scheduler = Mock()
    monkeypatch.setattr(scheduler_catalog, "BackgroundScheduler", lambda **_kwargs: background_scheduler)
    monkeypatch.setattr(scheduler_catalog, "get_plugin_manager", lambda: Mock())
    monkeypatch.setattr(scheduler_catalog, "get_mediaserver_configs", lambda **_kwargs: [])
    monkeypatch.setattr(scheduler, "init_workflow_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "init_agent_task_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "init_plugin_jobs", lambda: None)

    before = datetime.now(pytz.timezone("Asia/Shanghai"))
    scheduler._initialize_catalog(_config())
    after = datetime.now(pytz.timezone("Asia/Shanghai"))

    calls = [
        call
        for call in background_scheduler.add_job.call_args_list
        if call.kwargs.get("id") == "netdisk_recycle_bin_clean"
    ]
    assert len(calls) == 1
    assert calls[0].kwargs["hours"] == 1
    next_run = calls[0].kwargs["next_run_time"]
    assert before + timedelta(minutes=4) < next_run < after + timedelta(minutes=6)
    assert "netdisk_recycle_bin_clean" in scheduler._jobs


def test_clear_recycle_bins_only_active_pan115_and_isolates_failures(monkeypatch) -> None:
    """
    只清理 115 且状态为 active 的账号；安全密钥跳过，真实异常计入失败。
    """
    views = [
        NetdiskAccountView(
            id=1, platform="pan115", name="甲", is_primary=False, status="active"
        ),
        NetdiskAccountView(
            id=2, platform="pan115", name="乙", is_primary=False, status="active"
        ),
        NetdiskAccountView(
            id=3, platform="pan115", name="丙", is_primary=False, status="active"
        ),
        NetdiskAccountView(
            id=4, platform="pan115", name="丁", is_primary=False, status="active"
        ),
        NetdiskAccountView(
            id=5, platform="pan115", name="戊", is_primary=False, status="expired"
        ),
    ]
    calls: list[int] = []

    class _Store:
        """
        账号仓储替身：断言平台过滤后返回固定账号。
        """

        def list_accounts(self, platform=None):
            """
            返回固定账号列表。
            """
            assert platform is NetdiskPlatform.PAN115
            return views

    class _Service:
        """
        账号服务替身：第二个账号真实异常，第三个账号开启安全密钥。
        """

        def __init__(self) -> None:
            """
            绑定仓储替身。
            """
            self.store = _Store()

        def clear_recycle_bin(self, account_id: int) -> dict:
            """
            记录调用并模拟失败、跳过与成功。
            """
            calls.append(account_id)
            if account_id == 2:
                raise RuntimeError("boom")
            if account_id == 3:
                raise NetdiskError(
                    NetdiskErrorCode.UNSUPPORTED,
                    "账号开启回收站安全密钥，暂不支持自动清空",
                    platform=NetdiskPlatform.PAN115,
                )
            return {"account_id": account_id, "cleared": True, "message": "回收站已清空"}

    monkeypatch.setattr(scheduler_chain, "NetdiskAccountService", lambda: _Service())
    chain = object.__new__(scheduler_chain.SchedulerChain)

    result = chain.clear_netdisk_recycle_bins()

    assert calls == [1, 2, 3, 4]
    assert result == {"checked": 4, "cleared": 2, "skipped": 1, "failed": 1}
