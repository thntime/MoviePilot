"""
验证网盘任务重新开始时复位对账标志。

同一分享二次提交会复用同一个任务 hash；若 `reconciled` 仍为 True，失败对账
会直接跳过它（reconcile.py 的循环开头），订阅集数不回滚、缺集被永久跳过。
"""

from typing import Any, Dict

import pytest

from app.modules.netdisk import store as netdisk_store
from app.modules.netdisk import NetdiskModule

_TASK = "2db84ae7bcec6a2b"


@pytest.fixture()
def fake_store(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Dict[str, Any]]:
    """
    用内存字典替换任务存储。

    :param monkeypatch: pytest 补丁工具
    :return: 任务 hash 到记录的映射
    """
    records: Dict[str, Dict[str, Any]] = {}

    def upsert_task(task_hash: str, record: Dict[str, Any]) -> None:
        """
        合并写入任务记录。

        :param task_hash: 任务标识
        :param record: 待写入字段
        """
        records.setdefault(task_hash, {}).update(record)

    def get_task(task_hash: str) -> Any:
        """
        读取任务记录。

        :param task_hash: 任务标识
        :return: 任务记录；不存在时返回 None
        """
        return records.get(task_hash)

    monkeypatch.setattr(netdisk_store, "upsert_task", upsert_task)
    monkeypatch.setattr(netdisk_store, "get_task", get_task)
    return records


def test_begin_task_resets_reconciled(fake_store: Dict[str, Dict[str, Any]]) -> None:
    """已对账的任务再次开始时，对账标志必须复位。"""
    netdisk_store.upsert_task(_TASK, {"state": "error", "error": "下载未完成", "reconciled": True})
    assert netdisk_store.get_task(_TASK)["reconciled"] is True

    NetdiskModule()._begin_netdisk_task(_TASK, {"title": "国剧 S01E01-E18"})

    record = netdisk_store.get_task(_TASK)
    assert record["reconciled"] is False
    assert record["state"] == "downloading"
    assert record["title"] == "国剧 S01E01-E18"
    assert fake_store[_TASK]["reconciled"] is False


def test_begin_task_keeps_new_task_clean(fake_store: Dict[str, Dict[str, Any]]) -> None:
    """全新任务开始时同样带 reconciled=False，不影响其它字段。"""
    NetdiskModule()._begin_netdisk_task("fresh-task", {"state": "error", "progress": 0.0})

    record = netdisk_store.get_task("fresh-task")
    assert record["reconciled"] is False
    assert record["state"] == "downloading"
    assert record["progress"] == 0.0
