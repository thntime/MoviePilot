"""
站点写操作应用用例。
"""

import copy
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional, TypeAlias

from app.application.configuration import SystemConfigStagingPort
from app.application.outbox import SyncUnitOfWork
from app.application.security.image import site_image_domains
from app.application.site.contract import (
    SiteMutation,
    SitePriorityMutation,
    SiteStagingPort,
    SiteWriteResult,
)
from app.application.subscription.contract import (
    SubscriptionPatch,
    SubscriptionReferenceStagingPort,
)
from app.application.subscription.delete import AsyncUnitOfWork
from app.schemas.common import JsonData
from app.schemas.exception import SiteDomainConflictError
from app.schemas.types import SystemConfigKey

SiteMutationResult: TypeAlias = SiteWriteResult
SiteIndexerLoader = Callable[
    [str],
    Awaitable[Optional[Mapping[str, JsonData]]],
]
SiteEventPublisher = Callable[[dict[str, JsonData]], Awaitable[None]]
DomainExtractor = Callable[[str], str]
UrlNormalizer = Callable[[str], str]

# tg 为前端 TG 频道沿用的历史类型值，行为与 PT 一致，仅用于保留既有创建流程。
_SITE_KINDS = frozenset({"pt", "cd", "tg"})
_SITE_AUTH_TYPES = frozenset({"cookie", "api", "auth"})
_SITE_KIND_AUTH_TYPES = {
    "pt": frozenset({"cookie", "api"}),
    "cd": frozenset({"auth"}),
    "tg": frozenset({"cookie", "api"}),
}


def _resolve_site_kind_auth(
    values: Mapping[str, JsonData],
    *,
    default_kind: str = "pt",
    default_auth_type: str = "cookie",
) -> tuple[str, str, Optional[str]]:
    """
    解析站点类型与认证方式，返回解析值或中文校验失败原因。

    :param values: 待写入的站点字段
    :param default_kind: 未显式提供站点类型时采用的默认值
    :param default_auth_type: 未显式提供认证方式且类型为 PT 时采用的默认值
    :return: 站点类型、认证方式与失败原因；成功时原因为 None
    """
    raw_kind = values.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind else default_kind
    raw_auth_type = values.get("auth_type")
    if isinstance(raw_auth_type, str) and raw_auth_type:
        auth_type = raw_auth_type
    elif kind == "cd":
        # CD 站点只有 AUTH 一种可用认证方式，未显式提供时直接推导。
        auth_type = "auth"
    else:
        auth_type = default_auth_type
    if kind not in _SITE_KINDS:
        return "", "", f"站点类型不支持：{kind}，仅支持 pt/cd"
    if auth_type not in _SITE_AUTH_TYPES:
        return "", "", f"认证方式不支持：{auth_type}，仅支持 cookie/api/auth"
    if auth_type not in _SITE_KIND_AUTH_TYPES[kind]:
        return "", "", f"{kind} 站点不支持 {auth_type} 认证方式"
    return kind, auth_type, None


@dataclass(frozen=True, slots=True)
class SiteReferenceMutation:
    """
    站点引用在 SystemConfig 和订阅表中原子提交后的结果。

    :param rss_sites: 清理引用后保留的 RSS 站点主键
    :param subscription_ids: 本次站点引用发生修改的订阅主键
    """

    rss_sites: tuple[int, ...]
    subscription_ids: tuple[int, ...]


class SyncSiteReferenceMutationService:
    """
    在一个同步 UoW 内原子清理 RSS 和订阅站点引用。
    """

    def __init__(
        self,
        configuration: SystemConfigStagingPort,
        subscriptions: SubscriptionReferenceStagingPort,
        unit_of_work: SyncUnitOfWork,
        publish: Callable[[Mapping[SystemConfigKey, JsonData]], None],
    ) -> None:
        """
        注入共享 Session 的配置、订阅、UoW 与提交后快照发布器。
        """
        self._configuration = configuration
        self._subscriptions = subscriptions
        self._unit_of_work = unit_of_work
        self._publish = publish

    def apply(self, site_id: int | str) -> SiteReferenceMutation:
        """
        清空通配站点或移除指定站点，并在任一步失败时整体回滚。
        """
        if site_id != "*" and not isinstance(site_id, int):
            raise ValueError("站点引用清理只接受整数 ID 或通配符 *")
        try:
            result, changes = self._stage(site_id)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
        self._publish(copy.deepcopy(changes))
        return result

    def _stage(
        self,
        site_id: int | str,
    ) -> tuple[SiteReferenceMutation, dict[SystemConfigKey, JsonData]]:
        """
        在锁定的同步事实源中暂存 RSS 和订阅站点变化。
        """
        changes: dict[SystemConfigKey, JsonData] = {}
        original_rss = self._configuration.get_for_update(SystemConfigKey.RssSites)
        rss_sites = [
            int(value)
            for value in original_rss
            if isinstance(value, int) and site_id != "*" and value != site_id
        ] if isinstance(original_rss, list) else []
        if rss_sites != (original_rss or []):
            rss_payload = list[JsonData](rss_sites)
            self._configuration.stage_set(SystemConfigKey.RssSites, rss_payload)
            changes[SystemConfigKey.RssSites] = rss_payload

        subscription_ids = []
        for subscription in self._subscriptions.list_for_reference_rewrite():
            original_sites = list(subscription.sites or [])
            sites = [
                value
                for value in original_sites
                if site_id != "*" and value != site_id
            ]
            if sites == original_sites:
                continue
            self._subscriptions.stage_update(
                subscription.id,
                SubscriptionPatch({"sites": list[JsonData](sites)}),
            )
            subscription_ids.append(subscription.id)
        return (
            SiteReferenceMutation(tuple(rss_sites), tuple(subscription_ids)),
            changes,
        )


class SiteMutationCommand:
    """
    统一执行站点新增、更新、优先级和删除事务。
    """

    def __init__(
        self,
        *,
        repository: SiteStagingPort,
        unit_of_work: AsyncUnitOfWork,
        auth_level_provider: Callable[[], int],
        indexer_loader: SiteIndexerLoader,
        domain_extractor: DomainExtractor,
        url_normalizer: UrlNormalizer,
        publish_updated: SiteEventPublisher,
        publish_deleted: SiteEventPublisher,
    ) -> None:
        """
        保存站点验证、持久化、事务和提交后事件端口。
        """
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._auth_level_provider = auth_level_provider
        self._indexer_loader = indexer_loader
        self._domain_extractor = domain_extractor
        self._url_normalizer = url_normalizer
        self._publish_updated = publish_updated
        self._publish_deleted = publish_deleted

    async def create(
        self,
        payload: Mapping[str, JsonData],
    ) -> SiteMutationResult:
        """
        校验并新增站点，提交成功后发布站点更新事件。
        """
        values = dict(payload)
        raw_url = values.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            return SiteMutationResult(False, "站点地址不能为空")
        kind, auth_type, error = _resolve_site_kind_auth(values)
        if error:
            return SiteMutationResult(False, error)
        # CD 站点不依赖官方站点资源包，不需要用户认证；PT 站点保持原有认证门槛。
        if kind != "cd" and self._auth_level_provider() < 2:
            return SiteMutationResult(False, "用户未通过认证，无法使用站点功能！")

        domain = self._domain_extractor(raw_url)
        if kind == "cd":
            username = values.get("username")
            password = values.get("password")
            if not isinstance(username, str) or not username.strip():
                return SiteMutationResult(False, "CD 站点必须填写用户名")
            if not isinstance(password, str) or not password:
                return SiteMutationResult(False, "CD 站点必须填写密码")
            if await self._repository.async_get_by_domain(domain):
                return SiteMutationResult(False, f"{domain} 站点己存在")
            raw_name = values.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else domain
            values.update(
                {
                    "domain": domain,
                    "url": self._url_normalizer(raw_url),
                    "name": name,
                    "public": 0,
                }
            )
            if values.get("timeout") is None:
                values["timeout"] = 90
            if values.get("auth_refresh_interval") is None:
                values["auth_refresh_interval"] = 300
        else:
            site_info = await self._indexer_loader(domain)
            if not site_info:
                return SiteMutationResult(False, "该站点不支持，请检查站点域名是否正确")
            if await self._repository.async_get_by_domain(domain):
                return SiteMutationResult(False, f"{domain} 站点己存在")
            values.update(
                {
                    "domain": domain,
                    "url": self._url_normalizer(raw_url),
                    "name": site_info.get("name"),
                    "public": 1 if site_info.get("public") else 0,
                }
            )

        values.pop("id", None)
        values["kind"] = kind
        values["auth_type"] = auth_type
        try:
            await self._repository.stage_create(SiteMutation(values))
            await self._commit()
        except SiteDomainConflictError:
            await self._unit_of_work.rollback()
            return SiteMutationResult(False, f"{domain} 站点已存在")
        await self._publish_updated({"domain": domain})
        return SiteMutationResult(True)

    async def update(
        self,
        payload: Mapping[str, JsonData],
    ) -> SiteMutationResult:
        """
        更新站点并在提交后发布完整的兼容事件载荷。
        """
        values = dict(payload)
        site_id = values.get("id")
        if not isinstance(site_id, int) or not site_id:
            return SiteMutationResult(False, "站点不存在")
        site_info = await self._repository.get_by_id(site_id)
        if not site_info:
            return SiteMutationResult(False, "站点不存在")
        kind, auth_type, error = _resolve_site_kind_auth(
            values,
            default_kind=getattr(site_info, "kind", None) or "pt",
            default_auth_type=getattr(site_info, "auth_type", None) or "cookie",
        )
        if error:
            return SiteMutationResult(False, error)
        values["kind"] = kind
        values["auth_type"] = auth_type
        password = values.get("password")
        if password is None or password == "":
            values.pop("password", None)

        raw_url = values.get("url")
        normalized_url = self._url_normalizer(raw_url if isinstance(raw_url, str) else "")
        normalized_domain = self._domain_extractor(normalized_url)
        values["url"] = normalized_url
        values["domain"] = normalized_domain
        values.pop("id", None)
        try:
            await self._repository.stage_update(site_id, SiteMutation(values))
            await self._commit()
        except SiteDomainConflictError:
            await self._unit_of_work.rollback()
            return SiteMutationResult(False, f"{normalized_domain} 站点已存在")
        await self._publish_updated(
            {
                "site_id": site_id,
                "domain": normalized_domain,
                "name": values.get("name"),
                "site_url": normalized_url,
            }
        )
        return SiteMutationResult(True)

    async def set_cookie(
        self,
        site_id: int,
        cookie: str,
        ua: Optional[str] = None,
    ) -> SiteMutationResult:
        """
        仅更新站点 Cookie 与可选 User-Agent，并发布站点更新事件。
        """
        site_info = await self._repository.get_by_id(site_id)
        if site_info is None:
            return SiteMutationResult(False, "站点不存在")
        values: dict[str, JsonData] = {"cookie": cookie}
        if ua is not None:
            values["ua"] = ua
        await self._repository.stage_update(site_id, SiteMutation(values))
        await self._commit()
        await self._publish_updated(
            {
                "site_id": site_id,
                "domain": site_info.domain,
                "name": site_info.name,
                "site_url": site_info.url,
            }
        )
        return SiteMutationResult(True)

    async def update_priorities(
        self,
        priorities: Sequence[Mapping[str, JsonData]],
    ) -> SiteMutationResult:
        """
        在同一事务中更新全部站点优先级。
        """
        mutations: list[SitePriorityMutation] = []
        for priority in priorities:
            site_id = priority.get("id")
            value = priority.get("pri")
            if isinstance(site_id, int) and isinstance(value, int):
                mutations.append(SitePriorityMutation(site_id=site_id, priority=value))
        await self._repository.stage_priorities(tuple(mutations))
        await self._commit()
        return SiteMutationResult(True)

    async def delete(self, site_id: int) -> SiteMutationResult:
        """
        删除站点，并确保删除事件只在提交成功后发送。
        """
        await self._repository.stage_delete(site_id)
        await self._commit()
        await self._publish_deleted({"site_id": site_id})
        return SiteMutationResult(True)

    async def reset(self) -> SiteMutationResult:
        """
        清空全部站点，并在提交后发布通配站点删除事件。
        """
        await self._repository.stage_reset()
        await self._commit()
        await self._publish_deleted({"site_id": "*"})
        return SiteMutationResult(True)

    async def _commit(self) -> None:
        """
        提交当前站点事务，失败时回滚并保留原始异常。
        """
        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
        site_image_domains.invalidate()
