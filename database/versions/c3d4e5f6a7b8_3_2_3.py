"""
3.2.3 新增网盘账号表，保存加密凭据、主账号标记与登录态。

凭据由应用层加密后写入 `credential_enc`，数据库不保存明文。
"""

# Alembic 的 op 是运行期代理。
# pylint: disable=no-member

import json

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b7e1c9d2f4a6"
branch_labels = None
depends_on = None

_TABLE = "netdiskaccount"


def upgrade() -> None:
    """
    幂等创建网盘账号表及其索引。
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("credential_enc", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=True, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=True, server_default="active"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("platform", "name", name="uq_netdiskaccount_platform_name"),
    )
    op.create_index("ix_netdiskaccount_platform", _TABLE, ["platform"])
    _migrate_downloader_type("quark", "netdisk")


def _migrate_downloader_type(source: str, target: str) -> None:
    """
    把下载器配置中的历史类型改写为新类型，保留其余字段。

    :param source: 历史类型值
    :param target: 新类型值
    """
    connection = op.get_bind()
    if "systemconfig" not in sa.inspect(connection).get_table_names():
        return
    rows = connection.execute(
        sa.text("SELECT id, CAST(value AS TEXT) AS value FROM systemconfig WHERE key = 'Downloaders'")
    ).fetchall()
    for row in rows:
        try:
            configs = json.loads(row.value or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(configs, list):
            continue
        changed = False
        for item in configs:
            if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == source:
                item["type"] = target
                changed = True
        if not changed:
            continue
        connection.execute(
            sa.text("UPDATE systemconfig SET value = :value WHERE id = :id"),
            {"value": json.dumps(configs, ensure_ascii=False), "id": row.id},
        )


def downgrade() -> None:
    """
    删除网盘账号表与索引。
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    _migrate_downloader_type("netdisk", "quark")
    op.drop_table(_TABLE)
