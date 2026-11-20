"""
网盘候选的文件树质量判定与平台过滤，与具体搜索来源无关。

CD 站点与 PanSou 站点共用同一套规则：平台是否可用、文件类型占比、标题是否
与目标类型相符、集数能否确定、是否短剧、压缩包是否主导、标题与文件名是否
冲突，并在通过时把真实大小、季号与集数写回候选。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.application.configuration import get_configured_system_config
from app.domain import episode as episode_rules
from app.domain.context import TorrentInfo
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

# 标题中已存在的标准季集标记，避免重复追加。
_STANDARD_EPISODE = re.compile(r"\bS\d{1,2}\s*E\d{1,4}(?:\s*-\s*E\d{1,4})?\b", re.IGNORECASE)
# 标题中已存在的中文集数标记，写回标准季集前先剥掉，避免「第197集 S01E197」重复。
_CN_EPISODE_MARK = re.compile(r"第\s*\d{1,4}\s*[集话話期]|\d{1,4}\s*[-~—至到]\s*\d{1,4}\s*[集话話期]")
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
    ".m2ts",
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
# 字幕、图片与刮削附属文件，既不参与集数也不参与类型占比。
_SUBTITLE_SUFFIXES = {
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".jpg", ".jpeg", ".png", ".webp", ".nfo",
}
# 广告与说明类文件：能证明「这不是正片」，但不能让候选因此被丢。
_JUNK_SUFFIXES = {
    ".txt", ".url", ".apk", ".mobi", ".html", ".htm", ".exe", ".doc", ".pdf",
}
# 媒体文件覆盖压缩包集数的下限；低于该比例视为压缩包主导资源。
_MEDIA_COVERAGE_THRESHOLD = 0.8
# 正片（或音频）占媒体总大小的下限，用来区分影视与有声书/音乐分享。
_TYPE_RATIO_THRESHOLD = 0.8
# 单集中位时长超过该值时按电影/合集处理，不再套短剧判据。
_LONG_FEATURE_SECONDS = 7200
# 没有时长信息时，平均单集低于该大小且集数偏多，按疑似有声书丢弃。
_SUSPECT_AUDIO_BOOK_EPISODES = 20
# 正片文件名里出现这些词说明不是正片，不能计入正片集合。
_NON_FEATURE_PATTERN = re.compile(
    r"花絮|预告|特典|番外|OST|主题曲|片头|片尾|sample|trailer|广告|公众号|更多资源|最新地址",
    re.IGNORECASE,
)
# 标题里出现这些词说明资源形态不是影视正片，直接丢弃。
_TITLE_AUDIO_BOOK_PATTERN = re.compile(
    r"有声小说|有声书|听书|广播剧|评书|相声|解说|朗读", re.IGNORECASE
)
# 标题里出现这些词说明是预告/花絮类资源，正片不足两条时丢弃。
_TITLE_TRAILER_PATTERN = re.compile(r"预告|花絮|片花|特辑|片段", re.IGNORECASE)
# 标题里出现这些词只打标记，不丢弃（用户可能就是要短剧或合集）。
_TITLE_FLAG_PATTERN = re.compile(r"短剧|合集", re.IGNORECASE)
# 蓝光原盘的目录特征。
_BLURAY_PATTERN = re.compile(r"BDMV[/\\]STREAM", re.IGNORECASE)
# 季号：第2季 / S02 / Season 2。
_SEASON_PATTERN = re.compile(
    r"(?:第\s*(\d{1,3})\s*季|[Ss](\d{1,3})(?!\d)|[Ss]eason\s*(\d{1,3}))", re.IGNORECASE
)
# 标题里与片名无关的噪声，抽片名前先剥掉。
_TITLE_NOISE_PATTERN = re.compile(
    r"第\s*\d{1,4}\s*[集话話期]|\d{1,4}\s*[-~—至到]\s*\d{1,4}\s*[集话話期]"
    r"|[Ss]\d{1,3}\s*[Ee]\d{1,4}(?:\s*-\s*[Ee]?\d{1,4})?"
    r"|\d{3,4}\s*[Pp]|\d{1,2}\s*[Kk]|更新至?|全集?|共|高清|蓝光|原盘|收藏版|国语|粤语|中字|双语|"
    r"WEB-?DL|BluRay|HDTV|REMUX|x26[45]|H\.?26[45]",
    re.IGNORECASE,
)
# 片名片段：连续两个以上的汉字。
_CJK_SEGMENT = re.compile(r"[\u4e00-\u9fff]{2,}")
# 文件名里的独立数字，两侧不能紧邻字母数字，避免把 4K、1080P 当成集数。
_BARE_NUMBER = re.compile(r"(?<![0-9A-Za-z])(\d{1,4})(?![0-9A-Za-z])")
# 合集判定：单个正片至少这么大（或这么久）才算一部独立作品。
_COLLECTION_MIN_MB = 500
_COLLECTION_MIN_SECONDS = 3000
# 标题片名至少这么长才参与「标题与文件名冲突」判定，避免泛称误杀。
_TITLE_NAME_MIN_CHARS = 4


@dataclass(frozen=True)
class FileGroups:
    """
    分享文件树按用途分类后的结果。

    :param feature: 正片候选（视频后缀且名字不像花絮/预告/广告）
    :param audio: 音频文件
    :param archive: 压缩包
    :param subtitle: 字幕、图片与刮削附属文件
    :param junk: 广告与说明类文件
    :param truncated: 文件树是否因数量上限被截断
    """

    feature: Tuple[Dict[str, Any], ...] = ()
    audio: Tuple[Dict[str, Any], ...] = ()
    archive: Tuple[Dict[str, Any], ...] = ()
    subtitle: Tuple[Dict[str, Any], ...] = ()
    junk: Tuple[Dict[str, Any], ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class EpisodeForm:
    """
    候选的集数形态判定结果。

    :param kind: series（多集包）、single（单集分享）、feature（单片电影）、collection（多部合集）
    :param season: 季号，无法识别时为 1
    :param episodes: 集号并集，单片与合集为空
    :param source: 集号来源，file/title/path/none
    """

    kind: str
    season: int
    episodes: Tuple[int, ...] = ()
    source: str = "none"

    @property
    def episode_count(self) -> int:
        """
        返回可确定的集数条数。

        :return: 集号数量
        """
        return len(self.episodes)


@dataclass
class _Sizes:
    """
    媒体分类的大小与数量统计。

    :param feature: 正片总大小（字节）
    :param audio: 音频总大小（字节）
    :param archive: 压缩包总大小（字节）
    """

    feature: int = 0
    audio: int = 0
    archive: int = 0


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
        if _entry_suffix(entry) in _VIDEO_EXTENSIONS:
            videos.append(entry)
    return videos


def media_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    只保留视频与音乐文件，压缩包不参与集数识别。

    :param files: 分享文件树中的文件条目
    :return: 媒体文件条目列表
    """
    return [entry for entry in files or [] if _entry_suffix(entry) in _MEDIA_SUFFIXES]


def archive_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    只保留压缩包文件，用于判断资源是否主要依赖压缩包。

    :param files: 分享文件树中的文件条目
    :return: 压缩包文件条目列表
    """
    return [entry for entry in files or [] if _entry_suffix(entry) in _ARCHIVE_SUFFIXES]


def classify_files(files: List[Dict[str, Any]]) -> FileGroups:
    """
    把分享文件树按用途分成五类，后续所有判据都基于分类结果。

    :param files: 分享文件树中的文件条目
    :return: 分类结果
    """
    feature: List[Dict[str, Any]] = []
    audio: List[Dict[str, Any]] = []
    archive: List[Dict[str, Any]] = []
    subtitle: List[Dict[str, Any]] = []
    junk: List[Dict[str, Any]] = []
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        suffix = _entry_suffix(entry)
        if suffix in _VIDEO_SUFFIXES:
            if _NON_FEATURE_PATTERN.search(_entry_name(entry)):
                junk.append(entry)
                continue
            feature.append(entry)
        elif suffix in _AUDIO_SUFFIXES:
            audio.append(entry)
        elif suffix in _ARCHIVE_SUFFIXES:
            archive.append(entry)
        elif suffix in _SUBTITLE_SUFFIXES:
            subtitle.append(entry)
        else:
            junk.append(entry)
    return FileGroups(
        feature=tuple(feature),
        audio=tuple(audio),
        archive=tuple(archive),
        subtitle=tuple(subtitle),
        junk=tuple(junk),
    )


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
        if episode_numbers(_entry_name(entry))
        or (allowed and episode_rules.windowed_episode_numbers(_entry_name(entry), allowed))
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
    return not episode_numbers(_entry_name(videos[0]))


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


def type_ratio_ok(groups: FileGroups, is_music: bool = False) -> bool:
    """
    判断媒体类型占比是否达标：影视看正片，音乐看音频，都按大小算。

    没有大小信息时退回按文件个数计算，避免平台不给大小时整批候选被误杀。

    :param groups: 文件分类结果
    :param is_music: 目标是否为音乐
    :return: 占比达标返回 True
    """
    sizes = _sizes_of(groups)
    if is_music:
        total = sizes.audio + sizes.feature
        if total > 0:
            return sizes.audio / total >= _TYPE_RATIO_THRESHOLD
        count = len(groups.audio) + len(groups.feature)
        return count > 0 and len(groups.audio) / count >= _TYPE_RATIO_THRESHOLD
    total = sizes.feature + sizes.audio + sizes.archive
    if total > 0:
        return sizes.feature / total >= _TYPE_RATIO_THRESHOLD
    count = len(groups.feature) + len(groups.audio) + len(groups.archive)
    return count > 0 and len(groups.feature) / count >= _TYPE_RATIO_THRESHOLD


def title_block_reason(title: str, groups: FileGroups) -> Optional[str]:
    """
    判断标题是否说明这不是影视正片。

    :param title: 候选标题
    :param groups: 文件分类结果
    :return: 丢弃原因；可以保留时返回 None
    """
    text = str(title or "")
    if not text:
        return None
    if _TITLE_AUDIO_BOOK_PATTERN.search(text):
        return "标题是音频内容"
    if _TITLE_TRAILER_PATTERN.search(text) and len(groups.feature) < 2:
        return "标题是预告花絮类"
    return None


def title_flags(title: str) -> List[str]:
    """
    提取标题里的标记词，只用于展示与排查，不参与去留。

    :param title: 候选标题
    :return: 标记列表
    """
    text = str(title or "")
    return sorted({matched.group(0) for matched in _TITLE_FLAG_PATTERN.finditer(text)})


def _coverage_ratio(
    features: List[Dict[str, Any]],
    numbers: List[List[int]],
    title: str,
) -> float:
    """
    计算集号可解析比例。

    严格解析不出集号时，若标题声明了集数上限（「更200集」「更新至218集」），
    把文件名里落在 1..上限 的数字也算作可解析：站点为规避网盘审核，常把部分
    集数写成 `140.4K.mp4` 或干脆不带集号，只按严格规则会把这些整包误杀。
    这些数字只参与「集数是否清晰」的判定，不写回候选集号，避免造假集数。

    :param features: 正片文件条目
    :param numbers: 每个正片文件严格解析出的集号
    :param title: 候选标题
    :return: 可解析比例（0-1）
    """
    if not features:
        return 0.0
    parsed = sum(1 for item in numbers if item)
    ratio = parsed / len(features)
    if ratio >= _EPISODE_COVERAGE_THRESHOLD:
        return ratio
    declared_max = max(episode_rules.title_episode_numbers(title) or [0])
    if declared_max <= 1:
        return ratio
    lenient = parsed
    for entry, item in zip(features, numbers):
        if item:
            continue
        if _has_number_within(_entry_name(entry), declared_max):
            lenient += 1
    return lenient / len(features)


def _has_number_within(name: str, upper: int) -> bool:
    """
    判断文件名里是否有落在 1..upper 的独立数字。

    :param name: 文件名
    :param upper: 集数上限
    :return: 命中返回 True
    """
    for matched in _BARE_NUMBER.finditer(str(name or "")):
        value = int(matched.group(1))
        if 0 < value <= upper and is_plausible_episode(value):
            return True
    return False


def episode_form(torrent: TorrentInfo, groups: FileGroups) -> Optional[EpisodeForm]:
    """
    判定候选属于哪种集数形态，并给出季号与集号。

    四种形态：多集包、单集分享（文件名或标题有集号）、单片电影、多部合集。
    正片数量为零或集号完全无法确定时返回 None，由调用方丢弃。

    :param torrent: 待判定的网盘候选
    :param groups: 文件分类结果
    :return: 集数形态；无法确定时返回 None
    """
    features = list(groups.feature)
    if not features:
        return None
    season = _season_of_entries(features) or 1
    windows = episode_rules.declared_episode_window(str(torrent.title or ""))
    window: Set[int] = set()
    for numbers in windows.values():
        window.update(numbers)
    numbers = [_entry_episodes(entry, window) for entry in features]
    if features and all(_is_bluray(entry) for entry in features):
        # 蓝光原盘：文件全是 00001.m2ts 这类命名，不能按集数判断。
        return EpisodeForm("feature", season, (), "path")
    if len(features) >= 2:
        if _coverage_ratio(features, numbers, str(torrent.title or "")) >= _EPISODE_COVERAGE_THRESHOLD:
            union = sorted({number for item in numbers for number in item})
            return EpisodeForm("series", season, tuple(union), "file")
        if _looks_like_collection(features):
            return EpisodeForm("collection", season, (), "file")
        return None
    single = numbers[0]
    if single:
        return EpisodeForm("single", season, tuple(sorted(set(single))), "file")
    title_numbers = episode_rules.title_episode_numbers(str(torrent.title or ""))
    if title_numbers:
        return EpisodeForm("single", season, tuple(title_numbers), "title")
    if _is_standalone_feature(features[0]):
        return EpisodeForm("feature", season, (), "none")
    return None


def apply_share_detail(
    torrent: TorrentInfo,
    detail: Dict[str, Any],
    files: List[Dict[str, Any]],
    form: Optional[EpisodeForm] = None,
    groups: Optional[FileGroups] = None,
) -> None:
    """
    用分享详情补齐候选大小，并用文件树的真实集数覆盖标题推断值。

    原始标题在改写前另存到 `raw_title`，避免下游再拿不到频道文案；
    集号只从正片文件名推导，音频文件不参与，避免有声小说把标题改错。

    :param torrent: 待补齐的网盘候选
    :param detail: share_detail 返回的分享详情
    :param files: 分享文件树中的文件条目
    :param form: 已算好的集数形态，为空时按视频文件重新推导
    :param groups: 已算好的文件分类，为空时重新分类
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
    raw_title = str(torrent.title or "")
    if not getattr(torrent, "raw_title", None):
        torrent.raw_title = raw_title
    grouped = groups if groups is not None else classify_files(files)
    resolved = form if form is not None else None
    if resolved is None:
        resolved = episode_form(torrent, grouped)
    if resolved is None:
        return
    torrent.season = resolved.season
    torrent.episodes = list(resolved.episodes)
    torrent.episode_source = resolved.source
    torrent.episode_kind = resolved.kind
    flags = title_flags(raw_title)
    if resolved.kind == "collection" and "合集" not in flags:
        flags.append("合集")
    if flags:
        torrent.quality_flags = flags
    token = _form_token(resolved)
    if not token:
        return
    title = _CN_EPISODE_MARK.sub(" ", _STANDARD_EPISODE.sub(" ", raw_title))
    torrent.title = " ".join(f"{title} {token}".split())


def passes_quality(
    torrent: TorrentInfo,
    detail: Dict[str, Any],
    config_getter: Optional[Callable[[], Any]] = None,
    media_type: Optional[Any] = None,
) -> bool:
    """
    判断候选是否通过质量检查。

    判定顺序固定：文件树完整 → 类型占比 → 标题形态 → 集数形态 → 时长大小
    → 压缩包主导 → 命名一致性。任一环节不通过即丢弃；通过时把真实大小、
    季号与集数写回候选。

    :param torrent: 待检查的网盘候选
    :param detail: 分享详情，含 `_files` 文件树
    :param config_getter: 读取系统配置的入口；为空时使用宿主默认实现
    :param media_type: 目标媒体类型，MUSIC 时只按音频占比判定
    :return: 通过返回 True
    """
    files = detail.get("_files")
    if not isinstance(files, list):
        # 拿不到完整文件树：丢弃，避免展示错误集数。
        return False
    groups = classify_files(files)
    is_music = _is_music_type(media_type)
    if not type_ratio_ok(groups, is_music):
        sizes = _sizes_of(groups)
        logger.info(
            f"{torrent.title} 内容类型不符，已丢弃"
            f"（正片 {sizes.feature // 1024 // 1024}MB，音频 {sizes.audio // 1024 // 1024}MB，"
            f"压缩包 {sizes.archive // 1024 // 1024}MB）"
        )
        return False
    if is_music:
        apply_share_detail(torrent, detail, files, groups=groups)
        return True
    blocked = title_block_reason(str(torrent.title or ""), groups)
    if blocked:
        logger.info(f"{torrent.title} {blocked}，已丢弃")
        return False
    form = episode_form(torrent, groups)
    if form is None:
        parsed = sum(1 for entry in groups.feature if episode_numbers(_entry_name(entry)))
        logger.info(
            f"{torrent.title} 集数不清晰，已丢弃"
            f"（正片 {len(groups.feature)} 个，可解析 {parsed} 个）"
        )
        return False
    if _too_short(torrent, groups, form, config_getter):
        return False
    if _archive_dominant(groups):
        logger.info(f"{torrent.title} 压缩包资源暂不支持，已丢弃")
        return False
    if _title_conflict(str(torrent.title or ""), groups):
        logger.info(f"{torrent.title} 标题与文件名指向不同作品，已丢弃")
        return False
    apply_share_detail(torrent, detail, files, form=form, groups=groups)
    return True


def _entry_name(entry: Dict[str, Any]) -> str:
    """
    取文件条目里用于判定的名称。

    :param entry: 文件条目
    :return: 文件名
    """
    return str(entry.get("name") or "")


def _entry_path(entry: Dict[str, Any]) -> str:
    """
    取文件条目的相对路径，缺失时退回文件名。

    :param entry: 文件条目
    :return: 相对路径
    """
    return str(entry.get("path") or entry.get("name") or "")


def _entry_suffix(entry: Dict[str, Any]) -> str:
    """
    取文件后缀（含点、小写）。

    :param entry: 文件条目
    :return: 后缀
    """
    name = _entry_name(entry)
    return Path(name).suffix.lower() if "." in name else ""


def _entry_size(entry: Dict[str, Any]) -> int:
    """
    取文件大小（字节）。

    :param entry: 文件条目
    :return: 字节数
    """
    try:
        return max(0, int(entry.get("size") or 0))
    except (TypeError, ValueError):
        return 0


def _entry_duration(entry: Dict[str, Any]) -> int:
    """
    取视频时长（秒）。

    :param entry: 文件条目
    :return: 秒数
    """
    try:
        return max(0, int(entry.get("duration") or 0))
    except (TypeError, ValueError):
        return 0


def _entry_episodes(entry: Dict[str, Any], window: Set[int]) -> List[int]:
    """
    取单个正片文件的集号，通用规则优先，标题声明窗口兜底。

    :param entry: 文件条目
    :param window: 标题声明的集号窗口
    :return: 集号列表
    """
    name = _entry_name(entry)
    numbers = episode_numbers(name)
    if numbers:
        return numbers
    if window:
        return episode_rules.windowed_episode_numbers(name, window)
    return []


def _sizes_of(groups: FileGroups) -> _Sizes:
    """
    汇总三类媒体的总大小。

    :param groups: 文件分类结果
    :return: 大小统计
    """
    return _Sizes(
        feature=sum(_entry_size(entry) for entry in groups.feature),
        audio=sum(_entry_size(entry) for entry in groups.audio),
        archive=sum(_entry_size(entry) for entry in groups.archive),
    )


def _is_music_type(media_type: Optional[Any]) -> bool:
    """
    判断目标媒体类型是否为音乐。

    :param media_type: 媒体类型或类型文本
    :return: 音乐返回 True
    """
    if media_type is None:
        return False
    value = getattr(media_type, "value", media_type)
    return str(value or "").strip().lower() in {"music", "音乐"}


def _season_of_entries(entries: List[Dict[str, Any]]) -> Optional[int]:
    """
    从正片文件的相对路径里取季号，文件名优先，其次目录名。

    :param entries: 正片文件条目
    :return: 季号；无法识别时返回 None
    """
    for entry in entries:
        matched = _SEASON_PATTERN.search(_entry_path(entry))
        if not matched:
            continue
        value = matched.group(1) or matched.group(2) or matched.group(3)
        if value and 0 < int(value) <= 99:
            return int(value)
    return None


def _is_bluray(entry: Dict[str, Any]) -> bool:
    """
    判断文件是否位于蓝光原盘的 STREAM 目录下。

    :param entry: 文件条目
    :return: 是原盘片段返回 True
    """
    return bool(_BLURAY_PATTERN.search(_entry_path(entry)))


def _is_standalone_feature(entry: Dict[str, Any]) -> bool:
    """
    判断单个正片是否够格当一部电影。

    拿不到时长时按证据不足放行：除夸克外所有平台都不给 duration，用大小
    兜底会把正常资源误杀，短剧另有单集中位时长判据兜住。

    :param entry: 正片文件条目
    :return: 够格返回 True
    """
    duration = _entry_duration(entry)
    if duration > 0:
        return duration >= _DEFAULT_SHORT_DRAMA_SECONDS
    return True


def _looks_like_collection(features: List[Dict[str, Any]]) -> bool:
    """
    判断多文件分享是否是多部独立作品的合集。

    每个正片都得像一整部作品，并且彼此能区分开（年份不同或片名片段不同），
    否则更像是无法识别集号的零散文件，按集数不清晰丢弃。

    :param features: 正片文件条目
    :return: 像合集返回 True
    """
    if len(features) < 2:
        return False
    for entry in features:
        duration = _entry_duration(entry)
        if duration > 0:
            if duration < _COLLECTION_MIN_SECONDS:
                return False
            continue
        if _entry_size(entry) < _COLLECTION_MIN_MB * 1024 * 1024:
            return False
    years = []
    segments = []
    for entry in features:
        name = _entry_name(entry)
        matched = re.search(r"(?:19|20)\d{2}", name)
        years.append(matched.group(0) if matched else "")
        found = _CJK_SEGMENT.findall(_TITLE_NOISE_PATTERN.sub(" ", name))
        segments.append(max(found, key=len) if found else "")
    if all(years) and len(set(years)) == len(years):
        return True
    return bool(all(segments)) and len(set(segments)) == len(segments)


def _too_short(
    torrent: TorrentInfo,
    groups: FileGroups,
    form: EpisodeForm,
    config_getter: Optional[Callable[[], Any]],
) -> bool:
    """
    判断正片是否短得像预告、短剧或有声书。

    有单集时长时按中位时长判；拿不到时长（除夸克外所有平台）时，用
    「平均单集偏小且集数偏多」作为疑似有声书的替代判据。

    :param torrent: 待判定的网盘候选
    :param groups: 文件分类结果
    :param form: 集数形态
    :param config_getter: 读取系统配置的入口
    :return: 需要丢弃返回 True
    """
    duration_limit, size_limit = quality_limits(config_getter)
    durations = [_entry_duration(entry) for entry in groups.feature]
    durations = [value for value in durations if value > 0]
    if durations:
        median = statistics.median(durations)
        if median > _LONG_FEATURE_SECONDS:
            return False
        if median < duration_limit:
            logger.info(f"{torrent.title} 单集中位时长 {median:.0f} 秒过短，已丢弃")
            return True
        return False
    sizes = [_entry_size(entry) for entry in groups.feature]
    sizes = [value for value in sizes if value > 0]
    if not sizes or form.episode_count < _SUSPECT_AUDIO_BOOK_EPISODES:
        return False
    average_mb = sum(sizes) / len(sizes) / 1024 / 1024
    if average_mb < size_limit:
        logger.info(
            f"{torrent.title} 平均单集 {average_mb:.0f}MB 且 {form.episode_count} 集，疑似有声书，已丢弃"
        )
        return True
    return False


def _archive_dominant(groups: FileGroups) -> bool:
    """
    判断压缩包是否覆盖了大部分集数。

    :param groups: 文件分类结果
    :return: 压缩包主导返回 True
    """
    if not groups.archive:
        return False
    media_episodes: Set[int] = set()
    for entry in (*groups.feature, *groups.audio):
        media_episodes.update(episode_numbers(_entry_name(entry)))
    archive_episodes: Set[int] = set()
    for entry in groups.archive:
        archive_episodes.update(episode_numbers(_entry_name(entry)))
    if not archive_episodes:
        return False
    union = media_episodes | archive_episodes
    coverage = len(media_episodes) / len(union) if union else 1.0
    return coverage < _MEDIA_COVERAGE_THRESHOLD


def _title_name(title: str) -> str:
    """
    从标题里抽出最长的中文片名片段。

    :param title: 候选标题
    :return: 片名片段；抽不出时为空字符串
    """
    cleaned = _TITLE_NOISE_PATTERN.sub(" ", str(title or ""))
    segments = _CJK_SEGMENT.findall(cleaned)
    return max(segments, key=len) if segments else ""


def _title_conflict(title: str, groups: FileGroups) -> bool:
    """
    判断标题片名与正片文件名里的片名是否完全对不上。

    只在两边都有足够长的片名、且互不包含时判为冲突；片名太短（如「好资源」
    「仙逆」这类泛称）一律放行，避免把频道文案式标题误杀。文件一侧取相对路径
    的全部片段，分享根目录通常就是片名，能救回「按篇章命名」的正常整包；并且
    要求这个陌生片名覆盖六成以上的正片，单个花絮或杂项文件名不足以否掉整包。

    :param title: 候选标题
    :param groups: 文件分类结果
    :return: 冲突返回 True
    """
    name = _title_name(title)
    if len(name) < _TITLE_NAME_MIN_CHARS:
        return False
    sample = groups.feature[:20]
    counts: Dict[str, int] = {}
    for entry in sample:
        for segment in set(_CJK_SEGMENT.findall(_entry_path(entry))):
            if len(segment) >= 3:
                counts[segment] = counts.get(segment, 0) + 1
    if not counts:
        return False
    if any(segment in name or name in segment for segment in counts):
        return False
    majority = max(2, len(sample) * 0.6)
    return any(count >= majority for count in counts.values())


def _form_token(form: EpisodeForm) -> Optional[str]:
    """
    把集数形态拼成标题里的标准季集标记。

    :param form: 集数形态
    :return: 形如 S01E197 或 S01E01-E24 的标记；单片与合集返回 None
    """
    if form.kind not in {"series", "single"} or not form.episodes:
        return None
    begin, end = min(form.episodes), max(form.episodes)
    prefix = f"S{form.season:02d}"
    if begin == end:
        return f"{prefix}E{begin:02d}"
    return f"{prefix}E{begin:02d}-E{end:02d}"
