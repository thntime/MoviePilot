"""
待处理内容校验记录表 c9e5a1f7b3d2 的 Alembic 迁移测试。
"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.c9e5a1f7b3d2_3_2_8_18"


def _bind(monkeypatch, connection):
    """
    把迁移绑定到隔离数据库连接。
    """
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    return migration


@pytest.fixture()
def connection():
    """
    提供内存 SQLite 连接。
    """
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        yield conn
    engine.dispose()


def test_upgrade_creates_content_verify_table(monkeypatch, connection):
    """
    升级后表、字段与索引都存在。
    """
    migration = _bind(monkeypatch, connection)
    migration.upgrade()

    inspector = sa.inspect(connection)
    assert "contentverify" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("contentverify")}
    assert {
        "src", "storage", "title", "media_type", "season", "episodes", "score",
        "confidence", "reason", "hits", "frames", "file_count", "total_size",
        "median_duration", "platform", "share_code", "download_hash", "decision",
        "created_at", "updated_at",
    } <= columns
    indexes = {index["name"] for index in inspector.get_indexes("contentverify")}
    assert "ix_contentverify_decision" in indexes


def test_upgrade_is_idempotent(monkeypatch, connection):
    """
    表已存在时重复升级不报错。
    """
    migration = _bind(monkeypatch, connection)
    migration.upgrade()
    migration.upgrade()

    assert "contentverify" in sa.inspect(connection).get_table_names()


def test_downgrade_drops_table(monkeypatch, connection):
    """
    回滚后表被移除。
    """
    migration = _bind(monkeypatch, connection)
    migration.upgrade()
    migration.downgrade()

    assert "contentverify" not in sa.inspect(connection).get_table_names()
