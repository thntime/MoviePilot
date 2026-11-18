"""
补充网盘失败对账 owner 的分支与兜底路径覆盖。

覆盖媒体库存在性查询、孤儿目录清理、账号/个人盘/本地查询异常、
复用任务提交与历史补写异常、容量告警守卫等未走到的分支。
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.netdisk.existing import ExistingHit
from app.chain.download.reconcile import DownloadFailureReconcileOwner
from app.domain.netdisk import DriveFile, NetdiskPlatform


class _CoverChain(DownloadFailureReconcileOwner):
    """
    用最小依赖驱动对账 owner，可按 hash 返回不同下载历史。
    """

    def __init__(self, torrents=None, history=None, history_by_hash=None):
        """保存任务投影、默认历史与按 hash 的历史映射。"""
        self._torrents = list(torrents or [])
        self._history = history
        self._history_by_hash = dict(history_by_hash or {})
        self.download_history_repository = SimpleNamespace(get_by_hash=self._get_by_hash)
        self.marked = []
        self.module_result = None

    def _get_by_hash(self, task_hash):
        """按 hash 返回预置历史，缺省回落到默认快照。"""
        if task_hash in self._history_by_hash:
            return self._history_by_hash[task_hash]
        return self._history

    def list_torrents(self, *args, **kwargs):
        """返回预置任务投影。"""
        return self._torrents

    def run_module(self, method, **kwargs):
        """记录模块调用并返回预设结果。"""
        self.marked.append((method, kwargs))
        return self.module_result

    def post_message(self, message):
        """记录告警消息。"""
        self.marked.append(("message", message))

    def _download_failure_ttl(self, error_msg):
        """统一返回瞬时冷却秒数。"""
        return 3600


def _failed_torrent(**overrides):
    """构造一条失败网盘任务投影。"""
    values = {
        "netdisk_platform": "quark", "state": "error", "hash": "hx",
        "reconciled": False, "error": "网盘未返回可用下载链接",
        "title": "示例", "account_id": 7, "downloader": "Netdisk",
        "save_path": "/downloads/quark/abc", "path": "/downloads/quark/abc",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _failed_history(**overrides):
    """构造一条失败任务下载历史快照。"""
    values = {
        "media_source": "tmdb", "media_id": "100", "title": "示例",
        "seasons": "S01", "episodes": "E01-E02",
        "note": {"source": 'Subscribe|{"id": 7}'},
        "path": "/downloads/quark/abc",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_subscribe_chain(monkeypatch: pytest.MonkeyPatch, rolled, searched, finished=None):
    """记录订阅回滚与重搜调用，避免触发真实订阅搜索。"""

    class FakeSubscribeChain:
        """最小订阅门面替身。"""

        @staticmethod
        def parse_subscribe_source_keyword(source: str):
            """按真实格式解析测试来源。"""
            if not str(source or "").startswith("Subscribe|"):
                return None
            try:
                return json.loads(str(source).split("|", 1)[1])
            except (IndexError, ValueError):
                return {"id": 7}

        def rollback_download_facts(self, subscribe_id, episodes):
            """记录回滚集数。"""
            rolled.append((subscribe_id, list(episodes)))
            return True

        def search(self, sid=None, **kwargs):
            """记录搜索并在需要时通知测试。"""
            searched.append(sid)
            if finished is not None:
                finished.set()

    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain", FakeSubscribeChain)


def test_failed_episodes_skips_unrelated_tasks() -> None:
    """
    无网盘平台、无历史、来源或媒体不匹配的任务都不贡献失败集数。
    """
    torrents = [
        SimpleNamespace(state="error", hash="h-none", reconciled=False),
        SimpleNamespace(netdisk_platform="quark", state="error", hash="h-missing", reconciled=False),
        SimpleNamespace(netdisk_platform="quark", state="error", hash="h-source", reconciled=False),
        SimpleNamespace(netdisk_platform="quark", state="error", hash="h-media", reconciled=False),
    ]
    histories = {
        "h-missing": None,
        "h-source": SimpleNamespace(media_source="douban", media_id="100", seasons="S01", episodes="E01"),
        "h-media": SimpleNamespace(media_source="tmdb", media_id="200", seasons="S01", episodes="E01"),
    }
    chain = _CoverChain(torrents, history_by_hash=histories)

    assert chain.failed_netdisk_episodes("tmdb", "100", 1) == set()


def test_failed_episodes_tolerates_torrent_query_error() -> None:
    """
    下载器不可用时跳过失败集数过滤，返回空集合而不是抛出异常。
    """
    chain = _CoverChain()

    def _boom(*args, **kwargs):
        """模拟下载器查询异常。"""
        raise RuntimeError("下载器不可用")

    chain.list_torrents = _boom

    assert chain.failed_netdisk_episodes("tmdb", "100", 1) == set()


def test_partial_retry_reads_object_decision() -> None:
    """
    AI 批次决策是对象而非字典时，仍能识别 retry_missing 并返回缺失集。
    """
    torrent = SimpleNamespace(
        state="completed", partial=True,
        ai_decision=SimpleNamespace(decision="retry_missing"),
        missing_episodes=[3],
    )

    assert DownloadFailureReconcileOwner._partial_retry_episodes(torrent) == {3}


def test_paused_undelivered_handles_bad_and_naive_timestamp() -> None:
    """
    暂停时间无法解析或缺少时区时都按超过等待窗口处理。
    """
    bad = SimpleNamespace(state="paused", paused_reason="user", paused_at="not-a-time")
    naive = SimpleNamespace(state="paused", paused_reason="user", paused_at="2020-01-01T00:00:00")

    assert DownloadFailureReconcileOwner._paused_undelivered(bad) is True
    assert DownloadFailureReconcileOwner._paused_undelivered(naive) is True


def test_is_download_file_transferred_returns_false_on_error() -> None:
    """
    下载文件状态查询异常时保守保留文件，返回 False。
    """
    chain = _CoverChain()

    def _boom(_path):
        """模拟仓储查询异常。"""
        raise RuntimeError("仓储不可用")

    chain.download_history_repository = SimpleNamespace(get_file_by_fullpath=_boom)

    assert chain.is_download_file_transferred("/downloads/a.mkv") is False


def test_cleanup_netdisk_artifacts_collects_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    清理入口只收集有效下载目录与带路径的活跃任务，并透传已整理判定回调。
    """

    class _Helper:
        """返回一条有效目录与一条空目录配置。"""

        def get_download_dirs(self):
            """返回下载目录配置。"""
            return [
                SimpleNamespace(download_path=str(tmp_path)),
                SimpleNamespace(download_path=""),
            ]

    monkeypatch.setattr("app.application.directory.DirectoryHelper", _Helper)
    chain = _CoverChain([
        SimpleNamespace(path="/downloads/one"),
        SimpleNamespace(path=None),
    ])
    chain.module_result = {"orphans": 2}

    assert chain.cleanup_netdisk_artifacts() == {"orphans": 2}
    method, kwargs = chain.marked[0]
    assert method == "cleanup_orphans"
    assert kwargs["download_dirs"] == [str(tmp_path)]
    assert kwargs["active_paths"] == ["/downloads/one"]
    assert callable(kwargs["is_transferred"])


def test_cleanup_netdisk_artifacts_ignores_non_dict_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    模块返回非字典结果时清理入口返回空统计。
    """

    class _Helper:
        """返回空目录列表。"""

        def get_download_dirs(self):
            """没有配置下载目录。"""
            return []

    monkeypatch.setattr("app.application.directory.DirectoryHelper", _Helper)
    chain = _CoverChain()
    chain.module_result = None

    assert chain.cleanup_netdisk_artifacts() == {}


def test_reconcile_skips_tasks_without_platform_or_running() -> None:
    """
    非网盘任务、已对账任务与仍在下载的任务都不参与对账。
    """
    torrents = [
        SimpleNamespace(state="error", hash="h1", reconciled=False),
        SimpleNamespace(netdisk_platform="quark", state="error", hash="h2", reconciled=True),
        SimpleNamespace(netdisk_platform="quark", state="downloading", hash="h3", reconciled=False),
    ]
    chain = _CoverChain(torrents)

    assert chain.reconcile_netdisk_failures() == 0
    assert chain.marked == []


def test_cloud_existing_query_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘已有文件查询异常时按未命中处理，继续回滚并触发换源。
    """
    chain = _CoverChain([_failed_torrent()], _failed_history())
    chain._existing_accounts = lambda platform, account_id: [(7, "主账号")]

    def _boom(*args, **kwargs):
        """模拟个人盘查询异常。"""
        raise RuntimeError("个人盘不可用")

    monkeypatch.setattr("app.chain.download.reconcile.find_complete_media", _boom)
    monkeypatch.setattr(
        "app.chain.download.reconcile.find_local_complete", lambda *a, **k: None,
    )
    rolled, searched = [], []
    finished = threading.Event()
    _patch_subscribe_chain(monkeypatch, rolled, searched, finished)

    assert chain.reconcile_netdisk_failures() == 1
    assert finished.wait(1.0)
    assert rolled == [(7, [1, 2])]
    assert searched == [7]


def test_transfer_local_path_missing_file_returns_false(tmp_path: Path) -> None:
    """
    本地命中文件不可读时跳过整理并返回 False。
    """
    chain = _CoverChain()

    assert chain._transfer_local_path(  # pylint: disable=protected-access
        tmp_path / "不存在.mkv", _failed_history(), _failed_torrent(),
    ) is False


def test_transfer_local_path_handles_chain_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    整理入口抛异常或返回失败时都返回 False，交由上层回落到换源。
    """
    target = tmp_path / "示例.S01E01.mkv"
    target.write_bytes(b"x")
    chain = _CoverChain()

    class _Raising:
        """整理入口抛异常。"""

        def do_transfer(self, **kwargs):
            """模拟整理异常。"""
            raise RuntimeError("整理异常")

    monkeypatch.setattr("app.chain.transfer.facade.TransferChain", _Raising)
    assert chain._transfer_local_path(  # pylint: disable=protected-access
        target, _failed_history(), _failed_torrent(),
    ) is False

    class _Refusing:
        """整理入口返回失败。"""

        def do_transfer(self, **kwargs):
            """模拟整理失败结果。"""
            return False, "整理失败"

    monkeypatch.setattr("app.chain.transfer.facade.TransferChain", _Refusing)
    assert chain._transfer_local_path(  # pylint: disable=protected-access
        target, _failed_history(), _failed_torrent(),
    ) is False


def test_existing_accounts_returns_empty_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    账号查询异常时按未命中处理，返回空账号列表。
    """

    class _Boom:
        """账号服务查询直接抛异常。"""

        def list(self, platform):
            """模拟账号查询异常。"""
            raise RuntimeError("账号库不可用")

    monkeypatch.setattr("app.chain.download.reconcile.NetdiskAccountService", _Boom)
    chain = _CoverChain()

    assert chain._existing_accounts(  # pylint: disable=protected-access
        NetdiskPlatform.QUARK, 7,
    ) == []


def test_find_local_existing_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    本地扫描异常时按未命中处理，返回 None。
    """

    def _boom(*args, **kwargs):
        """模拟本地扫描异常。"""
        raise RuntimeError("目录不可读")

    monkeypatch.setattr("app.chain.download.reconcile.find_local_complete", _boom)
    chain = _CoverChain()

    assert chain._find_local_existing(  # pylint: disable=protected-access
        _failed_history(), _failed_torrent(), 1, {1},
    ) is None


def test_media_library_exists_requires_complete_identity() -> None:
    """
    缺少媒体来源、媒体标识或标题时不做媒体库查询。
    """
    chain = _CoverChain()
    chain.get_no_exists_info = lambda meta, mediainfo: (True, None)

    assert chain._media_library_exists(  # pylint: disable=protected-access
        SimpleNamespace(media_source=None, media_id="100", title="示例"), 1, {1},
    ) is False
    assert chain._media_library_exists(  # pylint: disable=protected-access
        SimpleNamespace(media_source="tmdb", media_id="", title="示例"), 1, {1},
    ) is False
    assert chain._media_library_exists(  # pylint: disable=protected-access
        SimpleNamespace(media_source="tmdb", media_id="100", title=""), 1, {1},
    ) is False


def test_media_library_exists_queries_library() -> None:
    """
    身份完整时用下载历史构造季集范围并返回媒体库存在性。
    """
    calls = []

    def _getter(meta, mediainfo):
        """记录查询参数并返回已存在。"""
        calls.append((meta, mediainfo))
        return True, None

    chain = _CoverChain()
    chain.get_no_exists_info = _getter
    history = SimpleNamespace(media_source="tmdb", media_id="100", title="示例")

    assert chain._media_library_exists(  # pylint: disable=protected-access
        history, 2, {3, 5},
    ) is True
    meta, mediainfo = calls[0]
    assert meta.begin_season == 2 and meta.end_season == 2
    assert meta.begin_episode == 3 and meta.end_episode == 5
    assert mediainfo.media_id == "100"
    assert mediainfo.seasons == {2: [3, 5]}


def test_media_library_exists_handles_query_error() -> None:
    """
    媒体库查询异常时按未命中处理，返回 False。
    """

    def _boom(meta, mediainfo):
        """模拟媒体库查询异常。"""
        raise RuntimeError("媒体库不可用")

    chain = _CoverChain()
    chain.get_no_exists_info = _boom
    history = SimpleNamespace(media_source="tmdb", media_id="100", title="示例")

    assert chain._media_library_exists(  # pylint: disable=protected-access
        history, 1, {1},
    ) is False


def test_download_dirs_maps_file_to_parent(tmp_path: Path) -> None:
    """
    历史路径指向文件时取其父目录，并对重复目录去重。
    """
    target = tmp_path / "示例.S01E01.mkv"
    target.write_bytes(b"x")
    chain = _CoverChain()

    dirs = chain._download_dirs(  # pylint: disable=protected-access
        SimpleNamespace(path=str(target)),
        SimpleNamespace(save_path=str(tmp_path), path=str(tmp_path)),
    )

    assert dirs == [str(tmp_path)]


def test_existing_download_dir_falls_back_to_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    任务与历史都没有路径时回退到配置中首个非空下载目录。
    """

    class _Helper:
        """返回一条空目录与一条有效目录配置。"""

        def get_download_dirs(self):
            """返回下载目录配置。"""
            return [
                SimpleNamespace(download_path=""),
                SimpleNamespace(download_path=str(tmp_path)),
            ]

    monkeypatch.setattr("app.application.directory.DirectoryHelper", _Helper)
    chain = _CoverChain()

    assert chain._existing_download_dir(  # pylint: disable=protected-access
        SimpleNamespace(save_path=None, path=None), SimpleNamespace(path=None),
    ) == tmp_path


def test_resubmit_existing_without_files_returns_false() -> None:
    """
    命中结果没有文件条目时直接返回 False，不提交复用任务。
    """
    chain = _CoverChain()
    hit = ExistingHit(account_id=7, files=[])

    assert chain._resubmit_existing(  # pylint: disable=protected-access
        _failed_torrent(), _failed_history(), {1}, 1, hit, None,
    ) is False
    assert chain.marked == []


def test_resubmit_existing_survives_cleanup_errors() -> None:
    """
    旧任务清理与标记异常都不阻断新任务提交与历史补写。
    """
    history = _failed_history()
    added = []
    chain = _CoverChain([], history)
    chain.download_history_repository = SimpleNamespace(
        get_by_hash=lambda _hash: history,
        add=lambda payload, files=(): added.append(payload),
    )

    def _run_module(method, **kwargs):
        """下载提交成功，旧任务清理与标记都抛异常。"""
        chain.marked.append((method, kwargs))
        if method == "download_existing":
            return ("Netdisk", "newhash", None, "")
        if method == "remove_torrents":
            raise RuntimeError("清理异常")
        if method == "mark_reconciled":
            raise RuntimeError("标记异常")
        return True

    chain.run_module = _run_module
    hit = ExistingHit(
        account_id=7, files=[DriveFile(file_id="f1", name="示例.S01E01.mkv")],
    )

    assert chain._resubmit_existing(  # pylint: disable=protected-access
        _failed_torrent(), history, {1}, 1, hit, 'Subscribe|{"id": 7}',
    ) is True
    assert added


def test_history_media_runtime_handles_missing_and_invalid_note() -> None:
    """
    历史备注缺失、非字典或时长非法时返回 None，合法时长转为整数。
    """
    assert DownloadFailureReconcileOwner._history_media_runtime(
        SimpleNamespace(note=None),
    ) is None
    assert DownloadFailureReconcileOwner._history_media_runtime(
        SimpleNamespace(note={"media_runtime": "abc"}),
    ) is None
    assert DownloadFailureReconcileOwner._history_media_runtime(
        SimpleNamespace(note={"media_runtime": "45"}),
    ) == 45


def test_record_existing_history_without_repository_add() -> None:
    """
    下载历史仓储没有写入入口时跳过补写。
    """
    chain = _CoverChain()
    chain.download_history_repository = SimpleNamespace()

    chain._record_existing_history(  # pylint: disable=protected-access
        _failed_history(), _failed_torrent(), "newhash", "existing",
        Path("/downloads"), 7, [SimpleNamespace(name="示例.S01E01.mkv")], {1},
    )

    assert chain.marked == []


def test_record_existing_history_swallows_write_error() -> None:
    """
    历史补写异常不影响复用任务提交结果。
    """

    def _boom(payload, files=()):
        """模拟历史写入异常。"""
        raise RuntimeError("写入异常")

    chain = _CoverChain()
    chain.download_history_repository = SimpleNamespace(add=_boom)

    chain._record_existing_history(  # pylint: disable=protected-access
        _failed_history(), _failed_torrent(), "newhash", "existing",
        Path("/downloads"), 7, [SimpleNamespace(name="示例.S01E01.mkv")], {1},
    )


def test_existing_source_marker_for_non_subscribe() -> None:
    """
    非订阅来源统一写 existing 标记，便于下一轮识别复用任务。
    """
    assert DownloadFailureReconcileOwner._existing_source(None) == "existing"


def test_record_capacity_failure_swallows_write_error() -> None:
    """
    失败台账写入异常只记录日志，不阻断对账流程。
    """

    class _Repo:
        """失败台账写入直接抛异常。"""

        def record_failure(self, payload):
            """模拟台账写入异常。"""
            raise RuntimeError("台账不可用")

    chain = _CoverChain()
    chain.download_failure_repository = _Repo()
    history = _failed_history(note={"failure_fingerprint": "fp"})

    chain._record_capacity_failure(  # pylint: disable=protected-access
        history, _failed_torrent(), 'Subscribe|{"id": 7}',
    )


def test_alert_netdisk_capacity_ignores_non_capacity_error() -> None:
    """
    失败原因不属于容量问题时不发容量告警。
    """
    chain = _CoverChain()

    chain._alert_netdisk_capacity(  # pylint: disable=protected-access
        _failed_torrent(error="分享文件解析失败"), NetdiskPlatform.QUARK,
    )

    assert chain.marked == []
