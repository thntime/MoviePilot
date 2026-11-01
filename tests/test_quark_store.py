"""
验证网盘下载任务存储的读写、状态流转与损坏容错。

任务索引是进程重启后恢复任务的唯一来源，损坏或写入失败时只能降级，不能抛出。
"""

import json
from pathlib import Path

import pytest

from app.modules.quark import store


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    把任务索引文件指向临时配置目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))


def _store_file() -> Path:
    """
    返回当前隔离目录下的任务索引文件路径。
    """
    return store._store_path()  # pylint: disable=protected-access


def test_missing_store_reads_empty() -> None:
    """索引文件不存在时，读取接口都返回空值而不是报错。"""
    assert store.all_tasks() == []
    assert store.get_task("nope") is None
    assert store.find_by_share_code("abc") is None


def test_upsert_merges_fields_and_sets_metadata() -> None:
    """写入同一任务时合并字段，并补齐 hash 与更新时间。"""
    store.upsert_task("t1", {"title": "第一版", "state": "downloading"})
    store.upsert_task("t1", {"progress": 50.0})

    record = store.get_task("t1")
    assert record is not None
    assert record["title"] == "第一版"
    assert record["progress"] == 50.0
    assert record["hash"] == "t1"
    assert record["updated_at"] > 0


def test_update_progress_writes_state_size_and_speed() -> None:
    """进度更新同时刷新状态、大小与格式化速度。"""
    store.upsert_task("t1", {"state": "downloading", "progress": 0.0})

    store.update_progress("t1", 42.567, state="downloading", size=2048, dlspeed=1024 * 1024)

    record = store.get_task("t1")
    assert record is not None
    assert record["progress"] == 42.57
    assert record["size"] == 2048.0
    assert record["dlspeed_bytes"] == 1024 * 1024
    assert record["dlspeed"].endswith("/s")


def test_update_progress_keeps_optional_fields_when_omitted() -> None:
    """未传入的状态与大小保持原值，速度不允许为负。"""
    store.upsert_task("t1", {"state": "downloading", "size": 10.0, "progress": 1.0})

    store.update_progress("t1", 5.0, dlspeed=-10)

    record = store.get_task("t1")
    assert record is not None
    assert record["state"] == "downloading"
    assert record["size"] == 10.0
    assert record["dlspeed_bytes"] == 0


def test_update_progress_ignores_unknown_task() -> None:
    """任务不存在时进度更新直接返回，不创建空记录。"""
    store.update_progress("missing", 10.0)

    assert store.get_task("missing") is None


def test_find_by_share_code_matches_and_trims() -> None:
    """按分享码查找支持首尾空白，空分享码不参与匹配。"""
    store.upsert_task("t1", {"share_code": "abc123"})

    assert store.find_by_share_code(" abc123 ") is not None
    assert store.find_by_share_code("other") is None
    assert store.find_by_share_code("   ") is None


def test_remove_task_deletes_only_existing() -> None:
    """删除已存在的任务会同步落盘，删除不存在的任务不写文件。"""
    store.upsert_task("t1", {"state": "completed"})
    store.remove_task("t1")

    assert store.get_task("t1") is None
    store.remove_task("t1")
    assert _store_file().exists()


def test_corrupt_store_falls_back_to_empty() -> None:
    """索引文件不是合法 JSON 时降级为空表，并保留原文件等待人工处理。"""
    _store_file().write_text("{not json", encoding="utf-8")

    assert store.all_tasks() == []


def test_non_object_store_falls_back_to_empty() -> None:
    """索引顶层不是对象时同样降级为空表。"""
    _store_file().write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert store.all_tasks() == []


def test_dump_failure_is_logged_not_raised() -> None:
    """写入索引失败只记录告警，不能把异常抛给下载线程。"""
    # 让临时文件位置被同名目录占用，write_text 必然失败。
    _store_file().with_suffix(".tmp").mkdir()

    store.upsert_task("t1", {"state": "downloading"})

    assert store.get_task("t1") is None
