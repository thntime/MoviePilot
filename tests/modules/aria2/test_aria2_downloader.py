"""
aria2 下载器模块提交与本地任务登记回归。
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from torrentool.bencode import Bencode

import app.modules.aria2 as aria2_module
from app.adapters.cache.backends import FileBackend
from app.modules.aria2 import Aria2Module
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.types import TorrentQueryStatus

_DOWNLOADER = "test-downloader"


def _make_module(monkeypatch, tmp_path, server) -> Aria2Module:
    """
    构造只替换外部客户端的模块实例，并把登记缓存指向临时目录。
    """
    module = Aria2Module.__new__(Aria2Module)
    monkeypatch.setattr(module, "get_instance", lambda _name=None: server)
    monkeypatch.setattr(module, "normalize_path", lambda path, _name=None: path.as_posix())
    monkeypatch.setattr(module, "normalize_return_path", lambda path, _name=None: path.as_posix())
    monkeypatch.setattr(module, "get_default_config_name", lambda: _DOWNLOADER)
    monkeypatch.setattr(aria2_module, "_task_cache", lambda: FileBackend(tmp_path))
    return module


def _read_registry(tmp_path) -> dict:
    """
    读取测试临时目录中的本地任务登记。
    """
    raw = FileBackend(tmp_path).get("tasks", region="aria2_tasks")
    return json.loads(raw) if raw else {}


def _torrent_content() -> tuple[bytes, str]:
    """
    构造只在内存解析的合法种子与对应的 v1 infohash。
    """
    info = {"name": "Test.Show.S01E01.1080p.mkv", "piece length": 16384, "pieces": b"x" * 20, "length": 21}
    return Bencode.encode({"info": info}), hashlib.sha1(Bencode.encode(info)).hexdigest()


def test_download_uri_returns_content_hash(monkeypatch, tmp_path) -> None:
    """
    直链提交返回 40 位内容摘要、下载器名称并写入本地登记。
    """
    url = "https://example.com/Test.Show.S01E01.1080p.mkv"
    server = Mock()
    server.add_uri.return_value = "gid-1"
    server.save_session.return_value = True
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=url, download_dir=Path("/downloads"), cookie="uid=1",
        category="TV", label="tag1", downloader=_DOWNLOADER,
    )

    expected_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    assert result == (_DOWNLOADER, expected_hash, "Original", "添加下载任务成功")
    server.add_uri.assert_called_once()
    uris, options = server.add_uri.call_args.args
    assert uris == [url]
    assert options["dir"] == "/downloads/TV"
    assert options["header"] == ["Cookie: uid=1"]
    assert _read_registry(tmp_path)["gid-1"]["hash"] == expected_hash
    assert _read_registry(tmp_path)["gid-1"]["downloader"] == _DOWNLOADER
    assert "tag1" in _read_registry(tmp_path)["gid-1"]["tags"]
    server.save_session.assert_called_once()


def test_download_uri_uses_netdisk_label_as_stable_task_id(monkeypatch, tmp_path) -> None:
    """
    网盘直链通过 label 传入的稳定标识优先于内容摘要，且不进入任务标签。
    """
    url = "https://openlist.example.com/d/Media/x.mkv?sign=rotating"
    stable_id = "a" * 40
    server = Mock()
    server.add_uri.return_value = "gid-3"
    server.save_session.return_value = True
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=url, download_dir=Path("/downloads"), cookie="",
        label=f"netdisk:{stable_id},netdisk", downloader=_DOWNLOADER,
    )

    assert result[:2] == (_DOWNLOADER, stable_id)
    tags = _read_registry(tmp_path)["gid-3"]["tags"]
    assert "netdisk" in tags
    assert not any(tag.startswith("netdisk:") for tag in tags)


def test_download_torrent_bytes_uses_add_torrent(monkeypatch, tmp_path) -> None:
    """
    种子字节提交走 addTorrent，并用 infohash 作为任务标识。
    """
    content, expected_hash = _torrent_content()
    server = Mock()
    server.add_torrent.return_value = "gid-2"
    server.save_session.return_value = True
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=content, download_dir=Path("/downloads"), cookie="",
        downloader=_DOWNLOADER,
    )

    assert result[:3] == (_DOWNLOADER, expected_hash, "Original")
    server.add_torrent.assert_called_once()
    assert server.add_torrent.call_args.args[0] == content
    assert "pause" not in server.add_torrent.call_args.args[1]
    assert _read_registry(tmp_path)["gid-2"]["hash"] == expected_hash


def test_download_failure_returns_reason(monkeypatch, tmp_path) -> None:
    """
    客户端未返回 gid 时返回空任务标识和中文原因。
    """
    server = Mock()
    server.add_uri.return_value = None
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content="https://example.com/Test.Show.S01E01.mkv",
        download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )

    assert result[:3] == (None, None, None)
    assert result[3]
    assert _read_registry(tmp_path) == {}


def test_download_torrent_with_episodes_selects_files(monkeypatch, tmp_path) -> None:
    """
    种子任务按集数暂停提交，对齐文件索引后写入 select-file 并恢复任务。
    """
    content, _ = _torrent_content()
    server = Mock()
    server.add_torrent.return_value = "gid-4"
    server.save_session.return_value = True
    server.change_option.return_value = True
    server.unpause.return_value = True
    server.get_files.return_value = [
        {"index": "1", "path": "/downloads/Test.Show.S01E01.1080p.mkv",
         "length": "10", "completedLength": "0", "selected": "true"},
        {"index": "2", "path": "/downloads/Test.Show.S01E02.1080p.mkv",
         "length": "10", "completedLength": "0", "selected": "true"},
    ]
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=content, download_dir=Path("/downloads"), cookie="",
        episodes={2}, downloader=_DOWNLOADER,
    )

    assert result[:3] == (_DOWNLOADER, hashlib.sha1(Bencode.encode({
        "name": "Test.Show.S01E01.1080p.mkv", "piece length": 16384,
        "pieces": b"x" * 20, "length": 21,
    })).hexdigest(), "Original")
    assert server.add_torrent.call_args.args[1]["pause"] == "true"
    server.change_option.assert_called_once_with("gid-4", {"select-file": "2"})
    server.unpause.assert_called_once_with("gid-4")


def test_torrent_files_returns_relative_paths(monkeypatch, tmp_path) -> None:
    """
    文件列表去掉任务下载目录前缀，返回任务内相对路径。
    """
    task_hash = "c" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-5": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = {"dir": "/downloads"}
    server.get_files.return_value = [{
        "index": "1", "path": "/downloads/Test.Show/Test.Show.S01E01.mkv",
        "length": "100", "completedLength": "50", "selected": "true",
    }]
    module = _make_module(monkeypatch, tmp_path, server)

    files = module.torrent_files(tid=task_hash, downloader=_DOWNLOADER)

    assert files is not None
    assert files[0].name == "Test.Show/Test.Show.S01E01.mkv"
    assert files[0].size == 100
    assert files[0].progress == pytest.approx(50.0)


def test_list_torrents_ignores_unregistered_gid(monkeypatch, tmp_path) -> None:
    """
    只投影本地登记中的 gid，aria2 中陌生任务不进入结果。
    """
    task_hash = "a" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-1": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = {
        "gid": "gid-1", "status": "active", "totalLength": "100", "completedLength": "50",
        "downloadSpeed": "10", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": "/downloads/Test.Show.S01E01.mkv", "length": "100",
                   "completedLength": "50", "selected": "true"}],
        "bittorrent": {"info": {"name": "Test.Show.S01E01"}},
    }
    server.tell_stopped.return_value = [{
        "gid": "stranger", "status": "complete", "totalLength": "1", "completedLength": "1",
        "dir": "/downloads", "bittorrent": {"info": {"name": "Stranger"}},
    }]
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)

    assert torrents is not None
    assert len(torrents) == 1
    assert torrents[0].hash == task_hash
    server.tell_status.assert_called_once_with("gid-1")


def test_list_torrents_maps_complete_state(monkeypatch, tmp_path) -> None:
    """
    已完成任务映射为完成状态并给出 100% 进度。
    """
    task_hash = "b" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-3": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = {
        "gid": "gid-3", "status": "complete", "totalLength": "100", "completedLength": "100",
        "downloadSpeed": "0", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": "/downloads/Test.Show.S01E01.mkv", "length": "100",
                   "completedLength": "100", "selected": "true"}],
        "bittorrent": {"info": {"name": "Test.Show.S01E01"}},
    }
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)

    assert torrents is not None
    assert torrents[0].state == "completed"
    assert torrents[0].progress == pytest.approx(100.0)
    assert torrents[0].hash == task_hash
    assert torrents[0].tags == ""


def test_download_skips_existing_task_with_same_identity(monkeypatch, tmp_path) -> None:
    """
    同一稳定标识重复提交时直接复用已登记任务，不再调用 addUri。
    """
    url = "https://example.com/Test.Show.S01E01.1080p.mkv"
    task_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-existing": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = {"gid": "gid-existing", "status": "active"}
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=url, download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )

    assert result == (_DOWNLOADER, task_hash, "Original", DOWNLOAD_TASK_ALREADY_EXISTS)
    server.add_uri.assert_not_called()
    server.save_session.assert_not_called()
    assert set(_read_registry(tmp_path)) == {"gid-existing"}


def test_download_same_identity_twice_submits_once(monkeypatch, tmp_path) -> None:
    """
    同一标识连续提交两次只调用一次 addUri，第二次返回任务已存在说明。
    """
    url = "https://example.com/Test.Show.S01E02.1080p.mkv"
    task_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    server = Mock()
    server.add_uri.return_value = "gid-twice"
    server.save_session.return_value = True
    server.tell_status.return_value = {"gid": "gid-twice", "status": "active"}
    module = _make_module(monkeypatch, tmp_path, server)

    first = module.download(
        content=url, download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )
    second = module.download(
        content=url, download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )

    assert first == (_DOWNLOADER, task_hash, "Original", "添加下载任务成功")
    assert second == (_DOWNLOADER, task_hash, "Original", DOWNLOAD_TASK_ALREADY_EXISTS)
    server.add_uri.assert_called_once()
    server.save_session.assert_called_once()


def test_download_resubmits_when_registered_gid_is_gone(monkeypatch, tmp_path) -> None:
    """
    登记的 gid 已失效且没有终态快照时，清理登记并重新提交。
    """
    url = "https://example.com/Test.Show.S01E01.1080p.mkv"
    task_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-stale": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = None
    server.tell_stopped.return_value = []
    server.add_uri.return_value = "gid-new"
    server.save_session.return_value = True
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=url, download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )

    assert result[:2] == (_DOWNLOADER, task_hash)
    server.add_uri.assert_called_once()
    assert set(_read_registry(tmp_path)) == {"gid-new"}


def test_download_multiple_uris_submits_each_uri(monkeypatch, tmp_path) -> None:
    """
    多行直链逐条调用 addUri，登记为同一任务标识下的多个分片。
    """
    content = "https://example.com/Test.Show.S01E01.mkv\n\nhttps://example.com/Test.Show.S01E02.mkv\n"
    server = Mock()
    server.add_uri.side_effect = ["gid-multi-1", "gid-multi-2"]
    server.save_session.return_value = True
    module = _make_module(monkeypatch, tmp_path, server)

    result = module.download(
        content=content, download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )

    expected_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()
    assert result[:2] == (_DOWNLOADER, expected_hash)
    assert server.add_uri.call_count == 2
    assert [call.args[0] for call in server.add_uri.call_args_list] == [
        ["https://example.com/Test.Show.S01E01.mkv"],
        ["https://example.com/Test.Show.S01E02.mkv"],
    ]
    registry = _read_registry(tmp_path)
    assert set(registry) == {"gid-multi-1", "gid-multi-2"}
    assert {entry["hash"] for entry in registry.values()} == {expected_hash}
    assert {entry["part"] for entry in registry.values()} == {0, 1}
    assert {entry["part_total"] for entry in registry.values()} == {2}

    server.tell_status.side_effect = lambda gid: {"gid": gid, "status": "active"}
    duplicate = module.download(
        content=content, download_dir=Path("/downloads"), cookie="", downloader=_DOWNLOADER,
    )
    assert duplicate == (_DOWNLOADER, expected_hash, "Original", DOWNLOAD_TASK_ALREADY_EXISTS)
    assert server.add_uri.call_count == 2


def test_list_torrents_single_uri_uses_download_dir(monkeypatch, tmp_path) -> None:
    """
    单条直链任务以下载目录作为任务路径，标题仍保留文件名。
    """
    task_hash = "9" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-single": {
                "hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                "part": 0, "part_total": 1,
            },
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = {
        "gid": "gid-single", "status": "active", "totalLength": "100", "completedLength": "50",
        "downloadSpeed": "10", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": "/downloads/Test.Show.S01E01.mkv", "length": "100",
                   "completedLength": "50", "selected": "true"}],
    }
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)

    assert torrents is not None
    assert len(torrents) == 1
    assert Path(torrents[0].path) == Path("/downloads")
    assert Path(torrents[0].content_path) == Path("/downloads")
    assert torrents[0].save_path == "/downloads"
    assert torrents[0].title == "Test.Show.S01E01.mkv"
    assert torrents[0].size == 100


def test_list_torrents_merges_multiple_uris(monkeypatch, tmp_path) -> None:
    """
    同一任务标识下的多个分片合并为一条记录，大小按各分片汇总。
    """
    task_hash = "8" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-part-1": {"hash": task_hash, "tags": ["tag1"], "downloader": _DOWNLOADER,
                           "part": 0, "part_total": 2},
            "gid-part-2": {"hash": task_hash, "tags": ["tag1"], "downloader": _DOWNLOADER,
                           "part": 1, "part_total": 2},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.side_effect = lambda gid: {
        "gid": gid, "status": "active", "totalLength": "100", "completedLength": "50",
        "downloadSpeed": "10", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": f"/downloads/{gid}.mkv", "length": "100",
                   "completedLength": "50", "selected": "true"}],
    }
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)

    assert torrents is not None
    assert len(torrents) == 1
    torrent = torrents[0]
    assert torrent.hash == task_hash
    assert torrent.size == 200
    assert torrent.progress == pytest.approx(50.0)
    assert torrent.state == "downloading"
    assert torrent.title == "downloads"
    assert Path(torrent.path) == Path("/downloads")
    assert Path(torrent.content_path) == Path("/downloads")
    assert torrent.save_path == "/downloads"
    assert torrent.tags == "tag1"


def test_list_torrents_merges_partial_completion_state(monkeypatch, tmp_path) -> None:
    """
    只有全部分片完成时合并状态才是完成，部分完成仍取最落后的状态。
    """
    task_hash = "7" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-part-a": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                           "part": 0, "part_total": 2},
            "gid-part-b": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                           "part": 1, "part_total": 2},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    states = {"gid-part-a": "complete", "gid-part-b": "active"}
    server = Mock()
    server.tell_status.side_effect = lambda gid: {
        "gid": gid, "status": states[gid], "totalLength": "100",
        "completedLength": "100" if states[gid] == "complete" else "40",
        "downloadSpeed": "0", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": f"/downloads/{gid}.mkv", "length": "100",
                   "completedLength": "100" if states[gid] == "complete" else "40",
                   "selected": "true"}],
    }
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)
    assert torrents is not None
    assert len(torrents) == 1
    assert torrents[0].state == "downloading"
    assert torrents[0].progress == pytest.approx(70.0)

    states["gid-part-b"] = "complete"
    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)
    assert torrents is not None
    assert len(torrents) == 1
    assert torrents[0].state == "completed"
    assert torrents[0].progress == pytest.approx(100.0)


def test_list_torrents_merges_failed_part_as_failed(monkeypatch, tmp_path) -> None:
    """
    任一分片失败且没有完成快照时，合并后的任务状态为失败。
    """
    task_hash = "6" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-ok": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                       "part": 0, "part_total": 2},
            "gid-bad": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                        "part": 1, "part_total": 2},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.side_effect = lambda gid: {
        "gid": gid,
        "status": "complete" if gid == "gid-ok" else "error",
        "totalLength": "100", "completedLength": "100" if gid == "gid-ok" else "10",
        "downloadSpeed": "0", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": f"/downloads/{gid}.mkv", "length": "100",
                   "completedLength": "100" if gid == "gid-ok" else "10", "selected": "true"}],
    }
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)

    assert torrents is not None
    assert len(torrents) == 1
    assert torrents[0].state == "failed"


def test_list_torrents_merges_completed_snapshots(monkeypatch, tmp_path) -> None:
    """
    重启后各分片都查不到时，用各自快照合并为一条已完成任务。
    """
    task_hash = "5" * 40
    snapshot = {
        "status": "complete",
        "dir": "/downloads",
        "files": ["Test.Show.S01E01.mkv"],
        "totalLength": 100,
        "completedLength": 100,
    }
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-snap-1": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                           "part": 0, "part_total": 2, "snapshot": snapshot},
            "gid-snap-2": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER,
                           "part": 1, "part_total": 2, "snapshot": snapshot},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = None
    server.tell_stopped.return_value = []
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(
        status=TorrentQueryStatus.TRANSFER, downloader=_DOWNLOADER, include_all_tags=True,
    )

    assert torrents is not None
    assert len(torrents) == 1
    assert torrents[0].state == "completed"
    assert torrents[0].size == 200
    assert torrents[0].progress == pytest.approx(100.0)
    assert Path(torrents[0].path) == Path("/downloads")


def test_list_torrents_persists_completed_snapshot(monkeypatch, tmp_path) -> None:
    """
    观察到终态时把状态、目录、文件与长度写入本地登记，供重启后继续查询。
    """
    task_hash = "d" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-6": {"hash": task_hash, "tags": [], "downloader": _DOWNLOADER},
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = {
        "gid": "gid-6", "status": "complete", "totalLength": "100", "completedLength": "100",
        "downloadSpeed": "0", "uploadSpeed": "0", "dir": "/downloads",
        "files": [{"index": "1", "path": "/downloads/Test.Show/Test.Show.S01E01.mkv", "length": "100",
                   "completedLength": "100", "selected": "true"}],
        "bittorrent": {"info": {"name": "Test.Show"}},
    }
    module = _make_module(monkeypatch, tmp_path, server)

    module.list_torrents(downloader=_DOWNLOADER, include_all_tags=True)

    snapshot = _read_registry(tmp_path)["gid-6"]["snapshot"]
    assert snapshot == {
        "status": "complete",
        "dir": "/downloads",
        "files": ["Test.Show/Test.Show.S01E01.mkv"],
        "totalLength": 100,
        "completedLength": 100,
    }


def test_list_torrents_falls_back_to_completed_snapshot(monkeypatch, tmp_path) -> None:
    """
    aria2 重启后活动与已停止任务都查不到时，用本地快照投影已完成任务。
    """
    task_hash = "e" * 40
    FileBackend(tmp_path).set(
        "tasks",
        json.dumps({
            "gid-7": {
                "hash": task_hash,
                "tags": ["tag1"],
                "downloader": _DOWNLOADER,
                "snapshot": {
                    "status": "complete",
                    "dir": "/downloads",
                    "files": ["Test.Show/Test.Show.S01E01.mkv"],
                    "totalLength": 100,
                    "completedLength": 100,
                },
            },
        }).encode("utf-8"),
        region="aria2_tasks",
    )
    server = Mock()
    server.tell_status.return_value = None
    server.tell_stopped.return_value = []
    module = _make_module(monkeypatch, tmp_path, server)

    torrents = module.list_torrents(
        status=TorrentQueryStatus.TRANSFER, downloader=_DOWNLOADER, include_all_tags=True,
    )

    assert torrents is not None
    assert len(torrents) == 1
    torrent = torrents[0]
    assert torrent.state == "completed"
    assert torrent.progress == pytest.approx(100.0)
    assert torrent.size == 100
    assert torrent.hash == task_hash
    assert torrent.downloader == _DOWNLOADER
    assert torrent.tags == "tag1"
    assert Path(torrent.path) == Path("/downloads")
    assert Path(torrent.content_path) == Path("/downloads")

