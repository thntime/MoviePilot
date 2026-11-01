"""
通用网盘下载器模块。

只处理 ``download_type == "netdisk"`` 的候选：按平台归一化分享链接，通过
``NetdiskResolveService`` 递归列出文件、过滤集数并逐文件取链，再交给平台无关的
分片引擎或 HLS 下载器。其他内容一律返回 None，交给其他下载器处理。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from app.application.netdisk.registry import find_adapter
from app.application.netdisk.resolve import NetdiskResolveService
from app.domain.episode import episode_numbers, is_special_name
from app.domain.netdisk import (
    DirectLink,
    NetdiskPlatform,
    NetdiskTuning,
    ShareFile,
    normalize_platform,
    parse_share_link,
)
from app.modules._base.downloader import _DownloaderModuleBase
from app.modules.netdisk import store
from app.modules.netdisk.engine import (
    HlsDownloader,
    ChunkedDownloader,
    DownloadCancelled,
    DownloadError,
)
from app.runtime.log import logger
from app.schemas.dashboard import DownloaderInfo
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.transfer import DownloaderFile, DownloaderTorrent
from app.schemas.types import (
    DownloaderType,
    ModuleType,
    TorrentQueryStatus,
    TorrentStatus,
)

# 只下载视频与常见归档，忽略图片、字幕与说明文件。
_DOWNLOAD_SUFFIXES = frozenset({
    ".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb", ".zip", ".7z", ".rar",
})
# 分享目录最大递归深度。
# 平台硬上限：迅雷超过 8 路并发会被 CDN 降级为整文件响应。
_PLATFORM_CONCURRENCY_CAP = {NetdiskPlatform.XUNLEI: 8}
_MAX_WALK_DEPTH = 3


def _as_text(value: Any) -> str:
    """
    将字段转换为去除首尾空白的文本。
    """
    if value is None:
        return ""
    return str(value).strip()


def _share_code(url: str) -> str:
    """
    从任意受支持的分享链接中解析分享码。

    :param url: 分享链接
    :return: 分享码；无法解析时为空
    """
    ref = parse_share_link(_as_text(url))
    return ref.share_id if ref else ""


def _task_hash(share_code: str, name: str) -> str:
    """
    生成稳定的任务标识。

    :param share_code: 分享码
    :param name: 文件名或链接
    :return: 任务标识
    """
    return hashlib.sha1(f"{share_code}:{name}".encode("utf-8")).hexdigest()[:16]


class NetdiskModule(_DownloaderModuleBase[Any]):
    """
    通用网盘下载器：多平台取链、并发分片或 HLS 下载，并接入整理流程。
    """

    def __init__(self) -> None:
        """
        初始化模块状态与后台任务登记表。
        """
        super().__init__()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._stops: Dict[str, threading.Event] = {}
        # 已被用户删除的任务，后台线程收尾时不得再把记录写回去。
        self._discarded: Set[str] = set()
        self._lock = threading.RLock()

    def init_module(self) -> None:
        """
        按下载器配置创建本类型的实例，并校准重启后遗留的运行中状态。
        """
        super().init_service(service_name="netdisk")
        self._reconcile_store_state()

    def _reconcile_store_state(self) -> None:
        """
        进程重启后内存里的任务表是空的，store 里的 downloading 记录必须回落为 paused，
        否则前端会显示一个永远不动、又暂停不了的任务。
        """
        for record in store.all_tasks():
            if _as_text(record.get("state")) != "downloading":
                continue
            task_hash = _as_text(record.get("hash"))
            if task_hash and task_hash in self._tasks:
                continue
            if task_hash:
                store.upsert_task(task_hash, {
                    "state": "paused", "dlspeed": None, "dlspeed_bytes": 0,
                })
                logger.info(f"网盘任务已回落为暂停：{task_hash}")

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查是否配置了可用的网盘下载器实例。

        :return: 整体成功；没有实例时返回 None
        """
        configs = self.get_configs()
        if not configs:
            return None
        return True, ""

    def init_setting(self) -> None:
        """
        由下载器实例配置驱动，不声明独立的模块开关。
        """

    def stop(self) -> None:
        """
        停止所有在跑的下载任务。
        """
        with self._lock:
            for event in self._stops.values():
                event.set()
            self._stops.clear()

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "Netdisk"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        """
        获取模块子类型。
        """
        return DownloaderType.Netdisk

    @staticmethod
    def get_priority() -> int:
        """
        优先级必须小于 qbittorrent 的 1，避免网盘内容被其他下载器的失败结果短路。
        """
        return 0

    # ---- 下载 ----

    def download(self, content: Union[Path, str, bytes], download_dir: Path, cookie: str,
                 episodes: Optional[Set[int]] = None, category: Optional[str] = None,
                 label: Optional[str] = None, downloader: Optional[str] = None,
                 custom_words: Optional[str] = None, download_type: Optional[str] = None,
                 netdisk_platform: Optional[str] = None,
                 share_password: Optional[str] = None,
                 netdisk_share_token: Optional[str] = None
                 ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        提交网盘下载任务，立即返回并在后台执行。

        :param content: 分享链接
        :param download_dir: 下载目录
        :param cookie: 站点 Cookie，网盘下载不使用
        :param episodes: 目标集数集合，网盘整包下载时忽略
        :param category: 下载分类子目录
        :param label: 任务标签
        :param downloader: 下载器实例名称
        :param custom_words: 识别词，网盘下载不使用
        :param download_type: 资源类型；只有 netdisk 由本模块处理
        :param netdisk_platform: 网盘平台标识
        :param share_password: 分享提取码
        :param netdisk_share_token: 候选透传的分享令牌，当前由适配器重新解析
        :return: 下载器名称、任务标识、内容布局与说明；非网盘内容返回 None
        """
        if _as_text(download_type) != "netdisk":
            return None
        url = _as_text(content)
        passcode = _as_text(share_password)
        ref = parse_share_link(url, passcode)
        if ref is None:
            return None, None, None, "无法从分享链接中解析分享码"
        platform = normalize_platform(netdisk_platform) if _as_text(netdisk_platform) else ref.platform
        if platform is None:
            return None, None, None, f"暂不支持 {_as_text(netdisk_platform)} 网盘下载"
        if platform != ref.platform:
            return None, None, None, "分享链接与候选平台不一致"
        if netdisk_share_token:
            # 令牌只在候选透传时存在，取链仍以适配器解析结果为准。
            logger.debug(f"收到候选分享令牌：{ref.share_id}")
        code = ref.share_id
        config = self._select_config(downloader)
        if config is None:
            return None, None, None, "未配置网盘下载器实例"
        name = _as_text(getattr(config, "name", "")) or "Netdisk"
        task_tuning = self._instance_tuning(config, platform)
        target_episodes = set(episodes or ())
        task_key = _task_hash(code, url)
        with self._lock:
            if task_key in self._tasks:
                return name, task_key, None, DOWNLOAD_TASK_ALREADY_EXISTS
            # 同一分享的不同链接形式（带 ?pwd= / # / 长短链）不应变成第二个任务：
            # 复用活跃任务的 hash，链路的下载记录才能继续关联。
            existing = store.find_by_share_code(code)
            if existing and _as_text(existing.get("state")) in {"downloading", "paused"}:
                covered = set(existing.get("episodes") or [])
                if not target_episodes or target_episodes <= covered:
                    return (
                        name,
                        _as_text(existing.get("hash")),
                        None,
                        DOWNLOAD_TASK_ALREADY_EXISTS,
                    )
            stop_event = threading.Event()
            self._stops[task_key] = stop_event
            self._tasks[task_key] = {"stop": stop_event}
            self._discarded.discard(task_key)
        # 任务目录与记录必须在返回前同步落盘：链路提交后会立刻回读 save_path 写下载历史，
        # 放到后台线程里会出现竞态，导致 history.path 退化成下载根目录、手动整理串任务。
        task_dir = self._task_dir(Path(download_dir), code)
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"创建网盘下载目录失败：{task_dir} - {err}")
        store.upsert_task(task_key, {
            "title": f"{platform.value}:{code}",
            "name": code,
            "downloader": name,
            "state": "downloading",
            "progress": 0.0,
            "size": 0.0,
            "save_path": str(task_dir),
            "path": str(task_dir),
            "category": label,
            "files": [],
            "share_code": code,
            "share_url": url,
            "netdisk_platform": platform.value,
            "passcode": passcode,
            "share_token": _as_text(netdisk_share_token),
            "episodes": sorted(target_episodes),
        })
        thread = threading.Thread(
            target=self._run_task,
            args=(task_key, platform, url, code, passcode, task_dir, label,
                  target_episodes or None, _as_text(netdisk_share_token),
                  task_tuning),
            daemon=True,
            name=f"netdisk-download-{task_key}",
        )
        thread.start()
        return name, task_key, None, ""

    def _select_config(self, downloader: Optional[str]) -> Optional[Any]:
        """
        选择网盘下载器配置；显式名称不存在时回退到默认配置。

        :param downloader: 下载器实例名称
        :return: 下载器配置；没有配置时返回 None
        """
        configs = self.get_configs()
        if not configs:
            return None
        if downloader and downloader in configs:
            return configs[downloader]
        marked = next(
            (conf for conf in configs.values() if getattr(conf, "default", False)),
            None,
        )
        return marked or next(iter(configs.values()))

    @staticmethod
    def _instance_tuning(config: Any, platform: NetdiskPlatform) -> NetdiskTuning:
        """
        读取下载器实例覆盖项，并把线程数夹在平台硬上限内。

        :param config: 下载器实例配置
        :param platform: 网盘平台
        :return: 合并后的参数快照
        """
        base = NetdiskResolveService().tuning
        raw = getattr(config, "config", {}) or {}
        adapter = find_adapter(platform)
        cap = max(1, min(512, int(getattr(adapter, "max_concurrency", 512) or 512)))
        cap = min(cap, _PLATFORM_CONCURRENCY_CAP.get(platform, cap))

        def int_value(key: str, default: int, minimum: int, maximum: int) -> int:
            """
            读取并夹紧整型配置。
            """
            try:
                number = int(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(minimum, min(maximum, number))

        def bool_value(key: str, default: bool) -> bool:
            """
            读取布尔配置。
            """
            value = raw.get(key)
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        # 实例配置沿用历史单位 KB/s，引擎与系统设置使用字节/秒。
        if "rate_limit" in raw:
            rate_limit = int_value("rate_limit", 0, 0, 1 << 30) * 1024
        else:
            rate_limit = base.rate_limit
        return replace(
            base,
            threads=int_value("threads", base.threads, 1, cap),
            rate_limit=rate_limit,
            preempt_enabled=bool_value("preempt", base.preempt_enabled),
            multi_source=bool_value("multi_source", base.multi_source),
        )

    def _run_task(self, task_key: str, platform: NetdiskPlatform, share_url: str,
                  share_code: str, passcode: str, download_dir: Path,
                  label: Optional[str],
                  episodes: Optional[Set[int]] = None,
                  share_token: str = "",
                  tuning: Optional[NetdiskTuning] = None) -> None:
        """
        后台执行分享解析、取链与下载。

        :param task_key: 任务标识
        :param platform: 网盘平台
        :param share_url: 分享链接
        :param share_code: 分享码
        :param passcode: 提取码
        :param download_dir: 下载目录
        :param label: 任务标签
        :param episodes: 目标集数集合，为空表示整包下载
        :param share_token: 候选透传的分享令牌
        :param tuning: 下载器实例覆盖后的参数快照
        """
        task_dir = download_dir
        stop_event = self._stops.get(task_key) or threading.Event()
        resolver = NetdiskResolveService()
        try:
            resolved = resolver.walk(
                share_url, passcode, platform, max_depth=_MAX_WALK_DEPTH,
                share_token=share_token,
            )
            files = self._collect_files(resolved.files, episodes)
            if resolved.detail.title:
                store.upsert_task(task_key, {"title": resolved.detail.title})
            if not files:
                if episodes:
                    raise DownloadError(
                        f"分享内没有目标集数（{sorted(episodes)}）对应的文件"
                    )
                raise DownloadError("分享内没有可下载的视频文件")
            total_size = sum(int(item.size or 0) for item in files)
            store.upsert_task(task_key, {
                "size": float(total_size),
                "files": [
                    {"name": item.name, "size": int(item.size or 0), "progress": 0.0}
                    for item in files
                ],
            })
            done_before = 0.0
            for item in files:
                if stop_event.is_set():
                    raise DownloadError("下载已取消")
                self._download_one(
                    resolver, platform, share_url, passcode, item, task_dir,
                    task_key, stop_event, total_size, done_before,
                    share_token, tuning,
                )
                done_before += float(item.size or 0)
            if task_key not in self._discarded:
                store.upsert_task(task_key, {"state": "completed", "progress": 100.0})
            logger.info(f"网盘下载完成：{platform.value}:{share_code}")
        except DownloadCancelled:
            logger.info(f"网盘下载已暂停：{share_code}")
            if task_key not in self._discarded:
                store.upsert_task(task_key, {"state": "paused"})
        except Exception as err:  # noqa: BLE001
            # 模块热重载后异常类对象可能不是同一个，按消息兜底识别用户暂停。
            if isinstance(err, DownloadCancelled) or "下载已取消" in str(err):
                logger.info(f"网盘下载已暂停：{share_code}")
                if task_key not in self._discarded:
                    store.upsert_task(task_key, {"state": "paused"})
            else:
                logger.warn(f"网盘下载失败：{share_code} - {err}")
                if task_key not in self._discarded:
                    store.upsert_task(task_key, {"state": "error", "error": str(err)})
        finally:
            with self._lock:
                self._stops.pop(task_key, None)
                self._tasks.pop(task_key, None)

    @staticmethod
    def _task_dir(download_dir: Path, share_code: str) -> Path:
        """
        为每个任务分配独立子目录，避免不同分享的同名文件互相覆盖。

        :param download_dir: 配置的下载目录
        :param share_code: 分享码，同时作为子目录名
        :return: 该任务的落盘目录
        """
        if download_dir.name == share_code:
            return download_dir
        return download_dir / share_code

    def _collect_files(self, files: Sequence[ShareFile],
                       episodes: Optional[Set[int]] = None) -> List[ShareFile]:
        """
        过滤出待下载文件，并给不含季集标记的文件名补前缀。

        :param files: 分享内递归展开后的全部文件
        :param episodes: 目标集数集合；为空表示整包下载
        :return: 过滤后的文件条目
        """
        wanted = [item for item in files if not item.is_dir and self._wanted(item.name)]
        wanted = self._filter_episodes(wanted, episodes)
        return [replace(item, name=self._episode_name(item.name)) for item in wanted]

    @staticmethod
    def _filter_episodes(files: Sequence[ShareFile],
                         episodes: Optional[Set[int]]) -> List[ShareFile]:
        """
        只保留目标集数对应的文件。

        必须在补 `S01Exx` 前缀之前用原始文件名解析：补前缀会让原本解析不出的文件
        变得可解析，从而破坏「解析不确定就全下」的兜底语义。

        放行规则（任一命中即保留，宁可多下也不漏集）：
        没有目标集数、文件名解析不出唯一集数、集数在目标集合内则保留。

        :param files: 分享内的文件条目
        :param episodes: 目标集数集合
        :return: 过滤后的文件条目
        """
        wanted = set(episodes or ())
        if not wanted:
            return list(files)
        kept: List[ShareFile] = []
        for item in files:
            if is_special_name(item.name):
                continue
            numbers = episode_numbers(item.name)
            if len(numbers) != 1 or numbers[0] in wanted:
                kept.append(item)
        logger.info(
            f"目标集数 {sorted(wanted)}，分享内 {len(files)} 个文件，选中 {len(kept)} 个"
        )
        return kept

    @staticmethod
    def _episode_name(name: str) -> str:
        """
        给不含季集标记的文件名补上 `S01Exx` 前缀，便于整理阶段识别集数。

        分享里的文件名常见 `164 4K.mkv`、`01~4K.mp4`，MetaInfo 无法识别季集，
        整理会以「未识别到文件集数」拒绝。补前缀后 `S01E164 164 4K.mkv` 可正常解析。
        候选集数不唯一、或文件名已带标记时原样返回，绝不猜测。

        :param name: 分享内的原始文件名
        :return: 补好集数前缀的文件名
        """
        numbers = episode_numbers(name)
        if len(numbers) != 1:
            return name
        if re.search(r"[Ss]\d{1,2}\s*[Ee]\d{1,4}", name):
            return name
        return f"S01E{numbers[0]:03d} {name}"

    @staticmethod
    def _wanted(name: str) -> bool:
        """
        判断文件是否需要下载。

        :param name: 文件名
        :return: 视频或归档返回 True
        """
        suffix = name[name.rfind("."):].lower() if "." in name else ""
        return suffix in _DOWNLOAD_SUFFIXES

    def _download_one(self, resolver: NetdiskResolveService, platform: NetdiskPlatform,
                      share_url: str, passcode: str, item: ShareFile,
                      download_dir: Path, task_key: str, stop_event: threading.Event,
                      total_size: int = 0, done_before: float = 0.0,
                      share_token: str = "", tuning: Optional[NetdiskTuning] = None) -> None:
        """
        下载单个文件：按多源开关取链，直链失效时重新取链。

        :param resolver: 取链服务
        :param platform: 网盘平台
        :param share_url: 分享链接
        :param passcode: 提取码
        :param item: 文件条目
        :param download_dir: 下载目录
        :param task_key: 任务标识
        :param stop_event: 停止信号
        :param total_size: 整任务字节数，用于把单文件进度换算成整任务进度
        :param done_before: 本文件之前已完成任务的字节数
        :param share_token: 候选透传的分享令牌
        :param tuning: 下载器实例覆盖后的参数快照
        """
        tuning = tuning or resolver.tuning
        multi_source = bool(tuning.multi_source)
        links = tuple(resolver.resolve_file(
            share_url, passcode, item, platform, multi_source=multi_source,
            share_token=share_token,
        ))
        if not links:
            raise DownloadError(f"无法获取下载直链：{item.name}")
        size = int(item.size or links[0].size or 0)
        is_hls = any(link.is_hls for link in links)
        if not is_hls and size <= 0:
            raise DownloadError(f"无法确定文件大小：{item.name}")
        target = download_dir / item.name
        seen: List[DirectLink] = list(links)

        def url_provider(refresh: bool = False) -> Sequence[DirectLink]:
            """
            返回直链集合；刷新时跳过缓存并重新选择账号取链。

            :param refresh: 是否请求刷新
            :return: 当前直链集合
            """
            nonlocal links
            if not refresh:
                return links
            fresh = tuple(resolver.resolve_file(
                share_url, passcode, item, platform,
                multi_source=multi_source, force_refresh=True,
                share_token=share_token,
            ))
            if fresh:
                links = fresh
                seen.extend(fresh)
            return links

        sample = {"at": time.monotonic(), "done": float(done_before)}

        def on_progress(percent: float) -> None:
            """把单文件进度换算成整任务进度，并记录实时下载速度。"""
            done = done_before + size * max(min(float(percent), 100.0), 0.0) / 100.0
            now = time.monotonic()
            speed: Optional[float] = None
            elapsed = now - float(sample["at"])
            if elapsed >= 1.0:
                speed = max(done - float(sample["done"]), 0.0) / elapsed
                sample["at"], sample["done"] = now, done
            overall = (done / total_size * 100.0) if total_size > 0 else percent
            store.update_progress(task_key, overall, dlspeed=speed)

        adapter = find_adapter(platform)
        max_concurrency = int(getattr(adapter, "max_concurrency", 0) or 0)
        try:
            if is_hls:
                HlsDownloader(
                    target=target,
                    links=links,
                    retries=tuning.retries,
                    buffer_size=tuning.buffer_size,
                    on_progress=on_progress,
                    stop_event=stop_event,
                ).run(url_provider)
            else:
                ChunkedDownloader(
                    target=target,
                    total=size,
                    links=links,
                    threads=tuning.threads,
                    rate_limit_bytes=tuning.rate_limit,
                    preempt=tuning.preempt_enabled,
                    buffer_size=tuning.buffer_size,
                    retries=tuning.retries,
                    preempt_bps=tuning.preempt_bps,
                    preempt_age_seconds=tuning.preempt_age_seconds,
                    refresh_seconds=tuning.refresh_seconds,
                    max_concurrency=max_concurrency,
                    on_progress=on_progress,
                    stop_event=stop_event,
                ).run(url_provider)
        finally:
            self._cleanup_links(resolver, seen)

    @staticmethod
    def _cleanup_links(resolver: NetdiskResolveService, links: Sequence[DirectLink]) -> None:
        """
        清理下载过程中所有转存句柄，同一句柄只清理一次。

        :param resolver: 取链服务
        :param links: 下载过程中使用过的直链
        """
        handles = set()
        for link in links:
            handle = link.cleanup_handle
            if handle is None or handle in handles:
                continue
            handles.add(handle)
            resolver.cleanup(link)

    # ---- 任务抽象 ----

    def list_torrents(self, status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]] = None,
                      hashs: Optional[Union[list[str], str]] = None,
                      downloader: Optional[str] = None,
                      include_all_tags: bool = False) -> Optional[List[DownloaderTorrent]]:
        """
        返回网盘下载任务列表，供整理流程与任务页使用。

        :param status: 查询状态；为空返回全部
        :param hashs: 指定任务标识
        :param downloader: 下载器实例名称
        :param include_all_tags: 是否包含未设置系统标签的任务
        :return: 任务列表
        """
        wanted = {hashs} if isinstance(hashs, str) else set(hashs or [])
        query = self._normalize_query_status(status)
        results: List[DownloaderTorrent] = []
        for record in store.all_tasks():
            if wanted and record.get("hash") not in wanted:
                continue
            if downloader and record.get("downloader") != downloader:
                continue
            progress = float(record.get("progress") or 0.0)
            state = _as_text(record.get("state")) or "downloading"
            if query == TorrentQueryStatus.TRANSFER and state != "completed":
                continue
            if query == TorrentQueryStatus.COMPLETED and state not in {"completed", "transferred"}:
                continue
            results.append(DownloaderTorrent(
                downloader=record.get("downloader"),
                hash=record.get("hash"),
                title=record.get("title"),
                name=record.get("name"),
                path=Path(str(record.get("path"))) if record.get("path") else None,
                size=float(record.get("size") or 0.0),
                progress=progress,
                state=state,
                save_path=record.get("save_path"),
                content_path=record.get("path"),
                category=record.get("category"),
                dlspeed=record.get("dlspeed"),
            ))
        return results

    def torrent_files(self, tid: str, downloader: Optional[str] = None) -> Optional[List[DownloaderFile]]:
        """
        返回任务内的文件清单。

        :param tid: 任务标识
        :param downloader: 下载器实例名称
        :return: 文件列表；任务不存在时为空
        """
        record = store.get_task(tid)
        if not record:
            return None
        files = record.get("files") or []
        return [
            DownloaderFile(
                id=index,
                name=str(item.get("name") or ""),
                size=int(item.get("size") or 0),
                progress=float(item.get("progress") or 0.0),
            )
            for index, item in enumerate(files)
        ]

    def remove_torrents(self, hashs: Union[str, list[str]], delete_file: Optional[bool] = True,
                        downloader: Optional[str] = None) -> Optional[bool]:
        """
        删除任务，并按需删除已下载文件与断点计划。

        :param hashs: 任务标识或列表
        :param delete_file: 是否删除文件
        :param downloader: 下载器实例名称
        :return: 是否处理成功
        """
        targets = [hashs] if isinstance(hashs, str) else list(hashs or [])
        for task_hash in targets:
            record = store.get_task(task_hash)
            if not record:
                continue
            stop_event = self._stops.get(task_hash)
            if stop_event:
                stop_event.set()
            if delete_file:
                for item in record.get("files") or []:
                    path = Path(record.get("path") or "") / str(item.get("name") or "")
                    # 分片计划与文件同名绑定，删除任务时必须一并清掉，否则残留孤儿计划。
                    plan_path = path.with_suffix(path.suffix + ".plan.json")
                    hls_plan = path.with_suffix(path.suffix + ".hls.json")
                    hls_part = path.with_suffix(path.suffix + ".hls.part")
                    for candidate in (path, plan_path, hls_plan, hls_part):
                        try:
                            candidate.unlink(missing_ok=True)
                        except Exception as err:  # noqa: BLE001
                            logger.warn(f"删除网盘下载文件失败：{candidate} - {err}")
            self._discarded.add(task_hash)
            store.remove_task(task_hash)
        return True

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        整理完成后标记任务状态。

        :param hashs: 任务标识
        :param downloader: 下载器实例名称
        """
        store.upsert_task(hashs, {"state": "transferred"})

    def set_torrents_tag(self, hashs: Union[str, list[str]], tags: list[str],
                         downloader: Optional[str] = None) -> Optional[bool]:
        """
        网盘任务不使用标签，直接返回成功。

        :param hashs: 任务标识
        :param tags: 标签列表
        :param downloader: 下载器实例名称
        :return: 固定返回 True
        """
        return True

    def start_torrents(self, hashs: Union[list[str], str],
                       downloader: Optional[str] = None) -> Optional[bool]:
        """
        恢复已停止的网盘下载任务，复用分片计划继续下载。

        :param hashs: 任务标识
        :param downloader: 下载器实例名称
        :return: 是否至少恢复了一个任务；没有可恢复任务时返回 None
        """
        targets = [hashs] if isinstance(hashs, str) else list(hashs or [])
        started = False
        for task_hash in targets:
            record = store.get_task(task_hash)
            if not record:
                continue
            share_code = _as_text(record.get("share_code"))
            share_url = _as_text(record.get("share_url"))
            platform = normalize_platform(record.get("netdisk_platform"))
            download_dir = _as_text(record.get("path"))
            if not share_code or not share_url or platform is None or not download_dir:
                continue
            with self._lock:
                if task_hash in self._tasks:
                    continue
                stop_event = threading.Event()
                self._stops[task_hash] = stop_event
                self._tasks[task_hash] = {"stop": stop_event}
            store.upsert_task(task_hash, {"state": "downloading", "error": None})
            thread = threading.Thread(
                target=self._run_task,
                args=(task_hash, platform, share_url, share_code,
                      _as_text(record.get("passcode")), Path(download_dir),
                      record.get("category"),
                      set(record.get("episodes") or []) or None,
                      _as_text(record.get("share_token")), None),
                daemon=True,
                name=f"netdisk-download-{task_hash}",
            )
            thread.start()
            started = True
        return True if started else None

    def stop_torrents(self, hashs: Union[list[str], str],
                      downloader: Optional[str] = None) -> Optional[bool]:
        """
        停止正在执行的网盘下载任务。

        :param hashs: 任务标识
        :param downloader: 下载器实例名称
        :return: 是否成功发出停止信号
        """
        targets = [hashs] if isinstance(hashs, str) else list(hashs or [])
        stopped = False
        for task_hash in targets:
            event = self._stops.get(task_hash)
            if event:
                event.set()
                stopped = True
                continue
            # 线程已经不在（例如进程重启过），仍要把状态改成暂停，否则前端暂停按钮无效。
            record = store.get_task(task_hash)
            if record and _as_text(record.get("state")) == "downloading":
                store.upsert_task(task_hash, {
                    "state": "paused", "dlspeed": None, "dlspeed_bytes": 0,
                })
                stopped = True
        return stopped

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[DownloaderInfo]]:
        """
        返回下载器概览；网盘下载器没有独立进程，速率由在跑任务累加。

        :param downloader: 下载器实例名称
        :return: 单元素概览列表
        """
        speed = 0.0
        for record in store.all_tasks():
            if downloader and record.get("downloader") != downloader:
                continue
            if _as_text(record.get("state")) != "downloading":
                continue
            speed += float(record.get("dlspeed_bytes") or 0)
        return [DownloaderInfo(download_speed=speed, upload_speed=0.0)]
