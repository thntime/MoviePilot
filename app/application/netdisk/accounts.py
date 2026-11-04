"""
网盘账号用例：凭据校验、主账号选择、探测与刷新。

本模块只编排端口，不直接访问数据库或具体 Adapter。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence

from app.application.netdisk import registry, selection
from app.application.netdisk.contract import (
    NetdiskAccountDetail,
    NetdiskAccountRecord,
    NetdiskAccountStore,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskAdapter,
)
from app.application.netdisk.keepalive import RefreshThrottle
from app.domain.netdisk import (
    AccountProbe,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    mask_credential,
    normalize_platform,
)
from app.runtime.log import logger

# 续期节流必须跨用例实例共享：取链与接口每次请求都会新建服务，90 分钟窗口才有效。
_REFRESH_THROTTLE = RefreshThrottle()


def reset_refresh_throttle() -> None:
    """
    清空进程级续期节流状态，供测试与生命周期重置使用。
    """
    _REFRESH_THROTTLE.reset()


@dataclass(frozen=True, slots=True)
class NetdiskAccountFailure:
    """
    一轮批量刷新中需要用户重新登录的账号。

    :param account_id: 账号主键
    :param platform: 平台标识
    :param name: 账号名称
    :param message: 可直接展示的失败原因，不含凭据明文
    """

    account_id: int
    platform: str
    name: str
    message: str


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
        nickname: str = "",
    ) -> NetdiskAccountView:
        """
        创建账号并在保存前校验凭据。

        :param platform: 平台标识
        :param name: 账号名称
        :param credential: 明文凭据字段
        :param is_primary: 是否设为主账号
        :param validate: 是否调用平台接口校验
        :param nickname: 登录账号昵称/手机号
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
        resolved_nickname = nickname
        if not resolved_nickname:
            # 新增账号时尽量从平台侧解析昵称；失败不影响账号创建。
            try:
                resolved_nickname = adapter.login_account_name(payload) or ""
            except Exception:  # noqa: BLE001 - 昵称解析失败不影响创建
                resolved_nickname = ""
        return self.store.create_account(
            NetdiskAccountWrite(
                platform=resolved,
                name=clean_name,
                credential=payload,
                is_primary=is_primary,
                nickname=resolved_nickname,
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
        nickname: Optional[str] = None,
    ) -> NetdiskAccountView:
        """
        更新账号；更换凭据时先校验并恢复为 active。

        :param account_id: 账号主键
        :param name: 新名称
        :param credential: 新凭据
        :param is_primary: 是否设为主账号
        :param validate: 是否调用平台接口校验
        :param nickname: 新昵称；未提供时尝试从新凭据解析
        :return: 更新后的脱敏投影
        """
        record = self._require_record(account_id)
        payload = None
        adapter: Optional[NetdiskAdapter] = None
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
        resolved_nickname = nickname
        if resolved_nickname is None and adapter is not None and payload is not None:
            try:
                resolved_nickname = adapter.login_account_name(payload) or None
            except Exception:  # noqa: BLE001 - 昵称解析失败不影响凭据更新
                resolved_nickname = None
        updated = self.store.update_account(
            account_id,
            name=name,
            credential=payload,
            is_primary=is_primary,
            status="active" if payload is not None else None,
            nickname=resolved_nickname,
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

    def detail(self, account_id: int) -> NetdiskAccountDetail:
        """
        读取单账号详情，附带明文凭据。

        仅供超级管理员鉴权的详情接口使用；调用方不得写入日志。

        :param account_id: 账号主键
        :return: 账号投影与明文凭据
        :raises NetdiskError: 账号不存在时抛出
        """
        record = self._require_record(account_id)
        view = next(
            (item for item in self.store.list_accounts(record.platform) if item.id == account_id),
            None,
        )
        if view is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号不存在", platform=record.platform)
        return NetdiskAccountDetail(
            account=view,
            credential={str(key): str(value) for key, value in (record.credential or {}).items()},
        )

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
        record = self._refresh_for_request(self._require_record(account_id))
        adapter = registry.get_adapter(record.platform)
        try:
            probe = adapter.probe_account(record)
        except NetdiskError as error:
            probe = AccountProbe(status="expired", message=error.message)
        self.store.touch_account(account_id, probe)
        quota_used: Optional[int] = None
        quota_total: Optional[int] = None
        nickname = ""
        try:
            if adapter.drive_capabilities.quota:
                quota = adapter.quota(record)
                quota_used = quota.used
                quota_total = quota.total
        except Exception:  # noqa: BLE001 - 配额失败不影响探测结果
            pass
        try:
            nickname = adapter.login_account_name(record.credential or {})
        except Exception:  # noqa: BLE001 - 昵称失败不影响探测结果
            nickname = ""
        if quota_used is not None or nickname:
            self.store.update_account(
                account_id,
                nickname=nickname or None,
                quota_used=quota_used,
                quota_total=quota_total,
            )
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
        _REFRESH_THROTTLE.mark(account_id, record.platform)
        return self._require_record(account_id)

    def refresh_all(self) -> List[NetdiskAccountFailure]:
        """
        批量刷新并探测全部个人网盘账号。

        支持刷新的平台先写回新凭据，再统一探测登录态；单个账号异常只记录告警，
        不中断其余账号。

        :return: 需要重新登录的账号列表
        """
        failures: list[NetdiskAccountFailure] = []
        for view in self.store.list_accounts():
            try:
                failure = self._refresh_one(view.id)
            except Exception as error:  # noqa: BLE001 - 单账号异常不中断整轮
                logger.warning(f"网盘账号 {view.platform} / {view.name} 刷新异常：{error}")
                failures.append(
                    NetdiskAccountFailure(
                        account_id=view.id,
                        platform=view.platform,
                        name=view.name,
                        message=str(error) or type(error).__name__,
                    )
                )
                continue
            if failure is not None:
                failures.append(failure)
        return failures

    def refresh_quotas(self) -> dict[str, int]:
        """
        遍历全部账号刷新空间占用。

        只写回已用与总空间；单账号失败只记录告警，不修改状态、检测时间与凭据。

        :return: 检查、更新与失败账号数量
        """
        checked = 0
        updated = 0
        failed = 0
        for view in self.store.list_accounts():
            checked += 1
            try:
                record = self.store.get_record(view.id)
                if record is None:
                    continue
                adapter = registry.get_adapter(record.platform)
                if not adapter.drive_capabilities.quota:
                    continue
                quota = adapter.quota(record)
                self.store.update_quota(record.id, quota.used, quota.total)
                updated += 1
            except Exception as error:  # noqa: BLE001 - 单账号空间刷新失败不影响其他账号
                failed += 1
                logger.warning(
                    f"网盘账号空间刷新失败：platform={view.platform} "
                    f"account={view.name} error={type(error).__name__}"
                )
        return {"checked": checked, "updated": updated, "failed": failed}

    def _refresh_record(self, record: NetdiskAccountRecord) -> tuple[NetdiskAccountRecord, str]:
        """
        按平台 TTL 或适配器临期判断惰性续期。

        临期判断不受 90 分钟节流限制；刷新失败保留旧凭据。

        :param record: 账号记录
        :return: 续期后的账号记录与失败原因；无需续期或成功时原因为空
        """
        try:
            adapter = registry.get_adapter(record.platform)
        except NetdiskError:
            # 平台未注册时保持原记录，由真正发起请求的调用方给出明确错误。
            return record, ""
        if not getattr(adapter, "supports_refresh", False):
            return record, ""
        needs_refresh = getattr(adapter, "needs_refresh", None)
        try:
            expiring = bool(needs_refresh(record)) if callable(needs_refresh) else False
        except Exception:  # noqa: BLE001 - 临期判断失败按未临期处理
            expiring = False
        if not expiring and not _REFRESH_THROTTLE.due(record.id, record.platform):
            return record, ""
        try:
            credential = adapter.refresh_account(record)
        except Exception as error:  # noqa: BLE001 - 续期失败保留旧凭据
            message = str(error) or type(error).__name__
            logger.warning(f"网盘账号 {record.platform.value} / {record.name} 续期失败：{message}")
            return record, message
        if not credential:
            return record, ""
        self.store.update_account(record.id, credential=credential, status="active")
        _REFRESH_THROTTLE.mark(record.id, record.platform)
        refreshed = self.store.get_record(record.id)
        return refreshed or record, ""

    def _refresh_for_request(self, record: NetdiskAccountRecord) -> NetdiskAccountRecord:
        """
        在把账号交给适配器发起真实请求前完成惰性续期。

        :param record: 账号记录
        :return: 可直接用于请求的账号记录
        """
        refreshed, _ = self._refresh_record(record)
        return refreshed

    def _refresh_one(self, account_id: int) -> Optional[NetdiskAccountFailure]:
        """
        刷新并探测单个账号，返回失败项或 None。

        :param account_id: 账号主键
        :return: 需要重新登录时返回失败项
        """
        record = self.store.get_record(account_id)
        if record is None:
            logger.warning(f"网盘账号 {account_id} 不存在，跳过刷新")
            return None
        record, refresh_error = self._refresh_record(record)
        adapter = registry.get_adapter(record.platform)
        try:
            probe = adapter.probe_account(record)
        except NetdiskError as error:
            probe = AccountProbe(status="expired", message=error.message)
        except Exception as error:  # noqa: BLE001 - 探测异常按失效处理
            probe = AccountProbe(status="expired", message=str(error) or type(error).__name__)
            logger.warning(f"网盘账号 {record.platform.value} / {record.name} 探测异常：{probe.message}")
        self.store.touch_account(record.id, probe)
        # 定时刷新与启动首跑同样补写平台昵称；解析失败保留已有昵称。
        try:
            nickname = adapter.login_account_name(record.credential or {})
        except Exception:  # noqa: BLE001 - 昵称解析失败不影响刷新结果
            nickname = ""
        if nickname:
            self.store.update_account(record.id, nickname=nickname)
        if probe.status == "expired":
            return NetdiskAccountFailure(
                account_id=record.id,
                platform=record.platform.value,
                name=record.name,
                message=probe.message or refresh_error or "登录态已失效",
            )
        if refresh_error:
            return NetdiskAccountFailure(
                account_id=record.id,
                platform=record.platform.value,
                name=record.name,
                message=refresh_error,
            )
        return None

    def select(self, platform: object, exclude_ids: Sequence[int] = ()) -> NetdiskAccountRecord:
        """
        按状态、剩余容量、近期速度与主账号顺序选择可用账号。

        :param platform: 平台标识
        :param exclude_ids: 需要跳过的账号主键
        :return: 可用账号记录
        :raises NetdiskError: 没有可用账号时抛出 AUTH_EXPIRED
        """
        resolved = self._require_platform(platform)
        excluded = set(exclude_ids)
        views = self.store.list_accounts(resolved)

        def free_ratio(item: NetdiskAccountView) -> float:
            """
            计算账号剩余容量比例；平台未返回配额时使用中性值。
            """
            if item.quota_total <= 0:
                return 0.5
            return max(item.quota_total - item.quota_used, 0) / item.quota_total

        ordered = sorted(
            views,
            key=lambda item: (
                item.status != "active",
                -free_ratio(item),
                -selection.account_speed(item.id),
                not item.is_primary,
                item.id,
            ),
        )
        for view in ordered:
            if view.id in excluded:
                continue
            record = self.store.get_record(view.id)
            if record is not None and record.status == "active":
                return self._refresh_for_request(record)
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
            return self._refresh_for_request(record)
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
