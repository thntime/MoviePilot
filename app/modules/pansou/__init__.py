"""
PanSou 网盘搜索模块，按 PS 站点地址搜索 PanSou 并把链接展开为网盘候选。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from app.application.netdisk import candidates
from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.domain.context import TorrentInfo
from app.domain.netdisk import parse_share_link
from app.modules import _ModuleBase
from app.modules.pansou.client import PansouClient, PansouError
from app.runtime.log import logger
from app.schemas.types import ModuleType, OtherModulesType

# 分享有效性检查的并发数、总预算与单次搜索最多检查的候选数。
_ALIVE_WORKERS = 16
_ALIVE_BUDGET_SECONDS = 30
_MAX_ALIVE_CHECKS = 200
# 有效性结果缓存，避免同一分享在多次搜索中重复检查。
_ALIVE_CACHE_TTL = 1800
_ALIVE_CACHE_LIMIT = 2000
_alive_cache: Dict[str, Tuple[float, bool, Optional[Dict[str, Any]]]] = {}
_alive_lock = threading.Lock()

# 标题里声明的总集数（更新218集 / 全219集 / 共12话）；频道文案经常与实际分享不符。
_CLAIM_EPISODE_PATTERN = re.compile(r"(?:更新至?|全集?|共)\s*(\d{1,4})\s*[集话]")
# 声明集数低于该值时不做一致性判断，避免短片、综艺等正常分集被误伤。
_CLAIM_MIN_EPISODES = 60
# 实际最大集号低于声明集数的该比例时，判定标题与实际内容不符。
_CLAIM_TOLERANCE_RATIO = 0.5
# 各平台允许的分享域名后缀：频道文案经常把链接写成错域名，下载必然失败。
_PLATFORM_HOST_SUFFIXES = {
    "quark": ("quark.cn",),
    "uc": ("uc.cn",),
    "pan115": ("115.com", "115cdn.com"),
    "baidu": ("baidu.com",),
    "xunlei": ("xunlei.com",),
    "tianyi": ("189.cn",),
    "pan123": ("123pan.com", "123pan.cn", "123684.com", "123865.com", "123912.com"),
    "pan139": ("139.com",),
    "guangya": ("guangyapan.com",),
}


class PansouModule(_ModuleBase):
    """
    按 PS 站点配置搜索 PanSou，并把平台分组的分享链接展开为网盘候选。

    客户端按站点缓存，配置指纹变化时重建；模块不落盘 token，也不实现转存与下载。
    """

    def __init__(self) -> None:
        """
        初始化站点客户端缓存与并发锁。
        """
        super().__init__()
        self._client_lock = threading.RLock()
        self._clients: Dict[str, Tuple[Tuple[Any, ...], PansouClient]] = {}

    def init_module(self) -> None:
        """
        配置重载时丢弃旧客户端，后续请求按最新站点配置重建。
        """
        with self._client_lock:
            self._clients.clear()

    def init_setting(self) -> None:
        """
        PanSou 由 PS 站点配置驱动，不声明独立的模块开关。
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
        return "PanSou"

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
        return OtherModulesType.Pansou

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高。
        """
        return 2

    @staticmethod
    def _as_text(value: Any) -> str:
        """
        将站点字段转换为去除首尾空白的文本。
        """
        if value is None:
            return ""
        return str(value).strip()

    @classmethod
    def _site_address(cls, site: Dict[str, Any]) -> str:
        """
        读取站点 PanSou 地址，缺项时返回空字符串。

        :param site: PS 站点配置
        :return: 去掉尾部斜杠的服务地址
        """
        return cls._as_text(site.get("url")).rstrip("/")

    @classmethod
    def _site_token(cls, site: Dict[str, Any]) -> str:
        """
        读取站点 Bearer token，未开启认证时为空。

        :param site: PS 站点配置
        :return: token 文本
        """
        return cls._as_text(site.get("token"))

    @classmethod
    def _site_timeout(cls, site: Dict[str, Any]) -> int:
        """
        读取站点请求超时，非法值回退为 30 秒。
        """
        try:
            timeout = int(site.get("timeout") or 30)
        except (TypeError, ValueError):
            return 30
        return timeout if timeout > 0 else 30

    def _site_key(self, site: Dict[str, Any]) -> str:
        """
        返回站点客户端缓存键，优先使用站点 id，其次使用服务地址。
        """
        return self._as_text(site.get("id") or site.get("url"))

    def _get_client(self, site: Dict[str, Any]) -> PansouClient:
        """
        按站点缓存客户端，配置指纹变化时重建。

        :param site: PS 站点配置
        :return: 该站点的 PanSou 客户端
        """
        key = self._site_key(site)
        address = self._site_address(site)
        token = self._site_token(site)
        fingerprint = (address, token, self._site_timeout(site))
        with self._client_lock:
            cached = self._clients.get(key)
            if cached and cached[0] == fingerprint:
                return cached[1]
            client = PansouClient(
                base_url=address,
                token=token,
                timeout=self._site_timeout(site),
            )
            self._clients[key] = (fingerprint, client)
            return client

    def _warn_unavailable(self, site: Dict[str, Any], action: str) -> None:
        """
        记录站点缺少 PanSou 地址的统一告警。
        """
        site_name = self._as_text(site.get("name") or site.get("id")) or "未知站点"
        logger.warn(f"PS 站点 {site_name} 缺少 PanSou 地址，无法{action}")

    def _build_torrents(self, site: Dict[str, Any], entries: List[Dict[str, Any]]) -> List[TorrentInfo]:
        """
        将归一化条目展开为网盘候选，并按平台与分享链接去重。

        :param site: PS 站点配置
        :param entries: 客户端返回的归一化条目
        :return: 网盘候选列表
        """
        torrents: List[TorrentInfo] = []
        seen: set[str] = set()
        site_name = self._as_text(site.get("name")) or None
        site_downloader = self._as_text(site.get("downloader")) or None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            url = self._as_text(entry.get("url"))
            if not url:
                continue
            platform = self._as_text(entry.get("platform"))
            if not candidates.platform_supported(platform) or not candidates.platform_enabled(platform):
                continue
            if not self._platform_matches_host(platform, url):
                # 频道文案里的链接可能是错域名或伪造域名（如 pan.qoark.cn），下载必然失败，直接丢弃。
                logger.info(f"{self._as_text(entry.get('title')) or url} 链接域名与平台不符，已丢弃：{url}")
                continue
            # 同一分享可能以不同链接形式重复出现，按分享码去重；无法解析分享码时保留完整链接。
            share_ref = parse_share_link(url, self._as_text(entry.get("password")) or None)
            dedup_key = f"{share_ref.platform.value}:{share_ref.share_id}" if share_ref and share_ref.share_id else url
            dedup_key = dedup_key.rstrip("/") or url
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            title = self._as_text(entry.get("title"))
            source = self._as_text(entry.get("source"))
            torrents.append(TorrentInfo(
                site=site.get("id"),
                site_name=site_name,
                site_downloader=site_downloader,
                title=title or url,
                description=None,
                enclosure=url,
                page_url=url,
                pubdate=self._as_text(entry.get("pubdate")) or None,
                download_type="netdisk",
                netdisk_platform=platform or None,
                share_password=self._as_text(entry.get("password")) or None,
                # 网盘资源转存后本地下载不产生 PT 计费，按 1x 上传 / 免费下载展示。
                uploadvolumefactor=1.0,
                downloadvolumefactor=0.0,
                labels=[source] if source else [],
            ))
        return torrents

    @staticmethod
    def _platform_matches_host(platform: str, url: str) -> bool:
        """
        判断分享链接的域名是否属于该平台。

        频道文案里的链接常写成错域名（如把 quark 写成 pan.qoark.cn），这类链接
        永远取不到内容，应在进入取链前丢弃；未知平台不做判断，交给平台过滤。

        :param platform: PanSou 返回的平台标识
        :param url: 分享链接
        :return: 域名属于该平台或无法判断时返回 True
        """
        suffixes = _PLATFORM_HOST_SUFFIXES.get(str(platform or "").strip())
        if not suffixes:
            return True
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)

    @staticmethod
    def _alive_cache_key(site: Dict[str, Any], identity: str) -> str:
        """
        构造分享有效性缓存键，按站点隔离不同 PanSou 实例。

        :param site: PS 站点配置
        :param identity: 分享标识
        :return: 缓存键
        """
        return f"{site.get('id') or site.get('url')}:{identity}"

    @staticmethod
    def _cached_alive(key: str) -> Optional[Tuple[bool, Optional[Dict[str, Any]]]]:
        """
        读取缓存中的分享有效性与分享详情。

        只缓存有效性，集数与短剧判定每次搜索都重新计算，阈值调整后才能立即生效。

        :param key: 缓存键
        :return: 命中时返回 (有效性, 分享详情)，未命中或过期返回 None
        """
        now = time.time()
        with _alive_lock:
            hit = _alive_cache.get(key)
        if hit and now - hit[0] < _ALIVE_CACHE_TTL:
            return bool(hit[1]), hit[2]
        return None

    @staticmethod
    def _store_alive(key: str, alive: bool, detail: Optional[Dict[str, Any]] = None) -> None:
        """
        写入分享有效性缓存，超过容量时整体清空。

        :param key: 缓存键
        :param alive: 分享是否有效
        :param detail: 有效时一并缓存的分享详情，供后续搜索复用
        """
        with _alive_lock:
            if len(_alive_cache) >= _ALIVE_CACHE_LIMIT:
                _alive_cache.clear()
            _alive_cache[key] = (time.time(), alive, detail if alive else None)

    @staticmethod
    def _mark_unverified(torrent: TorrentInfo) -> None:
        """
        在候选描述中标注未验证状态，避免与已确认有效的分享混淆。

        :param torrent: 网盘候选
        """
        marker = "（未验证）"
        description = str(torrent.description or "")
        if marker not in description:
            torrent.description = f"{description}{marker}" if description else marker

    @staticmethod
    def _inspect_remote_share(
        torrent: TorrentInfo,
        platform: str,
        cached_detail: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        用本机统一取链适配器检查网盘候选。

        明确失效返回 `alive=False`；鉴权、超时或网络异常返回未验证标记，
        由调用方保留候选而不是直接丢弃。缓存中已有完整文件树时直接复用，避免重复遍历。

        :param torrent: 待检查的网盘候选
        :param platform: 平台标识
        :param cached_detail: 缓存中已有的分享详情，含 `_files` 时跳过网络请求
        :return: (分享详情, 是否通过质量检查)
        """
        from app.application.netdisk.resolve import NetdiskResolveService
        from app.domain.netdisk import NetdiskError, NetdiskErrorCode

        if cached_detail and cached_detail.get("_files"):
            return cached_detail, PansouModule._passes_pansou_quality(torrent, cached_detail)
        try:
            service = NetdiskResolveService()
            walked = service.walk(
                str(torrent.enclosure or ""),
                torrent.share_password,
                platform,
            )
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.SHARE_INVALID:
                return {"alive": False}, False
            return {"alive": True, "unverified": True}, True
        except Exception:  # noqa: BLE001 - 网络与超时统一按未验证保留
            return {"alive": True, "unverified": True}, True
        files = [
            {
                "name": item.name,
                "size": item.size,
                "duration": int(getattr(item, "duration", 0) or 0),
            }
            for item in walked.files
        ]
        # 分享根目录是文件夹时详情大小为 0，用递归展开后的文件大小求和兜底。
        file_size = int(walked.detail.size or 0) or sum(int(item.size or 0) for item in walked.files)
        detail = {
            "alive": True,
            "file_size": file_size,
            "file_names": [item.name for item in walked.detail.files],
            "stoken": walked.detail.stoken,
            "_files": files,
        }
        return detail, PansouModule._passes_pansou_quality(torrent, detail)

    @classmethod
    def _passes_pansou_quality(cls, torrent: TorrentInfo, detail: Dict[str, Any]) -> bool:
        """
        在通用质量门之前先做 PanSou 来源专属的内容一致性检查。

        PanSou 的标题来自频道文案，与实际分享内容可能完全无关：例如标题写
        「斗破苍穹 年番 4K 更新218集」，分享里却是另一部短剧的 40 个编号文件。
        这里用标题声明的总集数与分享实际集号做交叉校验，明显对不上的直接丢弃。

        :param torrent: 待检查的网盘候选
        :param detail: 分享详情，含 `_files` 文件树
        :return: 通过返回 True
        """
        files = detail.get("_files")
        if isinstance(files, list) and cls._claim_conflicts(torrent.title or "", files):
            return False
        return candidates.passes_quality(torrent, detail)

    @staticmethod
    def _claim_conflicts(title: str, files: List[Dict[str, Any]]) -> bool:
        """
        判断标题声明的总集数是否与实际分享的集号明显矛盾。

        只对声明集数不小于 `_CLAIM_MIN_EPISODES` 的标题生效，且要求实际最大集号
        低于声明集数的一半才判定矛盾，避免把「只放最近若干集」的正常分享误伤。

        :param title: 候选标题（PanSou 的频道文案）
        :param files: 分享文件树中的文件条目
        :return: 判定为内容与标题不符时返回 True
        """
        claims = [int(value) for value in _CLAIM_EPISODE_PATTERN.findall(str(title or ""))]
        if not claims:
            return False
        claim = max(claims)
        if claim < _CLAIM_MIN_EPISODES:
            return False
        numbers: set[int] = set()
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            numbers.update(candidates.episode_numbers(str(entry.get("name") or "")))
        if not numbers:
            return False
        if max(numbers) >= claim * _CLAIM_TOLERANCE_RATIO:
            return False
        logger.info(
            f"{title} 标题声明 {claim} 集，分享实际最大集号 {max(numbers)}，内容与标题不符，已丢弃"
        )
        return True

    def _inspect_share(
        self,
        torrent: TorrentInfo,
        cached_detail: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        检查单个候选：统一走本机取链，可复用缓存并执行质量检查。

        在线程池中执行，避免逐个候选串行请求。

        :param torrent: 待检查的网盘候选
        :param cached_detail: 缓存中已有的分享详情，含 `_files` 时跳过网络请求
        :return: (分享详情, 是否通过质量检查)；无法判定时详情为 None
        """
        platform = str(torrent.netdisk_platform or "").strip()
        return self._inspect_remote_share(torrent, platform, cached_detail)

    @staticmethod
    def _share_identity(torrent: TorrentInfo) -> str:
        """
        提取候选的缓存键片段，优先使用统一解析出的平台分享码。

        :param torrent: 网盘候选
        :return: 缓存键片段
        """
        url = str(torrent.enclosure or "")
        share_ref = parse_share_link(url, torrent.share_password)
        if share_ref is not None and share_ref.share_id:
            return f"{share_ref.platform.value}:{share_ref.share_id}"
        # 没有可解析分享码时保留完整地址，避免同一域名下不同分享共用缓存键。
        return url.rstrip("/") or url

    def _filter_alive_shares(
        self,
        site: Dict[str, Any],
        torrents: List[TorrentInfo],
    ) -> List[TorrentInfo]:
        """
        保留有效或无法判定的分享，丢弃明确失效的分享，并按发布时间优先检查最新的若干条。

        无法判定（鉴权、超时、网络异常）的候选保留并在描述中标注未验证。

        :param site: PS 站点配置
        :param torrents: 去重后的网盘候选
        :return: 有效分享候选，保持原顺序
        """
        if not torrents:
            return []
        ordered = sorted(
            torrents,
            key=lambda torrent: torrent.pubdate or "",
            reverse=True,
        )[:_MAX_ALIVE_CHECKS]
        alive_flags: Dict[int, bool] = {}
        pending: List[Tuple[TorrentInfo, str, Optional[Dict[str, Any]]]] = []
        for torrent in ordered:
            cache_key = self._alive_cache_key(site, self._share_identity(torrent))
            cached = self._cached_alive(cache_key)
            if cached is not None and not cached[0]:
                alive_flags[id(torrent)] = False
                continue
            pending.append((torrent, cache_key, cached[1] if cached else None))
        if pending:
            executor = ThreadPoolExecutor(max_workers=_ALIVE_WORKERS)
            try:
                futures = {
                    executor.submit(self._inspect_share, torrent, cached_detail): (torrent, cache_key)
                    for torrent, cache_key, cached_detail in pending
                }
                for future in as_completed(futures, timeout=_ALIVE_BUDGET_SECONDS):
                    torrent, cache_key = futures[future]
                    try:
                        detail, passed = future.result()
                    except Exception:  # noqa: BLE001 - 无法判定按未验证保留
                        detail, passed = {"alive": True, "unverified": True}, True
                    if detail is None:
                        detail, passed = {"alive": True, "unverified": True}, True
                    if not detail.get("alive"):
                        self._store_alive(cache_key, False)
                        alive_flags[id(torrent)] = False
                        continue
                    if not passed:
                        alive_flags[id(torrent)] = False
                        continue
                    if detail.get("unverified"):
                        self._mark_unverified(torrent)
                    else:
                        self._store_alive(cache_key, True, detail)
                    alive_flags[id(torrent)] = True
            except TimeoutError:
                logger.debug("PS 站点分享有效性检查超时，未完成的候选按未验证保留")
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        for torrent in ordered:
            if id(torrent) in alive_flags:
                continue
            alive_flags[id(torrent)] = True
            self._mark_unverified(torrent)
        return [torrent for torrent in ordered if alive_flags.get(id(torrent))]

    def search_netdisk_torrents(
        self,
        site: Dict[str, Any],
        keyword: str,
        mtype: Optional[Any] = None,
        page: int = 0,
    ) -> Optional[List[TorrentInfo]]:
        """
        搜索一个 PS 站点的网盘分享，并把每条链接展开为候选。

        :param site: PS 站点配置；非 ps 站点返回 None 交由其他模块处理
        :param keyword: 搜索关键词
        :param mtype: 媒体类型，PanSou 搜索不使用
        :param page: 页码，PanSou 搜索不使用
        :return: 网盘候选列表；非 ps 站点返回 None，失败时返回空列表
        """
        if not isinstance(site, dict) or site.get("kind") != "ps":
            return None
        if not self._as_text(keyword):
            report_site_search_outcome(attempted=True, outcome="success")
            report_site_search_page(raw_count=0, has_more=False)
            return []
        if not self._site_address(site):
            self._warn_unavailable(site, "搜索网盘资源")
            report_site_search_outcome(
                attempted=True,
                outcome="error",
                error="PS 站点缺少 PanSou 地址",
            )
            return []
        try:
            entries = self._get_client(site).search(keyword)
        except PansouError as err:
            site_name = self._as_text(site.get("name") or site.get("id")) or "未知站点"
            logger.warn(f"PS 站点 {site_name} 网盘搜索失败：{err}")
            report_site_search_outcome(attempted=True, outcome="error", error=str(err))
            return []
        except Exception as err:  # noqa: BLE001 - 未预期异常同样按站点失败处理
            site_name = self._as_text(site.get("name") or site.get("id")) or "未知站点"
            logger.warn(f"PS 站点 {site_name} 网盘搜索失败：{err}")
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
        异步搜索一个 PS 站点的网盘分享，阻塞请求放到线程池执行。
        """
        return await asyncio.to_thread(self.search_netdisk_torrents, site, keyword, mtype, page)

    def test_netdisk_site(self, site: Dict[str, Any]) -> Optional[Tuple[bool, str]]:
        """
        调用 /api/health 验证 PS 站点地址与 token 是否可用。

        :param site: PS 站点配置；非 ps 站点返回 None
        :return: 是否可用及失败原因
        """
        if not isinstance(site, dict) or site.get("kind") != "ps":
            return None
        if not self._site_address(site):
            return False, "PS 站点缺少 PanSou 地址"
        try:
            payload = self._get_client(site).health()
        except PansouError as err:
            return False, str(err)
        except Exception as err:  # noqa: BLE001 - 未预期异常统一按连接失败返回
            return False, f"PanSou 连接失败：{err}"
        status = self._as_text(payload.get("status")).lower()
        if status and status != "ok":
            return False, f"PanSou 状态异常：{status}"
        return True, ""
