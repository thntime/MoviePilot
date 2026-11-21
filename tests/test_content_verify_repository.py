"""
验证待处理内容校验记录的数据库实现：建表、写入、按状态查询与状态流转。
"""

from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.netdisk import content_verify
from app.db.adapters.contentverify import SqlAlchemyContentVerifyRepository
from app.db.models.contentverify import ContentVerify


@pytest.fixture()
def repository() -> Iterator[SqlAlchemyContentVerifyRepository]:
    """
    在内存 SQLite 上建表并返回仓库实现。

    :return: 仓库实现
    """
    engine = create_engine("sqlite://")
    ContentVerify.__table__.create(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    try:
        yield SqlAlchemyContentVerifyRepository(sync_session=factory)
    finally:
        engine.dispose()


def _record(**kwargs: object) -> content_verify.VerifyRecord:
    """
    构造一条记录。

    :param kwargs: 覆盖字段
    :return: 记录对象
    """
    fields: dict[str, object] = {
        "src": "/downloads/share/a.mkv",
        "storage": "local",
        "title": "斗破苍穹",
        "media_type": "电视剧",
        "season": "1",
        "episodes": "E01-E02",
        "score": 3,
        "confidence": 0.93,
        "reason": "文件名是另一部作品",
        "hits": ["时长"],
        "frames": ["/config/frames/a.jpg"],
        "file_count": 2,
        "total_size": 4096,
        "median_duration": 1300,
        "platform": "quark",
        "share_code": "abc",
        "download_hash": "h1",
    }
    fields.update(kwargs)
    return content_verify.VerifyRecord(**fields)  # type: ignore[arg-type]


def test_save_assigns_id_and_timestamps(
    repository: SqlAlchemyContentVerifyRepository,
) -> None:
    """写入后拿到自增主键与判定时间。"""
    saved = repository.save(_record())

    assert saved.id.isdigit()
    assert saved.created_at
    assert saved.decision == content_verify.DECISION_REJECT


def test_list_returns_pending_only(repository: SqlAlchemyContentVerifyRepository) -> None:
    """默认只列出待处理记录，处理过的不再出现。"""
    pending = repository.save(_record(src="/downloads/a.mkv"))
    repository.save(_record(src="/downloads/b.mkv", decision=content_verify.DECISION_MANUAL))

    rows = repository.list()

    assert [row.id for row in rows] == [pending.id]
    assert rows[0].src == "/downloads/a.mkv"


def test_round_trips_all_fields(repository: SqlAlchemyContentVerifyRepository) -> None:
    """证据、抽帧与体积等字段原样回读。"""
    repository.save(_record())

    row = repository.list()[0]

    assert row.hits == ["时长"]
    assert row.frames == ["/config/frames/a.jpg"]
    assert row.total_size == 4096
    assert row.confidence == pytest.approx(0.93)
    assert row.platform == "quark"


def test_get_returns_none_for_missing(repository: SqlAlchemyContentVerifyRepository) -> None:
    """不存在的标识与非法标识都返回空。"""
    assert repository.get("999") is None
    assert repository.get("not-a-number") is None


def test_update_decision_moves_record_out_of_pending(
    repository: SqlAlchemyContentVerifyRepository,
) -> None:
    """人工处理后记录离开待处理列表。"""
    saved = repository.save(_record())

    updated = repository.update_decision(saved.id, content_verify.DECISION_MANUAL)

    assert updated is not None and updated.decision == content_verify.DECISION_MANUAL
    assert repository.list() == []
    assert repository.list(content_verify.DECISION_MANUAL)[0].id == saved.id
