"""
CloudSaver 网盘搜索模块，按 CD 站点凭据提供网盘候选、连接测试与 token 刷新。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.domain.context import TorrentInfo
from app.modules import _ModuleBase
from app.modules.cloudsaver.cloudsaver import CloudSaverClient, CloudSaverError, parse_share_code
from app.runtime.log import logger
from app.schemas.types import ModuleType, OtherModulesType


# 从分享文件名校验集数：中文集数、E01 形式与纯数字文件名。
_EPISODE_NAME_PATTERNS = (
    re.compile(r"第\s*(\d{1,4})\s*[集话]"),
    re.compile(r"[Ee][Pp]?\s*(\d{1,4})"),
    re.compile(r"^(\d{1,4})\.[A-Za-z0-9]{2,5}$"),
)
_STANDARD_EPISODE = re.compile(r"\bS\d{1,2}\s*E\d{1,4}\b", re.IGNORECASE)

# 当前只展示夸克分享；其他平台既不支持转存，也没有有效性检查接口。
_ALLOWED_PLATFORMS = frozenset({"quark"})
# 分享有效性检查的并发数、总预算与单次搜索最多检查的候选数。
_ALIVE_WORKERS = 16
_ALIVE_BUDGET_SECONDS = 30
_MAX_ALIVE_CHECKS = 200
# 有效性结果缓存，避免同一分享在多次搜索中重复检查。
_ALIVE_CACHE_TTL = 1800
_ALIVE_CACHE_LIMIT = 2000
_alive_cache: Dict[str, Tuple[float, bool]] = {}
_alive_lock = threading.Lock()


class CloudSaverModule(_ModuleBase):
    """
    按 CD 站点自身的 CloudSaver 凭据登录，并把搜索结果展开为网盘候选。

    客户端按站点缓存，配置指纹变化时重建；模块不落盘 token，也不实现转存与下载。
    """

    def __init__(self) -> None:
        """
        初始化站点客户端缓存与并发锁。
        """
        super().__init__()
        self._client_lock = threading.RLock()
        self._clients: Dict[str, Tuple[Tuple[Any, ...], CloudSaverClient]] = {}

    def init_module(self) -> None:
        """
        配置重载时丢弃旧客户端，后续请求按最新站点配置重建。
        """
        with self._client_lock:
            self._clients.clear()

    def init_setting(self) -> None:
        """
        CloudSaver 由 CD 站点配置驱动，不声明独立的模块开关。
        """

    def stop(self) -> None:
        """
        释放缓存的客户端；token 只在内存中，无需额外清理。
        """
        with self._client_lock:
            self._clients.clear()

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        模块级没有全局配置，可用性由 test_netdisk_site 按站点校验。
        """
        return None

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "CloudSaver"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Indexer

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型。
        """
        return OtherModulesType.CloudSaver

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高。
        """
        return 1

    @staticmethod
    def _as_text(value: Any) -> str:
        """
        将站点字段转换为去除首尾空白的文本。
        """
        if value is None:
            return ""
        return str(value).strip()

    @classmethod
    def _site_credentials(cls, site: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
        """
        读取站点 CloudSaver 地址与凭据，缺项时返回 None。

        :param site: CD 站点配置
        :return: 地址、用户名与密码三元组；任一缺失时为空
        """
        url = cls._as_text(site.get("url")).rstrip("/")
        username = cls._as_text(site.get("username"))
        password = cls._as_text(site.get("password"))
        if not url or not username or not password:
            return None
        return url, username, password

    @classmethod
    def _site_timeout(cls, site: Dict[str, Any]) -> int:
        """
        读取站点请求超时，非法值回退为 90 秒。
        """
        try:
            timeout = int(site.get("timeout") or 90)
        except (TypeError, ValueError):
            return 90
        return timeout if timeout > 0 else 90

    def _site_key(self, site: Dict[str, Any]) -> str:
        """
        返回站点客户端缓存键，优先使用站点 id，其次使用服务地址。
        """
        return self._as_text(site.get("id") or site.get("url"))

    def _get_client(self, site: Dict[str, Any]) -> CloudSaverClient:
        """
        按站点缓存客户端，配置指纹变化时重建。

        :param site: CD 站点配置
        :return: 该站点的 CloudSaver 客户端
        """
        key = self._site_key(site)
        url, username, password = self._site_credentials(site) or ("", "", "")
        fingerprint = (url, username, password, self._site_timeout(site))
        with self._client_lock:
            cached = self._clients.get(key)
            if cached and cached[0] == fingerprint:
                return cached[1]
            client = CloudSaverClient(
                base_url=url,
                username=username,
                password=password,
                timeout=self._site_timeout(site),
            )
            self._clients[key] = (fingerprint, client)
            return client

    def _warn_unavailable(self, site: Dict[str, Any], action: str) -> None:
        """
        记录站点缺少凭据的统一告警。
        """
        site_name = self._as_text(site.get("name") or site.get("id")) or "未知站点"
        logger.warn(f"CD 站点 {site_name} 缺少 CloudSaver 地址或凭据，无法{action}")

    def _build_torrents(self, site: Dict[str, Any], entries: List[Dict[str, Any]]) -> List[TorrentInfo]:
        """
        将归一化条目展开为网盘候选，并按平台与分享链接去重。

        :param site: CD 站点配置
        :param entries: 客户端返回的归一化条目
        :return: 网盘候选列表
        """
        torrents: List[TorrentInfo] = []
        seen: set[Tuple[str, str]] = set()
        site_name = self._as_text(site.get("name")) or None
        site_downloader = self._as_text(site.get("downloader")) or None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            title = self._as_text(entry.get("title"))
            content = self._as_text(entry.get("content"))
            channel = self._as_text(entry.get("channel"))
            pubdate = self._as_text(entry.get("pubdate")) or None
            for link in entry.get("links") or []:
                if not isinstance(link, dict):
                    continue
                url = self._as_text(link.get("url"))
                if not url:
                    continue
                platform = self._as_text(link.get("platform"))
                if platform not in _ALLOWED_PLATFORMS:
                    continue
                # 同一分享可能以不同链接形式重复出现，按分享码去重，无法解析时退回链接本身。
                share_code = parse_share_code(url)
                dedup_key = share_code or url.split("?", 1)[0].rstrip("/")
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                labels = [platform] if platform else []
                if channel and channel not in labels:
                    labels.append(channel)
                torrents.append(TorrentInfo(
                    site=site.get("id"),
                    site_name=site_name,
                    site_downloader=site_downloader,
                    title=title or url,
                    description=content or None,
                    enclosure=url,
                    page_url=url,
                    pubdate=pubdate,
                    download_type="netdisk",
                    netdisk_platform=platform or None,
                    share_password=self._as_text(link.get("password")) or None,
                    # 网盘资源转存后本地下载不产生 PT 计费，按 1x 上传 / 免费下载展示。
                    uploadvolumefactor=1.0,
                    downloadvolumefactor=0.0,
                    labels=labels,
                ))
        return torrents

    @staticmethod
    def _alive_cache_key(site: Dict[str, Any], share_code: str) -> str:
        """
        构造分享有效性缓存键，按站点隔离不同 CloudSaver 实例。

        :param site: CD 站点配置
        :param share_code: 分享码
        :return: 缓存键
        """
        return f"{site.get('id') or site.get('url')}:{share_code}"

    def _cached_alive(self, key: str) -> Optional[bool]:
        """
        读取缓存中的分享有效性。

        :param key: 缓存键
        :return: 命中时返回有效性，未命中或过期返回 None
        """
        now = time.time()
        with _alive_lock:
            hit = _alive_cache.get(key)
        if hit and now - hit[0] < _ALIVE_CACHE_TTL:
            return hit[1]
        return None

    @staticmethod
    def _store_alive(key: str, alive: bool) -> None:
        """
        写入分享有效性缓存，超过容量时整体清空。

        :param key: 缓存键
        :param alive: 分享是否有效
        """
        with _alive_lock:
            if len(_alive_cache) >= _ALIVE_CACHE_LIMIT:
                _alive_cache.clear()
            _alive_cache[key] = (time.time(), alive)

    @staticmethod
    def _episode_token(file_names: List[str]) -> Optional[str]:
        """
        从分享文件名推导季集标记，优先使用最小与最大集数。

        :param file_names: 分享根目录文件名列表
        :return: 形如 S01E12 或 S01E01-E24 的标记；无法推导时为空
        """
        episodes: List[int] = []
        for name in file_names or []:
            for pattern in _EPISODE_NAME_PATTERNS:
                matched = pattern.search(name)
                if matched:
                    episodes.append(int(matched.group(1)))
                    break
        episodes = [value for value in episodes if 0 < value <= 9999]
        if not episodes:
            return None
        begin, end = min(episodes), max(episodes)
        if begin == end:
            return f"S01E{begin:02d}"
        return f"S01E{begin:02d}-E{end:02d}"

    @staticmethod
    def _apply_share_detail(torrent: TorrentInfo, detail: Dict[str, Any]) -> None:
        """
        用分享详情补齐候选大小与集数。

        :param torrent: 待补齐的网盘候选
        :param detail: share_detail 返回的分享详情
        """
        file_size = int(detail.get("file_size") or 0)
        if file_size > 0:
            torrent.size = float(file_size)
        if _STANDARD_EPISODE.search(torrent.title or ""):
            return
        token = CloudSaverModule._episode_token(detail.get("file_names") or [])
        if token:
            torrent.title = f"{torrent.title or ''} {token}".strip()

    def _filter_alive_shares(
        self,
        site: Dict[str, Any],
        torrents: List[TorrentInfo],
    ) -> List[TorrentInfo]:
        """
        只保留仍然有效的分享，并按发布时间优先检查最新的若干条。

        :param site: CD 站点配置
        :param torrents: 去重后的网盘候选
        :return: 有效分享候选，保持原顺序
        """
        if not torrents:
            return []
        client = self._get_client(site)
        ordered = sorted(
            torrents,
            key=lambda torrent: torrent.pubdate or "",
            reverse=True,
        )[:_MAX_ALIVE_CHECKS]
        alive_flags: Dict[int, bool] = {}
        pending: List[Tuple[TorrentInfo, str, str]] = []
        for torrent in ordered:
            share_code = parse_share_code(torrent.enclosure or "")
            if not share_code:
                continue
            cache_key = self._alive_cache_key(site, share_code)
            cached = self._cached_alive(cache_key)
            if cached is None:
                pending.append((torrent, share_code, cache_key))
            else:
                alive_flags[id(torrent)] = cached
        if pending:
            executor = ThreadPoolExecutor(max_workers=_ALIVE_WORKERS)
            try:
                futures = {
                    executor.submit(client.share_detail, share_code, torrent.share_password): (torrent, cache_key)
                    for torrent, share_code, cache_key in pending
                }
                for future in as_completed(futures, timeout=_ALIVE_BUDGET_SECONDS):
                    torrent, cache_key = futures[future]
                    try:
                        detail = future.result()
                    except Exception:
                        detail = None
                    if not detail:
                        # 无法判定时按失效处理，保证只展示确认可用的分享。
                        alive_flags[id(torrent)] = False
                        continue
                    alive = bool(detail.get("alive"))
                    self._store_alive(cache_key, alive)
                    alive_flags[id(torrent)] = alive
                    if alive:
                        self._apply_share_detail(torrent, detail)
            except TimeoutError:
                logger.debug("CD 站点分享有效性检查超时，未完成的候选按失效处理")
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        return [torrent for torrent in ordered if alive_flags.get(id(torrent))]

    def search_netdisk_torrents(
        self,
        site: Dict[str, Any],
        keyword: str,
        mtype: Optional[Any] = None,
        page: int = 0,
    ) -> Optional[List[TorrentInfo]]:
        """
        搜索一个 CD 站点的网盘分享，并把每条链接展开为候选。

        :param site: CD 站点配置；非 cd 站点返回 None 交由其他模块处理
        :param keyword: 搜索关键词
        :param mtype: 媒体类型，CloudSaver 搜索不使用
        :param page: 页码，CloudSaver 搜索不使用
        :return: 网盘候选列表；非 cd 站点返回 None，失败时返回空列表
        """
        if not isinstance(site, dict) or site.get("kind") != "cd":
            return None
        if not self._as_text(keyword):
            report_site_search_outcome(attempted=True, outcome="success")
            report_site_search_page(raw_count=0, has_more=False)
            return []
        if not self._site_credentials(site):
            self._warn_unavailable(site, "搜索网盘资源")
            report_site_search_outcome(
                attempted=True,
                outcome="error",
                error="CD 站点缺少 CloudSaver 地址或凭据",
            )
            return []
        try:
            entries = self._get_client(site).search(keyword)
        except Exception as err:
            site_name = self._as_text(site.get("name") or site.get("id")) or "未知站点"
            logger.warn(f"CD 站点 {site_name} 网盘搜索失败：{err}")
            report_site_search_outcome(attempted=True, outcome="error", error=str(err))
            return []
        torrents = self._filter_alive_shares(site, self._build_torrents(site, entries))
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(torrents), has_more=False)
        return torrents

    async def async_search_netdisk_torrents(
        self,
        site: Dict[str, Any],
        keyword: str,
        mtype: Optional[Any] = None,
        page: int = 0,
    ) -> Optional[List[TorrentInfo]]:
        """
        异步搜索一个 CD 站点的网盘分享，阻塞请求放到线程池执行。
        """
        return await asyncio.to_thread(self.search_netdisk_torrents, site, keyword, mtype, page)

    def test_netdisk_site(self, site: Dict[str, Any]) -> Optional[Tuple[bool, str]]:
        """
        尝试登录指定 CD 站点，验证 CloudSaver 地址与凭据是否可用。

        :param site: CD 站点配置；非 cd 站点返回 None
        :return: 是否可用及失败原因
        """
        if not isinstance(site, dict) or site.get("kind") != "cd":
            return None
        if not self._site_credentials(site):
            return False, "CD 站点缺少 CloudSaver 地址或凭据"
        try:
            self._get_client(site).login()
        except CloudSaverError as err:
            return False, str(err)
        except Exception as err:
            return False, f"CloudSaver 连接失败：{err}"
        return True, ""

    def refresh_netdisk_token(self, site: Dict[str, Any]) -> Optional[bool]:
        """
        强制重新登录指定 CD 站点，刷新内存中的 token。

        :param site: CD 站点配置；非 cd 站点返回 None
        :return: 是否刷新成功；非 cd 站点返回 None
        """
        if not isinstance(site, dict) or site.get("kind") != "cd":
            return None
        if not self._site_credentials(site):
            self._warn_unavailable(site, "刷新 token")
            return False
        try:
            self._get_client(site).refresh()
        except Exception as err:
            site_name = self._as_text(site.get("name") or site.get("id")) or "未知站点"
            logger.warn(f"CD 站点 {site_name} CloudSaver token 刷新失败：{err}")
            return False
        return True
