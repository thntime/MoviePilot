"""
网盘账号的 ORM 模型。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class NetdiskAccount(Base):
    """
    网盘账号表。

    凭据以加密后的 JSON 文本保存，展示层只读取脱敏投影。
    """

    __table_args__ = (
        UniqueConstraint("platform", "name", name="uq_netdiskaccount_platform_name"),
    )

    id = get_id_column()
    # 平台标识：quark / uc / pan139 / pan123 / pan115 / guangya / baidu / xunlei
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 用户可读的账号名称，同平台内唯一
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Fernet 加密后的凭据 JSON
    credential_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # 是否为主账号，同平台最多一个
    is_primary: Mapped[bool] = mapped_column(Boolean(), default=False)
    # 账号状态：active / expired / disabled
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 已知过期时间
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # 最近一次探测时间
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # 创建时间
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.now)
    # 更新时间
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
    )
