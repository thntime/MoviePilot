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


def test_download_accepts_episode_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    季级目标应被下载入口接受并透传给后台任务。
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    started = threading.Event()
    recorded: Dict[str, Any] = {}

    def fake_run_task(*args: Any) -> None:
        """记录后台任务参数并立即结束。"""
        recorded["args"] = args
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
