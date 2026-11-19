"""
网盘候选的文件树质量判定与平台过滤，与具体搜索来源无关。

CD 站点与 PanSou 站点共用同一套规则：平台是否可用、集数是否清晰、是否单片、
是否短剧、压缩包是否主导，并在通过时把真实大小与集数写回候选。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.application.configuration import get_configured_system_config
from app.domain import episode as episode_rules
from app.domain.context import TorrentInfo
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

# 标题中已存在的标准季集标记，避免重复追加。
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


def platform_supported(platform: str) -> bool:
    """
    判断平台是否在搜索结果中保留。

    已注册取链适配器的平台可以正常下载；没有适配器但已知的网盘平台
    （如天翼云盘）同样保留候选并如实展示，下载阶段再明确提示不支持。

    :param platform: 平台标识
    :return: 需要保留返回 True
    """
    from app.application.netdisk.registry import find_adapter, registered_platforms
    from app.domain.netdisk import DISPLAY_ONLY_PLATFORMS, normalize_platform

    if platform in DISPLAY_ONLY_PLATFORMS:
        return True
    if find_adapter(platform) is not None:
        return True
    if registered_platforms():
        return False
    return normalize_platform(platform) is not None


def platform_enabled(platform: str) -> bool:
    """
    判断平台是否在系统设置启用的网盘平台集合中。

    无法归一的平台不参与开关判断，交由 `platform_supported` 决定去留。

    :param platform: 平台标识
    :return: 需要保留返回 True
    """
    from app.application.netdisk.settings import enabled_platforms
    from app.domain.netdisk import normalize_platform

    normalized = normalize_platform(platform)
    if normalized is None:
        return True
    return normalized in enabled_platforms()


def episode_numbers(file_name: str) -> List[int]:
    """
    从一个文件名中提取集数，规则由 app.domain.episode 统一维护。

    :param file_name: 分享内的文件名或文件夹名
    :return: 该文件名包含的集数列表；没有可识别集数时为空
    """
    return episode_rules.episode_numbers(file_name)


def is_plausible_episode(value: int) -> bool:
    """
    判断一个数字是否可能是集数，排除分辨率与年份。

    :param value: 待判断的数字
    :return: 可能是集数返回 True
    """
    return episode_rules.is_plausible_episode(value)


def bare_episode(file_name: str) -> Optional[int]:
    """
    从文件名中提取裸数字集数，只在能明确判定时返回。

    :param file_name: 分享内的文件名
    :return: 集数；无法明确判定时为空
    """
    return episode_rules.bare_episode(file_name)


def episode_token(file_names: List[str]) -> Optional[str]:
    """
    从分享文件名推导季集标记，使用最小与最大集数构成连续区间。

    :param file_names: 分享内的真实文件名列表
    :return: 形如 S01E12 或 S01E153-E162 的标记；无法推导时为空
    """
    return episode_rules.episode_token(file_names)


def video_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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


def media_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    只保留视频与音乐文件，压缩包不参与集数识别。

    :param files: 分享文件树中的文件条目
    :return: 媒体文件条目列表
    """
    return [
        entry for entry in files or []
        if Path(str(entry.get("name") or "")).suffix.lower() in _MEDIA_SUFFIXES
    ]


def archive_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    只保留压缩包文件，用于判断资源是否主要依赖压缩包。

    :param files: 分享文件树中的文件条目
    :return: 压缩包文件条目列表
    """
    return [
        entry for entry in files or []
        if Path(str(entry.get("name") or "")).suffix.lower() in _ARCHIVE_SUFFIXES
    ]


def is_episode_clear(videos: List[Dict[str, Any]], window: Optional[Set[int]] = None) -> bool:
    """
    判断文件树的集数是否清晰：能解析出集数的视频占比达到下限。

    给出集数窗口时（来自候选标题声明的季集范围），只标集数的文件名
    （如 `140.4K.mp4`）也算可解析，避免站点为规避网盘审核的命名被误杀。

    :param videos: 视频文件条目
    :param window: 集数窗口；为空时只按通用规则判定
    :return: 集数清晰返回 True
    """
    if not videos:
        return False
    allowed = set(window or set())
    parsed = [
        entry for entry in videos
        if episode_numbers(entry.get("name") or "")
        or (allowed and episode_rules.windowed_episode_numbers(entry.get("name") or "", allowed))
    ]
    return len(parsed) / len(videos) >= _EPISODE_COVERAGE_THRESHOLD


def is_feature_share(videos: List[Dict[str, Any]]) -> bool:
    """
    判断是否为单片（电影）分享：只有一个视频文件且解析不出集数。

    电影分享的文件树就是「一个正片视频 + 字幕/NFO」，没有集数可解析；
    剧集分享即使单文件也带集数标记，多文件时仍走集数清晰度判定。

    :param videos: 视频文件条目
    :return: 是单片分享返回 True
    """
    if len(videos) != 1:
        return False
    return not episode_numbers(str(videos[0].get("name") or ""))


def quality_limits(config_getter: Optional[Callable[[], Any]] = None) -> Tuple[int, int]:
    """
    读取短剧判定的两个阈值，未配置或非法时使用默认值。

    :param config_getter: 读取系统配置的入口；为空时使用宿主默认实现，便于调用方替换配置源
    :return: 平均每集时长上限（秒）与平均每集大小上限（MB）
    """
    duration_limit = _DEFAULT_SHORT_DRAMA_SECONDS
    size_limit = _DEFAULT_SHORT_DRAMA_MB
    try:
        config = (config_getter or get_configured_system_config)()
        duration = config.get(SystemConfigKey.NetdiskShortDramaMaxDuration)
        size = config.get(SystemConfigKey.NetdiskShortDramaMaxSize)
        if isinstance(duration, (int, float)) and duration > 0:
            duration_limit = int(duration)
        if isinstance(size, (int, float)) and size > 0:
            size_limit = int(size)
    except Exception:  # noqa: BLE001
        pass
    return duration_limit, size_limit


def is_short_drama(videos: List[Dict[str, Any]], duration_limit: int, size_limit: int) -> bool:
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


def apply_share_detail(torrent: TorrentInfo, detail: Dict[str, Any], files: List[Dict[str, Any]]) -> None:
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
    token = episode_token(media_names)
    if not token:
        return
    title = _STANDARD_EPISODE.sub(" ", torrent.title or "")
    torrent.title = " ".join(f"{title} {token}".split())


def passes_quality(
    torrent: TorrentInfo,
    detail: Dict[str, Any],
    config_getter: Optional[Callable[[], Any]] = None,
) -> bool:
    """
    判断候选是否通过质量检查：文件树完整、媒体集数清晰、不是短剧、压缩包不主导。

    集数识别只使用视频与音乐文件；压缩包名称不参与集数映射。压缩包覆盖的集数
    占主导、媒体文件覆盖不足时，候选应丢弃，避免误报「符合条件」。

    检查通过时把真实大小与集数写回候选；不通过时候选应被丢弃。

    :param torrent: 待检查的网盘候选
    :param detail: 分享详情，含 `_files` 文件树
    :param config_getter: 读取系统配置的入口；为空时使用宿主默认实现
    :return: 通过返回 True
    """
    files = detail.get("_files")
    if not isinstance(files, list):
        # 拿不到完整文件树：丢弃，避免展示错误集数。
        return False
    videos = video_files(files)
    media_entries = media_files(files)
    clarity_files = videos or media_entries
    windows = episode_rules.declared_episode_window(str(torrent.title or ""))
    window: Set[int] = set()
    for numbers in windows.values():
        window.update(numbers)
    if not is_episode_clear(clarity_files, window) and not is_feature_share(videos):
        # 集数不清晰：丢弃，避免展示错误集数；单片（电影）分享没有集数，不属于此类。
        parsed = sum(1 for entry in clarity_files if episode_numbers(str(entry.get("name") or "")))
        logger.info(
            f"{torrent.title} 集数不清晰，已丢弃"
            f"（视频 {len(videos)} 个，可解析 {parsed} 个）"
        )
        return False
    media_episodes: Set[int] = set()
    for entry in media_entries:
        media_episodes.update(episode_numbers(entry.get("name") or ""))
    archive_episodes: Set[int] = set()
    for entry in archive_files(files):
        archive_episodes.update(episode_numbers(entry.get("name") or ""))
    if archive_episodes:
        union = media_episodes | archive_episodes
        coverage = len(media_episodes) / len(union) if union else 1.0
        if coverage < _MEDIA_COVERAGE_THRESHOLD:
            logger.info(
                f"{torrent.title} 压缩包资源暂不支持，已丢弃"
                f"（媒体覆盖 {len(media_episodes)}/{len(union)} 集）"
            )
            return False
    duration_limit, size_limit = quality_limits(config_getter)
    if is_short_drama(videos, duration_limit, size_limit):
        # 短剧判定依赖可调阈值，结论不写入缓存，阈值调整后下次搜索即可生效。
        return False
    apply_share_detail(torrent, detail, files)
    return True
