"""
待处理内容校验记录的表级数据访问入口。

只负责在当前会话内增删改查，提交与回滚由调用方事务管理。
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.contentverify import ContentVerify


def _row(model: ContentVerify) -> Dict[str, Any]:
    """
    把模型投影成脱离会话的普通字典。

    :param model: 记录模型
    :return: 字段字典
    """
    return {
        "id": str(model.id),
        "src": model.src or "",
        "storage": model.storage or "local",
        "title": model.title or "",
        "media_type": model.media_type or "",
        "season": model.season or "",
        "episodes": model.episodes or "",
        "score": int(model.score or 0),
        "confidence": float(model.confidence or 0.0),
        "reason": model.reason or "",
        "hits": list(model.hits or []),
        "frames": list(model.frames or []),
        "file_count": int(model.file_count or 0),
        "total_size": int(model.total_size or 0),
        "median_duration": int(model.median_duration or 0),
        "platform": model.platform or "",
        "share_code": model.share_code or "",
        "download_hash": model.download_hash or "",
        "decision": model.decision or "reject",
        "created_at": model.created_at.strftime("%Y-%m-%d %H:%M:%S") if model.created_at else "",
        "updated_at": model.updated_at.strftime("%Y-%m-%d %H:%M:%S") if model.updated_at else "",
    }


class ContentVerifyOper(DbOper):
    """
    待处理内容校验记录管理。
    """

    def create(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        在当前会话暂存一条新记录。

        :param values: 列值
        :return: 记录字典
        """

        def stage(session: Session) -> Dict[str, Any]:
            """
            写入并回读自增主键与时间戳。

            :param session: 调用方会话
            :return: 记录字典
            """
            record = ContentVerify(**values)
            session.add(record)
            session.flush()
            session.refresh(record)
            return _row(record)

        return self._execute_sync_write(stage)

    def list_all(self, decision: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        按判定时间倒序列出记录。

        :param decision: 只列出该状态的记录，为空时列出全部
        :return: 记录字典列表
        """

        def query(session: Session) -> List[Dict[str, Any]]:
            """
            执行查询。

            :param session: 调用方会话
            :return: 记录字典列表
            """
            statement = select(ContentVerify).order_by(ContentVerify.id.desc())
            if decision:
                statement = statement.where(ContentVerify.decision == decision)
            return [_row(record) for record in session.execute(statement).scalars().all()]

        return self._execute_sync_query(query)

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        """
        按主键读取一条记录。

        :param record_id: 记录主键
        :return: 记录字典；不存在时为空
        """

        def query(session: Session) -> Optional[Dict[str, Any]]:
            """
            执行查询。

            :param session: 调用方会话
            :return: 记录字典
            """
            record = session.get(ContentVerify, _as_int(record_id))
            return _row(record) if record is not None else None

        return self._execute_sync_query(query)

    def update_decision(self, record_id: str, decision: str) -> Optional[Dict[str, Any]]:
        """
        更新处理状态。

        :param record_id: 记录主键
        :param decision: 新状态
        :return: 更新后的记录字典；不存在时为空
        """

        def stage(session: Session) -> Optional[Dict[str, Any]]:
            """
            在当前会话更新并回读。

            :param session: 调用方会话
            :return: 记录字典
            """
            record = session.get(ContentVerify, _as_int(record_id))
            if record is None:
                return None
            record.decision = decision
            session.flush()
            session.refresh(record)
            return _row(record)

        return self._execute_sync_write(stage)


def _as_int(value: Any) -> int:
    """
    把外部传入的记录标识转成主键类型。

    :param value: 原始标识
    :return: 整数主键；无法转换时返回不可能命中的 -1
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return -1
