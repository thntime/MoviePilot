"""
验证网盘后台失败对账、订阅事实回滚与换源触发。
"""

import threading
from types import SimpleNamespace

import pytest

from app.application.netdisk import selection
from app.chain.download.reconcile import DownloadFailureReconcileOwner
from app.chain.subscribe.completion import SubscribeCompletionOwner
from app.domain.netdisk import NetdiskPlatform


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
    对账任务回滚订阅集数、冷却平台、标记任务并触发重新搜索。
    """
    torrent = SimpleNamespace(
        netdisk_platform="quark", state="error", hash="h1", reconciled=False,
        error="", title="示例",
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
    assert selection.is_platform_cooled(NetdiskPlatform.QUARK)
    assert chain.marked == [("mark_reconciled", {"hashs": ["h1"]})]


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
