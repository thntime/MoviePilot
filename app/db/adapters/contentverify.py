"""
待处理内容校验记录的数据库实现。

使用独立短 Session 完成读写，提交与回滚在本适配器内闭环。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Callable, List, Optional, TypeVar

from sqlalchemy.orm import Session

from app.application.netdisk.content_verify import (
    DECISION_REJECT,
    VerifyRecord,
    record_from_dict,
)
from app.db.oper.contentverify import ContentVerifyOper
from app.db.uow import SqlAlchemyUnitOfWork

T = TypeVar("T")


class SqlAlchemyContentVerifyRepository:
    """
    通过独立短 Session 实现待处理记录持久化端口。
    """

    def __init__(self, *, sync_session: Callable[[], Session]) -> None:
        """
        保存同步会话工厂。

        :param sync_session: 同步会话工厂
        """
        self._sync_session = sync_session

    def _read(self, operation: Callable[[ContentVerifyOper], T]) -> T:
        """
        在独立会话中执行只读操作。

        :param operation: 接收 Oper 的调用
        :return: 调用结果
        """
        with self._sync_session() as session:
            return operation(ContentVerifyOper(db=session))

    def _write(self, operation: Callable[[ContentVerifyOper], T]) -> T:
        """
        在独立事务中执行写操作。

        :param operation: 接收 Oper 的调用
        :return: 调用结果
        """
        with self._sync_session() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(ContentVerifyOper(db=session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    def save(self, record: VerifyRecord) -> VerifyRecord:
        """
        新增一条记录，判定时间与处理时间由数据库默认值填充。

        :param record: 待写入记录
        :return: 写入后的记录
        """
        values = asdict(record)
        for key in ("id", "created_at", "updated_at"):
            values.pop(key, None)
        values["decision"] = record.decision or DECISION_REJECT
        saved = self._write(lambda oper: oper.create(values))
        return record_from_dict(saved)

    def list(self, decision: Optional[str] = None) -> List[VerifyRecord]:
        """
        按状态读取记录。

        :param decision: 处理状态；为空时返回全部状态的记录
        :return: 记录列表
        """
        rows = self._read(lambda oper: oper.list_all(decision))
        return [record_from_dict(row) for row in rows]

    def get(self, record_id: str) -> Optional[VerifyRecord]:
        """
        读取单条记录。

        :param record_id: 记录标识
        :return: 记录；不存在时为空
        """
        row = self._read(lambda oper: oper.get(record_id))
        return record_from_dict(row) if row else None

    def update_decision(self, record_id: str, decision: str) -> Optional[VerifyRecord]:
        """
        更新记录的处理状态。

        :param record_id: 记录标识
        :param decision: 新状态
        :return: 更新后的记录；不存在时为空
        """
        row = self._write(lambda oper: oper.update_decision(record_id, decision))
        return record_from_dict(row) if row else None
