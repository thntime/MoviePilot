"""
验证网盘后台失败对账、订阅事实回滚与换源触发。
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.netdisk import selection
from app.application.netdisk.existing import ExistingHit
from app.chain.download import local_existing
from app.chain.download.local_existing import LocalExistingHit
from app.chain.download.reconcile import DownloadFailureReconcileOwner
from app.chain.subscribe.completion import SubscribeCompletionOwner
from app.domain.netdisk import DriveFile, NetdiskPlatform


@pytest.fixture(autouse=True)
def _clear_state():
    """
    每个用例开始前清空平台冷却与告警状态。
    """
    selection.clear_selection_state()
    yield
    selection.clear_selection_state()


class _ReconcileChain(DownloadFailureReconcileOwner):
    """
    用最小依赖驱动对账 owner。
    """

    def __init__(self, torrents, history):
        """保存任务与下载历史查询结果。"""
        self._torrents = torrents
        self.download_history_repository = SimpleNamespace(get_by_hash=lambda _hash: history)
        self.marked = []

    def list_torrents(self, *args, **kwargs):
        """返回预置失败任务。"""
        return self._torrents

    def run_module(self, method, **kwargs):
        """记录模块调用。"""
        self.marked.append((method, kwargs))
        return None

    def post_message(self, message):
        """记录告警消息。"""
        self.marked.append(("message", message))

    def _download_failure_ttl(self, error_msg):
        """非容量失败按瞬时冷却处理。"""
        return 3600

    def is_download_file_transferred(self, fullpath: str) -> bool:
        """复用真实实现。"""
        return DownloadFailureReconcileOwner.is_download_file_transferred(self, fullpath)


def test_failed_netdisk_episodes_reads_history() -> None:
    """
    失败任务按媒体身份与季返回覆盖集数。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="error", hash="h1", reconciled=False,
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E05-E06",
    )
    chain = _ReconcileChain([torrent], history)
    assert chain.failed_netdisk_episodes("tmdb", "100", 1) == {5, 6}
    assert chain.failed_netdisk_episodes("tmdb", "100", 2) == set()


def test_is_download_file_transferred_requires_deleted_state() -> None:
    """
    只有下载文件记录被标记删除后才视为已整理。
    """
    torrent = SimpleNamespace(netdisk_platform=None)
    chain = _ReconcileChain([], None)
    chain.download_history_repository = SimpleNamespace(
        get_file_by_fullpath=lambda path: SimpleNamespace(state=0 if path == "done" else 1)
    )
    assert chain.is_download_file_transferred("done") is True
    assert chain.is_download_file_transferred("pending") is False
    assert chain.is_download_file_transferred("missing") is False


def test_reconcile_rolls_back_and_marks_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    容量不足的任务回滚订阅集数、只冷却账号、标记任务并触发重新搜索。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="error", hash="h1", reconciled=False,
        error="剩余空间不足：需要 100 字节，当前可用 1 字节", title="示例",
        account_id=7,
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E05",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([torrent], history)
    rolled = []
    searched = []
    finished = threading.Event()

    class FakeSubscribeChain:
        """记录回滚与搜索调用。"""

        @staticmethod
        def parse_subscribe_source_keyword(source: str):
            """解析测试来源。"""
            return {"id": 7} if source.startswith("Subscribe|") else None

        def rollback_download_facts(self, subscribe_id, episodes):
            """记录回滚集数。"""
            rolled.append((subscribe_id, list(episodes)))
            return True

        def search(self, sid=None, **kwargs):
            """记录搜索并通知测试。"""
            searched.append(sid)
            finished.set()

    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain", FakeSubscribeChain)

    assert chain.reconcile_netdisk_failures() == 1
    assert rolled == [(7, [5])]
    assert finished.wait(1.0)
    assert searched == [7]
    assert selection.is_account_cooled(7)
    assert not selection.is_platform_cooled(NetdiskPlatform.QUARK)
    assert ("mark_reconciled", {"hashs": ["h1"]}) in chain.marked
    assert any(item[0] == "message" for item in chain.marked)


def test_reconcile_does_not_cool_platform_for_non_capacity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    解析/网络类失败只标记对账，不能把整个平台冷却，否则一次代码异常会阻塞后续全部任务。
    """
    torrent = SimpleNamespace(
        netdisk_platform="uc", state="error", hash="h2", reconciled=False,
        error="分享文件解析失败，请检查文件名或分享内容", title="示例",
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E05",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([torrent], history)

    class FakeSubscribeChain:
        """避免测试触发真实订阅搜索。"""

        @staticmethod
        def parse_subscribe_source_keyword(source: str):
            """解析测试来源。"""
            return {"id": 7} if source.startswith("Subscribe|") else None

        def rollback_download_facts(self, subscribe_id, episodes):
            """忽略回滚结果。"""
            return True

        def search(self, sid=None, **kwargs):
            """忽略搜索。"""
            return None

    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain", FakeSubscribeChain)

    assert chain.reconcile_netdisk_failures() == 1
    assert not selection.is_platform_cooled(NetdiskPlatform.UC)
    assert chain.marked == [("mark_reconciled", {"hashs": ["h2"]})]


class _CompletionChain(SubscribeCompletionOwner):
    """
    用最小依赖驱动订阅事实回滚。
    """

    def __init__(self, subscribe):
        """保存订阅快照并记录更新。"""
        self.subscription_repository = SimpleNamespace(get=lambda _id: subscribe)
        self.updated = None

    def _SubscribeChain__apply_subscribe_update(self, subscribe, update_data, scene=None):
        """记录更新字段。"""
        self.updated = dict(update_data)
        return subscribe


def test_rollback_download_facts_removes_note_and_priority() -> None:
    """
    回滚只移除目标集数，并同步清理按集优先级。
    """
    subscribe = SimpleNamespace(
        id=7, note=[1, 2, 5], episode_priority={"1": 80, "2": 90, "5": 70},
    )
    chain = _CompletionChain(subscribe)
    assert chain.rollback_download_facts(7, [5]) is True
    assert chain.updated["note"] == [1, 2]
    assert chain.updated["episode_priority"] == {"1": 80, "2": 90}


def test_capacity_ttl_is_thirty_minutes() -> None:
    """
    容量类失败使用 30 分钟专用冷却，短于普通瞬时失败。
    """
    from app.chain.download.failure import (
        DOWNLOAD_FAILURE_CAPACITY_TTL_SECONDS,
        DownloadFailureOwner,
    )

    assert DOWNLOAD_FAILURE_CAPACITY_TTL_SECONDS == 30 * 60
    assert DownloadFailureOwner._download_failure_ttl(
        "网盘账号剩余空间不足：需要 100 字节，可用 1 字节"
    ) == DOWNLOAD_FAILURE_CAPACITY_TTL_SECONDS


def test_failed_episodes_skips_reconciled_tasks() -> None:
    """
    已对账的失败任务不能再污染订阅缺集计算，否则会反复重下。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="error", hash="h9", reconciled=True,
        error="空间不足", title="示例",
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E05",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([torrent], history)

    assert chain.failed_netdisk_episodes("tmdb", "100", 1) == set()


def test_partial_retry_task_contributes_missing_episodes() -> None:
    """
    部分完成且 AI 建议补集的任务只把缺失集视为未下载，有效集仍算已交付。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="completed", hash="hp", reconciled=False,
        partial=True, missing_episodes=[2, 7],
        ai_decision={"decision": "retry_missing", "reason": "缺 2 集"},
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E01-E03",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([torrent], history)

    assert chain.failed_netdisk_episodes("tmdb", "100", 1) == {2, 7}


def test_partial_organize_now_does_not_contribute_failed_episodes() -> None:
    """
    部分完成但 AI 选择立即整理时，不把缺失集当成下载失败。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="completed", hash="hp2", reconciled=False,
        partial=True, missing_episodes=[2],
        ai_decision={"decision": "organize_now", "reason": "先整理"},
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E01-E03",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([torrent], history)

    assert chain.failed_netdisk_episodes("tmdb", "100", 1) == set()


def test_reconcile_partial_retry_rolls_back_missing_and_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    部分完成的订阅任务只回滚缺失集，记录资源冷却并触发补集搜索。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="completed", hash="hp3", reconciled=False,
        partial=True, missing_episodes=[2], error=None,
        ai_decision={"decision": "retry_missing", "reason": "缺 2 集"},
        title="示例", account_id=7,
    )
    history = SimpleNamespace(
        media_source="tmdb", media_id="100", seasons="S01", episodes="E01-E03",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([torrent], history)
    rolled = []
    searched = []
    finished = threading.Event()

    class FakeSubscribeChain:
        """记录回滚与搜索调用。"""

        @staticmethod
        def parse_subscribe_source_keyword(source: str):
            """解析测试来源。"""
            return {"id": 7} if source.startswith("Subscribe|") else None

        def rollback_download_facts(self, subscribe_id, episodes):
            """记录回滚集数。"""
            rolled.append((subscribe_id, list(episodes)))
            return True

        def search(self, sid=None, **kwargs):
            """记录搜索并通知测试。"""
            searched.append(sid)
            finished.set()

    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain", FakeSubscribeChain)

    assert chain.reconcile_netdisk_failures() == 1
    assert rolled == [(7, [2])]
    assert finished.wait(1.0)
    assert searched == [7]
    assert not selection.is_account_cooled(7)
    assert ("mark_reconciled", {"hashs": ["hp3"]}) in chain.marked


def _patch_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    用固定账号投影替代真实账号仓储，避免测试访问数据库。
    """

    class _FakeAccountService:
        """返回单个主账号。"""

        def list(self, platform=None):
            """忽略平台过滤。"""
            return [SimpleNamespace(id=7, name="主账号")]

    monkeypatch.setattr(
        "app.chain.download.reconcile.NetdiskAccountService", _FakeAccountService,
    )


def _patch_subscribe_chain(monkeypatch: pytest.MonkeyPatch, rolled, searched, finished=None):
    """
    记录订阅回滚与重搜调用，避免触发真实订阅搜索。
    """

    class FakeSubscribeChain:
        """最小订阅门面替身。"""

        @staticmethod
        def parse_subscribe_source_keyword(source: str):
            """解析测试来源。"""
            return {"id": 7} if source.startswith("Subscribe|") else None

        def rollback_download_facts(self, subscribe_id, episodes):
            """记录回滚集数。"""
            rolled.append((subscribe_id, list(episodes)))
            return True

        def search(self, sid=None, **kwargs):
            """记录搜索。"""
            searched.append(sid)
            if finished is not None:
                finished.set()

    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain", FakeSubscribeChain)


def _failed_torrent(**overrides):
    """构造一条失败网盘任务投影。"""
    values = {
        "netdisk_platform": "quark", "state": "error", "hash": "hx",
        "reconciled": False, "error": "网盘未返回可用下载链接",
        "title": "示例", "account_id": 7, "downloader": "Netdisk",
        "share_url": "https://pan.quark.cn/s/abc",
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
        "path": "/downloads/quark/abc", "custom_words": None, "media_category": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_reconcile_uses_cloud_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    网盘个人盘命中时重新提交下载，不回滚订阅也不触发换源搜索。
    """
    chain = _ReconcileChain([_failed_torrent()], _failed_history())
    _patch_accounts(monkeypatch)
    queried = []
    local_calls = []

    def _find_media(accounts, title, season, episodes, **kwargs):
        """记录查询参数并返回命中。"""
        queried.append((accounts, title, season, set(episodes)))
        return ExistingHit(
            account_id=7,
            files=[DriveFile(file_id="f1", name="示例.S01E01.mkv")],
        )

    monkeypatch.setattr(
        "app.chain.download.reconcile.find_complete_media", _find_media,
    )
    monkeypatch.setattr(
        "app.chain.download.reconcile.find_local_complete",
        lambda *args, **kwargs: local_calls.append(True),
    )
    rolled, searched = [], []
    _patch_subscribe_chain(monkeypatch, rolled, searched)

    assert chain.reconcile_netdisk_failures() == 1
    assert rolled == [] and searched == []
    assert local_calls == []
    assert queried and queried[0][0][0][0] == 7
    assert any(
        method == "download" and kwargs.get("source") == "existing"
        for method, kwargs in chain.marked
    )


def test_reconcile_uses_local_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    本地命中时标记整理完成并结束任务，不换源。
    """
    chain = _ReconcileChain([_failed_torrent()], _failed_history())
    _patch_accounts(monkeypatch)
    monkeypatch.setattr(
        "app.chain.download.reconcile.find_complete_media", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.chain.download.reconcile.find_local_complete",
        lambda *a, **k: LocalExistingHit(path=Path("/downloads/quark/abc/示例.S01E01.mkv")),
    )
    rolled, searched = [], []
    _patch_subscribe_chain(monkeypatch, rolled, searched)

    assert chain.reconcile_netdisk_failures() == 1
    assert rolled == [] and searched == []
    assert ("transfer_completed", {"hashs": ["hx"]}) in chain.marked
    assert ("mark_reconciled", {"hashs": ["hx"]}) in chain.marked


def test_reconcile_falls_back_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    网盘与本地都未命中时沿用回滚并触发订阅重搜。
    """
    chain = _ReconcileChain([_failed_torrent()], _failed_history())
    _patch_accounts(monkeypatch)
    monkeypatch.setattr(
        "app.chain.download.reconcile.find_complete_media", lambda *a, **k: None,
    )
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
    assert not any(method == "download" for method, _ in chain.marked)
    assert not any(method == "transfer_completed" for method, _ in chain.marked)


def test_reconcile_skips_shell_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    本地零字节空壳文件不视为完整，继续走回滚换源。
    """
    local_existing._CACHE.clear()
    target = tmp_path / "示例.S01E01.mkv"
    target.write_bytes(b"")
    target.with_name(target.name + ".done").touch()
    chain = _ReconcileChain(
        [_failed_torrent(save_path=str(tmp_path), path=str(tmp_path))],
        _failed_history(path=str(tmp_path)),
    )
    _patch_accounts(monkeypatch)
    monkeypatch.setattr(
        "app.chain.download.reconcile.find_complete_media", lambda *a, **k: None,
    )
    rolled, searched = [], []
    finished = threading.Event()
    _patch_subscribe_chain(monkeypatch, rolled, searched, finished)

    assert chain.reconcile_netdisk_failures() == 1
    assert finished.wait(1.0)
    assert searched == [7]
    assert not any(method == "transfer_completed" for method, _ in chain.marked)
