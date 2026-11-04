"""
验证网盘下载生命周期：目录回收、孤儿清理、转存兜底与正确性过滤。
"""

import json
import os
import time
from pathlib import Path

import pytest

import app.modules.netdisk as netdisk_module
from app.domain.netdisk import DirectLink, NetdiskPlatform, ShareFile, TransferHandle
from app.modules.netdisk import NetdiskModule, store
from app.modules.netdisk.engine import ChunkedDownloader, DownloadError


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    把任务存储指向临时目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield


def _file(name: str, file_id: str = "f1", size: int = 10) -> ShareFile:
    """
    构造分享文件条目。
    """
    return ShareFile(file_id=file_id, name=name, size=size)


def test_collect_files_filters_other_season() -> None:
    """
    S02E05 不能因为集号相同被当作 S01E05 下载。
    """
    files = [
        _file("剧名.S01E01.mkv", "a"),
        _file("剧名.S02E05.mkv", "b"),
    ]
    selected = NetdiskModule()._collect_files(files, {1, 5}, season=1)
    assert [item.name for item in selected] == ["剧名.S01E01.mkv"]


def test_collect_files_prefixes_duplicate_names() -> None:
    """
    递归目录拍平后的同名文件必须保留唯一前缀，不能静默覆盖。
    """
    files = [
        _file("01.mkv", "aaa111"),
        _file("01.mkv", "bbb222"),
    ]
    selected = NetdiskModule()._collect_files(files, None, season=1)
    names = [item.name for item in selected]
    assert len(names) == 2
    assert len(set(names)) == 2


def test_remove_torrents_removes_files_and_empty_task_dir(tmp_path: Path) -> None:
    """
    move 整理后的任务删除必须回收文件与空目录。
    """
    task_dir = tmp_path / "abc123"
    task_dir.mkdir()
    media = task_dir / "S01E01.mkv"
    media.write_bytes(b"data")
    module = NetdiskModule()
    store.upsert_task("hash1", {
        "state": "completed",
        "path": str(task_dir),
        "files": [{"name": media.name, "size": media.stat().st_size}],
    })

    assert module.remove_torrents("hash1", delete_file=True) is True
    assert not media.exists()
    assert not task_dir.exists()


def test_cleanup_orphans_removes_stale_empty_dir_and_skips_active(tmp_path: Path) -> None:
    """
    只清理超期且不在活动任务中的空目录。
    """
    root = tmp_path / "downloads"
    root.mkdir()
    stale = root / "abc123"
    stale.mkdir()
    (stale / ".netdisk-task").touch()
    active = root / "def456"
    active.mkdir()
    hidden = root / ".keep"
    hidden.mkdir()
    old = time.time() - 3 * 24 * 60 * 60
    for path in (stale, active, hidden):
        os.utime(path, (old, old))
    store.upsert_task("active", {
        "state": "downloading",
        "path": str(active),
        "files": [],
    })

    stats = NetdiskModule().cleanup_orphans(
        download_dirs=[str(root)],
        active_paths=[str(active)],
        empty_grace_seconds=60,
    )

    assert stats["empty_removed"] == 1
    assert not stale.exists()
    assert active.exists()
    assert hidden.exists()


def test_cleanup_orphans_prunes_terminal_records(tmp_path: Path) -> None:
    """
    终态任务记录超过保留期后由清理任务裁剪。
    """
    store.upsert_task("old", {"state": "completed", "files": []})
    path = store._store_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["old"]["updated_at"] = time.time() - 3600
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    stats = NetdiskModule().cleanup_orphans(
        download_dirs=[], record_ttl_seconds=60,
    )

    assert stats["records_removed"] == 1
    assert store.get_task("old") is None


def test_cleanup_links_continues_after_single_failure(tmp_path: Path) -> None:
    """
    单个转存句柄清理失败不能阻断其余句柄。
    """
    calls = []

    class Resolver:
        """记录清理调用的假解析服务。"""

        def cleanup(self, link: DirectLink) -> None:
            """按目录标识记录并让第一个句柄失败。"""
            calls.append(link.cleanup_handle.directory_id)
            if link.cleanup_handle.directory_id == "first":
                raise RuntimeError("boom")

    links = [
        DirectLink(
            url="", headers={},
            cleanup_handle=TransferHandle(
                platform=NetdiskPlatform.QUARK, account_id=1,
                directory_id="first", file_ids=("1",),
            ),
        ),
        DirectLink(
            url="", headers={},
            cleanup_handle=TransferHandle(
                platform=NetdiskPlatform.QUARK, account_id=1,
                directory_id="second", file_ids=("2",),
            ),
        ),
    ]

    NetdiskModule._cleanup_links(Resolver(), links, "task")
    assert calls == ["first", "second"]


def test_engine_skips_completed_file(tmp_path: Path) -> None:
    """
    目标文件已存在且大小匹配时，不重新下载。
    """
    target = tmp_path / "done.mkv"
    target.write_bytes(b"12345")
    progress = []
    downloader = ChunkedDownloader(
        target=target,
        total=5,
        links=[DirectLink(url="http://invalid.example/file", headers={})],
        on_progress=progress.append,
    )

    downloader.run(lambda refresh=False: downloader.links)

    assert progress == [100.0]


def test_cleanup_orphans_removes_transferred_nonempty_dir(tmp_path: Path) -> None:
    """
    非空任务目录在全部文件确认已整理且超过宽限期后被删除。
    """
    root = tmp_path / "downloads"
    root.mkdir()
    task = root / "abc123"
    task.mkdir()
    (task / ".netdisk-task").touch()
    media = task / "S01E01.mkv"
    media.write_bytes(b"data")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(task, (old, old))

    stats = NetdiskModule().cleanup_orphans(
        download_dirs=[str(root)],
        is_transferred=lambda path: path.endswith(".mkv"),
        empty_grace_seconds=60,
        nonempty_grace_seconds=60,
    )

    assert stats["transferred_removed"] == 1
    assert not task.exists()


def test_cleanup_orphans_skips_unmarked_user_dir(tmp_path: Path) -> None:
    """
    没有任务标记的用户目录即使为空也不会被定时清理。
    """
    root = tmp_path / "downloads"
    root.mkdir()
    user_dir = root / "我的备份"
    user_dir.mkdir()
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(user_dir, (old, old))

    stats = NetdiskModule().cleanup_orphans(
        download_dirs=[str(root)], empty_grace_seconds=60,
    )

    assert stats["empty_removed"] == 0
    assert user_dir.exists()


def test_cleanup_handles_persist_and_remove(tmp_path: Path) -> None:
    """
    转存句柄在任务记录中可持久化并在清理后移除。
    """
    store.upsert_task("task", {"state": "downloading", "files": []})
    store.add_cleanup_handles("task", [{
        "platform": "quark",
        "account_id": 1,
        "directory_id": "dir1",
        "file_ids": ["f1"],
    }])
    assert store.pending_cleanup_handles()[0]["directory_id"] == "dir1"
    store.remove_cleanup_handles("task", ["dir1"])
    assert store.pending_cleanup_handles() == []


def test_ensure_local_space_rejects_insufficient_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    本地磁盘余量不足时在下载前直接失败。
    """
    monkeypatch.setattr(
        netdisk_module.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 10})(),
    )
    with pytest.raises(DownloadError, match="本地磁盘余量不足"):
        NetdiskModule._ensure_local_space(tmp_path, 100)
