"""
3.2.8.18 新增待处理内容校验记录表。

记录来自下载后内容校验的判否，供「内容审核」列表人工整理或删除。
"""

import sqlalchemy as sa
from alembic import op

revision = "c9e5a1f7b3d2"
down_revision = "d8a4c6e29170"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    建表；已存在时跳过，兼容手工建过表的实例。
    """
    inspector = sa.inspect(op.get_bind())
    if "contentverify" in inspector.get_table_names():
        return
    op.create_table(
        "contentverify",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("src", sa.Text(), nullable=False, server_default=""),
        sa.Column("storage", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("media_type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("season", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("episodes", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("hits", sa.JSON(), nullable=False),
        sa.Column("frames", sa.JSON(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("median_duration", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("platform", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("share_code", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("download_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("decision", sa.String(length=16), nullable=False, server_default="reject"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_contentverify_decision", "contentverify", ["decision"])


def downgrade() -> None:
    """
    删表；只回滚本次新增结构。
    """
    op.drop_index("ix_contentverify_decision", table_name="contentverify")
    op.drop_table("contentverify")
