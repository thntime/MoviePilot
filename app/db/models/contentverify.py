"""
待处理内容校验记录的 ORM 模型。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class ContentVerify(Base):
    """
    下载完成但内容校验不通过的待处理记录。

    每条记录对应一次被拦下的整理：保留源路径、判定依据与抽帧图，供「内容审核」
    列表里人工点整理或删除。记录只增不删，处理结论写在 decision 上。
    """

    id = get_id_column()
    # 源路径（本地下载副本或网盘目录）
    src: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 源存储类型
    storage: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    # 目标媒体标题
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 媒体类型
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 季号文本
    season: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 集号文本
    episodes: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 本地证据得分
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 模型置信度
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 判否原因
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 命中的本地证据
    hits: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    # 抽帧图路径
    frames: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    # 文件数
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 文件总大小（字节）
    total_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 单集中位时长（秒）
    median_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 网盘平台
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 分享码
    share_code: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 下载任务标识
    download_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 处理状态：reject 待处理 / manual 已人工放行 / deleted 已删除
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="reject")
    # 判定时间
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=func.now())
    # 最后处理时间
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
