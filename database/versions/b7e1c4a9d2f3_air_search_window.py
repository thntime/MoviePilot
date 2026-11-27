"""
订阅新增播出窗口配置列。

定时搜索原本只按固定小时间隔空转；新增播出时刻与兜底周几后，定时搜索可以对齐
播出日历，只在更新之后的时间窗内搜索。两列都可空，存量订阅保持原行为。
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e1c4a9d2f3"
down_revision = "4c02986bd95d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    加播出时刻与兜底周几两列；表或列已存在时跳过，兼容手工建过的实例。
    """
    inspector = sa.inspect(op.get_bind())
    if "subscribe" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("subscribe")}
    if "air_search_time" not in columns:
        op.add_column("subscribe", sa.Column("air_search_time", sa.String(length=5), nullable=True))
    if "air_search_weekdays" not in columns:
        op.add_column(
            "subscribe",
            sa.Column("air_search_weekdays", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    """
    回滚本次新增的两列。
    """
    inspector = sa.inspect(op.get_bind())
    if "subscribe" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("subscribe")}
    if "air_search_weekdays" in columns:
        op.drop_column("subscribe", "air_search_weekdays")
    if "air_search_time" in columns:
        op.drop_column("subscribe", "air_search_time")
