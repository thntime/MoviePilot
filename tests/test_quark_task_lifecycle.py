"""
夸克下载器的任务登记与去重测试。

任务记录必须在 `download()` 返回前同步落盘：链路提交后会立刻回读 save_path 写下载历史。
"""

import threading
from unittest.mock import Mock

import pytest

from app.modules.quark import QuarkModule, store
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS


@pytest.fixture
def module(monkeypatch):
    """构造只带任务登记状态、且不真正下载的模块实例。"""
    instance = QuarkModule.__new__(QuarkModule)
    instance._tasks = {}
    instance._stops = {}
    instance._discarded = set()
    instance._lock = threading.RLock()
    client = Mock()
    client.name = "夸克网盘"
    instance.get_instance = Mock(return_value=client)
    monkeypatch.setattr(QuarkModule, "_run_task", lambda *args, **kwargs: None)
    instance._saved = {}

    def _dump(tasks):
        """用写入内容整体替换内存中的任务表，保持删除语义。"""
        instance._saved.clear()
        instance._saved.update(tasks)

    monkeypatch.setattr(store, "_load", lambda: dict(instance._saved))
    monkeypatch.setattr(store, "_dump", _dump)
    return instance


def test_download_registers_task_before_return(module, tmp_path) -> None:
    """download() 返回时任务记录与任务目录必须已经落盘。"""
    name, task_key, layout, error = module.download(
        content="https://pan.quark.cn/s/abc123",
        download_dir=str(tmp_path),
        cookie="",
        download_type="netdisk",
        netdisk_platform="quark",
    )

    assert error == ""
    assert task_key
    record = store.get_task(task_key)
    assert record is not None
    assert record["state"] == "downloading"
    assert record["path"] == str(tmp_path / "abc123")
    assert (tmp_path / "abc123").is_dir()
    assert layout is None


def test_download_reuses_active_task_with_same_share_code(module, tmp_path) -> None:
    """同一分享换一种链接形式提交时复用已有任务，不新建第二个任务。"""
    first = module.download(
        content="https://pan.quark.cn/s/abc123",
        download_dir=str(tmp_path),
        cookie="",
        download_type="netdisk",
        netdisk_platform="quark",
    )
    second = module.download(
        content="https://pan.quark.cn/s/abc123?pwd=1234#/list/share",
        download_dir=str(tmp_path),
        cookie="",
        download_type="netdisk",
        netdisk_platform="quark",
    )

    assert second[1] == first[1]
    assert second[3] == DOWNLOAD_TASK_ALREADY_EXISTS


def test_download_keeps_new_task_for_different_episodes(module, tmp_path) -> None:
    """同一分享补集时允许新建任务，目录复用且记录目标集。"""
    first = module.download(
        content="https://pan.quark.cn/s/abc123",
        download_dir=str(tmp_path),
        cookie="",
        download_type="netdisk",
        netdisk_platform="quark",
        episodes={153, 154},
    )
    store.upsert_task(first[1], {"state": "completed"})
    # 真实流程里 _run_task 结束时会把自己从运行表移除
    module._tasks.pop(first[1], None)
    second = module.download(
        content="https://pan.quark.cn/s/abc123",
        download_dir=str(tmp_path),
        cookie="",
        download_type="netdisk",
        netdisk_platform="quark",
        episodes={164},
    )

    assert second[3] == ""
    assert store.get_task(second[1])["episodes"] == [164]


def test_removed_task_is_not_resurrected(module, tmp_path) -> None:
    """删除任务后，后台线程收尾不得把记录写回去。"""
    _, task_key, _, _ = module.download(
        content="https://pan.quark.cn/s/abc123",
        download_dir=str(tmp_path),
        cookie="",
        download_type="netdisk",
        netdisk_platform="quark",
    )
    module.remove_torrents(task_key, delete_file=False)

    assert store.get_task(task_key) is None
    assert task_key in module._discarded


def test_reconcile_marks_orphan_running_task_as_paused(module) -> None:
    """进程重启后遗留的 downloading 记录必须回落为暂停，否则前端显示假任务。"""
    store.upsert_task("orphan", {"hash": "orphan", "state": "downloading", "dlspeed": "1M/s"})

    module._reconcile_store_state()

    record = store.get_task("orphan")
    assert record["state"] == "paused"
    assert record["dlspeed"] is None
    assert record["dlspeed_bytes"] == 0


def test_stop_orphan_task_still_updates_state(module) -> None:
    """线程已不在时点暂停，也要把记录改成暂停并返回成功。"""
    store.upsert_task("orphan", {"hash": "orphan", "state": "downloading"})

    assert module.stop_torrents("orphan") is True
    assert store.get_task("orphan")["state"] == "paused"
