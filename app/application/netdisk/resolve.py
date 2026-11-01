"""
网盘取链用例：策略分支、账号选择、直链缓存、刷新与限流。

本模块只编排端口，不直接访问网络或数据库。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence, TypeVar

from app.application.netdisk import registry
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

# 同一平台的两次取链调用之间的最小间隔，避免连续请求触发频控。
_PLATFORM_MIN_INTERVAL = 0.3
T = TypeVar("T")
# 直链缓存键：平台、分享码、文件标识、账号主键。
LinkCacheKey = tuple[NetdiskPlatform, str, str, int]
# 直链缓存条目上限，超出后清空最旧的一半。
_LINK_CACHE_LIMIT = 256
# 直链剩余有效期低于该值时不再复用缓存。
_LINK_CACHE_MARGIN = timedelta(minutes=1)


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
        pending = [(item, 1) for item in detail.files]
        while pending:
            item, depth = pending.pop(0)
            if item.is_dir:
                if depth >= max_depth:
                    continue
                def load_children(parent: ShareFile = item) -> list[ShareFile]:
                    """
                    读取当前目录的子项。
                    """
                    return adapter.list_files(ref, parent.file_id, account)

                children = self._call(adapter, load_children)
                pending.extend((child, depth + 1) for child in children)
                continue
            files.append(item)
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
        :return: 直链元组
        """
        adapter, ref = self.parse(url, password, platform, share_token)
        accounts = self._resolve_accounts(adapter, account_id, multi_source)
        links: list[DirectLink] = []
        for account in accounts:
            links.append(
                self._resolve_one(
                    adapter,
                    ref,
                    file,
                    account,
                    force_refresh=force_refresh,
                )
            )
        return tuple(links)

    def cleanup(self, link: DirectLink) -> None:
        """
        清理直链关联的转存内容。

        :param link: 已下载完成的直链
        """
        handle: Optional[TransferHandle] = link.cleanup_handle
        if handle is None:
            return
        adapter = registry.get_adapter(handle.platform)
        account = self._accounts.store.get_record(handle.account_id)
        try:
            adapter.cleanup(handle, account)
        except NetdiskError:
            return

    def _resolve_one(
        self,
        adapter: NetdiskAdapter,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        force_refresh: bool,
    ) -> DirectLink:
        """
        按策略为单个文件取链，并处理缓存与刷新。

        :param adapter: 平台适配器
        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param force_refresh: 是否跳过缓存并刷新账号
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
        allow_transfer = self._allow_transfer(adapter)
        current = account
        if force_refresh and current is not None:
            current = self._refresh_account(current)
        try:
            link = self._call(
                adapter,
                lambda: adapter.resolve(ref, file, current, allow_transfer=allow_transfer),
            )
        except NetdiskError as error:
            if error.code is not NetdiskErrorCode.AUTH_EXPIRED or current is None:
                raise
            current = self._refresh_account(current)
            link = self._call(
                adapter,
                lambda: adapter.resolve(ref, file, current, allow_transfer=allow_transfer),
            )
        self._remember_link(cache_key, link)
        return link

    def _allow_transfer(self, adapter: NetdiskAdapter) -> bool:
        """
        判断当前策略是否允许转存兜底。

        :param adapter: 平台适配器
        :return: 允许转存时返回 True
        """
        if not self.tuning.auto_transfer:
            return False
        return adapter.link_policy is not LinkPolicy.DIRECT

    def _resolve_accounts(
        self,
        adapter: NetdiskAdapter,
        account_id: Optional[int],
        multi_source: bool,
    ) -> list[Optional[NetdiskAccountRecord]]:
        """
        按平台要求与多源开关选择取链账号。

        :param adapter: 平台适配器
        :param account_id: 指定账号
        :param multi_source: 是否启用多源
        :return: 账号列表，游客链路为单个 None
        """
        if account_id:
            return [self._accounts.require_account(adapter.platform, account_id)]
        primary = self._select_primary(adapter)
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
