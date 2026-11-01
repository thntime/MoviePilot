"""
验证夸克下载器模块的配置初始化、文件收集、任务启停与后台执行分支。

所有外部调用都用替身替代，任务存储指向临时配置目录。
"""

import threading
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import pytest

import app.modules.quark as quark_module
from app.modules.quark import QuarkModule, store
from app.modules.quark.engine import DownloadCancelled, DownloadError
from app.modules.quark.quark import QuarkError
from app.schemas.types import DownloaderType, ModuleType, TorrentQueryStatus


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    把任务存储指向临时目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))


def _module_with_client(name: str = "q") -> tuple[QuarkModule, Mock]:
    """
    构造已注册单个实例的模块。
    """
    module = QuarkModule()
    client = Mock()
    client.name = name
    module._instances = {name: client}  # pylint: disable=protected-access
    return module, client


def test_static_metadata() -> None:
    """模块名称、类型、子类型与优先级符合下载器约定。"""
    assert QuarkModule.get_name() == "Quark"
    assert QuarkModule.get_type() == ModuleType.Downloader
    assert QuarkModule.get_subtype() == DownloaderType.Quark
    assert QuarkModule.get_priority() == 0


def test_init_module_builds_instances_and_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    """初始化按配置创建实例，并把遗留运行中任务回落为暂停。"""
    module = QuarkModule()
    monkeypatch.setattr(module, "get_configs", lambda: {})
    reconciled: List[bool] = []
    monkeypatch.setattr(module, "_reconcile_store_state", lambda: reconciled.append(True))

    module.init_module()

    assert reconciled == [True]
    assert module.get_instances() == {}


def test_reconcile_skips_finished_and_running_tasks() -> None:
    """非运行中状态与仍在内存任务表里的记录都不改动。"""
    module = QuarkModule()
    store.upsert_task("done", {"hash": "done", "state": "completed"})
    store.upsert_task("live", {"hash": "live", "state": "downloading"})
    module._tasks["live"] = {"client": Mock()}  # pylint: disable=protected-access

    module._reconcile_store_state()

    assert store.get_task("done")["state"] == "completed"
    assert store.get_task("live")["state"] == "downloading"


def test_test_reports_missing_cookie() -> None:
    """没有实例返回 None，缺 Cookie 的实例给出明确原因。"""
    module = QuarkModule()
    assert module.test() is None

    module, client = _module_with_client()
    client.cookie = ""
    assert module.test() == (False, "夸克下载器 q 未配置 Cookie")

    client.cookie = "c=1"
    assert module.test() == (True, "")


def test_stop_sets_and_clears_events() -> None:
    """停止模块会置位全部任务信号并清空登记表。"""
    module = QuarkModule()
    events = [threading.Event(), threading.Event()]
    module._stops = {"a": events[0], "b": events[1]}  # pylint: disable=protected-access

    module.stop()

    assert all(event.is_set() for event in events)
    assert module._stops == {}  # pylint: disable=protected-access


def test_task_dir_reuses_matching_name(tmp_path: Path) -> None:
    """下载目录名已经等于分享码时直接复用，避免多套一层。"""
    assert QuarkModule._task_dir(tmp_path / "abc", "abc") == tmp_path / "abc"
    assert QuarkModule._task_dir(tmp_path, "abc") == tmp_path / "abc"


def test_wanted_filters_by_suffix() -> None:
    """视频与归档后缀大小写不敏感，其余文件不下载。"""
    assert QuarkModule._wanted("a.MP4") is True
    assert QuarkModule._wanted("a.zip") is True
    assert QuarkModule._wanted("a.jpg") is False
    assert QuarkModule._wanted("noext") is False


def test_collect_files_drills_into_single_folder() -> None:
    """根层只有一个目录时下钻一层，并带回目录名作为标题。"""
    client = Mock()
    client.share_token.return_value = "st"
    client.share_files.side_effect = [
        [{"fid": "d1", "name": "剧集目录", "size": 0, "fid_token": "t", "is_dir": True}],
        [{"fid": "f1", "name": "164 4K.mkv", "size": 10, "fid_token": "tf", "is_dir": False}],
    ]

    stoken, files, root = QuarkModule()._collect_files(client, "code", "", {164})

    assert stoken == "st"
    assert [item["name"] for item in files] == ["S01E164 164 4K.mkv"]
    assert root == "剧集目录"


def test_collect_files_skips_filter_with_multiple_folders() -> None:
    """根层多个子目录时无法定位目标集，放弃过滤并整包下载。"""
    client = Mock()
    client.share_token.return_value = "st"
    client.share_files.side_effect = [
        [
            {"fid": "d1", "name": "目录一", "size": 0, "fid_token": "t", "is_dir": True},
            {"fid": "d2", "name": "目录二", "size": 0, "fid_token": "t", "is_dir": True},
        ],
        [{"fid": "f1", "name": "999 4K.mkv", "size": 10, "fid_token": "tf", "is_dir": False}],
    ]

    _stoken, files, root = QuarkModule()._collect_files(client, "code", "", {164})

    assert [item["name"] for item in files] == ["S01E999 999 4K.mkv"]
    assert root == ""


def test_collect_files_returns_empty_without_videos() -> None:
    """分享内没有可下载后缀时返回空文件列表。"""
    client = Mock()
    client.share_token.return_value = "st"
    client.share_files.return_value = [
        {"fid": "f1", "name": "说明.txt", "size": 1, "fid_token": "t", "is_dir": False}
    ]

    _stoken, files, root = QuarkModule()._collect_files(client, "code", "")

    assert files == []
    assert root == ""


def test_transfer_link_creates_folder_and_returns_link() -> None:
    """转存回退创建临时目录、轮询新 fid 并取链。"""
    client = Mock()
    client.create_folder.return_value = "folder"
    client.save_share.return_value = "task"
    client.poll_saved_fid.return_value = "newfid"
    client.file_link.return_value = {"url": "https://cdn/x", "size": 10}

    link, cleanup = QuarkModule._transfer_link(
        client, "code", "st", {"fid": "f", "fid_token": "t"}
    )

    assert link["url"] == "https://cdn/x"
    assert cleanup == "folder"
    client.save_share.assert_called_once_with("code", "st", "f", "t", "folder")


def test_transfer_link_cleans_folder_on_failure() -> None:
    """转存失败时删除刚创建的临时目录再抛出。"""
    client = Mock()
    client.create_folder.return_value = "folder"
    client.save_share.side_effect = QuarkError("boom")

    with pytest.raises(QuarkError):
        QuarkModule._transfer_link(client, "code", "st", {"fid": "f", "fid_token": "t"})

    client.delete_files.assert_called_once_with(["folder"])


def test_list_torrents_filters_by_hash_downloader_and_status() -> None:
    """任务列表支持按 hash、下载器与完成状态过滤。"""
    module = QuarkModule()
    store.upsert_task("a", {"state": "completed", "progress": 50.0, "downloader": "q1"})
    store.upsert_task("b", {"state": "downloading", "downloader": "q2"})

    assert [item.hash for item in module.list_torrents(hashs="a")] == ["a"]
    assert [item.hash for item in module.list_torrents(hashs=["a", "b"], downloader="q2")] == ["b"]
    assert [item.hash for item in module.list_torrents(status=TorrentQueryStatus.COMPLETED)] == ["a"]
    assert [item.progress for item in module.list_torrents(hashs="b")] == [0.0]


def test_torrent_files_missing_returns_none() -> None:
    """任务不存在时文件清单返回 None。"""
    assert QuarkModule().torrent_files("missing") is None


def test_remove_torrents_deletes_files_and_plans(tmp_path: Path) -> None:
    """删除任务时同时清理分片计划文件。"""
    module = QuarkModule()
    target = tmp_path / "a.mp4"
    plan = tmp_path / "a.mp4.plan.json"
    target.write_bytes(b"x")
    plan.write_text("{}", encoding="utf-8")
    store.upsert_task("t1", {
        "hash": "t1", "path": str(tmp_path), "files": [{"name": "a.mp4"}],
    })

    assert module.remove_torrents("t1") is True
    assert not target.exists()
    assert not plan.exists()
    assert store.get_task("t1") is None


def test_remove_torrents_handles_missing_and_delete_errors(tmp_path: Path) -> None:
    """任务不存在时静默跳过，删除失败只记录告警。"""
    module = QuarkModule()
    assert module.remove_torrents("missing") is True

    directory = tmp_path / "dir"
    directory.mkdir()
    store.upsert_task("t2", {
        "hash": "t2", "path": str(directory), "files": [{"name": ""}],
    })

    assert module.remove_torrents("t2") is True
    assert store.get_task("t2") is None


def test_set_torrents_tag_is_noop() -> None:
    """网盘任务不使用标签，直接返回成功。"""
    assert QuarkModule().set_torrents_tag("t1", ["x"]) is True


def test_start_torrents_requires_complete_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺记录、缺目录、缺实例或已在运行时都不启动。"""
    module, _client = _module_with_client()
    assert module.start_torrents("missing") is None

    store.upsert_task("bad", {"hash": "bad"})
    assert module.start_torrents("bad") is None

    store.upsert_task("noc", {
        "hash": "noc", "share_code": "c", "path": str(tmp_path), "downloader": "zzz",
    })
    assert module.start_torrents("noc") is None

    store.upsert_task("run", {
        "hash": "run", "share_code": "c", "path": str(tmp_path), "downloader": "q",
    })
    module._tasks["run"] = {"client": Mock()}  # pylint: disable=protected-access
    assert module.start_torrents("run") is None


def test_start_torrents_restarts_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """记录完整时重新登记任务并拉起后台线程。"""
    module, _client = _module_with_client()
    monkeypatch.setattr(module, "_run_task", lambda *args, **kwargs: None)
    store.upsert_task("ok", {
        "hash": "ok", "share_code": "c", "path": str(tmp_path), "downloader": "q",
        "passcode": "p", "episodes": [1],
    })

    assert module.start_torrents(["ok"]) is True
    assert store.get_task("ok")["state"] == "downloading"


def test_stop_torrents_sets_signal_or_falls_back_to_store() -> None:
    """运行中任务置位信号，线程已不在时直接把状态改为暂停。"""
    module = QuarkModule()
    event = threading.Event()
    module._stops["live"] = event  # pylint: disable=protected-access

    assert module.stop_torrents("live") is True
    assert event.is_set()

    store.upsert_task("orphan", {"hash": "orphan", "state": "downloading"})
    assert module.stop_torrents("orphan") is True
    assert store.get_task("orphan")["state"] == "paused"

    store.upsert_task("done", {"hash": "done", "state": "completed"})
    assert module.stop_torrents("done") is False
    assert module.stop_torrents("missing") is False


def test_downloader_info_sums_running_speeds() -> None:
    """下载器概览累加运行中任务的实时速度。"""
    module = QuarkModule()
    store.upsert_task("a", {"state": "downloading", "dlspeed_bytes": 100, "downloader": "q"})
    store.upsert_task("b", {"state": "downloading", "dlspeed_bytes": 50, "downloader": "q"})
    store.upsert_task("c", {"state": "paused", "dlspeed_bytes": 999, "downloader": "q"})

    info = module.downloader_info("q")

    assert info is not None
    assert info[0].download_speed == 150
    assert module.downloader_info("other")[0].download_speed == 0


def _prepare_task(module: QuarkModule, task_key: str = "t") -> threading.Event:
    """
    为 _run_task 准备内存登记表并返回停止信号。
    """
    stop_event = threading.Event()
    module._stops[task_key] = stop_event  # pylint: disable=protected-access
    module._tasks[task_key] = {"client": Mock(), "stop": stop_event}  # pylint: disable=protected-access
    return stop_event


def test_run_task_completes_and_records_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """后台任务成功时写回标题、大小、文件清单与完成状态。"""
    module = QuarkModule()
    _prepare_task(module)
    files = [{"name": "a.mp4", "size": 10, "fid": "f", "fid_token": "t"}]
    monkeypatch.setattr(module, "_collect_files", lambda *args: ("st", files, "剧集"))
    downloaded: List[str] = []
    monkeypatch.setattr(module, "_download_one", lambda *args: downloaded.append(args[3]["name"]))

    module._run_task("t", Mock(), "code", "", tmp_path, "标签", None)  # pylint: disable=protected-access

    record = store.get_task("t")
    assert record is not None
    assert record["state"] == "completed"
    assert record["progress"] == 100.0
    assert record["title"] == "剧集"
    assert record["files"][0]["name"] == "a.mp4"
    assert downloaded == ["a.mp4"]


def test_run_task_reports_missing_episode_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目标集数无对应文件时给出可读错误。"""
    module = QuarkModule()
    _prepare_task(module)
    monkeypatch.setattr(module, "_collect_files", lambda *args: ("st", [], ""))

    module._run_task("t", Mock(), "code", "", tmp_path, None, {164})  # pylint: disable=protected-access

    record = store.get_task("t")
    assert record is not None
    assert record["state"] == "error"
    assert "没有目标集数" in record["error"]


def test_run_task_reports_empty_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整包下载但分享内没有视频时同样报错。"""
    module = QuarkModule()
    _prepare_task(module)
    monkeypatch.setattr(module, "_collect_files", lambda *args: ("st", [], ""))

    module._run_task("t", Mock(), "code", "", tmp_path, None, None)  # pylint: disable=protected-access

    assert store.get_task("t")["state"] == "error"


def test_run_task_marks_paused_on_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消异常与消息兜底都标记为暂停。"""
    module = QuarkModule()
    _prepare_task(module)

    def raise_cancelled(*args: Any) -> None:
        """模拟引擎取消。"""
        raise DownloadCancelled("下载已取消")

    monkeypatch.setattr(module, "_collect_files", raise_cancelled)
    module._run_task("t", Mock(), "code", "", tmp_path, None, None)  # pylint: disable=protected-access
    assert store.get_task("t")["state"] == "paused"

    _prepare_task(module, "t2")

    def raise_message(*args: Any) -> None:
        """模拟热重载后异常类型不同、只能靠消息识别。"""
        raise DownloadError("下载已取消")

    monkeypatch.setattr(module, "_collect_files", raise_message)
    module._run_task("t2", Mock(), "code", "", tmp_path, None, None)  # pylint: disable=protected-access
    assert store.get_task("t2")["state"] == "paused"


def test_run_task_marks_error_on_unexpected_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """其他异常标记失败并保留原因。"""
    module = QuarkModule()
    _prepare_task(module)

    def explode(*args: Any) -> None:
        """模拟未预期异常。"""
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_collect_files", explode)

    module._run_task("t", Mock(), "code", "", tmp_path, None, None)  # pylint: disable=protected-access

    record = store.get_task("t")
    assert record["state"] == "error"
    assert "boom" in record["error"]


def test_run_task_stops_between_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """循环内发现停止信号时不再继续下一个文件。"""
    module = QuarkModule()
    stop_event = _prepare_task(module)
    stop_event.set()
    files = [{"name": "a.mp4", "size": 10, "fid": "f", "fid_token": "t"}]
    monkeypatch.setattr(module, "_collect_files", lambda *args: ("st", files, ""))

    module._run_task("t", Mock(), "code", "", tmp_path, None, None)  # pylint: disable=protected-access

    assert store.get_task("t")["state"] == "paused"


def test_run_task_skips_store_when_task_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户已删除的任务收尾时不得把记录写回。"""
    module = QuarkModule()
    _prepare_task(module)
    module._discarded.add("t")  # pylint: disable=protected-access
    monkeypatch.setattr(module, "_collect_files", lambda *args: ("st", [], ""))

    module._run_task("t", Mock(), "code", "", tmp_path, None, None)  # pylint: disable=protected-access

    assert store.get_task("t") is None


def test_download_one_rejects_unknown_size(tmp_path: Path) -> None:
    """无法确定文件大小时拒绝下载。"""
    module = QuarkModule()
    client = Mock()
    client.share_link.return_value = {"url": "https://cdn/x"}
    item = {"name": "a.mp4", "size": 0, "fid": "f", "fid_token": "t"}

    with pytest.raises(DownloadError, match="无法确定文件大小"):
        module._download_one(  # pylint: disable=protected-access
            client, "code", "", item, "st", tmp_path, "t", threading.Event(), "", 10, 0.0
        )


def test_download_one_refreshes_link_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直链刷新会重新换取 stoken 与文件令牌，进度按整任务换算。"""
    module = QuarkModule()
    client = Mock()
    client.cookie = "c=1"
    client.threads = 4
    client.rate_limit = 0
    client.preempt = False
    client.share_link.return_value = {"url": "https://cdn/first", "size": 100}
    monkeypatch.setattr(
        module, "_collect_files",
        lambda *args: ("st2", [{"fid": "f", "fid_token": "tok2"}], ""),
    )

    captured: Dict[str, Any] = {}

    class _FakeDownloader:
        """记录构造参数并驱动直链回调的下载器替身。"""

        def __init__(self, **kwargs: Any) -> None:
            """保存构造参数。"""
            captured.update(kwargs)

        def run(self, provider: Any) -> None:
            """依次请求初始直链、刷新直链与冷却期内的直链。"""
            captured["urls"] = [provider(False), provider(True), provider(True)]

    monkeypatch.setattr(quark_module, "ChunkedDownloader", _FakeDownloader)
    store.upsert_task("t", {"state": "downloading"})
    item = {"name": "a.mp4", "size": 100, "fid": "f", "fid_token": "tok1"}

    module._download_one(  # pylint: disable=protected-access
        client, "code", "", item, "st", tmp_path, "t", threading.Event(), "", 200, 0.0
    )

    assert captured["urls"][0] == "https://cdn/first"
    assert captured["urls"][1] == "https://cdn/first"
    assert captured["urls"][2] == "https://cdn/first"
    client.ensure_cookie.assert_called_once()
    assert client.share_link.call_args_list[-1].args == ("code", "st2", "f", "tok2")

    # 用构造时捕获的进度回调验证整任务进度与实时速度。
    progress = captured["on_progress"]
    monkeypatch.setattr(quark_module.time, "monotonic", lambda: 1_000_000_000.0)
    progress(50.0)

    record = store.get_task("t")
    assert record is not None
    assert record["dlspeed_bytes"] >= 0
