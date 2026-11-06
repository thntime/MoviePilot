"""
验证手动下载与订阅下载在容量/取链失败后的不同处理。
"""

import threading
from types import SimpleNamespace

import pytest

from app.application.history import DownloadHistorySnapshot
from app.application.netdisk import selection
from app.chain.download.reconcile import DownloadFailureReconcileOwner
from app.domain.netdisk import NetdiskPlatform


@pytest.fixture(autouse=True)
def _clear_state():
    """
    每个用例开始前清空平台/账号冷却与告警状态。
    """
    selection.clear_selection_state()
    yield
    selection.clear_selection_state()


class _ReconcileChain(DownloadFailureReconcileOwner):
    """
    用最小依赖驱动对账 owner。
    """

    def __init__(self, torrents, history_by_hash):
        """
        保存任务与下载历史查询结果。
        """
        self._torrents = torrents
        self.download_history_repository = SimpleNamespace(
            get_by_hash=lambda task_hash: history_by_hash.get(task_hash),
        )
        self.marked = []
        self.recorded = []
        self.download_failure_repository = SimpleNamespace(
            record_failure=self.recorded.append,
        )

    def list_torrents(self, *args, **kwargs):
        """
        返回预置失败任务。
        """
        return self._torrents

    def run_module(self, method, **kwargs):
        """
        记录模块调用。
        """
        self.marked.append((method, kwargs))
        return None

    def post_message(self, message):
        """
        记录告警消息。
        """
        self.marked.append(("message", message))

    def is_download_file_transferred(self, fullpath: str) -> bool:
        """
        对账用例不涉及文件整理状态。
        """
        return False


class _Calls:
    """
    记录订阅回滚与重新搜索调用。
    """

    def __init__(self):
        """
        初始化调用记录。
        """
        self.rolled = []
        self.searched = []
        self.finished = threading.Event()


def _fake_subscribe_chain(calls: _Calls):
    """
    构造订阅链替身类，避免触发真实订阅流程。
    """

    class FakeSubscribeChain:
        """记录回滚与搜索调用。"""

        @staticmethod
        def parse_subscribe_source_keyword(source: str):
            """解析测试来源。"""
            return {"id": 7} if str(source or "").startswith("Subscribe|") else None

        def rollback_download_facts(self, subscribe_id, episodes):
            """记录回滚集数。"""
            calls.rolled.append((subscribe_id, list(episodes)))
            return True

        def search(self, sid=None, **kwargs):
            """记录搜索并通知测试。"""
            calls.searched.append(sid)
            calls.finished.set()

    return FakeSubscribeChain


def _torrent(task_hash: str, *, account_id: int = 3):
    """
    构造容量不足的网盘失败任务。
    """
    return SimpleNamespace(
        netdisk_platform="quark",
        state="error",
        hash=task_hash,
        reconciled=False,
        error="网盘账号剩余空间不足：需要 100 字节，可用 1 字节",
        title="示例",
        account_id=account_id,
    )


def _history(source: str, fingerprint: str = "fp-1"):
    """
    用真实历史快照形状构造带来源与资源指纹的下载历史。
    """
    return DownloadHistorySnapshot(
        id=1,
        path="/downloads/示例",
        type="电视剧",
        title="示例剧集",
        year="2024",
        media_source="tmdb",
        media_id="100",
        seasons="S01",
        episodes="E05",
        torrent_name="示例剧集 S01E05",
        torrent_site="示例站",
        note={"source": source, "failure_fingerprint": fingerprint},
    )


def test_manual_failure_keeps_error_without_research(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A4：手动下载失败只标记对账，不回滚、不重新搜索。
    """
    calls = _Calls()
    monkeypatch.setattr(
        "app.chain.subscribe.facade.SubscribeChain", _fake_subscribe_chain(calls),
    )
    chain = _ReconcileChain(
        [_torrent("manual-1")],
        {"manual-1": _history("Manual")},
    )

    assert chain.reconcile_netdisk_failures() == 1
    assert calls.rolled == []
    assert calls.searched == []
    assert ("mark_reconciled", {"hashs": ["manual-1"]}) in chain.marked
    assert not any(item[0] == "message" for item in chain.marked)
    assert chain.recorded == []
    assert selection.is_account_cooled(3) is True


def test_subscribe_failure_rolls_back_and_alerts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A5：订阅失败回滚集数、触发重搜，容量告警只发一次。
    """
    calls = _Calls()
    monkeypatch.setattr(
        "app.chain.subscribe.facade.SubscribeChain", _fake_subscribe_chain(calls),
    )
    chain = _ReconcileChain(
        [_torrent("sub-1"), _torrent("sub-2")],
        {
            "sub-1": _history('Subscribe|{"id": 7}'),
            "sub-2": _history('Subscribe|{"id": 7}'),
        },
    )

    assert chain.reconcile_netdisk_failures() == 2
    assert calls.rolled == [(7, [5]), (7, [5])]
    assert sorted(calls.searched) == [7, 7]
    messages = [item for item in chain.marked if item[0] == "message"]
    assert len(messages) == 1
    assert len(chain.recorded) == 2
    assert chain.recorded[0].fingerprint == "fp-1"
    assert chain.recorded[0].error_message
    assert selection.is_account_cooled(3) is True
    assert selection.is_platform_cooled(NetdiskPlatform.QUARK) is False


def test_subscribe_capacity_without_fingerprint_skips_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    历史缺少资源指纹时不写台账，但仍完成回滚与重搜。
    """
    calls = _Calls()
    monkeypatch.setattr(
        "app.chain.subscribe.facade.SubscribeChain", _fake_subscribe_chain(calls),
    )
    history = DownloadHistorySnapshot(
        id=1,
        path="/downloads/示例",
        type="电视剧",
        title="示例剧集",
        media_source="tmdb",
        media_id="100",
        seasons="S01",
        episodes="E05",
        torrent_name="示例剧集 S01E05",
        note={"source": 'Subscribe|{"id": 7}'},
    )
    chain = _ReconcileChain([_torrent("sub-3")], {"sub-3": history})

    assert chain.reconcile_netdisk_failures() == 1
    assert chain.recorded == []
    assert calls.rolled == [(7, [5])]
