"""
验证网盘账号平均速度的 JSON 持久化适配器。
"""

import json
from pathlib import Path

from app.adapters.system.netdisk_speed import JsonNetdiskSpeedStore


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """速度文件不存在时返回空映射。"""
    store = JsonNetdiskSpeedStore(tmp_path / "speeds.json")
    assert store.load() == {}


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    """保存后可原样读回账号速度。"""
    store = JsonNetdiskSpeedStore(tmp_path / "speeds.json")
    store.save({7: 12.5, 9: 3.0})
    assert store.load() == {7: 12.5, 9: 3.0}


def test_corrupt_file_returns_empty(tmp_path: Path) -> None:
    """文件内容损坏时按空映射处理。"""
    path = tmp_path / "speeds.json"
    path.write_text("{broken", encoding="utf-8")
    assert JsonNetdiskSpeedStore(path).load() == {}


def test_save_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    """写入使用临时文件替换，且不残留临时文件。"""
    path = tmp_path / "speeds.json"
    store = JsonNetdiskSpeedStore(path)
    store.save({1: 1024.0})
    assert json.loads(path.read_text(encoding="utf-8")) == {"1": 1024.0}
    assert not path.with_suffix(".tmp").exists()
