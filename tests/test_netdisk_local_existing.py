"""
验证本地下载目录与媒体库已有完整文件的命中规则。
"""

from pathlib import Path

import pytest

from app.chain.download import local_existing
from app.chain.download.local_existing import find_local_complete


@pytest.fixture(autouse=True)
def _clear_cache():
    """
    每个用例前清空模块级短 TTL 缓存。
    """
    local_existing._CACHE.clear()
    yield
    local_existing._CACHE.clear()


def _episode_file(directory: Path, name: str, size: int = 100) -> Path:
    """写入带完成标记的集数文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"x" * size)
    target.with_name(target.name + ".done").touch()
    return target


def test_local_hit_in_download_dir(tmp_path: Path) -> None:
    """
    下载目录内存在覆盖全部目标集数的完整文件时返回首个命中文件。
    """
    target = _episode_file(tmp_path / "Example Show", "Example.Show.S01E01.mkv")

    hit = find_local_complete("Example Show", 1, {1}, [str(tmp_path)])

    assert hit is not None
    assert hit.in_library is False
    assert hit.path == target


def test_local_miss_when_shell_file(tmp_path: Path) -> None:
    """
    零字节空壳文件即使带完成标记也不视为完整。
    """
    directory = tmp_path / "Example Show"
    directory.mkdir(parents=True)
    target = directory / "Example.Show.S01E01.mkv"
    target.write_bytes(b"")
    target.with_name(target.name + ".done").touch()

    assert find_local_complete("Example Show", 1, {1}, [str(tmp_path)]) is None


def test_local_hit_from_mediaserver() -> None:
    """
    媒体库已存在目标剧集时直接命中，不再扫描下载目录。
    """
    hit = find_local_complete(
        "Example Show", 1, {1}, [],
        media_exists=lambda: True,
    )

    assert hit is not None
    assert hit.in_library is True
    assert hit.path is None
