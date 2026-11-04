"""
网盘下载任务的登记与持久化。

任务记录保存在配置目录下的单个 JSON 文件中，供下载器任务列表、整理流程与
进程重启后的恢复共用；所有读写都在进程内串行化。

首次读取时若只存在历史夸克任务索引 ``quark_download_tasks.json``，会把内容
复制到新索引后保留旧文件，避免升级后丢失在途任务。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.foundation import size as size_tools
from app.foundation.environment import get_config_path
from app.runtime.log import logger

_STORE_LOCK = threading.RLock()
_STORE_FILENAME = "netdisk_download_tasks.json"
_LEGACY_STORE_FILENAME = "quark_download_tasks.json"


def _store_path() -> Path:
    """
    返回任务记录的磁盘路径。

    :return: 配置目录下的任务索引文件
    """
    return get_config_path() / _STORE_FILENAME


def _legacy_store_path() -> Path:
    """
    返回历史夸克任务索引的磁盘路径。

    :return: 配置目录下的旧任务索引文件
    """
    return get_config_path() / _LEGACY_STORE_FILENAME


def _migrate_legacy() -> None:
    """
    把旧夸克任务索引复制为新索引，旧文件保留不删除。

    只在目标文件缺失时执行；旧文件损坏时按空表处理，不阻断启动。
    """
    target = _store_path()
    if target.exists():
        return
    legacy = _legacy_store_path()
    if not legacy.exists():
        return
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001
        logger.warn(f"历史网盘任务索引读取失败，跳过迁移：{err}")
        return
    if not isinstance(payload, dict):
        logger.warn("历史网盘任务索引不是对象，跳过迁移")
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(target)
        logger.info(f"已迁移历史网盘任务索引：{legacy.name} -> {target.name}")
    except Exception as err:  # noqa: BLE001
        logger.warn(f"迁移历史网盘任务索引失败：{err}")


def _load() -> Dict[str, Dict[str, Any]]:
    """
    读取全部任务记录，文件缺失或损坏时返回空字典。
    """
    _migrate_legacy()
    path = _store_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001
        logger.warn(f"网盘下载任务索引读取失败：{err}")
        return {}
    if not isinstance(payload, dict):
        return {}
    changed = False
    for task in payload.values():
        if isinstance(task, dict) and task.get("netdisk_platform") == "c139":
            task["netdisk_platform"] = "pan139"
            changed = True
    if changed:
        _dump(payload)
    return payload


def _dump(tasks: Dict[str, Dict[str, Any]]) -> None:
    """
    原子写回全部任务记录。
    """
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except Exception as err:  # noqa: BLE001
        logger.warn(f"网盘下载任务索引写入失败：{err}")


def all_tasks() -> List[Dict[str, Any]]:
    """
    返回全部任务记录的快照。

    :return: 任务记录列表
    """
    with _STORE_LOCK:
        return list(_load().values())


def get_task(task_hash: str) -> Optional[Dict[str, Any]]:
    """
    按任务标识读取一条记录。

    :param task_hash: 任务标识
    :return: 任务记录；不存在时为空
    """
    with _STORE_LOCK:
        return _load().get(task_hash)


def upsert_task(task_hash: str, record: Dict[str, Any]) -> None:
    """
    写入或更新一条任务记录。

    :param task_hash: 任务标识
    :param record: 任务字段
    """
    with _STORE_LOCK:
        tasks = _load()
        current = tasks.get(task_hash) or {}
        current.update(record)
        current["hash"] = task_hash
        current["updated_at"] = time.time()
        tasks[task_hash] = current
        _dump(tasks)


def update_progress(task_hash: str, progress: float, state: Optional[str] = None,
                    size: Optional[float] = None, dlspeed: Optional[float] = None) -> None:
    """
    更新任务进度与状态。

    :param task_hash: 任务标识
    :param progress: 完成百分比
    :param state: 业务状态；为空时保持原值
    :param size: 文件总大小；为空时保持原值
    :param dlspeed: 实时下载速度，单位字节每秒；为空时保持原值
    """
    with _STORE_LOCK:
        tasks = _load()
        record = tasks.get(task_hash)
        if not record:
            return
        record["progress"] = round(float(progress), 2)
        if state:
            record["state"] = state
        if size is not None:
            record["size"] = float(size)
        if dlspeed is not None:
            record["dlspeed_bytes"] = max(int(dlspeed), 0)
            record["dlspeed"] = f"{size_tools.format_compact_size(record['dlspeed_bytes'])}/s"
        record["updated_at"] = time.time()
        _dump(tasks)


def find_by_share_code(share_code: str) -> Optional[Dict[str, Any]]:
    """
    按分享码查找任务记录，用于同一分享不同链接形式的去重。

    :param share_code: 分享码
    :return: 匹配的任务记录；不存在时为空
    """
    code = str(share_code or "").strip()
    if not code:
        return None
    with _STORE_LOCK:
        for record in _load().values():
            if str(record.get("share_code") or "") == code:
                return record
    return None


def remove_task(task_hash: str) -> None:
    """
    删除一条任务记录。

    :param task_hash: 任务标识
    """
    with _STORE_LOCK:
        tasks = _load()
        if tasks.pop(task_hash, None) is not None:
            _dump(tasks)


def add_cleanup_handles(task_hash: str, handles: List[Dict[str, Any]]) -> None:
    """
    登记尚未清理的转存句柄，供进程重启后的补偿清理使用。

    :param task_hash: 任务标识
    :param handles: 句柄字段列表，按 directory_id 去重
    """
    if not handles:
        return
    with _STORE_LOCK:
        tasks = _load()
        record = tasks.get(task_hash)
        if not record:
            return
        current = {
            str(item.get("directory_id") or ""): item
            for item in record.get("cleanup_handles") or []
            if isinstance(item, dict)
        }
        for handle in handles:
            directory_id = str(handle.get("directory_id") or "")
            if directory_id:
                current[directory_id] = handle
        record["cleanup_handles"] = list(current.values())
        record["updated_at"] = time.time()
        _dump(tasks)


def remove_cleanup_handles(task_hash: str, directory_ids: List[str]) -> None:
    """
    删除已经完成清理的转存句柄登记。

    :param task_hash: 任务标识
    :param directory_ids: 已清理的云端目录标识
    """
    removed = {str(item) for item in directory_ids if item}
    if not removed:
        return
    with _STORE_LOCK:
        tasks = _load()
        record = tasks.get(task_hash)
        if not record:
            return
        record["cleanup_handles"] = [
            item for item in record.get("cleanup_handles") or []
            if isinstance(item, dict) and str(item.get("directory_id") or "") not in removed
        ]
        record["updated_at"] = time.time()
        _dump(tasks)


def pending_cleanup_handles() -> List[Dict[str, Any]]:
    """
    返回全部任务中尚未清理的转存句柄快照。

    :return: 含任务标识的句柄记录列表
    """
    with _STORE_LOCK:
        result: List[Dict[str, Any]] = []
        for task_hash, record in _load().items():
            for handle in record.get("cleanup_handles") or []:
                if isinstance(handle, dict):
                    result.append({"task_hash": task_hash, **handle})
        return result


def prune_terminal_tasks(max_age_seconds: float) -> int:
    """
    裁剪超过保留期的终态任务记录。

    :param max_age_seconds: 终态记录保留时长，单位秒
    :return: 被删除的记录数
    """
    if max_age_seconds <= 0:
        return 0
    deadline = time.time() - float(max_age_seconds)
    terminal_states = {"completed", "transferred", "error", "paused"}
    removed = 0
    with _STORE_LOCK:
        tasks = _load()
        for task_hash, record in list(tasks.items()):
            if str(record.get("state") or "") not in terminal_states:
                continue
            if float(record.get("updated_at") or 0) >= deadline:
                continue
            tasks.pop(task_hash, None)
            removed += 1
        if removed:
            _dump(tasks)
    return removed
