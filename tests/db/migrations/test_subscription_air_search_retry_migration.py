"""
订阅播出窗口重试间隔列的 Alembic 可逆迁移测试。
"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.c3f8a1d6e2b4_air_search_retry"


def _bind_migration(monkeypatch, connection):
    """
    把重试间隔迁移绑定到隔离 SQLite 连接。
    """
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_legacy_subscribe(connection) -> None:
    """
    创建迁移前的最小订阅表和一条已配置播出时刻的记录。
    """
    metadata = sa.MetaData()
    subscribe = sa.Table(
        "subscribe",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("air_search_time", sa.String(5), nullable=True),
    )
    metadata.create_all(connection)
    connection.execute(
        subscribe.insert(),
        {"id": 1, "name": "存量订阅", "air_search_time": "11:00"},
    )


def test_air_search_retry_migration_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    """
    存量订阅获得空值重试间隔列（按默认 15 分钟处理），迁移可重复执行并完整回滚。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_subscribe(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscribe")
        }
        assert "air_search_interval_minutes" in columns
        row = connection.execute(
            sa.text(
                "SELECT air_search_time, air_search_interval_minutes "
                "FROM subscribe WHERE id = 1"
            )
        ).mappings().one()
        assert dict(row) == {"air_search_time": "11:00", "air_search_interval_minutes": None}

        migration.downgrade()
        downgraded = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscribe")
        }
        assert "air_search_interval_minutes" not in downgraded

        migration.upgrade()
        assert connection.execute(
            sa.text("SELECT air_search_interval_minutes FROM subscribe WHERE id = 1")
        ).scalar_one() is None
    engine.dispose()


def test_air_search_retry_migration_skips_missing_table(monkeypatch) -> None:
    """
    订阅表不存在时迁移直接跳过，不抛异常。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        migration.downgrade()
        assert "subscribe" not in sa.inspect(connection).get_table_names()
    engine.dispose()
