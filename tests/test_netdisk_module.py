"""
验证通用网盘下载器模块的下载分流、任务抽象、取链编排与存储迁移。
"""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.modules.netdisk as netdisk_module
from app.adapters.external.netdisk.pan115 import Pan115Adapter
from app.api.endpoints import netdisk as netdisk_api
from app.application.netdisk import registry, share_blacklist
from app.application.netdisk.batch_judge import BatchDecision
from app.application.netdisk.contract import NetdiskTuning
from app.application.netdisk.resolve import ResolvedShare
from app.application.netdisk.share_blacklist import ShareBlacklistStore
from app.domain.netdisk import (
    DirectLink,
    NetdiskError,
    NetdiskErrorCode,
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


@pytest.fixture(autouse=True)
def _skip_ffprobe(monkeypatch: pytest.MonkeyPatch):
    """
    单测不调用真实 ffprobe：默认视为探测不到时长，需要时由用例自行覆盖。
    """
    monkeypatch.setattr(netdisk_module, "_probe_video_duration", lambda _target: None)
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


def test_download_blocked_by_share_blacklist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    已拉黑的分享在入口直接拒绝，且不创建下载任务。
    """
    blacklist = ShareBlacklistStore(tmp_path / "blacklist.json")
    blacklist.add("quark", "abc", "UC 直链不完整（1/2）")
    monkeypatch.setattr(share_blacklist, "_store", blacklist)

    module = NetdiskModule()
    result = module.download(
        "https://pan.quark.cn/s/abc", tmp_path, "", download_type="netdisk",
        netdisk_platform="quark",
    )

    assert result is not None
    assert "黑名单" in result[3]
    assert store.find_by_share_code("abc") is None


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


def test_download_accepts_episode_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    季级目标应被下载入口接受并透传给后台任务。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    started = threading.Event()
    recorded: Dict[str, Any] = {}

    def fake_run_task(*args: Any, **kwargs: Any) -> None:
        """记录后台任务参数并立即结束。"""
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        started.set()

    monkeypatch.setattr(module, "_run_task", fake_run_task)

    result = module.download(
        "https://pan.quark.cn/s/abc", tmp_path, "", download_type="netdisk",
        netdisk_platform="quark", episode_targets={1: {1}},
    )

    assert result is not None
    assert result[1]
    assert started.wait(2)
    assert recorded["args"][-1] == {1: {1}}
    record = store.get_task(result[1])
    assert record is not None
    assert record["episodes"] == [1]
    assert record["episode_targets"] == {"1": [1]}


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
    assert module.list_torrents() == []
    assert [item.hash for item in module.list_torrents(hashs="t1")] == ["t1"]

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
                     share_token: str = "",
                     concurrency: int = 1,
                     exclude_directories: tuple[str, ...] = (),
                     account_id: Optional[int] = None) -> tuple[DirectLink, ...]:
        """
        记录多源与刷新参数并返回固定直链。
        """
        del url, password, platform, share_token
        self.resolve_calls.append({
            "name": file.name,
            "multi_source": multi_source,
            "force_refresh": force_refresh,
            "concurrency": concurrency,
            "exclude_directories": exclude_directories,
            "account_id": account_id,
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
        total = int(self.kwargs["total"])
        target.write_bytes(b"d" * total)
        target.with_name(target.name + ".done").write_text(str(total), encoding="utf-8")
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
        target = self.kwargs["target"]
        target.write_bytes(b"hls")
        target.with_name(target.name + ".done").write_text("0", encoding="utf-8")
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
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", FakeChunkedDownloader)
    FakeChunkedDownloader.calls = []
    module = NetdiskModule()

    module._run_task(  # pylint: disable=protected-access
        "t1", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, {1}, "", None, 1,
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
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
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
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
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


def test_run_task_gives_each_file_full_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    多文件并发时 threads 是每个文件的分片并发数，不能被 workers 均分。
    """
    files = [
        ShareFile(file_id=str(index), name=f"{index:02d}.mkv", size=1)
        for index in range(3)
    ]
    links = tuple(
        DirectLink(url=f"https://cdn/{index}", headers={}, size=1)
        for index in range(3)
    )
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()
    tuning = NetdiskTuning(threads=12, max_tasks=3)
    seen: List[int] = []
    lock = threading.Lock()

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """记录每个文件收到的分片并发数，并写出完成标记。"""
        item = args[4]
        target = Path(args[5]) / item.name
        target.write_bytes(b"x" * int(item.size or 0))
        target.with_name(target.name + ".done").write_text(
            str(item.size or 0), encoding="utf-8"
        )
        with lock:
            seen.append(kwargs["threads"])

    monkeypatch.setattr(module, "_download_one", fake_download)

    module._run_task(  # pylint: disable=protected-access
        "t-threads", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", tuning,
    )

    assert sorted(seen) == [12, 12, 12]


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


class _ShareLevelFailResolver:
    """
    walk 抛出分享级错误的取链服务替身。
    """

    tuning = NetdiskTuning()

    def walk(self, *args: Any, **kwargs: Any) -> Any:
        """
        模拟分享内容不可用（仅预览流）。
        """
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "仅预览流")


def test_run_task_blacklists_share_level_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    分享级错误仍落失败记录，同时把分享加入黑名单。
    """
    blacklist = ShareBlacklistStore(tmp_path / "blacklist.json")
    monkeypatch.setattr(share_blacklist, "_store", blacklist)
    monkeypatch.setattr(
        netdisk_module, "NetdiskResolveService", lambda: _ShareLevelFailResolver()
    )
    module = NetdiskModule()
    store.upsert_task("t9", {"state": "downloading"})

    module._run_task(  # pylint: disable=protected-access
        "t9", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None,
    )

    entry = blacklist.is_blacklisted("quark", "abc")
    assert entry is not None
    assert entry.reason == "仅预览流"
    record = store.get_task("t9")
    assert record is not None
    assert record["state"] == "error"


def test_run_task_does_not_blacklist_generic_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    普通内部异常不拉黑分享，避免网络抖动导致分享被长期封禁。
    """
    blacklist = ShareBlacklistStore(tmp_path / "blacklist.json")
    monkeypatch.setattr(share_blacklist, "_store", blacklist)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: _FailingResolver())
    module = NetdiskModule()
    store.upsert_task("t10", {"state": "downloading"})

    module._run_task(  # pylint: disable=protected-access
        "t10", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None,
    )

    assert blacklist.is_blacklisted("quark", "abc") is None


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


def test_filter_episodes_accepts_display_season_text() -> None:
    """
    订阅链路传入的展示季文本（S01/S01-S02）不能触发 int() 异常。
    """
    files = [
        ShareFile(file_id="1", name="剧名.S01E01.mp4", size=1),
        ShareFile(file_id="2", name="剧名.S02E01.mp4", size=1),
    ]

    kept = NetdiskModule._filter_episodes(files, None, "S01")  # pylint: disable=protected-access
    assert [item.name for item in kept] == ["剧名.S01E01.mp4"]

    kept = NetdiskModule._filter_episodes(files, None, ["S01", "S02"])  # pylint: disable=protected-access
    assert len(kept) == 2

    # 无法识别的季参数按「不做季过滤」处理，不能中断整包下载。
    kept = NetdiskModule._filter_episodes(files, None, "无法识别")  # pylint: disable=protected-access
    assert len(kept) == 2


def test_filter_episodes_episode_targets_keeps_only_target_season() -> None:
    """
    季级目标优先于扁平集数并集，S02 同集号不能随 S01 一起下载。
    """
    files = [
        ShareFile(file_id="1", name="剧名.S01E01.mp4", size=1),
        ShareFile(file_id="2", name="剧名.S02E01.mp4", size=1),
    ]

    kept = NetdiskModule._filter_episodes(  # pylint: disable=protected-access
        files, {1}, episode_targets={1: {1}},
    )

    assert [item.name for item in kept] == ["剧名.S01E01.mp4"]


def test_filter_episodes_episode_targets_skips_unknown_season_for_multiseason() -> None:
    """
    多季目标下无法识别季号的文件不保留，单季目标仍按该季集号兜底。
    """
    files = [ShareFile(file_id="1", name="01.mp4", size=1)]

    multi = NetdiskModule._filter_episodes(  # pylint: disable=protected-access
        files, None, episode_targets={1: {1}, 2: {1}},
    )
    single = NetdiskModule._filter_episodes(  # pylint: disable=protected-access
        files, None, episode_targets={1: {1}},
    )

    assert multi == []
    assert [item.name for item in single] == ["01.mp4"]


def test_filter_episodes_without_episode_targets_keeps_old_behavior() -> None:
    """
    未传季级目标时保持原有的扁平集数过滤语义。
    """
    files = [
        ShareFile(file_id="1", name="剧名.S01E01.mp4", size=1),
        ShareFile(file_id="2", name="剧名.S02E01.mp4", size=1),
    ]

    kept = NetdiskModule._filter_episodes(files, {1})  # pylint: disable=protected-access

    assert len(kept) == 2


def test_run_task_translates_unexpected_parse_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    未预期的解析异常应转换成可展示原因，而不是把 Python 原文暴露给前端。
    """

    class _BrokenResolver:
        """walk 抛出未预期异常的取链服务替身。"""

        tuning = NetdiskTuning()

        def walk(self, *args: Any, **kwargs: Any) -> Any:
            """模拟季集解析失败。"""
            raise ValueError("invalid literal for int() with base 10: 'S01'")

    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: _BrokenResolver())
    module = NetdiskModule()
    store.upsert_task("t9", {"state": "downloading"})

    module._run_task(  # pylint: disable=protected-access
        "t9", NetdiskPlatform.UC, "https://drive.uc.cn/s/abc", "abc", "",
        tmp_path, None,
    )

    record = store.get_task("t9")
    assert record is not None
    assert record["state"] == "error"
    assert record["error"] == "分享文件解析失败，请检查文件名或分享内容"


def test_list_torrents_exposes_share_url() -> None:
    """
    下载管理卡片需要分享地址把分享码渲染成可点击链接。
    """
    store.upsert_task("t10", {
        "state": "downloading",
        "netdisk_platform": "uc",
        "share_code": "abc123",
        "share_url": "https://drive.uc.cn/s/abc123",
    })

    torrents = NetdiskModule().list_torrents()
    assert torrents is not None
    assert torrents[0].share_url == "https://drive.uc.cn/s/abc123"


def test_run_task_downloads_files_in_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    合集内的文件应按 max_tasks 并发下载，而不是一个文件下完再下一个。
    """
    files = tuple(
        ShareFile(file_id=str(index), name=f"{index:02d}.mkv", size=1)
        for index in range(3)
    )

    class _Resolver:
        """返回三个小文件的取链服务替身。"""

        tuning = NetdiskTuning(max_tasks=3, threads=6)

        def walk(self, *args: Any, **kwargs: Any) -> ResolvedShare:
            """返回固定文件列表。"""
            return ResolvedShare(
                ref=ShareRef(
                    platform=NetdiskPlatform.QUARK, share_id="abc", url="https://pan.quark.cn/s/abc"
                ),
                detail=ShareDetail(title="合集", size=3, files=files),
                files=files,
            )

    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda: _Resolver())
    module = NetdiskModule()
    store.upsert_task("t-parallel", {"state": "downloading"})

    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """写出文件与完成标记，并等待三个文件同时进入下载。"""
        nonlocal active, peak
        item = args[4]
        target = Path(args[5]) / item.name
        target.write_bytes(b"x" * int(item.size or 0))
        target.with_name(target.name + ".done").write_text(
            str(item.size or 0), encoding="utf-8"
        )
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1

    monkeypatch.setattr(module, "_download_one", fake_download)

    module._run_task(  # pylint: disable=protected-access
        "t-parallel", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None,
    )

    assert peak == 3
    record = store.get_task("t-parallel")
    assert record is not None
    assert record["state"] == "completed"


def test_run_task_isolates_file_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    单个文件下载失败不应拖垮整个任务：删除残片，其余文件正常入库并标记部分完成。
    """
    files = [ShareFile(file_id=str(i), name=f"{i:02d}.mkv", size=1) for i in range(3)]
    links = tuple(DirectLink(url=f"https://cdn/{i}", headers={}, size=1) for i in range(3))
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """模拟 02.mkv 写入残片后失败，其余文件正常完成。"""
        del kwargs
        item = args[4]
        if item.name == "02.mkv":
            (Path(args[5]) / item.name).write_bytes(b"partial")
            raise DownloadError("模拟损坏")
        target = Path(args[5]) / item.name
        target.write_bytes(b"x")
        target.with_name(target.name + ".done").write_text("1", encoding="utf-8")

    monkeypatch.setattr(module, "_download_one", fake_download)
    module._run_task(  # pylint: disable=protected-access
        "t-partial", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
    )
    record = store.get_task("t-partial")
    assert record is not None
    assert record["state"] == "completed"
    assert record["partial"] is True
    assert record["failed_files"][0]["name"] == "02.mkv"
    assert not (tmp_path / "02.mkv").exists()


def test_run_task_reports_reasons_when_all_files_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    全部文件失败时，错误文案必须带上前 3 个文件的原因，而不只是文件名。
    """
    files = [ShareFile(file_id=str(i), name=f"{i:02d}.mkv", size=1) for i in range(3)]
    links = tuple(DirectLink(url=f"https://cdn/{i}", headers={}, size=1) for i in range(3))
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """模拟所有文件写入残片后失败。"""
        del kwargs
        item = args[4]
        (Path(args[5]) / item.name).write_bytes(b"partial")
        raise DownloadError("模拟损坏")

    monkeypatch.setattr(module, "_download_one", fake_download)
    module._run_task(  # pylint: disable=protected-access
        "t-all-fail", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
    )

    record = store.get_task("t-all-fail")
    assert record is not None
    assert record["state"] == "error"
    assert "01.mkv" in record["error"]
    assert "模拟损坏" in record["error"]


def test_instance_tuning_allows_64_files() -> None:
    """
    实例 max_tasks 允许配置到 64，超过 64 时夹到 64。
    """
    tuning = NetdiskModule._instance_tuning(  # pylint: disable=protected-access
        SimpleNamespace(config={"max_tasks": 64}), NetdiskPlatform.QUARK
    )
    assert tuning.max_tasks == 64

    clamped = NetdiskModule._instance_tuning(  # pylint: disable=protected-access
        SimpleNamespace(config={"max_tasks": 100}), NetdiskPlatform.QUARK
    )
    assert clamped.max_tasks == 64


def test_instance_tuning_caps_xunlei_threads() -> None:
    """
    每文件连接数仍受平台上限夹取：迅雷夹到 8，夸克保留配置的 32。
    """
    xunlei = NetdiskModule._instance_tuning(  # pylint: disable=protected-access
        SimpleNamespace(config={"threads": 32}), NetdiskPlatform.XUNLEI
    )
    assert xunlei.threads == 8

    quark = NetdiskModule._instance_tuning(  # pylint: disable=protected-access
        SimpleNamespace(config={"threads": 32}), NetdiskPlatform.QUARK
    )
    assert quark.threads == 32


def test_run_task_workers_never_exceed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    文件数少于 max_tasks 时并发峰值等于文件数，不会凭空拉起多余 worker。
    """
    files = tuple(
        ShareFile(file_id=str(index), name=f"{index:02d}.mkv", size=1)
        for index in range(2)
    )

    class _Resolver:
        """返回两个小文件的取链服务替身。"""

        tuning = NetdiskTuning(max_tasks=64, threads=6)

        def walk(self, *args: Any, **kwargs: Any) -> ResolvedShare:
            """返回固定文件列表。"""
            return ResolvedShare(
                ref=ShareRef(
                    platform=NetdiskPlatform.QUARK, share_id="abc", url="https://pan.quark.cn/s/abc"
                ),
                detail=ShareDetail(title="合集", size=2, files=files),
                files=files,
            )

    monkeypatch.setattr(
        netdisk_module, "NetdiskResolveService", lambda **kwargs: _Resolver()
    )
    module = NetdiskModule()
    store.upsert_task("t-workers", {"state": "downloading"})

    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """写出文件与完成标记，并等待两个文件同时进入下载。"""
        nonlocal active, peak
        item = args[4]
        target = Path(args[5]) / item.name
        target.write_bytes(b"x" * int(item.size or 0))
        target.with_name(target.name + ".done").write_text(
            str(item.size or 0), encoding="utf-8"
        )
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1

    monkeypatch.setattr(module, "_download_one", fake_download)

    module._run_task(  # pylint: disable=protected-access
        "t-workers", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=64, threads=6),
    )

    assert peak == 2


def test_run_task_records_account_and_strategy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    任务记录保存实际取链账号、策略与容量失败原因。
    """
    files = [ShareFile(file_id="1", name="video.mp4", size=4)]
    links = (
        DirectLink(
            url="https://cdn/x", headers={}, size=4, account_id=9, strategy="transfer",
        ),
    )
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", FakeChunkedDownloader)
    FakeChunkedDownloader.calls = []
    module = NetdiskModule()

    module._run_task(  # pylint: disable=protected-access
        "t9", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None,
    )

    record = store.get_task("t9")
    assert record is not None
    assert record["account_id"] == 9
    assert record["strategy"] == "transfer"


def test_download_blocks_episode_request_when_whole_pack_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    整包任务进行中时，针对单集的重复提交必须复用原任务，
    不能在同一个分享目录上再开一个任务并发写文件。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    monkeypatch.setattr(NetdiskModule, "_run_task", lambda *args, **kwargs: None)
    store.upsert_task("whole", {
        "hash": "whole",
        "share_code": "abc",
        "state": "downloading",
        "episodes": [],
        "progress": 10.0,
    })

    result = module.download(
        "https://drive.uc.cn/s/abc", tmp_path, "", download_type="netdisk",
        netdisk_platform="uc", episodes={5},
    )

    assert result is not None
    assert result[1] == "whole"
    assert result[3] == netdisk_module.DOWNLOAD_TASK_ALREADY_EXISTS


def test_download_does_not_treat_other_season_targets_as_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    活跃任务只覆盖 S01E01 时，S02E01 请求不能被当成已覆盖而静默复用。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    monkeypatch.setattr(NetdiskModule, "_run_task", lambda *args, **kwargs: None)
    store.upsert_task("season1", {
        "hash": "season1",
        "share_code": "abc",
        "state": "downloading",
        "episodes": [1],
        "episode_targets": {"1": [1]},
        "progress": 10.0,
    })

    result = module.download(
        "https://drive.uc.cn/s/abc", tmp_path, "", download_type="netdisk",
        netdisk_platform="uc", episode_targets={2: {1}},
    )

    assert result is not None
    assert result[1] is None
    assert result[3] == "同一分享已有任务在下载其他集数，请等待其完成后再试"


def test_collect_files_keeps_single_version_per_episode() -> None:
    """
    同一集同时存在 mkv 与 mp4 时只保留一个版本，避免重复下载与识别冲突。
    """
    module = NetdiskModule()
    files = [
        ShareFile(file_id="1", name="04.mkv", size=100),
        ShareFile(file_id="2", name="04.mp4", size=300),
        ShareFile(file_id="3", name="05.mkv", size=200),
    ]

    collected = module._collect_files(files, season=1)  # pylint: disable=protected-access

    names = [item.name for item in collected]
    assert names == ["S01E004 04.mp4", "S01E005 05.mkv"]


def test_collect_files_keeps_multipart_episode_parts() -> None:
    """
    同一集的分片文件（Part1/Part2）文件名不同，不能被当成重复版本丢弃。
    """
    module = NetdiskModule()
    files = [
        ShareFile(file_id="1", name="S01E01 Part1.mkv", size=100),
        ShareFile(file_id="2", name="S01E01 Part2.mkv", size=100),
    ]

    collected = module._collect_files(files)  # pylint: disable=protected-access

    assert len(collected) == 2


def test_reconcile_store_resumes_downloading_and_keeps_paused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    重启后仍在下载的任务应自动恢复；用户暂停的任务保持暂停。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    monkeypatch.setattr(NetdiskModule, "_instance_tuning", lambda *args: NetdiskTuning())
    started: List[tuple] = []
    monkeypatch.setattr(module, "_run_task", lambda *args, **kwargs: started.append(args))
    store.upsert_task("run", {
        "hash": "run", "state": "downloading", "netdisk_platform": "uc",
        "share_url": "https://drive.uc.cn/s/abc", "share_code": "abc",
        "save_path": str(tmp_path / "abc"), "passcode": "", "episodes": [5],
        "downloader": "网盘", "category": None,
    })
    store.upsert_task("paused", {
        "hash": "paused", "state": "paused", "netdisk_platform": "uc",
        "share_url": "https://drive.uc.cn/s/def", "share_code": "def",
        "save_path": str(tmp_path / "def"),
    })

    module._reconcile_store_state()  # pylint: disable=protected-access

    assert len(started) == 1
    assert started[0][0] == "run"
    assert started[0][3] == "abc"
    assert store.get_task("run")["state"] == "downloading"
    assert store.get_task("paused")["state"] == "paused"


def test_reconcile_store_pauses_task_without_resume_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    缺少分享地址或目录的下载中记录无法恢复，应回落为暂停。
    """
    module = NetdiskModule()
    monkeypatch.setattr(module, "get_configs", lambda: {})
    store.upsert_task("broken", {
        "hash": "broken", "state": "downloading", "netdisk_platform": "uc",
        "share_code": "abc",
    })

    module._reconcile_store_state()  # pylint: disable=protected-access

    assert store.get_task("broken")["state"] == "paused"


def test_run_task_passes_instance_tuning_to_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    下载器实例配置（transfer_first / auto_transfer）必须传给取链服务，
    否则界面开了「转存优先」也不会生效。
    """
    captured: Dict[str, Any] = {}
    files = [ShareFile(file_id="1", name="video.mp4", size=4)]
    links = (DirectLink(url="https://cdn/x", headers={}, size=4),)
    resolver = FakeResolver(files, links)

    def factory(*args, **kwargs):
        """记录取链服务构造参数。"""
        captured.update(kwargs)
        return resolver

    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", factory)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", FakeChunkedDownloader)
    FakeChunkedDownloader.calls = []
    module = NetdiskModule()

    module._run_task(  # pylint: disable=protected-access
        "t10", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(transfer_first=True),
    )

    assert captured.get("settings") is not None
    assert captured["settings"].transfer_first is True


def _views(*frees):
    """
    按剩余空间构造账号视图。
    """
    return [
        SimpleNamespace(
            id=index + 1, status="active", quota_total=100 * 1024 ** 3,
            quota_used=100 * 1024 ** 3 - free,
        )
        for index, free in enumerate(frees)
    ]


def test_assign_accounts_distributes_files_across_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    多账号时按剩余容量把文件分给不同账号，3 个文件应分到 3 个账号。
    """
    module = NetdiskModule()
    monkeypatch.setattr(
        netdisk_module, "NetdiskAccountService",
        lambda *args, **kwargs: SimpleNamespace(list=lambda platform: _views(50, 50, 50)),
    )
    size = 6 * 1024 ** 3
    files = [ShareFile(file_id=str(i), name=f"0{i}.mp4", size=size) for i in range(1, 4)]

    assignment = module._assign_accounts(NetdiskPlatform.UC, files)  # pylint: disable=protected-access

    assert len(assignment) == 3
    assert len(set(assignment.values())) == 3


def test_assign_accounts_skips_single_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    单文件任务不分配账号，保留多源/自动选号逻辑。
    """
    module = NetdiskModule()
    monkeypatch.setattr(
        netdisk_module, "NetdiskAccountService",
        lambda *args, **kwargs: SimpleNamespace(list=lambda platform: _views(50, 50)),
    )
    files = [ShareFile(file_id="1", name="01.mp4", size=1024)]

    assert module._assign_accounts(NetdiskPlatform.UC, files) == {}  # pylint: disable=protected-access


def test_filter_episodes_drops_unparseable_when_episodes_matched() -> None:
    """
    订阅目标集数已被文件命中时，无法解析集数的电影/合集文件必须排除。
    """
    module = NetdiskModule()
    files = [
        ShareFile(file_id="1", name="221 4K.mp4", size=100),
        ShareFile(file_id="2", name="253 4K.mp4", size=100),
        ShareFile(file_id="3", name="Perfect.World.Movie.2026.mp4", size=100),
        ShareFile(file_id="4", name="三合一完整版.mp4", size=100),
    ]

    kept = module._filter_episodes(files, {221, 253})  # pylint: disable=protected-access

    assert [item.name for item in kept] == ["221 4K.mp4", "253 4K.mp4"]


def test_filter_episodes_keeps_unparseable_when_nothing_matches() -> None:
    """
    分享内文件名都无法解析集数时，保留原有兜底行为，避免漏集。
    """
    module = NetdiskModule()
    files = [
        ShareFile(file_id="1", name="第一集.mkv", size=100),
        ShareFile(file_id="2", name="第二集.mkv", size=100),
    ]

    kept = module._filter_episodes(files, {5})  # pylint: disable=protected-access

    assert len(kept) == 2


def test_transfer_completed_marks_transferred_and_hides_task() -> None:
    """
    整理完成后任务标记为 transferred 并从列表隐藏，但记录保留给清理流程。
    """
    module = NetdiskModule()
    store.upsert_task("done", {
        "hash": "done", "state": "completed", "progress": 100.0,
        "path": "/tmp/done", "files": [],
    })

    module.transfer_completed("done")

    assert store.get_task("done")["state"] == "transferred"
    assert module.list_torrents() == []
    assert [item.hash for item in module.list_torrents(hashs="done")] == ["done"]


def test_completed_file_requires_done_marker(tmp_path: Path) -> None:
    """
    仅大小匹配不能证明下载完成：必须没有计划文件且存在完成标记。
    """
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * 8)

    assert netdisk_module._is_completed_file(target, 8) is False

    target.with_name(target.name + ".done").write_text("8", encoding="utf-8")
    assert netdisk_module._is_completed_file(target, 8) is True

    target.with_suffix(target.suffix + ".plan.json").write_text("{}", encoding="utf-8")
    assert netdisk_module._is_completed_file(target, 8) is False


def test_collect_files_dedupes_episode_across_directories() -> None:
    """
    同一集的 mkv 与 mp4 位于不同子目录时，仍只保留体积更大的一个。
    """
    module = NetdiskModule()
    files = [
        ShareFile(file_id="1", name="04 4K.mkv", size=100, parent_id="dir-mkv"),
        ShareFile(file_id="2", name="04 4K.mp4", size=300, parent_id="dir-mp4"),
    ]

    collected = module._collect_files(files, season=1)  # pylint: disable=protected-access

    assert [item.name for item in collected] == ["S01E004 04 4K.mp4"]


def test_cleanup_orphans_removes_sidecar_only_dir(tmp_path: Path) -> None:
    """
    只剩 .done/.plan 等附属文件的目录应被视为空目录并清理。
    """
    module = NetdiskModule()
    root = tmp_path / "downloads"
    task_dir = root / "abc123"
    task_dir.mkdir(parents=True)
    (task_dir / ".netdisk-task").touch()
    (task_dir / "a.mp4.done").write_text("1", encoding="utf-8")
    (task_dir / "a.mp4.plan.json").write_text("{}", encoding="utf-8")

    stats = module.cleanup_orphans([str(root)], empty_grace_seconds=0)

    assert stats["empty_removed"] == 1
    assert not task_dir.exists()


def test_collect_files_uses_directory_season() -> None:
    """
    目录名带季号时按目录补季，不能把第二季文件标成 S01。
    """
    module = NetdiskModule()
    files = [
        ShareFile(file_id="1", name="05.mkv", size=100, path="第二季/05.mkv"),
    ]

    collected = module._collect_files(files, season=2)  # pylint: disable=protected-access

    assert [item.name for item in collected] == ["S02E005 05.mkv"]


def test_collect_files_without_season_keeps_name() -> None:
    """
    完全无季信息时不再硬编码 S01 前缀。
    """
    module = NetdiskModule()
    files = [ShareFile(file_id="1", name="05.mkv", size=100)]

    collected = module._collect_files(files)  # pylint: disable=protected-access

    assert [item.name for item in collected] == ["05.mkv"]


def test_collect_files_strips_path_traversal() -> None:
    """
    远端文件名不得带目录成分，避免写出任务目录。
    """
    module = NetdiskModule()
    files = [ShareFile(file_id="1", name="../../evil.mkv", size=100)]

    collected = module._collect_files(files, season=1)  # pylint: disable=protected-access

    assert [item.name for item in collected] == ["evil.mkv"]


def test_instance_tuning_uses_platform_overrides() -> None:
    """
    平台级覆盖优先于实例默认值，未覆盖的平台沿用默认值。
    """
    config = SimpleNamespace(
        config={
            "threads": 8,
            "max_tasks": 2,
            "platform_threads": {"quark": 64},
            "platform_max_tasks": {"quark": 3},
        }
    )

    quark = NetdiskModule._instance_tuning(  # pylint: disable=protected-access
        config, NetdiskPlatform.QUARK
    )
    assert quark.threads == 64
    assert quark.max_tasks == 3

    uc = NetdiskModule._instance_tuning(  # pylint: disable=protected-access
        config, NetdiskPlatform.UC
    )
    assert uc.threads == 8
    assert uc.max_tasks == 2


def test_platform_total_connection_cap() -> None:
    """
    迅雷按任务总连接上限限制并发文件数，每文件连接数保持不变。
    """
    assert netdisk_module._cap_platform_workers(NetdiskPlatform.XUNLEI, 8, 8) == 1
    assert netdisk_module._cap_platform_workers(NetdiskPlatform.XUNLEI, 2, 8) == 1
    assert netdisk_module._cap_platform_workers(NetdiskPlatform.XUNLEI, 2, 4) == 2
    assert netdisk_module._cap_platform_workers(NetdiskPlatform.QUARK, 8, 64) == 8


def test_probe_video_duration_rejects_short_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    探测时长不足 5 分钟的文件应删除并判为不可用。
    """
    target = tmp_path / "a.mkv"
    target.write_bytes(b"x")
    monkeypatch.setattr(netdisk_module, "_probe_video_duration", lambda _p: 120.0)
    with pytest.raises(NetdiskError):
        netdisk_module._validate_video_duration(target, expected=None)
    assert not target.exists()


def test_validate_video_duration_accepts_expected_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    时长落在期望值 ±30% 内时保留文件。
    """
    target = tmp_path / "b.mkv"
    target.write_bytes(b"x")
    monkeypatch.setattr(netdisk_module, "_probe_video_duration", lambda _p: 1400.0)
    netdisk_module._validate_video_duration(target, expected=1500)
    assert target.exists()


def test_validate_video_duration_rejects_mismatched_expected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    时长明显短于期望值时删除目标、完成标记与分片计划。
    """
    target = tmp_path / "c.mkv"
    target.write_bytes(b"x")
    done = target.with_name(target.name + ".done")
    plan = target.with_suffix(target.suffix + ".plan.json")
    done.write_text("1", encoding="utf-8")
    plan.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(netdisk_module, "_probe_video_duration", lambda _p: 600.0)
    with pytest.raises(NetdiskError):
        netdisk_module._validate_video_duration(target, expected=1500)
    assert not target.exists()
    assert not done.exists()
    assert not plan.exists()


def test_validate_video_duration_keeps_file_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    探测不到时长时只告警，不误删文件。
    """
    target = tmp_path / "d.mkv"
    target.write_bytes(b"x")
    monkeypatch.setattr(netdisk_module, "_probe_video_duration", lambda _p: None)
    netdisk_module._validate_video_duration(target, expected=1500)
    assert target.exists()


def test_download_one_rejects_short_file_after_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    下载成功后时长不达标时，单文件下载应删除产物并抛网盘错误。
    """
    item = ShareFile(file_id="1", name="short.mkv", size=4)
    links = (DirectLink(url="https://cdn/x", headers={}, size=4),)
    resolver = FakeResolver([item], links)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", FakeChunkedDownloader)
    FakeChunkedDownloader.calls = []
    monkeypatch.setattr(netdisk_module, "_probe_video_duration", lambda _p: 60.0)
    module = NetdiskModule()

    with pytest.raises(NetdiskError):
        module._download_one(  # pylint: disable=protected-access
            resolver, NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "",
            item, tmp_path, "t-short", threading.Event(),
        )

    assert not (tmp_path / "short.mkv").exists()
    assert not (tmp_path / "short.mkv.done").exists()


def test_run_task_derives_episode_runtimes_from_media_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    media_runtime 以分钟传入，应按文件展开为秒级期望时长。
    """
    files = [ShareFile(file_id="1", name="01.mkv", size=1)]
    links = (DirectLink(url="https://cdn/1", headers={}, size=1),)
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()
    captured: Dict[str, Any] = {}

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """记录单文件参数并写出完成标记。"""
        captured.update(kwargs)
        item = args[4]
        target = Path(args[5]) / item.name
        target.write_bytes(b"x")
        target.with_name(target.name + ".done").write_text("1", encoding="utf-8")

    monkeypatch.setattr(module, "_download_one", fake_download)
    module._run_task(  # pylint: disable=protected-access
        "t-runtime", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1), None, None,
        media_runtime=20,
    )

    assert captured["episode_runtimes"] == {"01.mkv": 1200}


def test_run_task_prefers_explicit_episode_runtimes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    显式集时长优先于 media_runtime 的整批回退值。
    """
    files = [
        ShareFile(file_id="1", name="01.mkv", size=1),
        ShareFile(file_id="2", name="02.mkv", size=1),
    ]
    links = (
        DirectLink(url="https://cdn/1", headers={}, size=1),
        DirectLink(url="https://cdn/2", headers={}, size=1),
    )
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()
    captured: Dict[str, Any] = {}

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """记录单文件参数并写出完成标记。"""
        captured.update(kwargs)
        item = args[4]
        target = Path(args[5]) / item.name
        target.write_bytes(b"x")
        target.with_name(target.name + ".done").write_text("1", encoding="utf-8")

    monkeypatch.setattr(module, "_download_one", fake_download)
    module._run_task(  # pylint: disable=protected-access
        "t-explicit", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1), None, None,
        episode_runtimes={"01.mkv": 600}, media_runtime=20,
    )

    assert captured["episode_runtimes"] == {"01.mkv": 600, "02.mkv": 1200}


def test_download_forwards_media_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    下载入口应把媒体时长透传给后台任务。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    started = threading.Event()
    recorded: Dict[str, Any] = {}

    def fake_run_task(*args: Any, **kwargs: Any) -> None:
        """记录后台任务参数并立即结束。"""
        recorded["kwargs"] = kwargs
        started.set()

    monkeypatch.setattr(module, "_run_task", fake_run_task)

    result = module.download(
        "https://pan.quark.cn/s/abc", tmp_path, "", download_type="netdisk",
        netdisk_platform="quark", media_runtime=25,
    )

    assert result is not None
    assert started.wait(2)
    assert recorded["kwargs"]["media_runtime"] == 25


def _partial_three_file_resolver(
    monkeypatch: pytest.MonkeyPatch, fail_name: str = "02.mkv"
) -> NetdiskModule:
    """
    构造三文件批次替身：指定文件写入残片后失败，其余文件正常完成。

    :param monkeypatch: pytest 替换工具
    :param fail_name: 需要失败的文件名
    :return: 已挂载替身取链服务与单文件下载的模块实例
    """
    files = [ShareFile(file_id=str(i), name=f"{i:02d}.mkv", size=1) for i in range(1, 4)]
    links = tuple(DirectLink(url=f"https://cdn/{i}", headers={}, size=1) for i in range(3))
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """模拟指定文件失败，其余文件写出完成标记。"""
        del kwargs
        item = args[4]
        target = Path(args[5]) / item.name
        if item.name == fail_name:
            target.write_bytes(b"partial")
            raise DownloadError("模拟损坏")
        target.write_bytes(b"x")
        target.with_name(target.name + ".done").write_text("1", encoding="utf-8")

    monkeypatch.setattr(module, "_download_one", fake_download)
    return module


def test_run_task_partial_records_ai_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    部分完成时调用批次 AI 决策并落库，订阅来源保留 retry_missing 建议。
    """
    module = _partial_three_file_resolver(monkeypatch)
    captured: Dict[str, Any] = {}

    def fake_judge(summary: Dict[str, Any]) -> BatchDecision:
        """记录摘要并返回补下缺失集的建议。"""
        captured.update(summary)
        return BatchDecision("retry_missing", "缺 2 集", [2])

    monkeypatch.setattr(netdisk_module, "judge_batch", fake_judge)

    module._run_task(  # pylint: disable=protected-access
        "t-partial", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
        source="Subscribe|test",
    )

    record = store.get_task("t-partial")
    assert record is not None
    assert record["partial"] is True
    assert record["missing_episodes"] == [2]
    assert record["ai_decision"]["decision"] == "retry_missing"
    assert record["warning"].startswith("部分完成")
    assert captured["source"] == "Subscribe|test"
    assert captured["failed_files"] == [{"name": "02.mkv", "reason": "模拟损坏"}]


def test_run_task_manual_downgrades_retry_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    手动来源不接受 retry_missing，必须降级为只做说明。
    """
    module = _partial_three_file_resolver(monkeypatch)
    monkeypatch.setattr(
        netdisk_module,
        "judge_batch",
        lambda summary: BatchDecision("retry_missing", "缺 2 集", [2]),
    )

    module._run_task(  # pylint: disable=protected-access
        "t-partial-manual", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
    )

    record = store.get_task("t-partial-manual")
    assert record is not None
    assert record["ai_decision"]["decision"] == "explain_only"
    assert record["ai_decision"]["reason"] == "缺 2 集"


def test_run_task_all_share_level_failures_blacklist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    每个文件都因分享失效失败时，任务错误应保留原因链并把分享拉黑。
    """
    blacklist = ShareBlacklistStore(tmp_path / "blacklist.json")
    monkeypatch.setattr(share_blacklist, "_store", blacklist)
    files = [ShareFile(file_id=str(i), name=f"{i:02d}.mkv", size=1) for i in range(1, 4)]
    links = tuple(DirectLink(url=f"https://cdn/{i}", headers={}, size=1) for i in range(3))
    resolver = FakeResolver(files, links)
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()

    def fake_download(*args: Any, **kwargs: Any) -> None:
        """模拟所有文件都因分享失效失败。"""
        del kwargs
        item = args[4]
        (Path(args[5]) / item.name).write_bytes(b"partial")
        raise NetdiskError(NetdiskErrorCode.SHARE_INVALID, "分享已失效")

    monkeypatch.setattr(module, "_download_one", fake_download)

    module._run_task(  # pylint: disable=protected-access
        "t-all-share-invalid", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
    )

    assert blacklist.is_blacklisted("quark", "abc") is not None
    record = store.get_task("t-all-share-invalid")
    assert record is not None
    assert record["state"] == "error"



def test_run_task_partial_share_level_failure_blacklists_share(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    部分完成时若失败文件是分享级错误，仍要拉黑分享，避免补集反复选中坏分享。
    """
    blacklist = ShareBlacklistStore(tmp_path / "blacklist.json")
    monkeypatch.setattr(share_blacklist, "_store", blacklist)
    module = _partial_three_file_resolver(monkeypatch, fail_name="02.mkv")
    original = module._download_one  # pylint: disable=protected-access

    def share_level_failure(*args: Any, **kwargs: Any) -> None:
        """把 02.mkv 的失败替换为仅预览流。"""
        item = args[4]
        if item.name == "02.mkv":
            (Path(args[5]) / item.name).write_bytes(b"partial")
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "仅预览流，无法完整下载")
        original(*args, **kwargs)

    monkeypatch.setattr(module, "_download_one", share_level_failure)
    monkeypatch.setattr(
        netdisk_module,
        "judge_batch",
        lambda summary: BatchDecision("retry_missing", "缺 2 集", [2]),
    )

    module._run_task(  # pylint: disable=protected-access
        "t-partial-share-level", NetdiskPlatform.QUARK,
        "https://pan.quark.cn/s/abc", "abc", "", tmp_path, None, None, "",
        NetdiskTuning(max_tasks=1), source="Subscribe|test",
    )

    record = store.get_task("t-partial-share-level")
    assert record is not None
    assert record["state"] == "completed"
    assert record["partial"] is True
    assert blacklist.is_blacklisted("quark", "abc") is not None


def test_run_task_empty_share_blacklists_share(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    空分享/无目标集数按分享级错误处理并拉黑，这是最常见的坏分享形态。
    """
    blacklist = ShareBlacklistStore(tmp_path / "blacklist.json")
    monkeypatch.setattr(share_blacklist, "_store", blacklist)
    resolver = FakeResolver([], ())
    monkeypatch.setattr(netdisk_module, "NetdiskResolveService", lambda **kwargs: resolver)
    module = NetdiskModule()

    module._run_task(  # pylint: disable=protected-access
        "t-empty-share", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
    )

    record = store.get_task("t-empty-share")
    assert record is not None
    assert record["state"] == "error"
    assert "分享内没有" in record["error"]
    assert blacklist.is_blacklisted("quark", "abc") is not None


def test_list_torrents_projects_partial_fields() -> None:
    """
    任务列表必须透传部分完成标记、缺失集与 AI 决策，任务页才能看出差别。
    """
    store.upsert_task("t-partial-projection", {
        "state": "completed",
        "netdisk_platform": "quark",
        "share_code": "abc",
        "partial": True,
        "warning": "部分完成：10 个有效，2 个失败",
        "missing_episodes": [2, 7],
        "ai_decision": {"decision": "retry_missing", "reason": "缺 2 集"},
    })

    torrents = NetdiskModule().list_torrents()

    assert torrents is not None
    task = next(item for item in torrents if item.hash == "t-partial-projection")
    assert task.partial is True
    assert task.warning == "部分完成：10 个有效，2 个失败"
    assert task.missing_episodes == [2, 7]
    assert task.ai_decision is not None
    assert task.ai_decision.decision == "retry_missing"
    assert task.ai_decision.reason == "缺 2 集"


def test_run_task_resume_reads_source_from_task_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    进程重启后的恢复任务没有内存来源，应从任务记录还原订阅来源，保留补集建议。
    """
    module = _partial_three_file_resolver(monkeypatch)
    store.upsert_task("t-partial-resume", {"source": "Subscribe|test"})
    monkeypatch.setattr(
        netdisk_module,
        "judge_batch",
        lambda summary: BatchDecision("retry_missing", "缺 2 集", [2]),
    )

    module._run_task(  # pylint: disable=protected-access
        "t-partial-resume", NetdiskPlatform.QUARK, "https://pan.quark.cn/s/abc", "abc", "",
        tmp_path, None, None, "", NetdiskTuning(max_tasks=1),
    )

    record = store.get_task("t-partial-resume")
    assert record is not None
    assert record["ai_decision"]["decision"] == "retry_missing"


def test_platform_info_exposes_pan115_recycle_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台信息接口把 115 的回收站能力透传给前端。"""
    monkeypatch.setattr(registry, "_adapters", {NetdiskPlatform.PAN115: Pan115Adapter()})

    payload = asyncio.run(netdisk_api.list_platforms(None))

    info = next(
        item for item in payload.data.platforms if item.platform == NetdiskPlatform.PAN115.value
    )
    assert info.recycle_bin is True
