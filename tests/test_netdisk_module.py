"""
验证通用网盘下载器模块的下载分流、任务抽象、取链编排与存储迁移。
"""

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.modules.netdisk as netdisk_module
from app.application.netdisk.contract import NetdiskTuning
from app.application.netdisk.resolve import ResolvedShare
from app.domain.netdisk import (
    DirectLink,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
    TransferHandle,
)
from app.modules.netdisk import NetdiskModule, _share_code, store
from app.modules.netdisk.engine import DownloadError
from app.schemas.types import DownloaderType, ModuleType, TorrentQueryStatus


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    把任务存储指向临时目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield


def test_share_code_parsing_across_platforms() -> None:
    """
    各平台分享链接都应解析出分享码，忽略锚点与查询参数。
    """
    assert _share_code("https://pan.quark.cn/s/abc123#/list/share") == "abc123"
    assert _share_code("https://pan.quark.cn/s/abc123?pwd=9527") == "abc123"
    assert _share_code("https://drive.uc.cn/s/uc123") == "uc123"
    assert _share_code("https://www.123pan.com/s/abc-def") == "abc-def"
    assert _share_code("https://pan.quark.cn/other") == ""


def test_module_metadata() -> None:
    """
    模块类型与子类型必须是通用网盘下载器。
    """
    assert NetdiskModule.get_name() == "Netdisk"
    assert NetdiskModule.get_type() == ModuleType.Downloader
    assert NetdiskModule.get_subtype() == DownloaderType.Netdisk
    assert NetdiskModule.get_priority() == 0


def test_download_ignores_non_netdisk() -> None:
    """
    非网盘内容不归本模块处理，必须返回 None 交给其他下载器。
    """
    module = NetdiskModule()
    assert module.download("magnet:?xt=urn:btih:abc", Path("/tmp"), "") is None
    assert module.download("magnet:?xt=urn:btih:abc", Path("/tmp"), "", download_type="torrent") is None


def test_download_rejects_unknown_platform() -> None:
    """
    无法归一化的平台直接返回明确错误。
    """
    module = NetdiskModule()
    result = module.download(
        "https://pan.quark.cn/s/abc", Path("/tmp"), "", download_type="netdisk",
        netdisk_platform="unknown",
    )
    assert result is not None
    assert "暂不支持" in result[3]


def test_download_rejects_platform_mismatch() -> None:
    """
    分享链接与候选平台不一致时拒绝下载。
    """
    module = NetdiskModule()
    result = module.download(
        "https://pan.quark.cn/s/abc", Path("/tmp"), "", download_type="netdisk",
        netdisk_platform="uc",
    )
    assert result is not None
    assert result[3] == "分享链接与候选平台不一致"


def test_download_requires_instance() -> None:
    """
    未配置网盘下载器实例时返回明确错误。
    """
    module = NetdiskModule()
    result = module.download(
        "https://pan.quark.cn/s/abc", Path("/tmp"), "", download_type="netdisk",
        netdisk_platform="quark",
    )
    assert result is not None
    assert result[3] == "未配置网盘下载器实例"


def test_download_accepts_share_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    候选透传的分享令牌不应影响任务提交。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    monkeypatch.setattr(NetdiskModule, "_run_task", lambda *args, **kwargs: None)

    result = module.download(
        "https://pan.quark.cn/s/abc", tmp_path, "", download_type="netdisk",
        netdisk_platform="quark", netdisk_share_token="token",
    )

    assert result is not None
    assert result[0] == "网盘"
    assert result[1]
    record = store.get_task(result[1])
    assert record is not None
    assert record["netdisk_platform"] == "quark"
    assert record["share_token"] == "token"


def test_task_store_and_list_torrents() -> None:
    """
    任务写入后应能被任务列表与文件清单读到，删除后消失。
    """
    module = NetdiskModule()
    store.upsert_task("t1", {
        "title": "quark:abc",
        "name": "abc",
        "downloader": "q",
        "state": "completed",
        "progress": 100.0,
        "size": 1024.0,
        "path": "/tmp",
        "files": [{"name": "a.mp4", "size": 1024, "progress": 100.0}],
    })

    transfer = module.list_torrents(status=TorrentQueryStatus.TRANSFER)
    assert transfer is not None
    assert [item.hash for item in transfer] == ["t1"]
    assert transfer[0].progress == 100.0

    files = module.torrent_files("t1")
    assert files is not None
    assert files[0].name == "a.mp4"

    module.transfer_completed("t1")
    assert store.get_task("t1")["state"] == "transferred"

    assert module.remove_torrents("t1") is True
    assert store.get_task("t1") is None


def test_list_torrents_filters_unfinished() -> None:
    """
    未完成的任务不出现在整理查询结果中。
    """
    module = NetdiskModule()
    store.upsert_task("t2", {"state": "downloading", "progress": 10.0, "files": []})
    assert module.list_torrents(status=TorrentQueryStatus.TRANSFER) == []


def test_store_migrates_legacy_quark_index() -> None:
    """
    旧夸克索引存在而新索引缺失时复制内容，并保留旧文件。
    """
    legacy = store._legacy_store_path()  # pylint: disable=protected-access
    legacy.write_text(
        '{"old": {"hash": "old", "state": "paused", "share_code": "abc"}}',
        encoding="utf-8",
    )

    tasks = store.all_tasks()

    assert [task["hash"] for task in tasks] == ["old"]
    assert legacy.exists()
    assert store._store_path().exists()  # pylint: disable=protected-access


class FakeResolver:
    """
    提供固定分享内容的取链服务替身。
    """

    def __init__(self, files: List[ShareFile], links: tuple[DirectLink, ...]) -> None:
        """
        保存文件与直链。
        """
        self.tuning = NetdiskTuning(threads=2, multi_source=True)
        self._files = files
        self._links = links
        self.resolve_calls: List[Dict[str, Any]] = []
        self.cleanup_calls: List[DirectLink] = []

    def walk(self, url: str, password: Optional[str], platform: NetdiskPlatform,
             max_depth: int = 3, share_token: str = "") -> ResolvedShare:
        """
        返回固定文件列表。
        """
        del max_depth, share_token
        return ResolvedShare(
            ref=ShareRef(platform=platform, share_id="abc", password=password),
            detail=ShareDetail(title="剧名"),
            files=tuple(self._files),
        )

    def resolve_file(self, url: str, password: Optional[str], file: ShareFile,
                     platform: NetdiskPlatform = None, *,
                     multi_source: bool = False,
                     force_refresh: bool = False,
                     share_token: str = "") -> tuple[DirectLink, ...]:
        """
        记录多源与刷新参数并返回固定直链。
        """
        del url, password, platform, share_token
        self.resolve_calls.append({
            "name": file.name,
            "multi_source": multi_source,
            "force_refresh": force_refresh,
        })
        return self._links

    def cleanup(self, link: DirectLink) -> None:
        """
        记录清理调用。
        """
        self.cleanup_calls.append(link)


class FakeChunkedDownloader:
    """
    不访问网络的分片下载器替身。
    """

    calls: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        """
        保存构造参数。
        """
        self.kwargs = kwargs
        FakeChunkedDownloader.calls.append(kwargs)

    def run(self, url_provider) -> None:
        """
        写出指定长度的内容并回报完成。
        """
        target = self.kwargs["target"]
        target.write_bytes(b"d" * int(self.kwargs["total"]))
        callback = self.kwargs.get("on_progress")
        if callback:
            callback(100.0)


class FakeHlsDownloader:
    """
    不访问网络的 HLS 下载器替身。
    """

    calls: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        """
        保存构造参数。
        """
        self.kwargs = kwargs
        FakeHlsDownloader.calls.append(kwargs)

    def run(self, url_provider) -> None:
        """
        写出固定内容并回报完成。
        """
        self.kwargs["target"].write_bytes(b"hls")
        callback = self.kwargs.get("on_progress")
        if callback:
            callback(100.0)


def test_run_task_filters_episodes_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    后台任务应过滤集数、补集数前缀、多源取链并在结束后清理转存内容。
    """
    files = [
        ShareFile(file_id="1", name="01 4K.mkv", size=4),
        ShareFile(file_id="2", name="02 4K.mkv", size=4),
        ShareFile(file_id="3", name="花絮.mp4", size=4),
    ]
    handle = TransferHandle(platform=NetdiskPlatform.QUARK, account_id=1, directory_id="d")
    links = (DirectLink(url="https://cdn/x", headers={"Cookie": "c"}, size=4,
                        cleanup_handle=handle),)
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: resolver)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", FakeChunkedDownloader)
    FakeChunkedDownloader.calls = []
    module = NetdiskModule()

    module._run_task(  # pylint: disable=protected-access
        "t1", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, {1},
    )

    record = store.get_task("t1")
    assert record is not None
    assert record["state"] == "completed"
    assert [item["name"] for item in record["files"]] == ["S01E001 01 4K.mkv"]
    assert resolver.resolve_calls[0]["multi_source"] is True
    assert len(resolver.cleanup_calls) == 1
    assert (tmp_path / "S01E001 01 4K.mkv").read_bytes() == b"dddd"


def test_run_task_uses_hls_downloader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    DirectLink.is_hls 为真时走 HLS 下载器。
    """
    files = [ShareFile(file_id="1", name="video.mp4", size=0)]
    links = (DirectLink(url="https://cdn/index.m3u8", headers={}, size=0, is_hls=True),)
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: resolver)
    monkeypatch.setattr(netdisk_module, "HlsDownloader", FakeHlsDownloader)
    FakeHlsDownloader.calls = []
    module = NetdiskModule()

    module._run_task(  # pylint: disable=protected-access
        "t2", NetdiskPlatform.UC, "https://drive.uc.cn/s/abc", "abc", "",
        tmp_path, None, None,
    )

    assert FakeHlsDownloader.calls
    assert store.get_task("t2")["state"] == "completed"
    assert (tmp_path / "video.mp4").read_bytes() == b"hls"


def test_run_task_applies_instance_tuning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    下载器实例覆盖的线程、限速、抢占与多源参数必须传到引擎。
    """
    files = [ShareFile(file_id="1", name="video.mp4", size=4)]
    links = (DirectLink(url="https://cdn/x", headers={}, size=4),)
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: resolver)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", FakeChunkedDownloader)
    FakeChunkedDownloader.calls = []
    module = NetdiskModule()
    tuning = NetdiskTuning(
        threads=7,
        rate_limit=123,
        preempt_enabled=False,
        multi_source=False,
    )

    module._run_task(  # pylint: disable=protected-access
        "t3", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", tuning,
    )

    kwargs = FakeChunkedDownloader.calls[0]
    assert kwargs["threads"] == 7
    assert kwargs["rate_limit_bytes"] == 123
    assert kwargs["preempt"] is False
    assert resolver.resolve_calls[0]["multi_source"] is False


def test_list_torrents_exposes_netdisk_source_and_error() -> None:
    """
    任务列表应携带网盘平台、分享码与最近一次失败原因，供前端展示来源与失败详情。
    """
    module = NetdiskModule()
    store.upsert_task("t6", {
        "state": "error",
        "error": "分片 3 重试次数超限：连接超时",
        "netdisk_platform": "xunlei",
        "share_code": "VP5RkWv5",
    })

    tasks = module.list_torrents()

    assert tasks is not None
    assert tasks[0].netdisk_platform == "xunlei"
    assert tasks[0].share_code == "VP5RkWv5"
    assert tasks[0].error == "分片 3 重试次数超限：连接超时"


class _FailingResolver:
    """
    walk 直接抛出内部错误的取链服务替身。
    """

    tuning = NetdiskTuning()

    def walk(self, *args: Any, **kwargs: Any) -> Any:
        """
        模拟分享解析失败。
        """
        raise DownloadError("无法获取分享文件列表")


def test_run_task_reports_internal_failure_as_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    没有外部停止信号时，内部异常必须落为失败，不能被前端当成自动暂停。
    """
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: _FailingResolver())
    module = NetdiskModule()
    store.upsert_task("t7", {"state": "downloading", "dlspeed": "1 MiB/s"})

    module._run_task(  # pylint: disable=protected-access
        "t7", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None,
    )

    record = store.get_task("t7")
    assert record is not None
    assert record["state"] == "error"
    assert "无法获取分享文件列表" in record["error"]
    assert record["dlspeed"] is None


def test_run_task_reports_stop_signal_as_paused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    外部停止信号置位时，即使底层抛出任意异常也应记录为暂停。
    """
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: _FailingResolver())
    module = NetdiskModule()
    stop_event = threading.Event()
    stop_event.set()
    module._stops["t8"] = stop_event
    store.upsert_task("t8", {"state": "downloading"})

    module._run_task(  # pylint: disable=protected-access
        "t8", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None,
    )

    record = store.get_task("t8")
    assert record is not None
    assert record["state"] == "paused"
    assert record["error"] is None
