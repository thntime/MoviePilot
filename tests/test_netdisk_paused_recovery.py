"""
验证网盘任务暂停原因记录与进程重启后的暂停恢复。
"""

import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from app.application.netdisk.contract import NetdiskTuning
from app.modules.netdisk import NetdiskModule, store


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    把任务存储指向临时目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield


def _record(task_hash: str, state: str, **extra) -> dict:
    """
    构造一条可恢复的网盘任务记录。

    :param task_hash: 任务标识
    :param state: 任务状态
    :param extra: 额外字段
    :return: 任务记录
    """
    record = {
        "hash": task_hash,
        "state": state,
        "netdisk_platform": "uc",
        "share_url": f"https://drive.uc.cn/s/{task_hash}",
        "share_code": task_hash,
        "save_path": f"/tmp/{task_hash}",
        "passcode": "",
        "episodes": [5],
        "downloader": "网盘",
        "category": None,
    }
    record.update(extra)
    return record


def _module(monkeypatch: pytest.MonkeyPatch, started: List[tuple]) -> NetdiskModule:
    """
    构造已打桩下载线程的网盘模块。

    :param monkeypatch: pytest monkeypatch
    :param started: 记录恢复时启动的线程参数
    :return: 网盘模块
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    monkeypatch.setattr(NetdiskModule, "_instance_tuning", lambda *args: NetdiskTuning())
    monkeypatch.setattr(module, "_run_task", lambda *args, **kwargs: started.append(args))
    return module


def test_shutdown_paused_task_resumes_and_clears_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    重启中断的任务在启动时自动恢复，并清除暂停标记。
    """
    started: List[tuple] = []
    module = _module(monkeypatch, started)
    store.upsert_task("shutdown", _record(
        "shutdown", "paused", paused_reason="shutdown",
        paused_at=datetime.now(timezone.utc).isoformat(),
    ))

    module._reconcile_store_state()  # pylint: disable=protected-access

    assert [args[0] for args in started] == ["shutdown"]
    record = store.get_task("shutdown")
    assert record["state"] == "downloading"
    assert not record.get("paused_reason")
    assert not record.get("paused_at")


def test_user_paused_task_stays_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    用户主动暂停的任务重启后保持暂停，不自动恢复。
    """
    started: List[tuple] = []
    module = _module(monkeypatch, started)
    store.upsert_task("user", _record(
        "user", "paused", paused_reason="user",
        paused_at=datetime.now(timezone.utc).isoformat(),
    ))

    module._reconcile_store_state()  # pylint: disable=protected-access

    assert started == []
    assert store.get_task("user")["state"] == "paused"


def test_unrecoverable_paused_task_stays_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    不可恢复的暂停任务重启后保持暂停，不自动恢复。
    """
    started: List[tuple] = []
    module = _module(monkeypatch, started)
    store.upsert_task("bad", _record(
        "bad", "paused", paused_reason="unrecoverable",
        paused_at=datetime.now(timezone.utc).isoformat(),
    ))

    module._reconcile_store_state()  # pylint: disable=protected-access

    assert started == []
    assert store.get_task("bad")["state"] == "paused"


def test_stop_records_shutdown_pause_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    应用停止触发的中断按 shutdown 记录暂停原因与时间。
    """
    module = _module(monkeypatch, [])
    store.upsert_task("running", _record("running", "downloading"))
    module._stops["running"] = threading.Event()  # pylint: disable=protected-access

    module.stop()
    module._mark_task_paused("running")  # pylint: disable=protected-access

    record = store.get_task("running")
    assert record["state"] == "paused"
    assert record["paused_reason"] == "shutdown"
    assert record["paused_at"]


def test_stop_torrents_records_user_pause_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    用户暂停任务按 user 记录暂停原因与时间。
    """
    module = _module(monkeypatch, [])
    store.upsert_task("idle", _record("idle", "downloading"))

    assert module.stop_torrents("idle") is True

    record = store.get_task("idle")
    assert record["state"] == "paused"
    assert record["paused_reason"] == "user"
    assert record["paused_at"]


def test_unrecoverable_reason_written_when_resume_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    缺少恢复信息的下载中任务回落为不可恢复暂停。
    """
    started: List[tuple] = []
    module = _module(monkeypatch, started)
    store.upsert_task("broken", {
        "hash": "broken", "state": "downloading", "netdisk_platform": "uc",
        "share_code": "abc",
    })

    module._reconcile_store_state()  # pylint: disable=protected-access

    assert started == []
    record = store.get_task("broken")
    assert record["state"] == "paused"
    assert record["paused_reason"] == "unrecoverable"
    assert record["paused_at"]
