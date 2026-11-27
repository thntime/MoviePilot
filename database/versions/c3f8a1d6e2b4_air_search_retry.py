"""
订阅新增播出窗口重试间隔列。

窗口只有 48 小时，按小时间隔重试会把窗口耗在等下一次到期上；新增分钟级间隔后，
窗口内可以按 15 分钟（默认）到任意分钟数重试。列可空，存量订阅按默认 15 分钟处理。
"""

import sqlalchemy as sa
from alembic import op

revision = "c3f8a1d6e2b4"
down_revision = "b7e1c4a9d2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    加窗口内重试间隔列；表或列已存在时跳过，兼容手工建过的实例。
    """
    inspector = sa.inspect(op.get_bind())
    if "subscribe" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("subscribe")}
    if "air_search_interval_minutes" in columns:
        return
    op.add_column(
        "subscribe",
        sa.Column("air_search_interval_minutes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """
    回滚本次新增的列。
    """
    inspector = sa.inspect(op.get_bind())
    if "subscribe" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("subscribe")}
    if "air_search_interval_minutes" in columns:
        op.drop_column("subscribe", "air_search_interval_minutes")
