"""
网盘账号用例：凭据校验、主账号选择、探测与刷新。

本模块只编排端口，不直接访问数据库或具体 Adapter。
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from app.application.netdisk import registry
from app.application.netdisk.contract import (
    NetdiskAccountRecord,
    NetdiskAccountStore,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskAdapter,
)
from app.domain.netdisk import (
    AccountProbe,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    mask_credential,
    normalize_platform,
)


def parse_cookie_header(text: str) -> dict[str, str]:
    """
    把浏览器复制的 Cookie 头解析为字段映射。

    重复字段保留最后一个值，空字段被忽略。

    :param text: Cookie 头原文
    :return: 字段名到值的映射
    """
    result: dict[str, str] = {}
    for part in str(text or "").split(";"):
        item = part.strip()
        if not part.strip():
            continue
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            continue
        result[name] = value.strip()
    return result


class NetdiskAccountService:
    """
    网盘账号的增删改查、选择、探测与刷新用例。
    """

    def __init__(self, store: Optional[NetdiskAccountStore] = None) -> None:
        """
        保存账号仓储；缺省时使用组合根注册的端口。

        :param store: 账号持久化端口
        """
        self._store = store

    @property
    def store(self) -> NetdiskAccountStore:
        """
        返回当前账号仓储。
        """
        return self._store or registry.get_account_store()

    def list(self, platform: object = None) -> list[NetdiskAccountView]:
        """
        列出账号脱敏投影。

        :param platform: 限定平台，None 表示全部
        :return: 账号列表
        """
        resolved = normalize_platform(platform) if platform is not None else None
        if platform is not None and resolved is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "未知的网盘平台")
        return self.store.list_accounts(resolved)

    def create(
        self,
        platform: object,
        name: str,
        credential: Mapping[str, str],
        *,
        is_primary: bool = False,
        validate: bool = True,
    ) -> NetdiskAccountView:
        """
        创建账号并在保存前校验凭据。

        :param platform: 平台标识
        :param name: 账号名称
        :param credential: 明文凭据字段
        :param is_primary: 是否设为主账号
        :param validate: 是否调用平台接口校验
        :return: 新账号的脱敏投影
        :raises NetdiskError: 平台未知、字段缺失或凭据无效时抛出
        """
        resolved = self._require_platform(platform)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号名称不能为空", platform=resolved)
        payload = {str(key): str(value) for key, value in credential.items() if str(value).strip()}
        if not payload:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "凭据不能为空", platform=resolved)
        adapter = registry.get_adapter(resolved)
        self._require_keys(adapter, payload)
        if validate:
            probe = adapter.validate_credential(payload)
            if probe.status != "active":
                raise NetdiskError(
                    NetdiskErrorCode.AUTH_EXPIRED,
                    probe.message or "凭据无效或已过期",
                    platform=resolved,
                    account_name=clean_name,
                )
        return self.store.create_account(
            NetdiskAccountWrite(
                platform=resolved,
                name=clean_name,
                credential=payload,
                is_primary=is_primary,
            )
        )

    def update(
        self,
        account_id: int,
        *,
        name: Optional[str] = None,
        credential: Optional[Mapping[str, str]] = None,
        is_primary: Optional[bool] = None,
        validate: bool = True,
    ) -> NetdiskAccountView:
        """
        更新账号；更换凭据时先校验。

        :param account_id: 账号主键
        :param name: 新名称
        :param credential: 新凭据
        :param is_primary: 是否设为主账号
        :param validate: 是否调用平台接口校验
        :return: 更新后的脱敏投影
        """
        record = self._require_record(account_id)
        payload = None
        if credential is not None:
            payload = {str(key): str(value) for key, value in credential.items() if str(value).strip()}
            if not payload:
                raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "凭据不能为空", platform=record.platform)
            adapter = registry.get_adapter(record.platform)
            self._require_keys(adapter, payload)
            if validate:
                probe = adapter.validate_credential(payload)
                if probe.status != "active":
                    raise NetdiskError(
                        NetdiskErrorCode.AUTH_EXPIRED,
                        probe.message or "凭据无效或已过期",
                        platform=record.platform,
                        account_name=record.name,
                    )
        updated = self.store.update_account(
            account_id,
            name=name,
            credential=payload,
            is_primary=is_primary,
        )
        if updated is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号不存在", platform=record.platform)
        return updated

    def delete(self, account_id: int) -> bool:
        """
        删除账号。

        :param account_id: 账号主键
        :return: 删除成功返回 True
        """
        return bool(self.store.delete_account(account_id))

    def set_primary(self, account_id: int) -> NetdiskAccountView:
        """
        把账号设为同平台主账号。

        :param account_id: 账号主键
        :return: 更新后的脱敏投影
        """
        record = self._require_record(account_id)
        updated = self.store.update_account(account_id, is_primary=True)
        if updated is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号不存在", platform=record.platform)
        return updated

    def probe(self, account_id: int) -> AccountProbe:
        """
        探测账号登录态并记录结果。

        :param account_id: 账号主键
        :return: 探测结果
        """
        record = self._require_record(account_id)
        adapter = registry.get_adapter(record.platform)
        try:
            probe = adapter.probe_account(record)
        except NetdiskError as error:
            probe = AccountProbe(status="expired", message=error.message)
        self.store.touch_account(account_id, probe)
        return probe

    def refresh(self, account_id: int) -> Optional[NetdiskAccountRecord]:
        """
        刷新账号令牌并写回新凭据。

        :param account_id: 账号主键
        :return: 刷新后的账号记录；平台不支持时返回 None
        """
        record = self._require_record(account_id)
        adapter = registry.get_adapter(record.platform)
        credential = adapter.refresh_account(record)
        if not credential:
            return None
        self.store.update_account(account_id, credential=credential, status="active")
        return self._require_record(account_id)

    def select(self, platform: object, exclude_ids: Sequence[int] = ()) -> NetdiskAccountRecord:
        """
        按主账号优先选择可用账号。

        :param platform: 平台标识
        :param exclude_ids: 需要跳过的账号主键
        :return: 可用账号记录
        :raises NetdiskError: 没有可用账号时抛出 AUTH_EXPIRED
        """
        resolved = self._require_platform(platform)
        excluded = set(exclude_ids)
        views = self.store.list_accounts(resolved)
        ordered = sorted(views, key=lambda item: (not item.is_primary, item.id))
        for view in ordered:
            if view.id in excluded:
                continue
            record = self.store.get_record(view.id)
            if record is not None and record.status == "active":
                return record
        raise NetdiskError(
            NetdiskErrorCode.AUTH_EXPIRED,
            f"{resolved.value} 平台没有可用账号，请先在网盘账号中添加并登录",
            platform=resolved,
        )

    def require_account(self, platform: object, account_id: Optional[int]) -> NetdiskAccountRecord:
        """
        按主键读取账号，未指定时选择默认账号。

        :param platform: 平台标识
        :param account_id: 指定账号主键
        :return: 账号记录
        """
        if account_id:
            record = self._require_record(account_id)
            if record.platform != self._require_platform(platform):
                raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号与平台不匹配")
            return record
        return self.select(platform)

    def _require_platform(self, platform: object) -> NetdiskPlatform:
        """
        解析平台标识。

        :param platform: 平台标识
        :return: 平台枚举
        """
        resolved = normalize_platform(platform)
        if resolved is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "未知的网盘平台")
        return resolved

    def _require_record(self, account_id: int) -> NetdiskAccountRecord:
        """
        按主键读取账号记录。

        :param account_id: 账号主键
        :return: 账号记录
        """
        record = self.store.get_record(account_id)
        if record is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号不存在")
        return record

    @staticmethod
    def _require_keys(adapter: NetdiskAdapter, credential: Mapping[str, str]) -> None:
        """
        校验凭据是否包含平台必填字段。

        :param adapter: 平台适配器
        :param credential: 明文凭据
        :raises NetdiskError: 缺少必填字段时抛出
        """
        fields: dict[str, str] = {}
        for key, value in credential.items():
            if key in {"cookie", "cookie_header"}:
                fields.update(parse_cookie_header(value))
            else:
                fields[str(key)] = str(value)
        missing = [key for key in adapter.cookie_required_keys if key not in fields]
        if missing:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "凭据缺少必填字段：" + "、".join(missing),
                platform=adapter.platform,
            )
