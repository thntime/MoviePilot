"""
整理失败回滚订阅的单元测试。
"""

from types import SimpleNamespace

from app.chain.transfer.settlement import TransferSettlementOwner


def test_transfer_failure_rolls_back_subscribe(monkeypatch) -> None:
    """
    整理失败时按下载历史里的订阅来源回滚集数，避免订阅假完成。
    """
    owner = object.__new__(TransferSettlementOwner)
    history = SimpleNamespace(note={"source": 'Subscribe|{"id": 7}'}, episodes="E05")
    owner.download_history_repository = SimpleNamespace(
        get_by_hash=lambda _hash: history
    )
    calls = {}

    class _FakeSubscribe:
        """记录回滚调用，避免真实触发搜索。"""

        @staticmethod
        def parse_subscribe_source_keyword(value: str):
            """只识别测试用的订阅来源。"""
            return {"id": 7} if "Subscribe" in value else None

        def rollback_download_facts(self, subscribe_id, episodes) -> bool:
            """记录回滚参数。"""
            calls["rollback"] = (subscribe_id, set(episodes))
            return True

        def search(self, **_kwargs) -> None:
            """重搜在后台线程执行，测试不等待。"""

    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain", _FakeSubscribe)

    owner._rollback_subscribe_on_transfer_failure(  # pylint: disable=protected-access
        SimpleNamespace(download_hash="h1")
    )

    assert calls["rollback"] == (7, {5})
