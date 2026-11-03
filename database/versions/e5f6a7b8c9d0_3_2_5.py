"""
3.2.5 网盘账号补充昵称与空间配额字段。
"""

# Alembic 的 op 是运行期代理。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_TABLE = "netdiskaccount"


def _columns() -> set[str]:
    """
    返回现有列名。

    :return: 列名集合
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """
    增加昵称与空间配额字段。
    """
    columns = _columns()
    if not columns:
        return
    with op.batch_alter_table(_TABLE) as batch:
        if "nickname" not in columns:
            batch.add_column(sa.Column("nickname", sa.String(64), nullable=True))
        if "quota_used" not in columns:
            batch.add_column(sa.Column("quota_used", sa.BigInteger(), nullable=True, server_default="0"))
        if "quota_total" not in columns:
            batch.add_column(sa.Column("quota_total", sa.BigInteger(), nullable=True, server_default="0"))


def downgrade() -> None:
    """
    移除新增字段。
    """
    columns = _columns()
    if not columns:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name in ("quota_total", "quota_used", "nickname"):
            if name in columns:
                batch.drop_column(name)
