"""
订阅播出窗口配置列的 Alembic 可逆迁移测试。
"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.b7e1c4a9d2f3_air_search_window"


def _bind_migration(monkeypatch, connection):
    """
    把播出窗口迁移绑定到隔离 SQLite 连接。
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
    创建迁移前的最小订阅表和一条存量订阅记录。
    """
    metadata = sa.MetaData()
    subscribe = sa.Table(
        "subscribe",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("search_interval", sa.Integer(), nullable=True),
    )
    metadata.create_all(connection)
    connection.execute(subscribe.insert(), {"id": 1, "name": "存量订阅", "search_interval": 24})


def test_air_search_window_migration_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    """
    存量订阅应获得两个空值列，迁移可重复执行并完整回滚。
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
        assert {"air_search_time", "air_search_weekdays"}.issubset(columns)
        row = connection.execute(
            sa.text(
                "SELECT search_interval, air_search_time, air_search_weekdays "
                "FROM subscribe WHERE id = 1"
            )
        ).mappings().one()
        # 存量订阅不因迁移改变原有搜索间隔，两个新列保持空值即原行为
        assert dict(row) == {
            "search_interval": 24,
            "air_search_time": None,
            "air_search_weekdays": None,
        }

        migration.downgrade()
        downgraded = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscribe")
        }
        assert "air_search_time" not in downgraded
        assert "air_search_weekdays" not in downgraded

        migration.upgrade()
        assert connection.execute(
            sa.text("SELECT air_search_time FROM subscribe WHERE id = 1")
        ).scalar_one() is None
    engine.dispose()


def test_air_search_window_migration_skips_missing_table(monkeypatch) -> None:
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
