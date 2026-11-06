"""
CloudSaver 网盘搜索模块，按 CD 站点凭据提供网盘候选、连接测试与 token 刷新。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.application.configuration import get_configured_system_config
from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.domain import episode as episode_rules
from app.domain.context import TorrentInfo
from app.domain.netdisk import parse_share_link
from app.modules import _ModuleBase
from app.modules.cloudsaver.cloudsaver import CloudSaverClient, CloudSaverError, parse_share_code
from app.runtime.log import logger
from app.schemas.types import ModuleType, OtherModulesType, SystemConfigKey

_STANDARD_EPISODE = re.compile(r"\bS\d{1,2}\s*E\d{1,4}(?:\s*-\s*E\d{1,4})?\b", re.IGNORECASE)
# 只把视频文件计入集数清晰度与短剧判定。
_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb",
})
# 能解析出集数的视频文件占比下限。
_EPISODE_COVERAGE_THRESHOLD = 0.8
# 短剧判定阈值的默认值，可在系统设置中修改。
_DEFAULT_SHORT_DRAMA_SECONDS = 300
_DEFAULT_SHORT_DRAMA_MB = 150

# 网盘候选只保留已注册取链适配器的平台；未实现平台不进入候选。
# 分享有效性检查的并发数、总预算与单次搜索最多检查的候选数。
_ALIVE_WORKERS = 16
_ALIVE_BUDGET_SECONDS = 30
_MAX_ALIVE_CHECKS = 200
# 有效性结果缓存，避免同一分享在多次搜索中重复检查。
_ALIVE_CACHE_TTL = 1800
_ALIVE_CACHE_LIMIT = 2000
_alive_cache: Dict[str, Tuple[float, bool, Optional[Dict[str, Any]]]] = {}
_alive_lock = threading.Lock()


def _platform_supported(platform: str) -> bool:
    """
    判断平台是否已注册取链适配器。

    :param platform: 平台标识
    :return: 已实现返回 True
    """
    from app.application.netdisk.registry import find_adapter, registered_platforms
    from app.domain.netdisk import normalize_platform

    if find_adapter(platform) is not None:
        return True
    if registered_platforms():
        return False
    return normalize_platform(platform) is not None


# 集数标记只统计视频文件，避免字幕/图片把候选标题改错。
_VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb", ".webm", ".mpg", ".mpeg",
}
# 音乐文件参与媒体集数识别；压缩包不参与。
_AUDIO_SUFFIXES = {
    ".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg", ".ape", ".wma", ".opus", ".dsf", ".dff",
}
_MEDIA_SUFFIXES = _VIDEO_SUFFIXES | _AUDIO_SUFFIXES
# 压缩包后缀：只用于判断资源是否主要依赖压缩包，不参与集数映射。
_ARCHIVE_SUFFIXES = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
}
# 媒体文件覆盖压缩包集数的下限；低于该比例视为压缩包主导资源。
_MEDIA_COVERAGE_THRESHOLD = 0.8


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
        seen: set[str] = set()
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
                if not _platform_supported(platform):
                    continue
                # 同一分享可能以不同链接形式重复出现，按分享码去重，无法解析时退回链接本身。
                share_code = parse_share_code(url)
                dedup_key = share_code or url.split("?", 1)[0].rstrip("/")
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                # 平台由 netdisk_platform 单独承载，卡片用网盘类型徽标展示；标签只保留渠道名。
                labels = [channel] if channel else []
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

    def _cached_alive(self, key: str) -> Optional[Tuple[bool, Optional[Dict[str, Any]]]]:
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
    def _episode_numbers(file_name: str) -> List[int]:
        """
        从一个文件名中提取集数，规则由 app.domain.episode 统一维护。

        :param file_name: 分享内的文件名或文件夹名
        :return: 该文件名包含的集数列表；没有可识别集数时为空
        """
        return episode_rules.episode_numbers(file_name)

    @staticmethod
    def _is_plausible_episode(value: int) -> bool:
        """
        判断一个数字是否可能是集数，排除分辨率与年份。

        :param value: 待判断的数字
        :return: 可能是集数返回 True
        """
        return episode_rules.is_plausible_episode(value)

    @staticmethod
    def _bare_episode(file_name: str) -> Optional[int]:
        """
        从文件名中提取裸数字集数，只在能明确判定时返回。

        :param file_name: 分享内的文件名
        :return: 集数；无法明确判定时为空
        """
        return episode_rules.bare_episode(file_name)

    @staticmethod
    def _episode_token(file_names: List[str]) -> Optional[str]:
        """
        从分享文件名推导季集标记，使用最小与最大集数构成连续区间。

        :param file_names: 分享内的真实文件名列表
        :return: 形如 S01E12 或 S01E153-E162 的标记；无法推导时为空
        """
        return episode_rules.episode_token(file_names)

    @staticmethod
    def _video_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        只保留视频文件，忽略图片、字幕与压缩包。

        :param files: 分享文件树中的文件条目
        :return: 视频文件条目列表
        """
        videos: List[Dict[str, Any]] = []
        for entry in files or []:
            name = str(entry.get("name") or "")
            suffix = name[name.rfind("."):].lower() if "." in name else ""
            if suffix in _VIDEO_EXTENSIONS:
                videos.append(entry)
        return videos

    @staticmethod
    def _media_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        只保留视频与音乐文件，压缩包不参与集数识别。

        :param files: 分享文件树中的文件条目
        :return: 媒体文件条目列表
        """
        return [
            entry for entry in files or []
            if Path(str(entry.get("name") or "")).suffix.lower() in _MEDIA_SUFFIXES
        ]

    @staticmethod
    def _archive_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        只保留压缩包文件，用于判断资源是否主要依赖压缩包。

        :param files: 分享文件树中的文件条目
        :return: 压缩包文件条目列表
        """
        return [
            entry for entry in files or []
            if Path(str(entry.get("name") or "")).suffix.lower() in _ARCHIVE_SUFFIXES
        ]

    @staticmethod
    def _is_episode_clear(videos: List[Dict[str, Any]]) -> bool:
        """
        判断文件树的集数是否清晰：能解析出集数的视频占比达到下限。

        :param videos: 视频文件条目
        :return: 集数清晰返回 True
        """
        if not videos:
            return False
        parsed = [entry for entry in videos if CloudSaverModule._episode_numbers(entry.get("name") or "")]
        return len(parsed) / len(videos) >= _EPISODE_COVERAGE_THRESHOLD

    @staticmethod
    def _quality_limits() -> Tuple[int, int]:
        """
        读取短剧判定的两个阈值，未配置或非法时使用默认值。

        :return: 平均每集时长上限（秒）与平均每集大小上限（MB）
        """
        duration_limit = _DEFAULT_SHORT_DRAMA_SECONDS
        size_limit = _DEFAULT_SHORT_DRAMA_MB
        try:
            config = get_configured_system_config()
            duration = config.get(SystemConfigKey.NetdiskShortDramaMaxDuration)
            size = config.get(SystemConfigKey.NetdiskShortDramaMaxSize)
            if isinstance(duration, (int, float)) and duration > 0:
                duration_limit = int(duration)
            if isinstance(size, (int, float)) and size > 0:
                size_limit = int(size)
        except Exception:  # noqa: BLE001
            pass
        return duration_limit, size_limit

    @staticmethod
    def _is_short_drama(videos: List[Dict[str, Any]], duration_limit: int, size_limit: int) -> bool:
        """
        判断是否为短剧：平均每集时长与平均每集大小同时低于阈值。

        :param videos: 视频文件条目
        :param duration_limit: 平均每集时长上限（秒）
        :param size_limit: 平均每集大小上限（MB）
        :return: 判定为短剧返回 True
        """
        durations = [int(entry.get("duration") or 0) for entry in videos if int(entry.get("duration") or 0) > 0]
        sizes = [int(entry.get("size") or 0) for entry in videos if int(entry.get("size") or 0) > 0]
        if not durations or not sizes:
            return False
        average_duration = sum(durations) / len(durations)
        average_size = sum(sizes) / len(sizes) / 1024 / 1024
        return average_duration < duration_limit and average_size < size_limit

    @staticmethod
    def _apply_share_detail(torrent: TorrentInfo, detail: Dict[str, Any], files: List[Dict[str, Any]]) -> None:
        """
        用分享详情补齐候选大小，并用文件树的真实集数覆盖标题推断值。

        标题里的季集标记可能只是标题推断（如「更新至164集」转成的 S01E164），
        文件树推导成功时先剥离再用真实区间重写；推导失败时保留标题原值。

        :param torrent: 待补齐的网盘候选
        :param detail: share_detail 返回的分享详情
        :param files: 分享文件树中的文件条目
        """
        file_size = int(detail.get("file_size") or 0)
        if file_size > 0:
            torrent.size = float(file_size)
        token_value = str(detail.get("stoken") or "").strip()
        if token_value:
            torrent.netdisk_share_token = token_value
        if detail.get("unverified"):
            marker = "（未验证）"
            description = str(torrent.description or "")
            if marker not in description:
                torrent.description = f"{description}{marker}" if description else marker
        media_names = [
            str(entry.get("name") or "")
            for entry in files or []
            if Path(str(entry.get("name") or "")).suffix.lower() in _MEDIA_SUFFIXES
        ]
        token = CloudSaverModule._episode_token(media_names)
        if not token:
            return
        title = _STANDARD_EPISODE.sub(" ", torrent.title or "")
        torrent.title = " ".join(f"{title} {token}".split())

    def _passes_quality(
        self,
        torrent: TorrentInfo,
        detail: Dict[str, Any],
    ) -> bool:
        """
        判断候选是否通过质量检查：文件树完整、媒体集数清晰、不是短剧、压缩包不主导。

        集数识别只使用视频与音乐文件；压缩包名称不参与集数映射。压缩包覆盖的集数
        占主导、媒体文件覆盖不足时，候选应丢弃，避免误报「符合条件」。

        检查通过时把真实大小与集数写回候选；不通过时候选应被丢弃。

        :param torrent: 待检查的网盘候选
        :param detail: 分享详情，含 `_files` 文件树
        :return: 通过返回 True
        """
        files = detail.get("_files")
        if not isinstance(files, list):
            # 拿不到完整文件树：丢弃，避免展示错误集数。
            return False
        videos = self._video_files(files)
        media_files = self._media_files(files)
        clarity_files = videos or media_files
        if not self._is_episode_clear(clarity_files):
            # 集数不清晰：丢弃，避免展示错误集数。
            return False
        media_episodes: Set[int] = set()
        for entry in media_files:
            media_episodes.update(self._episode_numbers(entry.get("name") or ""))
        archive_episodes: Set[int] = set()
        for entry in self._archive_files(files):
            archive_episodes.update(self._episode_numbers(entry.get("name") or ""))
        if archive_episodes:
            union = media_episodes | archive_episodes
            coverage = len(media_episodes) / len(union) if union else 1.0
            if coverage < _MEDIA_COVERAGE_THRESHOLD:
                logger.info(
                    f"{torrent.title} 压缩包资源暂不支持，已丢弃"
                    f"（媒体覆盖 {len(media_episodes)}/{len(union)} 集）"
                )
                return False
        duration_limit, size_limit = self._quality_limits()
        if self._is_short_drama(videos, duration_limit, size_limit):
            # 短剧判定依赖可调阈值，结论不写入缓存，阈值调整后下次搜索即可生效。
            return False
        self._apply_share_detail(torrent, detail, files)
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
    def _mark_unverified(torrent: TorrentInfo) -> None:
        """
        在候选描述中标注未验证状态，避免与已确认有效的分享混淆。

        :param torrent: 网盘候选
        """
        marker = "（未验证）"
        description = str(torrent.description or "")
        if marker not in description:
            torrent.description = f"{description}{marker}" if description else marker

    def _inspect_remote_share(
        self,
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
            return cached_detail, self._passes_quality(torrent, cached_detail)
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
        return detail, self._passes_quality(torrent, detail)

    @staticmethod
    def _share_identity(torrent: TorrentInfo) -> Tuple[str, str]:
        """
        提取候选的分享标识与缓存键片段。

        优先用统一解析规则读取平台分享码，覆盖 139、115 等非夸克链接；
        无法解析时退回夸克分享码或去掉查询串的链接本身，保证候选仍参与有效性检查。

        :param torrent: 网盘候选
        :return: 分享标识与缓存键片段
        """
        url = str(torrent.enclosure or "")
        ref = parse_share_link(url, torrent.share_password)
        if ref is not None and ref.share_id:
            return ref.share_id, f"{ref.platform.value}:{ref.share_id}"
        share_code = parse_share_code(url)
        if share_code:
            return share_code, share_code
        trimmed = url.split("?", 1)[0].rstrip("/")
        return url, trimmed or url

    def _filter_alive_shares(
        self,
        site: Dict[str, Any],
        torrents: List[TorrentInfo],
    ) -> List[TorrentInfo]:
        """
        保留有效或无法判定的分享，丢弃明确失效的分享，并按发布时间优先检查最新的若干条。

        无法判定（鉴权、超时、网络异常）的候选保留并在描述中标注未验证。

        :param site: CD 站点配置
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
            _, identity = self._share_identity(torrent)
            cache_key = self._alive_cache_key(site, identity)
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
                logger.debug("CD 站点分享有效性检查超时，未完成的候选按未验证保留")
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
