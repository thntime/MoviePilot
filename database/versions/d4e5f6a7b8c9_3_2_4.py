"""
3.2.4 网盘平台标识 c139 重命名为 pan139。

只迁移账号表的平台字段；历史任务索引在应用启动时按别名归一化。
"""

# Alembic 的 op 是运行期代理。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

_TABLE = "netdiskaccount"


def _has_table() -> bool:
    """
    判断网盘账号表是否存在。

    :return: 是否存在
    """
    return _TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    """
    把历史 c139 账号迁移为 pan139。
    """
    if not _has_table():
        return
    op.execute(sa.text(f"UPDATE {_TABLE} SET platform = 'pan139' WHERE platform = 'c139'"))


def downgrade() -> None:
    """
    回滚平台标识。
    """
    if not _has_table():
        return
    op.execute(sa.text(f"UPDATE {_TABLE} SET platform = 'c139' WHERE platform = 'pan139'"))
