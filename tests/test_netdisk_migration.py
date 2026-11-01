"""
网盘账号表 Alembic 迁移测试。
"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.c3d4e5f6a7b8_3_2_3"


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


def test_upgrade_creates_netdisk_account_table(monkeypatch, connection):
    """
    升级后表、唯一约束与索引存在。
    """
    migration = _bind(monkeypatch, connection)
    migration.upgrade()
    inspector = sa.inspect(connection)
    assert "netdiskaccount" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("netdiskaccount")}
    assert {"platform", "name", "credential_enc", "is_primary", "status", "expires_at"} <= columns
    uniques = inspector.get_unique_constraints("netdiskaccount")
    assert any(constraint["name"] == "uq_netdiskaccount_platform_name" for constraint in uniques)


def test_upgrade_is_idempotent(monkeypatch, connection):
    """
    重复升级不报错。
    """
    migration = _bind(monkeypatch, connection)
    migration.upgrade()
    migration.upgrade()
    assert "netdiskaccount" in sa.inspect(connection).get_table_names()


def test_upgrade_migrates_quark_downloader_config(monkeypatch, connection):
    """
    升级时把历史 quark 下载器配置改写为 netdisk，并保留其他字段。
    """
    connection.execute(
        sa.text("CREATE TABLE systemconfig (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
    )
    payload = '[{"name":"夸克","type":"quark","config":{"cookie":"x"}}]'
    connection.execute(
        sa.text("INSERT INTO systemconfig (id, key, value) VALUES (1, 'Downloaders', :value)"),
        {"value": payload},
    )
    migration = _bind(monkeypatch, connection)
    migration.upgrade()
    stored = connection.execute(
        sa.text("SELECT value FROM systemconfig WHERE id = 1")
    ).scalar_one()
    assert '"type": "netdisk"' in stored
    assert '"cookie": "x"' in stored


def test_downgrade_drops_table(monkeypatch, connection):
    """
    回滚后表被删除。
    """
    migration = _bind(monkeypatch, connection)
    migration.upgrade()
    migration.downgrade()
    assert "netdiskaccount" not in sa.inspect(connection).get_table_names()
