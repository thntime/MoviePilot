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


def _store(tmp_path: Path) -> content_verify.ContentVerifyStore:
    """
    构造独立的记录存储。

    :param tmp_path: 临时目录
    :return: 存储实例
    """
    return content_verify.ContentVerifyStore(tmp_path / "content_verify.json")


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
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> content_verify.ContentVerifyStore:
    """
    装配独立存储并替换全局单例。

    :param tmp_path: 临时目录
    :param monkeypatch: pytest 补丁工具
    :return: 存储实例
    """
    instance = _store(tmp_path)
    monkeypatch.setattr(content_verify, "get_content_verify_store", lambda: instance)
    return instance


def test_list_returns_only_pending(store: content_verify.ContentVerifyStore) -> None:
    """列表只返回待处理记录，已处理的不再出现。"""
    store.save(_record("/downloads/a/a.mkv"))
    store.save(_record("/downloads/b/b.mkv", decision=content_verify.DECISION_MANUAL))

    payload = asyncio.run(history_api.content_verify_list(page=1, count=30, _=None))

    assert payload["total"] == 1
    assert payload["items"][0]["src"] == "/downloads/a/a.mkv"
    assert payload["pending_size"] == 2048


def test_list_exposes_score_and_confidence(store: content_verify.ContentVerifyStore) -> None:
    """列表回显得分、置信度与原因，供人判断。"""
    store.save(_record("/downloads/a/a.mkv"))

    payload = asyncio.run(history_api.content_verify_list(page=1, count=30, _=None))

    item = payload["items"][0]
    assert item["score"] == 3
    assert item["confidence"] == pytest.approx(0.91)
    assert item["reason"] == "文件名是另一部作品"
    assert item["hits"] == ["时长"]


def test_list_returns_empty_when_nothing_pending(store: content_verify.ContentVerifyStore) -> None:
    """没有待处理记录时返回空列表而不是报错。"""
    payload = asyncio.run(history_api.content_verify_list(page=1, count=30, _=None))

    assert payload == {"items": [], "total": 0, "pending_size": 0}


def test_accept_marks_manual(
    store: content_verify.ContentVerifyStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


def test_accept_missing_record_returns_404(store: content_verify.ContentVerifyStore) -> None:
    """记录不存在时返回 404。"""
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            history_api.content_verify_accept(ContentVerifyActionRequest(id="nope"), _=None)
        )

    assert error.value.status_code == 404


def test_delete_rejects_path_outside_download_dirs(
    store: content_verify.ContentVerifyStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    store: content_verify.ContentVerifyStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    store: content_verify.ContentVerifyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远程存储记录不允许本地删除。"""
    saved = store.save(_record("/remote/a.mkv", storage="cloud"))

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            history_api.content_verify_delete(ContentVerifyActionRequest(id=saved.id), _=None)
        )

    assert error.value.status_code == 400
