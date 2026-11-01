"""
网盘账号表级访问。

只做查询与暂存，不自行提交事务。
"""

from typing import Any, Mapping, Optional, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.netdiskaccount import NetdiskAccount


class NetdiskAccountOper(DbOper):
    """
    网盘账号表的表级操作。
    """

    def list(self, platform: Optional[str] = None) -> list[NetdiskAccount]:
        """
        按平台列出账号。

        :param platform: 平台标识，None 表示全部
        :return: 账号模型列表
        """
        statement = select(NetdiskAccount).order_by(NetdiskAccount.platform, NetdiskAccount.id)
        if platform:
            statement = statement.where(NetdiskAccount.platform == platform)
        return list(
            self._execute_sync_query(lambda session: session.execute(statement).scalars().all())
        )

    def get(self, account_id: int) -> Optional[NetdiskAccount]:
        """
        按主键读取账号。

        :param account_id: 账号主键
        :return: 账号模型或 None
        """
        return cast(
            Optional[NetdiskAccount],
            self._execute_sync_query(
                lambda session: session.execute(
                    select(NetdiskAccount).where(NetdiskAccount.id == account_id)
                ).scalars().first()
            ),
        )

    def get_by_name(self, platform: str, name: str) -> Optional[NetdiskAccount]:
        """
        按平台与名称读取账号。

        :param platform: 平台标识
        :param name: 账号名称
        :return: 账号模型或 None
        """
        return cast(
            Optional[NetdiskAccount],
            self._execute_sync_query(
                lambda session: session.execute(
                    select(NetdiskAccount).where(
                        NetdiskAccount.platform == platform,
                        NetdiskAccount.name == name,
                    )
                ).scalars().first()
            ),
        )

    def get_primary(self, platform: str) -> Optional[NetdiskAccount]:
        """
        读取平台主账号。

        :param platform: 平台标识
        :return: 主账号模型或 None
        """
        return cast(
            Optional[NetdiskAccount],
            self._execute_sync_query(
                lambda session: session.execute(
                    select(NetdiskAccount).where(
                        NetdiskAccount.platform == platform,
                        NetdiskAccount.is_primary.is_(True),
                    )
                ).scalars().first()
            ),
        )

    def create(self, payload: Mapping[str, Any]) -> NetdiskAccount:
        """
        暂存新账号并刷新主键。

        :param payload: 账号字段
        :return: 新账号模型
        """
        def stage(session: Session) -> NetdiskAccount:
            """
            在调用方会话中加入账号并刷新主键。
            """
            model = NetdiskAccount(**dict(payload))
            session.add(model)
            session.flush()
            return model

        return self._execute_sync_write(stage)

    def update(self, model: NetdiskAccount, payload: Mapping[str, Any]) -> NetdiskAccount:
        """
        暂存账号字段更新。

        :param model: 账号模型
        :param payload: 待更新字段
        :return: 更新后的模型
        """
        return self._stage_update(model, dict(payload))

    def delete(self, model: NetdiskAccount) -> None:
        """
        暂存账号删除。

        :param model: 账号模型
        """
        self._stage_delete(NetdiskAccount, model.id)

    def clear_primary(self, platform: str, exclude_id: Optional[int] = None) -> None:
        """
        取消同平台其他账号的主账号标记。

        :param platform: 平台标识
        :param exclude_id: 保留的账号主键
        """
        def clear(session: Session) -> None:
            """
            在调用方会话中批量更新主账号标记。
            """
            statement = select(NetdiskAccount).where(
                NetdiskAccount.platform == platform,
                NetdiskAccount.is_primary.is_(True),
            )
            if exclude_id is not None:
                statement = statement.where(NetdiskAccount.id != exclude_id)
            for model in session.execute(statement).scalars().all():
                model.is_primary = False

        self._execute_sync_write(clear)
