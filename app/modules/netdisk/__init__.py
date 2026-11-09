"""
通用网盘下载器模块。

只处理 ``download_type == "netdisk"`` 的候选：按平台归一化分享链接，通过
``NetdiskResolveService`` 递归列出文件、过滤集数并逐文件取链，再交给平台无关的
分片引擎或 HLS 下载器。其他内容一律返回 None，交给其他下载器处理。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union, cast

from app.application.netdisk import selection
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.batch_judge import BatchDecision, judge_batch
from app.application.netdisk.registry import find_adapter
from app.application.netdisk.resolve import NetdiskResolveService
from app.application.netdisk.share_blacklist import get_share_blacklist
from app.domain.episode import episode_numbers, is_special_name
from app.domain.netdisk import (
    DISPLAY_ONLY_PLATFORMS,
    DirectLink,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    NetdiskTuning,
    ShareFile,
    TransferHandle,
    is_share_level_error,
    normalize_platform,
    parse_share_link,
)
from app.modules._base.downloader import _DownloaderModuleBase
from app.modules.netdisk import store
from app.modules.netdisk.engine import (
    ChunkedDownloader,
    DownloadCancelled,
    DownloadError,
    HlsDownloader,
    NotHls,
)
from app.runtime.log import logger
from app.schemas.dashboard import DownloaderInfo
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.transfer import DownloaderFile, DownloaderTorrent
from app.schemas.types import (
    DownloaderType,
    ModuleType,
    SystemConfigKey,
    TorrentQueryStatus,
    TorrentStatus,
)

# 只下载视频与常见归档，忽略图片、字幕与说明文件。
_DOWNLOAD_SUFFIXES = frozenset({
    ".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb",
})
# 分享目录最大递归深度。
# 平台硬上限：迅雷超过 8 路并发会被 CDN 降级为整文件响应。
_PLATFORM_CONCURRENCY_CAP = {NetdiskPlatform.XUNLEI: 8}
# 平台任务级总连接上限：迅雷实测超过 10 路并发会在约 1 分钟内返回 HTTP 503，
# 因此把单个任务的总连接数夹在 8 路（并发文件数 × 每文件连接数）。
_PLATFORM_TOTAL_CONCURRENCY_CAP = {NetdiskPlatform.XUNLEI: 8}
_MAX_WALK_DEPTH = 12
# 任务目录标记文件：只有带该标记的目录才允许被定时孤儿清理删除，避免误删用户目录。
_TASK_MARKER = ".netdisk-task"
# 文件名中的明确季集标记，用于按目标季过滤，避免跨季误下。
_SEASON_EPISODE_PATTERN = re.compile(r"[Ss](\d{1,3})\s*[Ee](\d{1,4})")
_SEASON_PATH_PATTERN = re.compile(
    r"第\s*(\d+)\s*季|[Ss]eason\s*(\d{1,2})|(?<![A-Za-z0-9])[Ss](\d{1,2})(?![0-9])"
)
# 目标季参数可能来自订阅展示文本（S01 / S01-S02）或数字，统一按此解析。
_SEASON_TOKEN_PATTERN = re.compile(r"^[Ss]?(\d{1,3})(?:\s*[-~]\s*[Ss]?(\d{1,3}))?$")
# 全局同时在跑的网盘任务上限，避免批量订阅一次性打满连接与磁盘。
class _TaskSlots:
    """
    按系统设置动态限制全局网盘任务数，避免硬编码与实例配置脱钩。
    """

    def __init__(self) -> None:
        """
        初始化计数器与等待条件。
        """
        self._count = 0
        self._condition = threading.Condition()

    @staticmethod
    def _limit() -> int:
        """
        读取全局并发上限；配置缺失或异常时回落到 3。

        :return: 夹在 1-16 之间的任务数
        """
        try:
            from app.application.configuration import get_configured_system_config

            value = get_configured_system_config().get(
                SystemConfigKey.NetdiskMaxConcurrentDownloads
            )
            return max(1, min(16, int(value or 3)))
        except Exception:  # noqa: BLE001 - 配置不可用时不影响下载
            return 3

    def __enter__(self) -> "_TaskSlots":
        """
        占用一个全局任务槽位。
        """
        while True:
            limit = self._limit()
            with self._condition:
                if self._count < limit:
                    self._count += 1
                    return self
                self._condition.wait(timeout=5.0)

    def __exit__(self, *args: Any) -> None:
        """
        释放任务槽位并唤醒等待者。
        """
        with self._condition:
            self._count = max(0, self._count - 1)
            self._condition.notify()


_TASK_SLOTS = _TaskSlots()
# 进度写入最小间隔与最小百分比变化，避免每个网络分片都全量重写任务索引。
_PROGRESS_INTERVAL_SECONDS = 1.0
_PROGRESS_DELTA_PERCENT = 0.5
# 没有期望时长时只要求视频至少 5 分钟，过滤预告片、花絮与空壳文件。
_MIN_VIDEO_DURATION_SECONDS = 300.0


def _as_text(value: Any) -> str:
    """
    将字段转换为去除首尾空白的文本。
    """
    if value is None:
        return ""
    return str(value).strip()


def _is_completed_file(target: Path, expected: int) -> bool:
    """
    判断目标文件是否真的下载完成。

    只比较大小不够：分片下载会先把文件预分配到目标大小，中断后大小仍然相等。
    因此必须同时满足「没有未完成的计划文件」和「存在完成标记」。

    :param target: 目标文件
    :param expected: 预期字节数
    :return: 确认已完成返回 True
    """
    try:
        if not target.exists():
            return False
        # 大小未知（HLS 等）时只信任完成标记，不做长度比较。
        if expected > 0 and target.stat().st_size != expected:
            return False
    except OSError:
        return False
    if target.with_suffix(target.suffix + ".plan.json").exists():
        return False
    return target.with_name(target.name + ".done").exists()


def _probe_video_duration(target: Path, timeout: float = 30.0) -> Optional[float]:
    """
    用 ffprobe 读取视频容器时长。

    :param target: 视频文件
    :param timeout: 探测超时秒数
    :return: 时长（秒）；探测失败或结果不可解析时返回 None
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(target)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _validate_video_duration(target: Path, expected: Optional[float] = None) -> None:
    """
    校验下载完成的视频时长，剔除空壳、截断或与预期明显不符的文件。

    :param target: 已下载完成的目标文件
    :param expected: 期望时长（秒）；为空时只校验最短时长
    :raises NetdiskError: 时长不达标时删除目标与附属文件后抛出
    """
    duration = _probe_video_duration(target)
    if duration is None:
        logger.warning(f"无法读取视频时长，跳过校验：{target.name}")
        return
    if expected:
        # 同集不同清晰度与片头片尾差异都会影响时长，允许 ±30% 偏差。
        valid = 0.7 * expected <= duration <= 1.3 * expected
    else:
        valid = duration >= _MIN_VIDEO_DURATION_SECONDS
    if valid:
        return
    for path in (target, target.with_name(target.name + ".done"),
                 target.with_suffix(target.suffix + ".plan.json")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, f"时长异常：{duration:.0f}s")


def _cap_platform_workers(platform: NetdiskPlatform, workers: int, threads: int) -> int:
    """
    按平台任务级总连接上限限制并发文件数，保持每文件连接数不变。

    :param platform: 网盘平台
    :param workers: 期望并发文件数
    :param threads: 每文件连接数
    :return: 夹紧后的并发文件数
    """
    total_cap = _PLATFORM_TOTAL_CONCURRENCY_CAP.get(platform)
    if not total_cap:
        return max(1, int(workers))
    per_file = max(1, int(threads))
    return max(1, min(int(workers), total_cap // per_file))


def _limit_workers(requested: int, file_sizes: Sequence[int], free_bytes: int) -> int:
    """
    按账号剩余空间限制并发文件数，避免多个转存同时超量。

    峰值按最大单文件大小乘以 1.2 余量估算；无法判断容量时保持原并发。

    :param requested: 期望的并发文件数
    :param file_sizes: 待下载文件大小列表
    :param free_bytes: 平台内最空闲账号的剩余字节数
    :return: 实际可用的并发文件数
    """
    workers = max(int(requested), 1)
    if workers <= 1 or free_bytes <= 0:
        return workers
    sizes = [int(size) for size in file_sizes if int(size or 0) > 0]
    if not sizes:
        return workers
    peak = max(sizes) * 1.2
    fits = int(free_bytes // peak) if peak > 0 else workers
    return max(1, min(workers, fits))


def _share_code(url: str) -> str:
    """
    从任意受支持的分享链接中解析分享码。

    :param url: 分享链接
    :return: 分享码；无法解析时为空
    """
    ref = parse_share_link(_as_text(url))
    return ref.share_id if ref else ""


def _normalize_episode_targets(value: Any) -> Optional[Dict[int, Set[int]]]:
    """
    把调用方或任务记录中的季级目标归一为季号到集号集合的映射。

    :param value: 季号到集号的映射，季号键可能来自 JSON 字符串
    :return: 归一后的映射；为空或无法识别时返回 None
    """
    if not isinstance(value, dict):
        return None
    targets: Dict[int, Set[int]] = {}
    for season, numbers in value.items():
        try:
            key = int(season)
            values = {int(number) for number in numbers or ()}
        except (TypeError, ValueError):
            continue
        if values:
            targets[key] = values
    return targets or None


def _task_covers_request(
        current: Dict[str, Any],
        target_episodes: Set[int],
        targets: Optional[Dict[int, Set[int]]],
) -> bool:
    """
    判断活跃任务记录是否已覆盖本次请求，季级目标存在时按季比较。

    :param current: 任务记录
    :param target_episodes: 本次请求的集号并集
    :param targets: 本次请求的季级目标
    :return: 已覆盖返回 True；需要等待或复用返回 False
    """
    covered = set(current.get("episodes") or ())
    if not covered or not target_episodes:
        # 空覆盖表示整包，已覆盖所有请求。
        return True
    covered_targets = _normalize_episode_targets(current.get("episode_targets"))
    if targets and covered_targets:
        return all(
            numbers <= covered_targets.get(season, set())
            for season, numbers in targets.items()
        )
    return target_episodes <= covered


def _normalize_seasons(season: Any) -> Optional[Set[int]]:
    """
    把调用方传入的季归一为季号集合。

    订阅链路可能传入展示文本 ``S01`` / ``S01-S02``，历史任务索引也可能保存字符串；
    无法识别时不做季过滤，避免一个格式问题让整包下载直接失败。

    :param season: 数字、字符串或季号序列
    :return: 季号集合；为空或无法识别时返回 None
    """
    if season is None or season == "":
        return None
    raw_values = season if isinstance(season, (list, tuple, set, frozenset)) else [season]
    seasons: Set[int] = set()
    for value in raw_values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if value >= 0:
                seasons.add(value)
            continue
        for token in re.split(r"[,，、\s]+", str(value).strip()):
            matched = _SEASON_TOKEN_PATTERN.match(token)
            if not matched:
                continue
            begin = int(matched.group(1))
            end = int(matched.group(2) or begin)
            if 0 <= begin <= end:
                seasons.update(range(begin, end + 1))
    if not seasons:
        logger.warning(f"无法识别目标季 {season!r}，本次不做季过滤")
        return None
    return seasons


def _friendly_download_error(error: Exception) -> str:
    """
    把后台异常转换成可展示的失败原因，避免把 Python 异常原文暴露给前端。

    :param error: 后台下载异常
    :return: 可直接展示的中文原因
    """
    if isinstance(error, NetdiskError):
        return _as_text(error.message) or "网盘操作失败"
    if isinstance(error, (ValueError, TypeError)):
        return "分享文件解析失败，请检查文件名或分享内容"
    if isinstance(error, OSError):
        return f"本地文件读写失败：{error}"
    return _as_text(str(error)) or type(error).__name__


def _task_hash(share_code: str, name: str) -> str:
    """
    生成稳定的任务标识。

    :param share_code: 分享码
    :param name: 文件名或链接
    :return: 任务标识
    """
    return hashlib.sha1(f"{share_code}:{name}".encode("utf-8")).hexdigest()[:16]


def _safe_filename(name: str) -> str:
    """
    去掉远端文件名里的目录成分，阻止路径穿越。

    :param name: 分享内的文件名
    :return: 仅含 basename 的安全文件名；非法时返回空串
    """
    cleaned = str(name or "").replace("\\", "/").split("/")[-1].strip()
    if cleaned in {"", ".", ".."}:
        return ""
    return cleaned


def _is_sidecar_name(name: str) -> bool:
    """
    判断是否为下载器自己的附属文件（标记、计划、完成标记）。

    :param name: 文件名
    :return: 属于附属文件返回 True
    """
    return (
        name == _TASK_MARKER
        or name.endswith(".plan.json")
        or name.endswith(".hls.json")
        or name.endswith(".hls.part")
        or name.endswith(".done")
    )


def _remove_empty_dir(path: Path) -> bool:
    """
    删除空目录；目录不存在、非空或只含标记文件时按需处理。

    :param path: 待删除目录
    :return: 实际删除返回 True
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    if any(not _is_sidecar_name(item.name) for item in entries):
        return False
    try:
        for item in entries:
            item.unlink(missing_ok=True)
        path.rmdir()
        return True
    except OSError:
        return False


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
        进程重启后恢复仍在下载中的任务；用户主动暂停的任务保持暂停。

        无法还原分享地址、目录或下载器实例的记录才回落为 paused，避免前端显示
        一个永远不动、又暂停不了的任务。
        """
        for record in store.all_tasks():
            if _as_text(record.get("state")) != "downloading":
                continue
            task_hash = _as_text(record.get("hash"))
            if not task_hash or task_hash in self._tasks:
                continue
            if self._resume_task(task_hash, record):
                continue
            store.upsert_task(task_hash, {
                "state": "paused", "error": None,
                "dlspeed": None, "dlspeed_bytes": 0,
            })
            logger.info(f"网盘任务缺少恢复信息，已回落为暂停：{task_hash}")

    def _resume_task(self, task_hash: str, record: Dict[str, Any]) -> bool:
        """
        按任务记录还原参数并重新启动下载线程。

        :param task_hash: 任务标识
        :param record: 任务记录
        :return: 已启动返回 True；缺少必要信息返回 False
        """
        platform = normalize_platform(record.get("netdisk_platform"))
        share_url = _as_text(record.get("share_url"))
        share_code = _as_text(record.get("share_code"))
        save_path = _as_text(record.get("save_path"))
        if platform is None or not share_url or not share_code or not save_path:
            return False
        config = self._select_config(_as_text(record.get("downloader")) or None)
        if config is None:
            return False
        tuning = self._instance_tuning(config, platform)
        episodes = set(record.get("episodes") or [])
        stop_event = threading.Event()
        with self._lock:
            self._stops[task_hash] = stop_event
            self._tasks[task_hash] = {"stop": stop_event}
            self._discarded.discard(task_hash)
        thread = threading.Thread(
            target=self._run_task,
            args=(
                task_hash, platform, share_url, share_code,
                _as_text(record.get("passcode")), Path(save_path),
                record.get("category"), episodes or None,
                _as_text(record.get("share_token")), tuning, record.get("season"),
                _normalize_episode_targets(record.get("episode_targets")),
            ),
            daemon=True,
            name=f"netdisk-download-{task_hash}",
        )
        thread.start()
        logger.info(f"网盘任务已恢复下载：{task_hash}")
        return True

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
                 netdisk_share_token: Optional[str] = None,
                 season: Optional[Union[int, str, Sequence[int]]] = None,
                 episode_targets: Optional[Dict[int, Set[int]]] = None,
                 episode_runtimes: Optional[Dict[str, int]] = None,
                 media_runtime: Optional[int] = None,
                 source: Optional[str] = None,
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
        :param season: 订阅目标季；为空时按文件名中的季集标记过滤
        :param episode_targets: 按季划分的目标集数；存在时优先于 episodes 过滤
        :param episode_runtimes: 文件名到期望时长的映射，单位为秒
        :param media_runtime: 单集时长（分钟），用于下载完成后的时长校验
        :param source: 下载来源标识，`Subscribe|` 前缀表示订阅来源
        :return: 下载器名称、任务标识、内容布局与说明；非网盘内容返回 None
        """
        if _as_text(download_type) != "netdisk":
            return None
        url = _as_text(content)
        passcode = _as_text(share_password)
        declared = _as_text(netdisk_platform)
        platform = normalize_platform(declared) if declared else None
        if declared and platform is None:
            # 已知但不支持取链的平台用中文名提示，其余保留原始标识便于排查。
            return None, None, None, f"暂不支持 {DISPLAY_ONLY_PLATFORMS.get(declared, declared)} 网盘下载"
        ref = parse_share_link(url, passcode)
        if ref is None:
            return None, None, None, "无法从分享链接中解析分享码"
        if platform is None:
            platform = ref.platform
        if platform != ref.platform:
            return None, None, None, "分享链接与候选平台不一致"
        if selection.is_platform_cooled(platform):
            remaining = selection.platform_cooldown_remaining(platform)
            return None, None, None, f"{platform.value} 平台容量冷却中，约 {int(remaining)} 秒后重试"
        if netdisk_share_token:
            # 令牌只在候选透传时存在，取链仍以适配器解析结果为准。
            logger.debug(f"收到候选分享令牌：{ref.share_id}")
        code = ref.share_id
        blocked = get_share_blacklist().is_blacklisted(platform.value, code)
        if blocked is not None:
            return None, None, None, (
                f"分享已加入黑名单（至 {blocked.expires_at}）：{blocked.reason}"
            )
        config = self._select_config(downloader)
        if config is None:
            return None, None, None, "未配置网盘下载器实例"
        name = _as_text(getattr(config, "name", "")) or "Netdisk"
        task_tuning = self._instance_tuning(config, platform)
        target_episodes = set(episodes or ())
        targets = _normalize_episode_targets(episode_targets)
        if targets and not target_episodes:
            # 只传季级目标时记录真实覆盖集，避免空覆盖被当成整包而误判已覆盖。
            target_episodes = set().union(*targets.values())
        task_key = _task_hash(code, url)
        with self._lock:
            if task_key in self._tasks:
                current = store.get_task(task_key) or {}
                if _task_covers_request(current, target_episodes, targets):
                    return name, task_key, None, DOWNLOAD_TASK_ALREADY_EXISTS
                # 活跃任务只覆盖部分集数时，不能让订阅误以为目标集数已下载。
                return (
                    name,
                    None,
                    None,
                    "同一分享的任务正在下载其他集数，请等待其完成后再试",
                )
            # 同一分享的不同链接形式（带 ?pwd= / # / 长短链）不应变成第二个任务：
            # 复用活跃任务的 hash，链路的下载记录才能继续关联。
            existing = store.find_by_share_code(code)
            if existing and _as_text(existing.get("state")) in {"downloading", "paused"}:
                # 空 episodes 表示整包：它已经覆盖所有单集请求，必须复用同一任务，
                # 否则两个任务会写同一个分享目录并互相截断。
                if _task_covers_request(existing, target_episodes, targets):
                    return (
                        name,
                        _as_text(existing.get("hash")),
                        None,
                        DOWNLOAD_TASK_ALREADY_EXISTS,
                    )
                # 互补集数也必须串行：task_dir 只按 share_code 命名，并发写会互相截断。
                return (
                    name,
                    None,
                    None,
                    "同一分享已有任务在下载其他集数，请等待其完成后再试",
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
            (task_dir / _TASK_MARKER).touch(exist_ok=True)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"创建网盘下载目录失败：{task_dir} - {err}")
        store.upsert_task(task_key, {
            "title": f"{platform.value}:{code}",
            "name": code,
            "downloader": name,
            "state": "downloading",
            "error": None,
            "progress": 0.0,
            "size": 0.0,
            "dlspeed": None,
            "dlspeed_bytes": 0,
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
            "episode_targets": (
                {str(key): sorted(value) for key, value in targets.items()} if targets else None
            ),
            "season": season,
            "source": source,
        })
        thread = threading.Thread(
            target=self._run_task,
            args=(task_key, platform, url, code, passcode, task_dir, label,
                  target_episodes or None, _as_text(netdisk_share_token),
                  task_tuning, season, targets),
            kwargs={
                "episode_runtimes": episode_runtimes,
                "media_runtime": media_runtime,
                "source": source,
            },
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
        platform_key = getattr(platform, "value", str(platform))

        def platform_value(mapping: Any, default: int) -> int:
            """
            读取平台级覆盖值，缺失或非法时回落到实例默认值。
            """
            if not isinstance(mapping, dict):
                return default
            value = mapping.get(platform_key)
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        threads_override = platform_value(raw.get("platform_threads"), 0)
        max_tasks_override = platform_value(raw.get("platform_max_tasks"), 0)

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
            threads=(
                max(1, min(cap, threads_override))
                if threads_override > 0
                else int_value("threads", base.threads, 1, cap)
            ),
            max_tasks=(
                max(1, min(64, max_tasks_override))
                if max_tasks_override > 0
                else int_value("max_tasks", base.max_tasks, 1, 64)
            ),
            rate_limit=rate_limit,
            preempt_enabled=bool_value("preempt", base.preempt_enabled),
            multi_source=bool_value("multi_source", base.multi_source),
            transfer_first=bool_value("transfer_first", base.transfer_first),
        )

    def _run_task(self, task_key: str, platform: NetdiskPlatform, share_url: str,
                  share_code: str, passcode: str, download_dir: Path,
                  label: Optional[str],
                  episodes: Optional[Set[int]] = None,
                  share_token: str = "",
                  tuning: Optional[NetdiskTuning] = None,
                  season: Optional[Union[int, str, Sequence[int]]] = None,
                  episode_targets: Optional[Dict[int, Set[int]]] = None,
                  episode_runtimes: Optional[Dict[str, int]] = None,
                  media_runtime: Optional[int] = None,
                  source: Optional[str] = None) -> None:
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
        :param season: 订阅目标季
        :param episode_targets: 按季划分的目标集数，优先于 episodes 过滤
        :param episode_runtimes: 文件名到期望时长的映射，单位为秒
        :param media_runtime: 单集时长（分钟），用于为本次任务的所有文件生成期望时长
        :param source: 下载来源标识，`Subscribe|` 前缀表示订阅来源
        """
        task_dir = download_dir
        stop_event = self._stops.get(task_key) or threading.Event()
        if not source:
            # 进程重启后的恢复任务没有内存来源，从任务记录取提交时写入的来源，
            # 否则订阅任务会被误判为手动下载而放弃补集。
            source = _as_text((store.get_task(task_key) or {}).get("source")) or None
        # 实例配置必须进入取链服务，否则 transfer_first / auto_transfer 只在下载引擎生效。
        resolver = (
            NetdiskResolveService(settings=tuning)
            if tuning is not None
            else NetdiskResolveService()
        )
        with _TASK_SLOTS:
            try:
                resolved = resolver.walk(
                    share_url, passcode, platform, max_depth=_MAX_WALK_DEPTH,
                    share_token=share_token,
                )
                files = self._collect_files(resolved.files, episodes, season, episode_targets)
                if resolved.detail.title:
                    store.upsert_task(task_key, {"title": resolved.detail.title})
                if not files:
                    if episode_targets:
                        wanted = sorted({
                            number for numbers in episode_targets.values() for number in numbers
                        })
                        raise NetdiskError(
                            NetdiskErrorCode.UNSUPPORTED,
                            f"分享内没有目标集数（{wanted}）对应的文件",
                        )
                    if episodes:
                        raise NetdiskError(
                            NetdiskErrorCode.UNSUPPORTED,
                            f"分享内没有目标集数（{sorted(episodes)}）对应的文件",
                        )
                    raise NetdiskError(
                        NetdiskErrorCode.UNSUPPORTED, "分享内没有可下载的视频文件"
                    )
                total_size = sum(int(item.size or 0) for item in files)
                self._ensure_local_space(task_dir, total_size)
                store.upsert_task(task_key, {
                    "size": float(total_size),
                    "files": [
                        {"name": item.name, "size": int(item.size or 0), "progress": 0.0}
                        for item in files
                    ],
                })
                task_tuning = tuning or resolver.tuning
                progress_lock = threading.Lock()
                progress_state = {
                    "completed": 0.0,
                    "active": {},
                    "at": time.monotonic(),
                    "done": 0.0,
                    "speed": 0.0,
                    "write_at": 0.0,
                    "percent": -100.0,
                }

                def on_file_progress(name: str, percent: float, file_size: int) -> None:
                    """汇总并发文件的完成字节，按时间/百分比节流写入任务进度。"""
                    with progress_lock:
                        progress_state["active"][name] = (
                            file_size * max(min(float(percent), 100.0), 0.0) / 100.0
                        )
                        done = progress_state["completed"] + sum(progress_state["active"].values())
                        now = time.monotonic()
                        speed: Optional[float] = None
                        elapsed = now - float(progress_state["at"])
                        if elapsed >= 1.0:
                            instant = max(done - float(progress_state["done"]), 0.0) / elapsed
                            # 分片并发会把字节成批送达，瞬时值会在 10MB/s 与几百 KB/s 间跳变；
                            # 用 EWMA 平滑展示速度，避免卡片像网络断开一样闪。
                            previous = float(progress_state["speed"])
                            speed = instant if previous <= 0 else previous * 0.7 + instant * 0.3
                            progress_state["speed"] = speed
                            progress_state["at"], progress_state["done"] = now, done
                        overall = (done / total_size * 100.0) if total_size > 0 else 0.0
                        if (
                            now - float(progress_state["write_at"]) < _PROGRESS_INTERVAL_SECONDS
                            and abs(overall - float(progress_state["percent"])) < _PROGRESS_DELTA_PERCENT
                        ):
                            return
                        progress_state["write_at"], progress_state["percent"] = now, overall
                        store.update_progress(task_key, overall, dlspeed=speed)

                pending: List[ShareFile] = []
                for item in files:
                    target = task_dir / item.name
                    expected = int(item.size or 0)
                    if _is_completed_file(target, expected):
                        # 上次已完成但记录未收尾：保留文件，不重复下载与转存。
                        logger.info(f"网盘文件已存在且确认完成，跳过：{target}")
                        progress_state["completed"] += float(expected)
                        continue
                    pending.append(item)

                if total_size > 0 and progress_state["completed"] > 0:
                    # 重试时先按已存在文件回写进度，避免任务失败前一直显示 0%。
                    store.update_progress(
                        task_key,
                        cast(float, progress_state["completed"]) / total_size * 100.0,
                    )

                workers = max(1, min(int(task_tuning.max_tasks), len(pending))) if pending else 1
                workers = self._limit_workers_for_task(workers, pending, platform, task_tuning)
                account_map = self._assign_accounts(platform, pending)
                per_file_threads = max(1, int(task_tuning.threads))
                workers = _cap_platform_workers(platform, workers, per_file_threads)
                # 期望时长只服务时长校验：显式单集时长优先，其余文件回落到整批单集时长。
                runtimes: Dict[str, int] = dict(episode_runtimes or {})
                if media_runtime and int(media_runtime) > 0:
                    runtime_seconds = int(media_runtime) * 60
                    for item in files:
                        runtimes.setdefault(item.name, runtime_seconds)
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix=f"netdisk-file-{task_key}"
                ) as pool:
                    futures = {
                        pool.submit(
                            self._download_one,
                            resolver, platform, share_url, passcode, item, task_dir,
                            task_key, stop_event, share_token, task_tuning,
                            on_file_progress, threads=per_file_threads,
                            concurrency=workers,
                            account_id=account_map.get(item.name),
                            episode_runtimes=runtimes,
                        ): item
                        for item in pending
                    }
                    file_errors: Dict[str, str] = {}
                    # 保留原始异常，全批失败时用 `raise ... from` 保住分享级错误的判定链。
                    file_exceptions: Dict[str, BaseException] = {}
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            future.result()
                        except DownloadCancelled:
                            # 用户暂停：保持 paused，不能被后续的「内部异常」分支记成失败。
                            for pending_future in futures:
                                pending_future.cancel()
                            raise
                        except Exception as err:  # noqa: BLE001
                            # 单文件失败只记账，不打断其他文件，也不让整个任务失败。
                            file_errors[item.name] = _friendly_download_error(err)
                            file_exceptions[item.name] = err
                            logger.warning(
                                f"网盘文件失败，继续其他文件：{item.name} - "
                                f"{file_errors[item.name]}"
                            )
                            with progress_lock:
                                progress_state["active"].pop(item.name, None)
                            continue
                        with progress_lock:
                            progress_state["completed"] += float(item.size or 0)
                            progress_state["active"].pop(item.name, None)
                # 只有每个文件都写了 .done 才算完成，防止预分配的空壳文件被整理入库。
                missing = [
                    item for item in pending
                    if not _is_completed_file(task_dir / item.name, int(item.size or 0))
                ]
                # 失败文件的残片（目标、完成标记、分片计划）必须清掉，
                # 否则下次重试会把残缺文件当成可续传的基础。
                for item in missing:
                    target = task_dir / item.name
                    for path in (target, target.with_name(target.name + ".done"),
                                 target.with_suffix(target.suffix + ".plan.json")):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                if not missing:
                    if task_key not in self._discarded:
                        store.upsert_task(task_key, {
                            "state": "completed", "progress": 100.0,
                            "error": None, "dlspeed": None, "dlspeed_bytes": 0,
                        })
                elif any(
                    _is_completed_file(task_dir / item.name, int(item.size or 0))
                    for item in pending
                ):
                    # 至少有一个有效文件：按部分完成入库，交给上层决定是否补齐。
                    # 分享级文件失败（如仅预览流）仍要拉黑分享，
                    # 否则补集搜索会反复选中同一个坏分享；有效文件已有 .done，不受影响。
                    for name, err in file_exceptions.items():
                        if is_share_level_error(err):
                            reason = _friendly_download_error(err)
                            logger.warning(
                                f"网盘文件分享级失败，拉黑分享："
                                f"{platform.value}:{share_code} - {name} - {reason}"
                            )
                            get_share_blacklist().add(platform.value, share_code, reason)
                    if task_key not in self._discarded:
                        valid_files = [
                            item for item in pending
                            if _is_completed_file(task_dir / item.name, int(item.size or 0))
                        ]
                        missing_episodes = sorted({
                            number for item in missing
                            for number in episode_numbers(item.name)
                        })
                        target_episodes = set(episodes or ())
                        if not target_episodes:
                            target_episodes = set().union(
                                *(_normalize_episode_targets(episode_targets) or {}).values()
                            )
                        summary = {
                            "media": {
                                "title": resolved.detail.title or label or share_code,
                                "season": season,
                            },
                            "target_episodes": sorted(target_episodes),
                            "valid_files": [
                                {"name": item.name, "size": int(item.size or 0)}
                                for item in valid_files
                            ],
                            "failed_files": [
                                {"name": item.name, "reason": file_errors.get(item.name, "未完成")}
                                for item in missing
                            ],
                            "missing_episodes": missing_episodes,
                            "source": source or "",
                        }
                        decision = judge_batch(summary)
                        is_subscription = bool(
                            source and str(source).startswith("Subscribe|")
                        )
                        if decision.decision == "retry_missing" and not is_subscription:
                            # 手动下载没有下一次订阅搜索，补下建议只会造成误导。
                            decision = BatchDecision("explain_only", decision.reason, [])
                        store.upsert_task(task_key, {
                            "state": "completed", "progress": 100.0,
                            "error": None, "dlspeed": None, "dlspeed_bytes": 0,
                            "partial": True,
                            "failed_files": summary["failed_files"],
                            # 补集只信本地确定性解析；AI 给出的集数仅保留在 ai_decision 展示。
                            "missing_episodes": missing_episodes,
                            "ai_decision": {
                                "decision": decision.decision,
                                "reason": decision.reason,
                                "missing_episodes": list(decision.missing_episodes),
                            },
                            "warning": f"部分完成：{len(valid_files)} 个有效，{len(missing)} 个失败",
                        })
                else:
                    # 全批失败时把原因一起带出来，否则用户只看到文件名，
                    # 无法区分是时长异常、直链失效还是磁盘写入失败。
                    reasons = "; ".join(
                        f"{item.name}（{file_errors.get(item.name, '未完成')}）"
                        for item in missing[:3]
                    )
                    error = DownloadError(f"下载未完成：{reasons}")
                    first_exception = next(iter(file_exceptions.values()), None)
                    if first_exception is not None:
                        # 保留原因链，外层才能识别「每个文件都因分享级错误失败」的分享。
                        raise error from first_exception
                    raise error
                logger.info(f"网盘下载完成：{platform.value}:{share_code}")
            except Exception as err:  # noqa: BLE001
                # 只有外部停止信号置位才算用户暂停；内部异常中止一律按失败记录，
                # 否则引擎的「下载已取消」会掩盖真实错误，前端表现为任务自动暂停。
                if stop_event.is_set():
                    logger.info(f"网盘下载已暂停：{share_code}")
                    if task_key not in self._discarded:
                        store.upsert_task(task_key, {
                            "state": "paused", "error": None,
                            "dlspeed": None, "dlspeed_bytes": 0,
                        })
                else:
                    error_text = _friendly_download_error(err)
                    if is_share_level_error(err):
                        # 分享本身不可用（失效、仅预览流等）时拉黑，
                        # 避免订阅反复重试同一个坏分享。
                        get_share_blacklist().add(platform.value, share_code, error_text)
                    capacity_error = error_text if (
                        isinstance(err, NetdiskError)
                        and err.code is NetdiskErrorCode.SPACE_INSUFFICIENT
                    ) or "空间不足" in error_text or "容量不足" in error_text else None
                    logger.warn(f"网盘下载失败：{share_code} - {error_text}")
                    if task_key not in self._discarded:
                        store.upsert_task(task_key, {
                            "state": "error", "error": error_text,
                            "capacity_error": capacity_error,
                            "dlspeed": None, "dlspeed_bytes": 0,
                        })
            finally:
                # 失败、取消或没有可下载文件时，任务目录可能为空；只回收空目录，
                # 部分完成的文件保留给续传。
                _remove_empty_dir(task_dir)
                with self._lock:
                    self._stops.pop(task_key, None)
                    self._tasks.pop(task_key, None)

    @staticmethod
    def _ensure_local_space(directory: Path, total_size: int) -> None:
        """
        下载前检查本地磁盘余量，不足时直接失败并给出明确原因。

        :param directory: 下载目录
        :param total_size: 本次需要写入的字节数
        :raises DownloadError: 余量不足时抛出
        """
        if total_size <= 0:
            return
        try:
            free = shutil.disk_usage(directory).free
        except OSError as error:
            logger.debug(f"读取下载目录余量失败，跳过预检：{directory} - {error}")
            return
        if free < total_size * 1.05:
            raise DownloadError(
                f"本地磁盘余量不足：需要 {total_size} 字节，当前可用 {free} 字节"
            )

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
                       episodes: Optional[Set[int]] = None,
                       season: Optional[Union[int, str, Sequence[int]]] = None,
                       episode_targets: Optional[Dict[int, Set[int]]] = None) -> List[ShareFile]:
        """
        过滤出待下载文件，并给不含季集标记的文件名补前缀。

        :param files: 分享内递归展开后的全部文件
        :param episodes: 目标集数集合；为空表示整包下载
        :param season: 订阅目标季；支持数字、S01/S01-S02 文本或季号序列；为空时不做季过滤
        :param episode_targets: 按季划分的目标集数；存在时优先于 episodes
        :return: 过滤后的文件条目
        """
        wanted = [item for item in files if not item.is_dir and self._wanted(item.name)]
        wanted = self._filter_episodes(wanted, episodes, season, episode_targets)
        selected: List[ShareFile] = []
        by_stem: Dict[str, int] = {}
        for item in wanted:
            # 同一文件名不同扩展名（如 04.mkv 与 04.mp4，可能位于不同子目录）视为同一集
            # 的不同版本，只保留体积更大的一个；同名同扩展名（分片、不同季同名文件）不合并。
            stem = f"{self._season_from_path(getattr(item, 'path', ''))}:" \
                   f"{Path(item.name).stem.lower()}"
            index = by_stem.get(stem)
            if index is not None:
                current = selected[index]
                same_suffix = Path(item.name).suffix.lower() == Path(current.name).suffix.lower()
                if same_suffix:
                    selected.append(item)
                    continue
                if int(item.size or 0) > int(current.size or 0):
                    logger.info(f"同一集存在多个版本，保留体积更大的文件：{item.name}")
                    selected[index] = item
                else:
                    logger.info(f"同一集存在多个版本，忽略较小文件：{item.name}")
                continue
            by_stem[stem] = len(selected)
            selected.append(item)
        result: List[ShareFile] = []
        used: Set[str] = set()
        for item in selected:
            name = self._episode_name(
                item.name, getattr(item, "path", ""),
                list(episode_targets) if episode_targets else season,
            )
            name = _safe_filename(name)
            if not name:
                logger.warning(f"忽略非法文件名：{item.name}")
                continue
            if name in used:
                # 递归目录拍平后同名文件会互相覆盖；保留唯一前缀而不是静默丢弃。
                prefix = _as_text(getattr(item, "file_id", ""))[:6] or "dup"
                candidate = f"{prefix}_{name}"
                while candidate in used:
                    candidate = f"_{candidate}"
                name = candidate
            used.add(name)
            result.append(replace(item, name=name))
        return result

    @staticmethod
    def _filter_episodes(files: Sequence[ShareFile],
                         episodes: Optional[Set[int]],
                         season: Optional[Union[int, str, Sequence[int]]] = None,
                         episode_targets: Optional[Dict[int, Set[int]]] = None) -> List[ShareFile]:
        """
        只保留目标季与目标集数对应的文件。

        必须在补 `S01Exx` 前缀之前用原始文件名解析：补前缀会让原本解析不出的文件
        变得可解析，从而破坏「解析不确定就全下」的兜底语义。

        放行规则（任一命中即保留，宁可多下也不漏集）：
        没有目标集数、文件名解析不出唯一集数、集数在目标集合内则保留。
        文件名带明确季号且与目标季不一致时默认排除；只有目标季唯一且分享内没有
        任何目标季精确命中时，同集号的其它季文件才作为兜底候选，处理上传方把
        连续集数误标成另一季的情况。

        :param files: 分享内的文件条目
        :param episodes: 目标集数集合
        :param season: 订阅目标季，支持数字、S01/S01-S02 文本或季号序列
        :param episode_targets: 按季划分的目标集数；存在时优先于 episodes
        :return: 过滤后的文件条目
        """
        targets = _normalize_episode_targets(episode_targets)
        if targets:
            return NetdiskModule._filter_by_episode_targets(files, targets)
        wanted = set(episodes or ())
        seasons = _normalize_seasons(season)
        single_season = next(iter(seasons)) if seasons and len(seasons) == 1 else None
        kept: List[ShareFile] = []
        fallback: List[ShareFile] = []
        cross_season: List[ShareFile] = []
        for item in files:
            if is_special_name(item.name):
                continue
            if seasons:
                matched = _SEASON_EPISODE_PATTERN.search(item.name)
                file_season = (
                    int(matched.group(1)) if matched
                    else NetdiskModule._season_from_path(getattr(item, "path", ""))
                )
                if file_season is not None and file_season not in seasons:
                    # 目标季唯一且集号命中时先记为候选，只有目标季完全没有
                    # 精确命中才会被选中，避免正常多季分享被跨季替代。
                    numbers = episode_numbers(item.name)
                    if single_season is not None and wanted \
                            and any(number in wanted for number in numbers):
                        fallback.append(item)
                        cross_season.append(item)
                    continue
            if not wanted:
                kept.append(item)
                continue
            numbers = episode_numbers(item.name)
            if numbers and any(number in wanted for number in numbers):
                kept.append(item)
                continue
            # 解析不出集数、或范围未命中时先记为兜底候选；只要有任何文件精确命中，
            # 这些候选（电影、合集、命名不规范的附加文件）就不应再下载。
            if not numbers or len(numbers) >= 2:
                fallback.append(item)
        if wanted:
            selected = kept if kept else fallback
            logger.info(
                f"目标季 {season} 目标集数 {sorted(wanted)}，分享内 {len(files)} 个文件，"
                f"精确命中 {len(kept)} 个，兜底候选 {len(fallback)} 个，选中 {len(selected)} 个"
            )
            if not kept and cross_season:
                logger.warning(
                    f"目标季 {single_season} 没有精确命中，使用其它季同集号文件兜底："
                    f"{[item.name for item in cross_season]}"
                )
            return selected
        return kept

    @staticmethod
    def _filter_by_episode_targets(files: Sequence[ShareFile],
                                   targets: Dict[int, Set[int]]) -> List[ShareFile]:
        """
        按文件季号取该季目标集过滤，未知季号只在单季目标下用该季集号兜底。

        单季目标下，目标季没有任何精确命中时，其它季的同集号文件作为兜底候选，
        处理上传方把连续集数误标成另一季的情况；多季目标保持只按目标季匹配。

        :param files: 分享内的文件条目
        :param targets: 已归一化的季号到目标集号映射
        :return: 过滤后的文件条目
        """
        kept: List[ShareFile] = []
        fallback: List[ShareFile] = []
        cross_season: List[ShareFile] = []
        single_target = next(iter(targets.values())) if len(targets) == 1 else None
        for item in files:
            if is_special_name(item.name):
                continue
            matched = _SEASON_EPISODE_PATTERN.search(item.name)
            file_season = (
                int(matched.group(1)) if matched
                else NetdiskModule._season_from_path(getattr(item, "path", ""))
            )
            if file_season is None:
                if single_target is None:
                    logger.info(f"无法识别季号，多季目标下跳过文件：{item.name}")
                    continue
                wanted = single_target
            else:
                season_target = targets.get(file_season)
                if not season_target:
                    numbers = episode_numbers(item.name)
                    if single_target is not None \
                            and any(number in single_target for number in numbers):
                        fallback.append(item)
                        cross_season.append(item)
                    continue
                wanted = season_target
            numbers = episode_numbers(item.name)
            if numbers and any(number in wanted for number in numbers):
                kept.append(item)
                continue
            if not numbers or len(numbers) >= 2:
                fallback.append(item)
        selected = kept if kept else fallback
        summary = ", ".join(
            f"S{season:02d}:{sorted(numbers)}" for season, numbers in sorted(targets.items())
        )
        logger.info(
            f"目标季 {summary}，分享内 {len(files)} 个文件，精确命中 {len(kept)} 个，"
            f"兜底候选 {len(fallback)} 个，选中 {len(selected)} 个"
        )
        if not kept and cross_season:
            logger.warning(
                f"目标季 {summary} 没有精确命中，使用其它季同集号文件兜底："
                f"{[item.name for item in cross_season]}"
            )
        return selected

    @staticmethod
    def _season_from_path(path: str) -> Optional[int]:
        """
        从相对路径的目录/文件名中提取季号。

        :param path: 相对分享根的路径
        :return: 季号；无法识别时返回 None
        """
        for matched in _SEASON_PATH_PATTERN.finditer(str(path or "")):
            value = next((group for group in matched.groups() if group), None)
            if value:
                return int(value)
        return None

    @classmethod
    def _episode_name(cls, name: str, path: str = "",
                      season: Optional[Union[int, str, Sequence[int]]] = None) -> str:
        """
        给不含季集标记的文件名补上 `S01Exx` 前缀，便于整理阶段识别集数。

        分享里的文件名常见 `164 4K.mkv`、`01~4K.mp4`，MetaInfo 无法识别季集，
        整理会以「未识别到文件集数」拒绝。补前缀后 `S01E164 164 4K.mkv` 可正常解析。
        候选集数不唯一时原样返回，绝不猜测。已带季集标记的文件在单季目标下若季号
        不一致，按目标季改写季号：跨季兜底选中的文件不能被整理阶段归到别的季。

        :param name: 分享内的原始文件名
        :param path: 相对分享根的路径，用于从目录名识别季号
        :param season: 订阅目标季；只有唯一季时才用于补前缀或改写季号
        :return: 补好集数前缀的文件名
        """
        numbers = episode_numbers(name)
        if len(numbers) != 1:
            return name
        target = cls._season_from_path(path)
        if target is None:
            normalized = _normalize_seasons(season)
            target = next(iter(normalized)) if normalized and len(normalized) == 1 else None
        matched = re.search(r"[Ss](\d{1,2})\s*[Ee](\d{1,4})", name)
        if matched:
            if target is None or int(matched.group(1)) == target:
                return name
            return (
                f"{name[:matched.start()]}S{target:02d}E{int(matched.group(2)):03d}"
                f"{name[matched.end():]}"
            )
        if target is None:
            # 无季信息时不再硬编码 S01，避免把 S02 文件标成第一季。
            return name
        return f"S{target:02d}E{numbers[0]:03d} {name}"

    @staticmethod
    def _wanted(name: str) -> bool:
        """
        判断文件是否需要下载。

        :param name: 文件名
        :return: 视频或归档返回 True
        """
        suffix = name[name.rfind("."):].lower() if "." in name else ""
        return suffix in _DOWNLOAD_SUFFIXES

    def _assign_accounts(
        self,
        platform: NetdiskPlatform,
        pending: Sequence[ShareFile],
    ) -> Dict[str, int]:
        """
        多账号时按剩余容量把待下载文件分配给不同账号。

        单文件或只有一个可用账号时返回空表，保留原有的自动选号/多源逻辑。

        :param platform: 网盘平台
        :param pending: 待下载文件
        :return: 文件名到账号主键的映射
        """
        if len(pending) <= 1:
            return {}
        try:
            views = NetdiskAccountService().list(platform)
        except Exception as error:  # noqa: BLE001 - 账号不可用时退回自动选号
            logger.debug(f"读取网盘账号失败：{platform.value} - {type(error).__name__}")
            return {}
        accounts = [view for view in views if view.status == "active"]
        if len(accounts) < 2:
            return {}
        sizes = [int(item.size or 0) for item in pending]
        unknown_budget = sum(sizes) + 1
        free = {
            view.id: (
                max(int(view.quota_total) - int(view.quota_used), 0)
                if int(view.quota_total or 0) > 0
                else unknown_budget
            )
            for view in accounts
        }
        assignment: Dict[str, int] = {}
        for item in sorted(pending, key=lambda entry: -int(entry.size or 0)):
            need = int(item.size or 0)
            candidates = [view for view in accounts if free[view.id] >= need] or accounts
            chosen = max(candidates, key=lambda view: (free[view.id], -view.id))
            assignment[item.name] = chosen.id
            free[chosen.id] = max(free[chosen.id] - need, 0)
        return assignment

    def _limit_workers_for_task(
        self,
        workers: int,
        pending: Sequence[ShareFile],
        platform: NetdiskPlatform,
        tuning: NetdiskTuning,
    ) -> int:
        """
        按平台最空闲账号的剩余空间限制并发文件数。

        只在可能发生转存时限制：免转存平台与关闭自动转存时保持原并发。

        :param workers: 期望并发数
        :param pending: 待下载文件
        :param platform: 网盘平台
        :param tuning: 参数快照
        :return: 实际并发数
        """
        if workers <= 1 or not tuning.auto_transfer:
            return workers
        try:
            adapter = find_adapter(platform)
        except Exception as error:  # noqa: BLE001 - 适配器不可用时不限制并发
            logger.debug(f"读取网盘适配器失败：{platform.value} - {type(error).__name__}")
            return workers
        if getattr(adapter, "link_policy", LinkPolicy.DIRECT) is LinkPolicy.DIRECT:
            return workers
        free_bytes = self._most_free_bytes(platform)
        if free_bytes <= 0:
            return workers
        return _limit_workers(workers, [int(item.size or 0) for item in pending], free_bytes)

    @staticmethod
    def _most_free_bytes(platform: NetdiskPlatform) -> int:
        """
        返回平台内最空闲账号的剩余字节数；无法判断时返回 0。

        :param platform: 网盘平台
        :return: 剩余字节数
        """
        try:
            views = NetdiskAccountService().list(platform)
        except Exception as error:  # noqa: BLE001 - 配额不可用不影响下载
            logger.debug(f"读取网盘账号配额失败：{platform.value} - {type(error).__name__}")
            return 0
        frees = [
            max(int(view.quota_total or 0) - int(view.quota_used or 0), 0)
            for view in views
            if int(view.quota_total or 0) > 0
        ]
        return max(frees, default=0)

    def _download_one(self, resolver: NetdiskResolveService, platform: NetdiskPlatform,
                      share_url: str, passcode: str, item: ShareFile,
                      download_dir: Path, task_key: str, stop_event: threading.Event,
                      share_token: str = "", tuning: Optional[NetdiskTuning] = None,
                      on_file_progress: Optional[Callable[[str, float, int], None]] = None,
                      threads: int = 0, concurrency: int = 1,
                      account_id: Optional[int] = None,
                      episode_runtimes: Optional[Dict[str, int]] = None) -> None:
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
        :param share_token: 候选透传的分享令牌
        :param tuning: 下载器实例覆盖后的参数快照
        :param on_file_progress: 单文件进度回调 `(文件名, 百分比, 大小)`
        :param threads: 该文件的分片并发数；为空时使用实例配置
        :param concurrency: 同时下载的文件数，用于计算转存峰值空间
        :param account_id: 分配到的账号主键；为空时自动选号
        :param episode_runtimes: 文件名到期望时长的映射，单位为秒
        """
        tuning = tuning or resolver.tuning
        multi_source = bool(tuning.multi_source)
        target = download_dir / item.name
        seen: List[DirectLink] = []
        size = int(item.size or 0)
        started_at = time.monotonic()
        links: Tuple[DirectLink, ...] = ()

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
                share_url, passcode, item, platform, account_id=account_id,
                multi_source=multi_source and account_id is None, force_refresh=True,
                share_token=share_token, concurrency=concurrency,
                exclude_directories=self._active_temp_directories,
            ))
            if fresh:
                links = fresh
                seen.extend(fresh)
                store.upsert_task(task_key, {
                    "account_id": int(getattr(fresh[0], "account_id", 0) or 0),
                    "strategy": getattr(fresh[0], "strategy", "") or "",
                })
                store.add_cleanup_handles(
                    task_key, self._handle_payloads(fresh),
                )
            return links

        def on_progress(percent: float) -> None:
            """把单文件进度交给任务级汇总，多文件并发时由外层统一折算总进度。"""
            if on_file_progress is not None:
                on_file_progress(item.name, percent, size)

        adapter = find_adapter(platform)
        max_concurrency = int(getattr(adapter, "max_concurrency", 0) or 0)
        single_stream = bool(getattr(adapter, "single_stream", False))
        try:
            links = tuple(resolver.resolve_file(
                share_url, passcode, item, platform, account_id=account_id,
                multi_source=multi_source and account_id is None,
                share_token=share_token, concurrency=concurrency,
                exclude_directories=self._active_temp_directories,
            ))
            if not links:
                raise DownloadError(f"无法获取下载直链：{item.name}")
            seen.extend(links)
            store.upsert_task(task_key, {
                "account_id": int(getattr(links[0], "account_id", 0) or 0),
                "strategy": getattr(links[0], "strategy", "") or "",
            })
            store.add_cleanup_handles(task_key, self._handle_payloads(links))
            size = int(item.size or links[0].size or 0)
            is_hls = any(link.is_hls for link in links)
            if not is_hls and size <= 0:
                raise DownloadError(f"无法确定文件大小：{item.name}")
            if is_hls:
                try:
                    HlsDownloader(
                        target=target,
                        links=links,
                        retries=tuning.retries,
                        buffer_size=tuning.buffer_size,
                        on_progress=on_progress,
                        stop_event=stop_event,
                    ).run(url_provider)
                except NotHls:
                    logger.warning(f"网盘直链不是 m3u8，回退普通下载：{item.name}")
                    is_hls = False
            if not is_hls:
                if size <= 0:
                    raise DownloadError(f"无法确定文件大小：{item.name}")
                ChunkedDownloader(
                    target=target,
                    total=size,
                    links=links,
                    threads=1 if single_stream else (threads or tuning.threads),
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
                    single_stream=single_stream,
                ).run(url_provider)
            # 下载器只保证字节完整；空壳、截断或错集必须在这里拦下，避免入库。
            _validate_video_duration(target, (episode_runtimes or {}).get(item.name))
        finally:
            self._cleanup_links(resolver, seen, task_key)
            account_id = int(getattr(links[0], "account_id", 0) or 0) if links else 0
            # 失败任务耗时极短，采样会污染账号速度；只在文件确认完成后记录。
            if account_id and _is_completed_file(target, size):
                selection.record_account_speed(
                    account_id, size, max(time.monotonic() - started_at, 0.001),
                )

    @staticmethod
    def _active_temp_directories() -> Set[str]:
        """
        收集所有任务仍在使用的转存目录标识，清理临时目录时跳过。

        :return: 目录标识集合
        """
        active: Set[str] = set()
        for record in store.all_tasks():
            handles = record.get("cleanup_handles") or []
            for handle in handles:
                if not isinstance(handle, dict):
                    continue
                directory_id = _as_text(handle.get("directory_id"))
                if directory_id:
                    active.add(directory_id)
        return active

    @staticmethod
    def _handle_payloads(links: Sequence[DirectLink]) -> List[Dict[str, Any]]:
        """
        把直链上的转存句柄投影为可持久化字段。

        :param links: 直链集合
        :return: 句柄字段列表
        """
        payloads: List[Dict[str, Any]] = []
        for link in links:
            handle = link.cleanup_handle
            if handle is None or not handle.directory_id:
                continue
            payloads.append({
                "platform": handle.platform.value,
                "account_id": int(handle.account_id or 0),
                "directory_id": handle.directory_id,
                "file_ids": list(handle.file_ids or ()),
            })
        return payloads

    @staticmethod
    def _cleanup_links(resolver: NetdiskResolveService, links: Sequence[DirectLink],
                       task_key: str = "") -> None:
        """
        清理下载过程中所有转存句柄，同一目录只清理一次，单句柄失败不影响其余句柄。

        :param resolver: 取链服务
        :param links: 下载过程中使用过的直链
        :param task_key: 任务标识，用于移除已完成的句柄登记
        """
        handles: Dict[str, TransferHandle] = {}
        for link in links:
            handle = link.cleanup_handle
            if handle is None or not handle.directory_id:
                continue
            handles.setdefault(handle.directory_id, handle)
        cleaned: List[str] = []
        for handle in handles.values():
            try:
                if resolver.cleanup(DirectLink(
                    url="",
                    headers={},
                    cleanup_handle=handle,
                    account_id=handle.account_id,
                )):
                    cleaned.append(handle.directory_id)
            except Exception as err:  # noqa: BLE001 - 单个句柄失败不阻断其余清理
                logger.warning(
                    f"清理网盘转存句柄失败：{handle.platform.value} {handle.directory_id} - "
                    f"{type(err).__name__}"
                )
        if task_key and cleaned:
            # 只注销确认清理成功的句柄，失败项留给 cleanup_orphans 补偿。
            store.remove_cleanup_handles(task_key, cleaned)

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
            # 已整理任务只在按 hash 精确查询时可见（清理流程需要），列表默认隐藏。
            if not wanted and state == "transferred":
                continue
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
                error=record.get("error"),
                netdisk_platform=record.get("netdisk_platform"),
                share_code=record.get("share_code"),
                share_url=record.get("share_url"),
                reconciled=bool(record.get("reconciled")),
                account_id=record.get("account_id"),
                strategy=record.get("strategy"),
                capacity_error=record.get("capacity_error"),
                partial=bool(record.get("partial")),
                warning=record.get("warning"),
                missing_episodes=record.get("missing_episodes"),
                ai_decision=record.get("ai_decision"),
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
                    done_mark = path.with_name(path.name + ".done")
                    for candidate in (path, plan_path, hls_plan, hls_part, done_mark):
                        try:
                            candidate.unlink(missing_ok=True)
                        except Exception as err:  # noqa: BLE001
                            logger.warn(f"删除网盘下载文件失败：{candidate} - {err}")
            self._cleanup_persisted_handles(task_hash, record)
            task_dir = Path(record.get("path") or "")
            if task_dir:
                _remove_empty_dir(task_dir)
            self._discarded.add(task_hash)
            store.remove_task(task_hash)
        return True

    def _cleanup_persisted_handles(self, task_hash: str, record: Dict[str, Any]) -> None:
        """
        清理任务记录中尚未完成的转存句柄，覆盖进程重启后的残留。

        :param task_hash: 任务标识
        :param record: 任务记录
        """
        handles = record.get("cleanup_handles") or []
        if not handles:
            return
        links: List[DirectLink] = []
        for item in handles:
            if not isinstance(item, dict):
                continue
            platform = normalize_platform(item.get("platform"))
            directory_id = _as_text(item.get("directory_id"))
            if platform is None or not directory_id:
                continue
            links.append(DirectLink(
                url="",
                headers={},
                cleanup_handle=TransferHandle(
                    platform=platform,
                    account_id=int(item.get("account_id") or 0),
                    directory_id=directory_id,
                    file_ids=tuple(item.get("file_ids") or ()),
                ),
                account_id=int(item.get("account_id") or 0),
            ))
        if links:
            self._cleanup_links(NetdiskResolveService(), links, task_hash)

    def cleanup_orphans(
        self,
        download_dirs: Sequence[str],
        active_paths: Sequence[str] = (),
        is_transferred: Optional[Callable[[str], bool]] = None,
        empty_grace_seconds: float = 24 * 60 * 60,
        nonempty_grace_seconds: float = 7 * 24 * 60 * 60,
        record_ttl_seconds: float = 30 * 24 * 60 * 60,
    ) -> Dict[str, int]:
        """
        清理孤儿下载目录、残留转存句柄与过期任务记录。

        只处理不在活动任务引用中的直接子目录：空目录按较短宽限期删除；非空目录
        必须全部文件都被 ``is_transferred`` 确认已整理后才删除。隐藏目录与回收站
        目录始终跳过。

        :param download_dirs: 下载根目录列表
        :param active_paths: 活动任务占用的目录
        :param is_transferred: 判断单个文件是否已整理的谓词；None 时只清空目录
        :param empty_grace_seconds: 空目录宽限期，单位秒
        :param nonempty_grace_seconds: 非空目录宽限期，单位秒
        :param record_ttl_seconds: 终态任务记录保留时长，单位秒
        :return: 各类清理数量统计
        """
        now = time.time()
        active = {str(Path(item).resolve()) for item in active_paths if item}
        stats = {
            "empty_removed": 0,
            "transferred_removed": 0,
            "records_removed": 0,
            "handles_cleaned": 0,
        }
        for root in download_dirs:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            try:
                children = list(root_path.iterdir())
            except OSError as error:
                logger.warning(f"扫描网盘下载目录失败：{root_path} - {error}")
                continue
            for child in children:
                if not child.is_dir() or child.is_symlink():
                    continue
                if child.name.startswith(".") or child.name in {"@Recycle", "#recycle", "@eaDir"}:
                    continue
                if str(child.resolve()) in active:
                    continue
                if not (child / _TASK_MARKER).exists():
                    # 没有任务标记的目录可能是用户手动放置的内容，定时清理不处理。
                    continue
                try:
                    age = now - child.stat().st_mtime
                    files = [
                        item for item in child.rglob("*")
                        if item.is_file() and not _is_sidecar_name(item.name)
                    ]
                except OSError:
                    continue
                if not files:
                    if age >= empty_grace_seconds and _remove_empty_dir(child):
                        stats["empty_removed"] += 1
                    continue
                if is_transferred is None or age < nonempty_grace_seconds:
                    continue
                if not all(is_transferred(str(item)) for item in files):
                    continue
                try:
                    shutil.rmtree(child)
                    stats["transferred_removed"] += 1
                except OSError as error:
                    logger.warning(f"删除已整理网盘目录失败：{child} - {error}")
        terminal_states = {"completed", "transferred", "error", "paused"}
        for record in store.all_tasks():
            task_hash = _as_text(record.get("hash"))
            if not task_hash or str(record.get("state") or "") not in terminal_states:
                continue
            if record.get("cleanup_handles"):
                self._cleanup_persisted_handles(task_hash, record)
                stats["handles_cleaned"] += 1
        stats["records_removed"] = store.prune_terminal_tasks(record_ttl_seconds)
        return stats

    def mark_reconciled(self, hashs: Union[str, list[str]],
                        downloader: Optional[str] = None) -> None:
        """
        标记失败任务已完成对账，避免定时任务重复回滚订阅事实。

        :param hashs: 任务标识或列表
        :param downloader: 下载器实例名称，网盘任务忽略
        """
        targets = [hashs] if isinstance(hashs, str) else list(hashs or [])
        for task_hash in targets:
            if store.get_task(task_hash):
                store.upsert_task(task_hash, {"reconciled": True})

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        整理完成后标记任务已整理。

        记录必须保留到 move 清理完成：`_can_delete_torrent` 依赖任务记录判断
        本地文件是否还在，提前删除会让下载目录永远不会被清理。任务列表在
        没有指定 hash 时隐藏 transferred，用户看不到已完成任务。

        :param hashs: 任务标识
        :param downloader: 下载器实例名称
        """
        targets = [hashs] if isinstance(hashs, str) else list(hashs or [])
        for task_hash in targets:
            if store.get_task(task_hash):
                store.upsert_task(task_hash, {"state": "transferred"})

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
            config = self._select_config(_as_text(record.get("downloader")) or None)
            tuning = self._instance_tuning(config, platform) if config is not None else None
            store.upsert_task(task_hash, {
                "state": "downloading", "error": None,
                "dlspeed": None, "dlspeed_bytes": 0,
            })
            thread = threading.Thread(
                target=self._run_task,
                args=(task_hash, platform, share_url, share_code,
                      _as_text(record.get("passcode")), Path(download_dir),
                      record.get("category"),
                      set(record.get("episodes") or []) or None,
                      _as_text(record.get("share_token")), tuning,
                      record.get("season"),
                      _normalize_episode_targets(record.get("episode_targets"))),
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
                    "state": "paused", "error": None,
                    "dlspeed": None, "dlspeed_bytes": 0,
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
