"""
3.2.2 为站点表增加站点类型与认证方式，支持 CD 网盘资源站的用户名密码认证。
"""

# Alembic 的 op 是运行期代理。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "b7e1c9d2f4a6"
down_revision = "97ad7239c1e0"
branch_labels = None
depends_on = None

_TABLE = "site"


def upgrade() -> None:
    """
    幂等增加站点类型、认证方式与 AUTH 凭据列，并回填存量站点默认值。
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "kind" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("kind", sa.String(), nullable=True, server_default="pt"),
        )
    if "auth_type" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("auth_type", sa.String(), nullable=True, server_default="cookie"),
        )
    if "username" not in columns:
        op.add_column(_TABLE, sa.Column("username", sa.String(), nullable=True))
    if "password" not in columns:
        op.add_column(_TABLE, sa.Column("password", sa.String(), nullable=True))
    if "auth_refresh_interval" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "auth_refresh_interval",
                sa.Integer(),
                nullable=True,
                server_default="300",
            ),
        )
    sites = sa.table(
        _TABLE,
        sa.column("kind", sa.String()),
        sa.column("auth_type", sa.String()),
    )
    op.execute(sites.update().where(sites.c.kind.is_(None)).values(kind="pt"))
    op.execute(
        sites.update().where(sites.c.auth_type.is_(None)).values(auth_type="cookie")
    )


def downgrade() -> None:
    """
    移除站点类型与认证方式相关列，恢复旧版站点结构。
    """
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name in (
        "auth_refresh_interval",
        "password",
        "username",
        "auth_type",
        "kind",
    ):
        if name in columns:
            op.drop_column(_TABLE, name)
