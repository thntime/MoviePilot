"""
合并两条迁移链：内容审核档位列（a3d7f1c85b42）与下载待确认表（c4e8f1a29b03）。

两条链都接在 d8a4c6e29170 之后，各自独立演进。合并上游后如果任其保持两个 head，
启动时的 `alembic upgrade head` 会因「存在多个 head」直接失败。这里只建立合并点、
不改动任何表结构：已升级到 a3d7f1c85b42 的实例会先补跑 c4e8f1a29b03，再落到本合并点。
"""

revision = "4c02986bd95d"
down_revision = ("a3d7f1c85b42", "c4e8f1a29b03")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    仅合并两条迁移分支，不改动表结构。
    """


def downgrade() -> None:
    """
    合并点没有可回滚的结构变化。
    """
