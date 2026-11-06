"""
网盘取链用例：策略分支、账号选择、直链缓存、刷新与限流。

本模块只编排端口，不直接访问网络或数据库。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Collection, Optional, Sequence, TypeVar

from app.application.netdisk import registry, selection
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import (
    NetdiskAccountRecord,
    NetdiskAdapter,
    NetdiskTuning,
)
from app.domain.netdisk import (
    DirectLink,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
    TransferHandle,
    normalize_platform,
    parse_share_link,
)
from app.runtime.log import logger

# 同一平台的两次取链调用之间的最小间隔，避免连续请求触发频控。
_PLATFORM_MIN_INTERVAL = 0.3
T = TypeVar("T")
# 直链缓存键：平台、分享码、文件标识、账号主键。
LinkCacheKey = tuple[NetdiskPlatform, str, str, int]
# 直链缓存条目上限，超出后清空最旧的一半。
_LINK_CACHE_LIMIT = 256
# 直链剩余有效期低于该值时不再复用缓存。
_LINK_CACHE_MARGIN = timedelta(minutes=1)
# 本应用创建的临时转存目录前缀；清理时只删除这些目录，绝不触碰用户文件。
_TEMP_DIR_PREFIXES = ("mp_tr_", "MoviePilot临时转存")


@dataclass(frozen=True, slots=True)
class ResolvedShare:
    """
    一次分享解析的完整结果。

    :param ref: 分享引用
    :param detail: 分享详情
    :param files: 递归展开后的文件列表
    """

    ref: ShareRef
    detail: ShareDetail
    files: tuple[ShareFile, ...] = ()


class NetdiskResolveService:
    """
    网盘取链编排服务。
    """

    def __init__(
        self,
        accounts: Optional[NetdiskAccountService] = None,
        settings: Optional[NetdiskTuning] = None,
    ) -> None:
        """
        保存账号服务与参数快照。

        :param accounts: 账号用例；缺省时使用组合根注册的仓储
        :param settings: 参数快照；缺省时读取注册端口
        """
        self._accounts = accounts or NetdiskAccountService()
        self._settings = settings
        self._lock = threading.Lock()
        self._last_call: dict[NetdiskPlatform, float] = {}
        self._link_cache: dict[LinkCacheKey, DirectLink] = {}
        self._quota_cache: dict[int, tuple[float, int, int]] = {}

    @property
    def tuning(self) -> NetdiskTuning:
        """
        返回当前参数快照；参数端口每次读取最新值。
        """
        if self._settings is None:
            return registry.current_tuning()
        load = getattr(self._settings, "load", None)
        return load() if callable(load) else self._settings

    def parse(
        self,
        url: str,
        password: Optional[str] = None,
        platform: object = None,
        share_token: str = "",
    ) -> tuple[NetdiskAdapter, ShareRef]:
        """
        解析分享链接并取得对应适配器。

        :param url: 分享链接或文案
        :param password: 已知提取码
        :param platform: 调用方已知的平台标识
        :param share_token: 候选透传的分享令牌，非空时不再重新换取
        :return: 适配器与分享引用
        :raises NetdiskError: 无法识别链接或平台未实现时抛出
        """
        ref = parse_share_link(url, password)
        if ref is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "无法识别的网盘分享链接")
        expected = normalize_platform(platform) if platform else None
        if expected is not None and expected != ref.platform:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "分享链接与候选平台不一致",
                platform=ref.platform,
            )
        adapter = registry.get_adapter(ref.platform)
        if (password and not ref.password) or share_token:
            ref = ShareRef(
                platform=ref.platform,
                share_id=ref.share_id,
                password=password or ref.password,
                url=ref.url,
                share_token=share_token or ref.share_token,
            )
        return adapter, ref

    def inspect(
        self,
        url: str,
        password: Optional[str] = None,
        platform: object = None,
        account_id: Optional[int] = None,
        share_token: str = "",
    ) -> ShareDetail:
        """
        获取分享详情，用于有效性检查与展示信息补充。

        :param url: 分享链接
        :param password: 提取码
        :param platform: 平台标识
        :param account_id: 指定账号
        :param share_token: 候选透传的分享令牌
        :return: 分享详情
        """
        adapter, ref = self.parse(url, password, platform, share_token)
        account = self._optional_account(adapter, account_id)
        return self._call(adapter, lambda: adapter.share_detail(ref, account))

    def list_files(
        self,
        url: str,
        password: Optional[str] = None,
        parent_id: str = "",
        platform: object = None,
        account_id: Optional[int] = None,
        share_token: str = "",
    ) -> list[ShareFile]:
        """
        列出分享目录内容。

        :param url: 分享链接
        :param password: 提取码
        :param parent_id: 父目录标识
        :param platform: 平台标识
        :param account_id: 指定账号
        :param share_token: 候选透传的分享令牌
        :return: 目录条目
        """
        adapter, ref = self.parse(url, password, platform, share_token)
        account = self._optional_account(adapter, account_id)
        return self._call(adapter, lambda: adapter.list_files(ref, parent_id, account))

    def walk(
        self,
        url: str,
        password: Optional[str] = None,
        platform: object = None,
        account_id: Optional[int] = None,
        *,
        max_depth: int = 3,
        share_token: str = "",
    ) -> ResolvedShare:
        """
        递归展开分享内的全部文件。

        :param url: 分享链接
        :param password: 提取码
        :param platform: 平台标识
        :param account_id: 指定账号
        :param max_depth: 最大递归深度
        :param share_token: 候选透传的分享令牌
        :return: 分享引用、详情与文件列表
        """
        adapter, ref = self.parse(url, password, platform, share_token)
        account = self._optional_account(adapter, account_id)
        detail = self._call(adapter, lambda: adapter.share_detail(ref, account))
        files: list[ShareFile] = []
        pending = [(item, 1, "") for item in detail.files]
        while pending:
            item, depth, prefix = pending.pop(0)
            if item.is_dir:
                if depth >= max_depth:
                    continue
                def load_children(parent: ShareFile = item) -> list[ShareFile]:
                    """
                    读取当前目录的子项。
                    """
                    return adapter.list_files(ref, parent.file_id, account)

                children = self._call(adapter, load_children)
                child_prefix = f"{prefix}{item.name}/"
                pending.extend((child, depth + 1, child_prefix) for child in children)
                continue
            # 保留相对路径，目录名里的季号（第二季/Season 2）不能丢。
            files.append(replace(item, path=f"{prefix}{item.name}"))
        return ResolvedShare(ref=ref, detail=detail, files=tuple(files))

    def resolve_file(
        self,
        url: str,
        password: Optional[str],
        file: ShareFile,
        platform: object = None,
        account_id: Optional[int] = None,
        *,
        multi_source: bool = False,
        force_refresh: bool = False,
        share_token: str = "",
        concurrency: int = 1,
        exclude_directories: Optional[Callable[[], Collection[str]]] = None,
    ) -> tuple[DirectLink, ...]:
        """
        获取一个文件的直链；多源模式下每个账号返回一条。

        :param url: 分享链接
        :param password: 提取码
        :param file: 目标文件
        :param platform: 平台标识
        :param account_id: 指定账号
        :param multi_source: 是否启用多账号多源
        :param force_refresh: 是否跳过缓存并刷新账号
        :param share_token: 候选透传的分享令牌
        :param concurrency: 同时转存的文件数，用于计算峰值空间
        :param exclude_directories: 惰性返回正在被其他任务使用的临时目录标识
        :return: 直链元组
        """
        adapter, ref = self.parse(url, password, platform, share_token)
        accounts = self._resolve_accounts(
            adapter, account_id, multi_source,
            allow_guest=adapter.platform is NetdiskPlatform.QUARK,
        )
        links: list[DirectLink] = []
        for account in accounts:
            links.append(
                self._resolve_one(
                    adapter,
                    ref,
                    file,
                    account,
                    force_refresh=force_refresh,
                    concurrency=concurrency,
                    exclude_directories=exclude_directories,
                )
            )
        return tuple(links)

    def cleanup(self, link: DirectLink) -> bool:
        """
        清理直链关联的转存内容。

        :param link: 已下载完成的直链
        :return: 已清理或无需清理返回 True；失败返回 False，供调用方保留补偿句柄
        """
        handle: Optional[TransferHandle] = link.cleanup_handle
        if handle is None:
            return True
        adapter = registry.get_adapter(handle.platform)
        account = self._accounts.store.get_record(handle.account_id)
        try:
            adapter.cleanup(handle, account)
            # 转存目录已删除，配额缓存必须失效，否则后续文件会按旧的已用空间误判。
            self._quota_cache.pop(int(handle.account_id or 0), None)
            return True
        except NetdiskError as error:
            logger.warning(
                f"清理网盘转存内容失败：{handle.platform.value} {handle.directory_id} - {error.message}"
            )
            return False
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载主流程
            logger.warning(
                f"清理网盘转存内容异常：{handle.platform.value} {handle.directory_id} - "
                f"{type(error).__name__}"
            )
            return False

    def _resolve_one(
        self,
        adapter: NetdiskAdapter,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        force_refresh: bool,
        concurrency: int = 1,
        exclude_directories: Optional[Callable[[], Collection[str]]] = None,
    ) -> DirectLink:
        """
        按策略为单个文件取链，并处理缓存、刷新与容量回退。

        :param adapter: 平台适配器
        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param force_refresh: 是否跳过缓存并刷新账号
        :param concurrency: 同时转存的文件数，用于计算峰值需求
        :param exclude_directories: 惰性返回正在被其他任务使用的临时目录标识，清理时跳过
        :return: 直链
        """
        cache_key = (
            ref.platform,
            ref.share_id,
            file.file_id,
            account.id if account else 0,
        )
        if not force_refresh:
            cached = self._cached_link(cache_key)
            if cached is not None:
                return cached
        current = account
        if force_refresh and current is not None:
            current = self._refresh_account(current)
        allow_transfer = self._allow_transfer(adapter)
        transfer_first = bool(self.tuning.transfer_first) and allow_transfer
        required = self._required_bytes(file, concurrency)
        strategy = "direct"
        if transfer_first and current is not None:
            current, allow_transfer, strategy = self._select_transfer_target(
                adapter, file, current, required, concurrency, exclude_directories,
            )
        recovered = False
        for attempt in range(3):
            try:
                link = self._call(
                    adapter,
                    lambda: adapter.resolve(
                        ref,
                        file,
                        current,
                        allow_transfer=allow_transfer,
                        transfer_first=transfer_first and allow_transfer,
                    ),
                )
                break
            except NetdiskError as error:
                if (
                    error.code is NetdiskErrorCode.AUTH_EXPIRED
                    and current is not None
                    and attempt == 0
                ):
                    current = self._refresh_account(current)
                    continue
                if (
                    error.code is NetdiskErrorCode.SPACE_INSUFFICIENT
                    and allow_transfer
                    and current is not None
                    and not recovered
                ):
                    recovered = True
                    current = self._recover_runtime_capacity(
                        adapter, current, required, exclude_directories,
                    )
                    continue
                raise
        else:  # pragma: no cover - 循环内已 raise 或 break
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘取链重试次数耗尽")
        if strategy:
            link = replace(link, strategy=strategy)
        self._remember_link(cache_key, link)
        return link

    @staticmethod
    def _required_bytes(file: ShareFile, concurrency: int) -> int:
        """
        计算转存峰值需求：单文件大小 × 并发数，并预留 20% 余量。

        :param file: 目标文件
        :param concurrency: 并发文件数
        :return: 需要的字节数
        """
        size = int(file.size or 0)
        if size <= 0:
            return 0
        return int(size * max(int(concurrency or 1), 1) * 1.2)

    def _select_transfer_target(
        self,
        adapter: NetdiskAdapter,
        file: ShareFile,
        account: NetdiskAccountRecord,
        required: int,
        concurrency: int,
        exclude_directories: Optional[Callable[[], Collection[str]]] = None,
    ) -> tuple[NetdiskAccountRecord, bool, str]:
        """
        转存优先时选出放得下的账号；都放不下时按回退链处理。

        :param adapter: 平台适配器
        :param file: 目标文件
        :param account: 首选账号
        :param required: 峰值需求字节数
        :param concurrency: 并发文件数
        :param exclude_directories: 惰性返回正在被其他任务使用的临时目录标识
        :return: (账号, 是否允许转存, 策略标签)
        """
        tried: set[int] = set()
        current = account
        purged = False
        while True:
            try:
                self._ensure_space(adapter, file, current, concurrency=concurrency)
                return current, True, "transfer"
            except NetdiskError as error:
                if error.code is not NetdiskErrorCode.SPACE_INSUFFICIENT:
                    raise
                tried.add(current.id)
                next_account = self._select_fitting(adapter.platform, required, tried)
                if next_account is not None:
                    current = next_account
                    continue
                if adapter.link_policy is not LinkPolicy.TRANSFER_REQUIRED:
                    return current, False, "direct_fallback"
                if purged or not self._can_ever_fit(adapter.platform, required):
                    raise
                purged = True
                self._purge_temp_dirs(
                    adapter, adapter.platform, current, exclude_directories,
                )
                continue

    def _recover_runtime_capacity(
        self,
        adapter: NetdiskAdapter,
        account: NetdiskAccountRecord,
        required: int,
        exclude_directories: Optional[Callable[[], Collection[str]]] = None,
    ) -> NetdiskAccountRecord:
        """
        取链时才撞上容量不足：先换账号，其次清理临时目录后重试原账号。

        :param adapter: 平台适配器
        :param account: 当前账号
        :param required: 峰值需求字节数
        :param exclude_directories: 惰性返回正在被其他任务使用的临时目录标识
        :return: 下一次尝试使用的账号
        """
        selection.cool_account(account.id)
        next_account = self._select_fitting(adapter.platform, required, {account.id})
        if next_account is not None:
            return next_account
        self._purge_temp_dirs(adapter, adapter.platform, account, exclude_directories)
        return account

    def _select_fitting(
        self,
        platform: NetdiskPlatform,
        required: int,
        exclude: set[int],
    ) -> Optional[NetdiskAccountRecord]:
        """
        选择剩余空间满足需求的账号；没有时返回 None。

        :param platform: 平台标识
        :param required: 峰值需求字节数
        :param exclude: 已尝试过的账号主键
        :return: 账号记录或 None
        """
        try:
            return self._accounts.select(
                platform, exclude_ids=tuple(exclude), required_bytes=required,
            )
        except NetdiskError:
            return None

    def _can_ever_fit(self, platform: NetdiskPlatform, required: int) -> bool:
        """
        判断所需空间是否可能被任一账号容纳；用于避免无意义的清理与重试。

        :param platform: 平台标识
        :param required: 峰值需求字节数
        :return: 可能容纳时返回 True
        """
        if required <= 0:
            return True
        try:
            views = self._accounts.list(platform)
        except NetdiskError:
            return True
        totals = [int(view.quota_total) for view in views if int(view.quota_total or 0) > 0]
        if not totals:
            return True
        return required <= max(totals)

    def _purge_temp_dirs(
        self,
        adapter: NetdiskAdapter,
        platform: NetdiskPlatform,
        current: Optional[NetdiskAccountRecord] = None,
        exclude: Optional[Callable[[], Collection[str]]] = None,
    ) -> int:
        """
        删除本应用创建的临时转存目录，并清空相关配额缓存。

        只匹配 `mp_tr_` 与 `MoviePilot临时转存` 前缀；缺少浏览或删除能力时跳过。
        正在被其他任务使用的目录通过 `exclude` 跳过，避免删掉 in-flight 转存。
        删除是否释放配额由平台决定，调用方必须重读配额再判断。

        :param adapter: 平台适配器
        :param platform: 平台标识
        :param current: 额外纳入清理的账号
        :param exclude: 惰性返回需要跳过的目录标识
        :return: 删除的目录数量
        """
        capabilities = adapter.drive_capabilities
        if not (capabilities.browse and capabilities.delete):
            return 0
        accounts: list[NetdiskAccountRecord] = []
        try:
            for view in self._accounts.list(platform):
                record = self._accounts.store.get_record(view.id)
                if record is not None:
                    accounts.append(record)
        except NetdiskError:
            pass
        if current is not None and all(item.id != current.id for item in accounts):
            accounts.append(current)
        removed = 0
        excluded = set(exclude() if callable(exclude) else (exclude or ()))
        for account in accounts:
            try:
                entries = adapter.list_dir(account, "")
            except Exception as error:  # noqa: BLE001 - 清理失败不阻断取链
                logger.debug(f"读取网盘根目录失败：{platform.value} - {type(error).__name__}")
                continue
            targets = [
                item for item in entries
                if item.is_dir
                and item.name.startswith(_TEMP_DIR_PREFIXES)
                and item.file_id not in excluded
            ]
            if not targets:
                continue
            try:
                adapter.delete_files(account, targets)
            except Exception as error:  # noqa: BLE001 - 清理失败不阻断取链
                logger.warning(f"清理临时转存目录失败：{platform.value} - {type(error).__name__}")
                continue
            self._quota_cache.pop(account.id, None)
            removed += len(targets)
        if removed:
            logger.info(f"已清理 {removed} 个网盘临时转存目录：{platform.value}")
        return removed

    def _allow_transfer(self, adapter: NetdiskAdapter) -> bool:
        """
        判断当前策略是否允许转存兜底。

        :param adapter: 平台适配器
        :return: 允许转存时返回 True
        """
        if not self.tuning.auto_transfer:
            return False
        return adapter.link_policy is not LinkPolicy.DIRECT

    def _ensure_space(
        self,
        adapter: NetdiskAdapter,
        file: ShareFile,
        account: NetdiskAccountRecord,
        *,
        concurrency: int = 1,
    ) -> None:
        """
        转存前检查账号剩余容量，避免把可预期的失败留到后台。

        需求按「单文件大小 × 并发文件数」估算峰值，并预留 20% 余量。
        容量不足只冷却当前账号，不牵连整个平台。

        :param adapter: 平台适配器
        :param file: 待转存文件
        :param account: 取链账号
        :param concurrency: 同时转存的文件数，用于计算峰值需求
        :raises NetdiskError: 剩余容量不足时抛出 `SPACE_INSUFFICIENT`
        """
        if not adapter.drive_capabilities.quota:
            return
        total, used = self._quota_for(adapter, account)
        if total <= 0:
            return
        free = max(total - used, 0)
        required = int(file.size or 0) * max(int(concurrency or 1), 1)
        if required <= 0:
            return
        # 预留 20% 余量，避免转存后账号写满影响后续文件。
        if free >= required * 1.2:
            return
        selection.cool_account(account.id)
        raise NetdiskError(
            NetdiskErrorCode.SPACE_INSUFFICIENT,
            f"{adapter.platform.value} 账号剩余空间不足：需要 {required} 字节，可用 {free} 字节",
            platform=adapter.platform,
        )

    def _quota_for(
        self,
        adapter: NetdiskAdapter,
        account: NetdiskAccountRecord,
    ) -> tuple[int, int]:
        """
        读取账号配额，并按短 TTL 缓存，避免逐文件调用平台接口。

        :param adapter: 平台适配器
        :param account: 账号记录
        :return: (总容量, 已用容量)，单位字节
        """
        now = time.monotonic()
        cached = self._quota_cache.get(account.id)
        if cached is not None and now - cached[0] < 600:
            return cached[1], cached[2]
        try:
            quota = adapter.quota(account)
        except Exception as error:  # noqa: BLE001 - 配额失败不阻断取链
            logger.debug(f"读取网盘配额失败：{adapter.platform.value} - {type(error).__name__}")
            return 0, 0
        total = max(int(quota.total or 0), 0)
        used = max(int(quota.used or 0), 0)
        self._quota_cache[account.id] = (now, total, used)
        return total, used

    def _resolve_accounts(
        self,
        adapter: NetdiskAdapter,
        account_id: Optional[int],
        multi_source: bool,
        *,
        allow_guest: bool = False,
    ) -> list[Optional[NetdiskAccountRecord]]:
        """
        按平台要求与多源开关选择取链账号。

        :param adapter: 平台适配器
        :param account_id: 指定账号
        :param multi_source: 是否启用多源
        :param allow_guest: 无账号时是否允许游客取链
        :return: 账号列表，游客链路为单个 None
        """
        if account_id:
            return [self._accounts.require_account(adapter.platform, account_id)]
        try:
            primary = self._select_primary(adapter)
        except NetdiskError as error:
            # 只有「无可用账号」才能回落游客；空间不足、限流等必须交给上层恢复链路。
            if not allow_guest or error.code is not NetdiskErrorCode.AUTH_EXPIRED:
                raise
            primary = None
        if primary is None:
            return [None]
        if not multi_source or not self.tuning.multi_source:
            return [primary]
        selected: list[Optional[NetdiskAccountRecord]] = [primary]
        excluded = {primary.id}
        for view in self._accounts.list(adapter.platform):
            if view.id in excluded or view.status != "active":
                continue
            record = self._accounts.store.get_record(view.id)
            if record is not None:
                selected.append(record)
        return selected

    def _select_primary(self, adapter: NetdiskAdapter) -> Optional[NetdiskAccountRecord]:
        """
        选择平台默认账号；允许游客的平台在没有账号时返回 None。

        :param adapter: 平台适配器
        :return: 账号记录或 None
        """
        try:
            return self._accounts.select(adapter.platform)
        except NetdiskError:
            if adapter.requires_account:
                raise
            return None

    def _optional_account(
        self,
        adapter: NetdiskAdapter,
        account_id: Optional[int],
    ) -> Optional[NetdiskAccountRecord]:
        """
        为只读操作选择账号；平台不要求登录时允许游客。

        :param adapter: 平台适配器
        :param account_id: 指定账号
        :return: 账号记录或 None
        """
        if account_id:
            return self._accounts.require_account(adapter.platform, account_id)
        return self._select_primary(adapter)

    def _refresh_account(self, account: NetdiskAccountRecord) -> NetdiskAccountRecord:
        """
        刷新账号并返回最新记录；刷新失败时保留原账号。

        :param account: 账号记录
        :return: 最新账号记录
        """
        try:
            refreshed = self._accounts.refresh(account.id)
        except NetdiskError:
            return account
        return refreshed or account

    def _call(self, adapter: NetdiskAdapter, operation: Callable[[], T]) -> T:
        """
        在平台限流下执行一次协议调用，并把未分类异常收敛为 UNKNOWN。

        :param adapter: 平台适配器
        :param operation: 无参调用
        :return: 调用结果
        """
        self._throttle(adapter.platform)
        try:
            return operation()
        except NetdiskError:
            raise
        except Exception as error:  # noqa: BLE001 - 协议层异常统一分类
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                f"网盘接口调用失败：{type(error).__name__}",
                platform=adapter.platform,
            ) from error

    def _throttle(self, platform: NetdiskPlatform) -> None:
        """
        保证同一平台的调用间隔不小于最小间隔。

        :param platform: 平台标识
        """
        with self._lock:
            last = self._last_call.get(platform, 0.0)
            now = time.monotonic()
            wait = _PLATFORM_MIN_INTERVAL - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_call[platform] = now

    def _cached_link(self, key: LinkCacheKey) -> Optional[DirectLink]:
        """
        读取未过期的直链缓存。

        :param key: 缓存键
        :return: 缓存直链或 None
        """
        with self._lock:
            link = self._link_cache.get(key)
        if link is None:
            return None
        expires_at = link.expires_at
        if expires_at is not None and datetime.now() >= expires_at - _LINK_CACHE_MARGIN:
            with self._lock:
                self._link_cache.pop(key, None)
            return None
        return link

    def _remember_link(self, key: LinkCacheKey, link: DirectLink) -> None:
        """
        写入直链缓存，超过上限时清理最旧的一半。

        :param key: 缓存键
        :param link: 直链
        """
        with self._lock:
            if len(self._link_cache) >= _LINK_CACHE_LIMIT:
                for stale in list(self._link_cache)[: _LINK_CACHE_LIMIT // 2]:
                    self._link_cache.pop(stale, None)
            self._link_cache[key] = link
