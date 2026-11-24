"""
3.2.8.31 内容审核记录新增审核档位列。

按组合矩阵把判定结果分流到四档（完全不可信、人工确认、AI 确认、系统确认），
供内容审核列表按档位筛选与人工分流。
"""

import sqlalchemy as sa
from alembic import op

revision = "a3d7f1c85b42"
down_revision = "c9e5a1f7b3d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    加审核档位列与索引；表或列已存在时跳过，兼容手工建过的实例。
    """
    inspector = sa.inspect(op.get_bind())
    if "contentverify" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("contentverify")}
    if "tier" in columns:
        return
    op.add_column(
        "contentverify",
        sa.Column("tier", sa.String(length=16), nullable=False, server_default=""),
    )
    op.create_index("ix_contentverify_tier", "contentverify", ["tier"])


def downgrade() -> None:
    """
    回滚本次新增的索引与列。
    """
    op.drop_index("ix_contentverify_tier", table_name="contentverify")
    op.drop_column("contentverify", "tier")
