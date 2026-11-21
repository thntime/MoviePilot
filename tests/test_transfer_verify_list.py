"""
验证「内容审核」列表：列表只出待处理、人工放行、删除路径校验与状态流转。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi import HTTPException

from app.api.endpoints import history as history_api
from app.application.netdisk import content_verify
from app.schemas.history import ContentVerifyActionRequest


class MemoryContentVerifyRepository:
    """
    内存版待处理记录仓库，替代数据库实现供接口用例使用。
    """

    def __init__(self) -> None:
        """初始化空记录表。"""
        self._rows: dict[str, content_verify.VerifyRecord] = {}
        self._sequence = 0

    def save(self, record: content_verify.VerifyRecord) -> content_verify.VerifyRecord:
        """
        新增一条记录并分配主键。

        :param record: 待写入记录
        :return: 写入后的记录
        """
        self._sequence += 1
        record.id = record.id or str(self._sequence)
        record.created_at = record.created_at or "2026-11-21 00:00:00"
        record.updated_at = "2026-11-21 00:00:00"
        self._rows[record.id] = record
        return record

    def list(self, decision: str | None = None) -> List[content_verify.VerifyRecord]:
        """
        按状态列出记录。

        :param decision: 处理状态；为空时返回待处理记录
        :return: 记录列表
        """
        target = decision or content_verify.DECISION_REJECT
        return [row for row in self._rows.values() if row.decision == target]

    def get(self, record_id: str) -> content_verify.VerifyRecord | None:
        """
        读取单条记录。

        :param record_id: 记录标识
        :return: 记录；不存在时为空
        """
        return self._rows.get(str(record_id))

    def update_decision(
        self, record_id: str, decision: str
    ) -> content_verify.VerifyRecord | None:
        """
        更新处理状态。

        :param record_id: 记录标识
        :param decision: 新状态
        :return: 更新后的记录
        """
        record = self._rows.get(str(record_id))
        if record is None:
            return None
        record.decision = decision
        return record


def _record(src: str, decision: str = content_verify.DECISION_REJECT, **kwargs: Any) -> Any:
    """
    构造一条记录。

    :param src: 源路径
    :param decision: 处理状态
    :param kwargs: 其他字段
    :return: 记录对象
    """
    fields: dict[str, Any] = {
        "src": src,
        "storage": "local",
        "title": "斗破苍穹",
        "media_type": "电视剧",
        "season": "1",
        "episodes": "E01-E02",
        "score": 3,
        "confidence": 0.91,
        "reason": "文件名是另一部作品",
        "hits": ["时长"],
        "file_count": 2,
        "total_size": 2048,
        "median_duration": 1300,
        "decision": decision,
    }
    fields.update(kwargs)
    return content_verify.VerifyRecord(**fields)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryContentVerifyRepository:
    """
    装配内存仓库并替换已注册的持久化实现。

    :param monkeypatch: pytest 补丁工具
    :return: 仓库实例
    """
    instance = MemoryContentVerifyRepository()
    monkeypatch.setattr(content_verify, "get_content_verify_store", lambda: instance)
    return instance


def test_list_returns_only_pending(store: MemoryContentVerifyRepository) -> None:
    """列表只返回待处理记录，已处理的不再出现。"""
    store.save(_record("/downloads/a/a.mkv"))
    store.save(_record("/downloads/b/b.mkv", decision=content_verify.DECISION_MANUAL))

    payload = asyncio.run(history_api.content_verify_list(page=1, count=30, _=None))

    assert payload["total"] == 1
    assert payload["items"][0]["src"] == "/downloads/a/a.mkv"
    assert payload["pending_size"] == 2048


def test_list_exposes_score_and_confidence(store: MemoryContentVerifyRepository) -> None:
    """列表回显得分、置信度与原因，供人判断。"""
    store.save(_record("/downloads/a/a.mkv"))

    payload = asyncio.run(history_api.content_verify_list(page=1, count=30, _=None))

    item = payload["items"][0]
    assert item["score"] == 3
    assert item["confidence"] == pytest.approx(0.91)
    assert item["reason"] == "文件名是另一部作品"
    assert item["hits"] == ["时长"]


def test_list_returns_empty_when_nothing_pending(store: MemoryContentVerifyRepository) -> None:
    """没有待处理记录时返回空列表而不是报错。"""
    payload = asyncio.run(history_api.content_verify_list(page=1, count=30, _=None))

    assert payload == {"items": [], "total": 0, "pending_size": 0}


def test_accept_marks_manual(
    store: MemoryContentVerifyRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人工放行后走一次强制整理，并把记录标记为已处理。"""
    source = tmp_path / "a.mkv"
    source.write_text("x", encoding="utf-8")
    saved = store.save(_record(str(source)))
    calls: List[dict] = []

    class _FakeChain:
        """记录整理调用并返回成功。"""

        def do_transfer(self, **kwargs: Any) -> tuple:
            """记录参数并返回成功。

            :param kwargs: 整理参数
            :return: (成功, 说明)
            """
            calls.append(kwargs)
            return True, "已整理"

    monkeypatch.setattr("app.chain.transfer.facade.TransferChain", _FakeChain)

    payload = asyncio.run(
        history_api.content_verify_accept(ContentVerifyActionRequest(id=saved.id), _=None)
    )

    assert payload["success"] is True
    assert calls and calls[0]["manual"] is True and calls[0]["force"] is True
    assert store.get(saved.id).decision == content_verify.DECISION_MANUAL


def test_accept_missing_record_returns_404(store: MemoryContentVerifyRepository) -> None:
    """记录不存在时返回 404。"""
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            history_api.content_verify_accept(ContentVerifyActionRequest(id="nope"), _=None)
        )

    assert error.value.status_code == 404


def test_delete_rejects_path_outside_download_dirs(
    store: MemoryContentVerifyRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路径不在下载目录内时拒绝删除。"""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    outside = tmp_path / "library" / "a.mkv"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    saved = store.save(_record(str(outside)))
    monkeypatch.setattr(
        "app.application.directory.DirectoryHelper.get_download_dirs",
        lambda self: [SimpleNamespace(download_path=str(download_dir))],
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            history_api.content_verify_delete(ContentVerifyActionRequest(id=saved.id), _=None)
        )

    assert error.value.status_code == 400
    assert outside.exists()
    assert store.get(saved.id).decision == content_verify.DECISION_REJECT


def test_delete_removes_file_and_marks_deleted(
    store: MemoryContentVerifyRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载目录内的文件删除成功，并把记录标记为已删除。"""
    download_dir = tmp_path / "downloads"
    target = download_dir / "a.mkv"
    download_dir.mkdir()
    target.write_text("x", encoding="utf-8")
    saved = store.save(_record(str(target)))
    monkeypatch.setattr(
        "app.application.directory.DirectoryHelper.get_download_dirs",
        lambda self: [SimpleNamespace(download_path=str(download_dir))],
    )

    payload = asyncio.run(
        history_api.content_verify_delete(ContentVerifyActionRequest(id=saved.id), _=None)
    )

    assert payload["success"] is True
    assert not target.exists()
    assert store.get(saved.id).decision == content_verify.DECISION_DELETED
    assert store.list() == []


def test_delete_rejects_remote_storage(
    store: MemoryContentVerifyRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远程存储记录不允许本地删除。"""
    saved = store.save(_record("/remote/a.mkv", storage="cloud"))

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            history_api.content_verify_delete(ContentVerifyActionRequest(id=saved.id), _=None)
        )

    assert error.value.status_code == 400
