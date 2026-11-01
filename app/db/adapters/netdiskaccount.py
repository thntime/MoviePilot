"""
网盘账号持久化端口的具体实现。

使用独立短 Session 完成读写，凭据在仓储内完成加解密。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, TypeVar

from sqlalchemy.orm import Session

from app.application.netdisk.contract import (
    CredentialCipher,
    NetdiskAccountRecord,
    NetdiskAccountView,
    NetdiskAccountWrite,
)
from app.db.models.netdiskaccount import NetdiskAccount
from app.db.oper.netdiskaccount import NetdiskAccountOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.netdisk import (
    AccountProbe,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    mask_credential,
)


T = TypeVar("T")


def _to_view(model: NetdiskAccount, credential: Mapping[str, str]) -> NetdiskAccountView:
    """
    把模型投影为脱敏视图。

    :param model: 账号模型
    :param credential: 已解密的凭据字段
    :return: 脱敏投影
    """
    return NetdiskAccountView(
        id=model.id,
        platform=model.platform,
        name=model.name,
        is_primary=bool(model.is_primary),
        status=model.status or "active",
        expires_at=model.expires_at,
        last_checked_at=model.last_checked_at,
        masked=mask_credential(credential),
    )


class TransactionalNetdiskAccountRepository:
    """
    通过独立短 Session 实现账号持久化端口。
    """

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        cipher: CredentialCipher,
    ) -> None:
        """
        保存会话工厂与凭据加解密器。

        :param sync_session: 同步会话工厂
        :param cipher: 凭据加解密器
        """
        self._sync_session = sync_session
        self._cipher = cipher

    def _read(self, operation: Callable[[NetdiskAccountOper], T]) -> T:
        """
        在独立会话中执行只读操作。

        :param operation: 接收 Oper 的调用
        :return: 调用结果
        """
        with self._sync_session() as session:
            return operation(NetdiskAccountOper(db=session))

    def _write(self, operation: Callable[[NetdiskAccountOper], T]) -> T:
        """
        在独立事务中执行写操作。

        :param operation: 接收 Oper 的调用
        :return: 调用结果
        """
        with self._sync_session() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(NetdiskAccountOper(db=session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    def _decrypt(self, model: NetdiskAccount) -> dict[str, str]:
        """
        解密账号凭据；失败时返回空字段。

        :param model: 账号模型
        :return: 凭据字段
        """
        plaintext = self._cipher.decrypt(model.credential_enc)
        if not plaintext:
            return {}
        try:
            data = json.loads(plaintext)
        except json.JSONDecodeError:
            return {}
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    def list_accounts(self, platform: Optional[NetdiskPlatform] = None) -> list[NetdiskAccountView]:
        """
        列出账号脱敏投影。

        :param platform: 限定平台
        :return: 账号列表
        """
        def load(oper: NetdiskAccountOper) -> list[NetdiskAccountView]:
            """
            在会话内读取账号并完成脱敏投影。
            """
            return [
                _to_view(model, self._decrypt(model))
                for model in oper.list(platform.value if platform else None)
            ]

        return self._read(load)

    def get_record(self, account_id: int) -> Optional[NetdiskAccountRecord]:
        """
        按主键读取完整账号记录。

        :param account_id: 账号主键
        :return: 账号记录或 None
        """
        model = self._read(lambda oper: oper.get(account_id))
        if model is None:
            return None
        credential = self._decrypt(model)
        try:
            platform = NetdiskPlatform(model.platform)
        except ValueError as error:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "账号的平台标识无效",
                account_name=model.name,
            ) from error
        return NetdiskAccountRecord(
            id=model.id,
            platform=platform,
            name=model.name,
            credential=credential,
            is_primary=bool(model.is_primary),
            status=model.status or "active",
            expires_at=model.expires_at,
        )

    def create_account(self, payload: NetdiskAccountWrite) -> NetdiskAccountView:
        """
        创建账号并维护同平台主账号唯一性。

        :param payload: 账号写入载荷
        :return: 新账号脱敏投影
        """
        encrypted = self._cipher.encrypt(json.dumps(dict(payload.credential), ensure_ascii=False))

        def create(oper: NetdiskAccountOper) -> NetdiskAccountView:
            """
            在事务内校验名称、写入账号并生成脱敏投影。
            """
            if oper.get_by_name(payload.platform.value, payload.name) is not None:
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "同平台下账号名称已存在",
                    platform=payload.platform,
                    account_name=payload.name,
                )
            if payload.is_primary:
                oper.clear_primary(payload.platform.value)
            model = oper.create(
                {
                    "platform": payload.platform.value,
                    "name": payload.name,
                    "credential_enc": encrypted,
                    "is_primary": payload.is_primary,
                    "status": "active",
                }
            )
            return _to_view(model, payload.credential)

        return self._write(create)

    def update_account(
        self,
        account_id: int,
        *,
        name: Optional[str] = None,
        credential: Optional[Mapping[str, str]] = None,
        is_primary: Optional[bool] = None,
        status: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> Optional[NetdiskAccountView]:
        """
        更新账号字段。

        :param account_id: 账号主键
        :param name: 新名称
        :param credential: 新凭据
        :param is_primary: 是否设为主账号
        :param status: 新状态
        :param expires_at: 新过期时间
        :return: 更新后的脱敏投影
        """
        encrypted = (
            self._cipher.encrypt(json.dumps(dict(credential), ensure_ascii=False))
            if credential is not None
            else None
        )

        def update(oper: NetdiskAccountOper) -> Optional[NetdiskAccountView]:
            """
            在事务内更新账号字段并生成脱敏投影。
            """
            model = oper.get(account_id)
            if model is None:
                return None
            fields: dict[str, Any] = {}
            if name is not None:
                duplicate = oper.get_by_name(model.platform, name)
                if duplicate is not None and duplicate.id != account_id:
                    raise NetdiskError(
                        NetdiskErrorCode.UNKNOWN,
                        "同平台下账号名称已存在",
                        account_name=name,
                    )
                fields["name"] = name
            if encrypted is not None:
                fields["credential_enc"] = encrypted
            if status is not None:
                fields["status"] = status
            if expires_at is not None:
                fields["expires_at"] = expires_at
            if is_primary is True:
                oper.clear_primary(model.platform, exclude_id=account_id)
                fields["is_primary"] = True
            elif is_primary is False:
                fields["is_primary"] = False
            if fields:
                model = oper.update(model, fields)
            return _to_view(model, credential if credential is not None else self._decrypt(model))

        return self._write(update)

    def delete_account(self, account_id: int) -> bool:
        """
        删除账号，并在删除主账号时提升同平台最近更新的账号。

        :param account_id: 账号主键
        :return: 删除成功返回 True
        """
        def delete(oper: NetdiskAccountOper) -> bool:
            """
            在事务内删除并维护主账号。
            """
            model = oper.get(account_id)
            if model is None:
                return False
            platform = model.platform
            was_primary = bool(model.is_primary)
            oper.delete(model)
            if not was_primary:
                return True
            remaining = [item for item in oper.list(platform) if item.id != account_id]
            if remaining:
                oper.update(max(remaining, key=lambda item: item.id), {"is_primary": True})
            return True

        return bool(self._write(delete))

    def touch_account(self, account_id: int, probe: AccountProbe) -> None:
        """
        记录账号探测结果。

        :param account_id: 账号主键
        :param probe: 探测结果
        """
        def touch(oper: NetdiskAccountOper) -> None:
            """
            在事务内写入状态与时间。
            """
            model = oper.get(account_id)
            if model is None:
                return
            fields: dict[str, Any] = {
                "last_checked_at": datetime.now(),
                "status": probe.status or "unknown",
            }
            if probe.expires_at is not None:
                fields["expires_at"] = probe.expires_at
            oper.update(model, fields)

        self._write(touch)
