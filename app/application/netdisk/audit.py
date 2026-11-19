"""
网盘分享体检：识别分享内的媒体、盘点内容、与媒体库比对缺口。

体检只读：不转存、不下载、不写盘。结果按媒体分组，组内给出分享含有的季集、
媒体库已有集数与仍缺的集号。媒体库证据缺失时一律标「未确认」，不得判为缺失，
否则用户会重复下载已有的集；媒体识别失败的分组保留标题与体量，交给用户手动指定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol, Sequence

from app.domain.episode import episode_numbers, is_special_name
from app.domain.metainfo import MetaInfoPath
from app.domain.netdisk import ShareFile, is_video_file_name
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.types import MediaType

# 单次体检最多处理的视频文件数，超出即截断并标记报告不完整。
_MAX_FILES = 5000
# 无法从路径或文件名判定季号时使用的默认季。
_DEFAULT_SEASON = 1
# 路径中的显式季号写法：Season 2 / 第二季 / S02。
_SEASON_IN_PATH = re.compile(
    r"[Ss]eason\s*(\d{1,3})|第\s*(\d{1,3})\s*季|(?<![0-9A-Za-z])[Ss](\d{1,3})(?![0-9A-Za-z])"
)
# 归一化标题时只保留字母、数字与汉字，抹平空格与分隔符差异。
_TITLE_NOISE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
# 报告里「媒体库是否有这部作品」的三种取值。
LIBRARY_PRESENT = "present"
LIBRARY_ABSENT = "absent"
LIBRARY_UNKNOWN = "unknown"
# 报告里单集状态的取值。
EPISODE_PRESENT = "present"
EPISODE_MISSING = "missing"
EPISODE_SHARE_ABSENT = "share_absent"
EPISODE_UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class ShareFileGroup:
    """
    分享内的一个媒体候选分组。

    :param key: 分组键，标题与年份归一化后的组合
    :param title: 组内识别出的媒体标题，无法识别时为空字符串
    :param year: 组内识别出的发行年份，无法识别时为空字符串
    :param sample_path: 用于媒体识别的代表路径，取组内第一个文件
    :param files: 组内视频文件
    :param total_bytes: 组内视频文件总字节数
    :param seasons: 从文件名与路径解析出的季号到集号集合
    :param mtype: 组内推断的媒体类型，取值为电影、电视剧或空字符串
    """

    key: str
    title: str
    year: str
    sample_path: str
    files: tuple[ShareFile, ...]
    total_bytes: int
    seasons: tuple[tuple[int, tuple[int, ...]], ...]
    mtype: str


@dataclass(frozen=True, slots=True)
class RecognizedMedia:
    """
    识别器产出的统一媒体身份，由调用方注入。

    :param media_source: 统一媒体身份的数据来源
    :param media_id: 对应来源的原生媒体标识
    :param title: 识别出的媒体标题
    :param year: 识别出的发行年份
    :param mtype: 媒体类型，取值为电影或电视剧
    :param aired_seasons: 已播出的季号到集号集合，用于计算缺集的分母
    """

    media_source: str
    media_id: str
    title: str
    year: str
    mtype: str
    aired_seasons: tuple[tuple[int, tuple[int, ...]], ...]


class MediaLibraryProbe(Protocol):
    """
    媒体库已有内容的只读探针，由调用方注入具体实现。
    """

    async def has_item(self, media: RecognizedMedia) -> bool:
        """
        判断媒体库缓存里是否存在该作品。

        :param media: 已识别的媒体身份
        :return: 存在返回 True；未同步或无法确认返回 False
        """
        ...

    async def season_episodes(self, media: RecognizedMedia, season: int) -> set[int]:
        """
        返回媒体库缓存里该作品某季已入库的集号集合。

        :param media: 已识别的媒体身份
        :param season: 目标季号
        :return: 集号集合；未命中或季集快照缺失时为空集合
        """
        ...


@dataclass(frozen=True, slots=True)
class AuditedMedia:
    """
    一个媒体分组的体检结果。

    :param title: 媒体标题，无法识别时取分组标题
    :param year: 发行年份，无法识别时为空字符串
    :param mtype: 媒体类型，取值为电影、电视剧或空字符串
    :param recognized: 是否成功识别到媒体身份
    :param media_source: 统一媒体身份的数据来源
    :param media_id: 对应来源的原生媒体标识
    :param files: 组内视频文件数
    :param total_bytes: 组内视频文件总字节数
    :param seasons: 分享内解析出的季号到集号集合
    :param episode_states: 按季号、集号升序排列的（季号, 集号, 状态）序列，电影为空
    :param missing_episodes: 已播出、媒体库没有且分享内可补的集号（跨季合并）
    :param existing_episodes: 已播出且媒体库已有的集号（跨季合并）
    :param season_missing: 按季号给出的缺集号，供前端只填当前季
    :param season_existing: 按季号给出的库内已有集号
    :param library_evidence: 媒体库证据来源，取值为 media_server 或 none
    :param library_state: 整部作品的存在性，取值为 present、absent 或 unknown
    :param truncated: 本次体检是否因文件数上限被截断
    """

    title: str
    year: str
    mtype: str
    recognized: bool
    media_source: str
    media_id: str
    files: int
    total_bytes: int
    seasons: tuple[tuple[int, tuple[int, ...]], ...]
    episode_states: tuple[tuple[int, int, str], ...]
    missing_episodes: tuple[int, ...]
    existing_episodes: tuple[int, ...]
    season_missing: tuple[tuple[int, tuple[int, ...]], ...]
    season_existing: tuple[tuple[int, tuple[int, ...]], ...]
    library_evidence: str
    library_state: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class ShareAuditReport:
    """
    一次分享体检的完整结果。

    :param share_title: 分享标题
    :param platform: 分享所在网盘平台标识
    :param file_count: 参与体检的视频文件数
    :param total_bytes: 参与体检的视频文件总字节数
    :param truncated: 是否因文件数上限被截断
    :param media: 按分组顺序排列的媒体体检结果
    :param unrecognized_files: 无法归入任何媒体分组的视频文件数
    """

    share_title: str
    platform: str
    file_count: int
    total_bytes: int
    truncated: bool
    media: tuple[AuditedMedia, ...]
    unrecognized_files: int


def _normalize(value: object) -> str:
    """
    把标题归一化为可比较的紧凑文本。

    :param value: 原始标题
    :return: 去掉空白与分隔符的小写文本
    """
    return _TITLE_NOISE.sub("", str(value or "").casefold())


def _identity_group_index(
    groups: Sequence[ShareFileGroup],
    identity: Optional[RecognizedMedia],
) -> Optional[int]:
    """
    选出应当套用调用方给定身份的分组。

    优先按标题归一化后相等匹配；没有匹配时取体量最大的分组，保证「用户点的这个
    资源就是这部剧」的语义成立，同时不把合集里的其它作品也算成同一部。

    :param groups: 分享文件分组
    :param identity: 调用方给定的媒体身份
    :return: 分组下标；没有分组或没有身份时为 None
    """
    if identity is None or not groups:
        return None
    wanted = _normalize(identity.title)
    if wanted:
        for index, group in enumerate(groups):
            if _normalize(group.title) == wanted:
                return index
    return max(range(len(groups)), key=lambda item: groups[item].total_bytes)


def _season_from_path(path: str) -> Optional[int]:
    """
    从文件相对路径的目录部分提取显式季号。

    :param path: 分享内相对路径，形如 ``剧名/Season 1/1080p/E01.mkv``
    :return: 最后一个可识别的季号；路径中没有季号时返回 None
    """
    matched: Optional[int] = None
    for part in Path(str(path or "")).parts[:-1]:
        found = _SEASON_IN_PATH.search(part)
        if not found:
            continue
        for group in found.groups():
            if group:
                matched = int(group)
                break
    return matched


def _top_directory(path: str) -> str:
    """
    取分享内相对路径的一级目录名，用于无法识别标题时的兜底分组。

    :param path: 分享内相对路径
    :return: 一级目录名；路径只有文件名时为空字符串
    """
    parts = Path(str(path or "")).parts
    return parts[0] if len(parts) > 1 else ""


def _infer_media_type(
    kinds: Sequence[str],
    seasons: Sequence[tuple[int, tuple[int, ...]]],
) -> str:
    """
    推断分组是电影还是电视剧。

    :param kinds: 组内每个文件单独解析出的媒体类型，无法判断时为空字符串
    :param seasons: 组内解析出的季集
    :return: 电影、电视剧或空字符串（无法判断）
    """
    votes_tv = sum(1 for kind in kinds if kind == MediaType.TV.value)
    votes_movie = sum(1 for kind in kinds if kind == MediaType.MOVIE.value)
    if any(numbers for _season, numbers in seasons):
        votes_tv += 1
    if votes_tv and votes_tv >= votes_movie:
        return MediaType.TV.value
    if votes_movie:
        return MediaType.MOVIE.value
    return ""


def group_share_files(files: Sequence[ShareFile]) -> tuple[ShareFileGroup, ...]:
    """
    把分享文件按媒体分组，并盘点每组含有的季集与体量。

    先按识别出的标题与年份分组；识别不出标题时按一级目录名兜底，再不行才归入
    「未识别」组。多部电影合集与多剧合集因此不会被合并成同一部作品的多集。

    :param files: 分享内递归展开的文件列表
    :return: 按首次出现顺序排列的分组
    """
    buckets: dict[str, list[tuple[ShareFile, str]]] = {}
    order: list[str] = []
    for item in files:
        if item.is_dir or not is_video_file_name(item.name) or is_special_name(item.name):
            continue
        try:
            meta = MetaInfoPath(Path(item.path or item.name))
        except Exception as error:  # noqa: BLE001 - 单个文件名解析失败不影响分组
            logger.debug(f"分享体检跳过无法解析的文件名 {item.name}：{type(error).__name__}")
            meta = None
        title = str(getattr(meta, "name", "") or "").strip() if meta else ""
        year = str(getattr(meta, "year", "") or "").strip() if meta else ""
        kind = ""
        if meta is not None and meta.type in (MediaType.TV, MediaType.MOVIE):
            kind = meta.type.value
        if title:
            key = f"{_normalize(title)}|{year}"
        else:
            top = _top_directory(item.path)
            key = f"dir:{_normalize(top)}" if top else "unrecognized"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((item, kind))

    groups: list[ShareFileGroup] = []
    for key in order:
        members = buckets[key]
        seasons: dict[int, set[int]] = {}
        for item, _kind in members:
            season = _season_from_path(item.path) or _DEFAULT_SEASON
            numbers = set(episode_numbers(item.name))
            seasons.setdefault(season, set()).update(numbers)
        first = members[0][0]
        meta = None
        try:
            meta = MetaInfoPath(Path(first.path or first.name))
        except Exception:  # noqa: BLE001 - 代表路径解析失败时退化为空标题
            meta = None
        title = str(getattr(meta, "name", "") or "").strip() if meta else ""
        year = str(getattr(meta, "year", "") or "").strip() if meta else ""
        frozen_seasons = tuple(
            (season, tuple(sorted(numbers))) for season, numbers in sorted(seasons.items())
        )
        groups.append(
            ShareFileGroup(
                key=key,
                title=title or _top_directory(first.path),
                year=year,
                sample_path=first.path or first.name,
                files=tuple(item for item, _kind in members),
                total_bytes=sum(
                    max(int(getattr(item, "size", 0) or 0), 0) for item, _kind in members
                ),
                seasons=frozen_seasons,
                mtype=_infer_media_type([kind for _item, kind in members], frozen_seasons),
            )
        )
    return tuple(groups)


async def audit_group(
    group: ShareFileGroup,
    recognized: Optional[RecognizedMedia],
    *,
    probe: MediaLibraryProbe,
    truncated: bool = False,
    season: Optional[int] = None,
) -> AuditedMedia:
    """
    把一个分组与媒体库比对，得出逐集状态与缺失集。

    :param group: 文件分组
    :param recognized: 识别出的媒体身份；未识别时为 None
    :param probe: 媒体库只读探针
    :param truncated: 本次体检是否被截断
    :param season: 只比对该季；None 表示比对分享内全部季
    :return: 该分组的体检结果
    """
    if recognized is None:
        return AuditedMedia(
            title=group.title,
            year=group.year,
            mtype=group.mtype,
            recognized=False,
            media_source="",
            media_id="",
            files=len(group.files),
            total_bytes=group.total_bytes,
            seasons=group.seasons,
            episode_states=(),
            missing_episodes=(),
            existing_episodes=(),
            season_missing=(),
            season_existing=(),
            library_evidence="none",
            library_state=LIBRARY_UNKNOWN,
            truncated=truncated,
        )

    if recognized.mtype == MediaType.MOVIE.value:
        present = await probe.has_item(recognized)
        return AuditedMedia(
            title=recognized.title or group.title,
            year=recognized.year or group.year,
            mtype=MediaType.MOVIE.value,
            recognized=True,
            media_source=recognized.media_source,
            media_id=recognized.media_id,
            files=len(group.files),
            total_bytes=group.total_bytes,
            seasons=group.seasons,
            episode_states=(),
            missing_episodes=(),
            existing_episodes=(),
            season_missing=(),
            season_existing=(),
            library_evidence="media_server" if present else "none",
            library_state=LIBRARY_PRESENT if present else LIBRARY_UNKNOWN,
            truncated=truncated,
        )

    aired_map = dict(recognized.aired_seasons)
    states: dict[tuple[int, int], str] = {}
    missing_by_season: dict[int, set[int]] = {}
    existing_by_season: dict[int, set[int]] = {}
    evidence = "none"
    for group_season, share_numbers in group.seasons:
        if season is not None and group_season != season:
            continue
        share_set = set(share_numbers)
        if group_season not in aired_map:
            # 识别结果没有该季的已播出集：整季未开播或数据源不提供季集，
            # 不把分享里的集号当分母，否则会把未播出的集算成缺失。
            for number in share_numbers:
                states[(group_season, number)] = EPISODE_UNCONFIRMED
            continue
        aired = aired_map[group_season]
        library_numbers = await probe.season_episodes(recognized, group_season)
        if library_numbers:
            evidence = "media_server"
        for number in aired:
            if library_numbers:
                if number in library_numbers:
                    states[(group_season, number)] = EPISODE_PRESENT
                    existing_by_season.setdefault(group_season, set()).add(number)
                elif number in share_set:
                    states[(group_season, number)] = EPISODE_MISSING
                    missing_by_season.setdefault(group_season, set()).add(number)
                else:
                    states[(group_season, number)] = EPISODE_SHARE_ABSENT
            else:
                states[(group_season, number)] = EPISODE_UNCONFIRMED
    if evidence == "none":
        missing_by_season.clear()
        existing_by_season.clear()
    missing = set().union(*missing_by_season.values()) if missing_by_season else set()
    existing = set().union(*existing_by_season.values()) if existing_by_season else set()
    return AuditedMedia(
        title=recognized.title or group.title,
        year=recognized.year or group.year,
        mtype=MediaType.TV.value,
        recognized=True,
        media_source=recognized.media_source,
        media_id=recognized.media_id,
        files=len(group.files),
        total_bytes=group.total_bytes,
        seasons=group.seasons,
        episode_states=tuple(
            (season_number, episode, state)
            for (season_number, episode), state in sorted(states.items())
        ),
        missing_episodes=tuple(sorted(missing)),
        existing_episodes=tuple(sorted(existing)),
        season_missing=tuple(
            (season_number, tuple(sorted(numbers)))
            for season_number, numbers in sorted(missing_by_season.items())
        ),
        season_existing=tuple(
            (season_number, tuple(sorted(numbers)))
            for season_number, numbers in sorted(existing_by_season.items())
        ),
        library_evidence=evidence,
        library_state=(
            LIBRARY_PRESENT
            if evidence != "none"
            else LIBRARY_UNKNOWN
        ),
        truncated=truncated,
    )


class ShareAuditService:
    """
    分享体检编排：解析文件树、分组盘点、逐组识别并与媒体库比对。
    """

    def __init__(
        self,
        *,
        walk: Optional[Callable[..., object]] = None,
        recognize: Optional[Callable[[str], Awaitable[Optional[RecognizedMedia]]]] = None,
        probe: MediaLibraryProbe,
        max_files: int = _MAX_FILES,
    ) -> None:
        """
        保存解析、识别与媒体库探针，缺省解析使用网盘取链服务。

        :param walk: 分享文件树展开函数，签名与 ``NetdiskResolveService.walk`` 一致
        :param recognize: 路径识别函数；为 None 时所有分组按未识别处理
        :param probe: 媒体库只读探针
        :param max_files: 单次体检最多处理的视频文件数
        """
        if walk is None:
            from app.application.netdisk.resolve import NetdiskResolveService

            walk = NetdiskResolveService().walk
        self._walk = walk
        self._recognize = recognize
        self._probe = probe
        self._max_files = max(int(max_files), 1)

    async def audit(
        self,
        url: str,
        password: Optional[str] = None,
        platform: object = None,
        account_id: Optional[int] = None,
        *,
        identity: Optional[RecognizedMedia] = None,
        season: Optional[int] = None,
    ) -> ShareAuditReport:
        """
        体检一个分享链接。

        调用方已知目标媒体时传入 ``identity``：它只套到标题归一化后匹配的分组，
        没有匹配分组时套到体量最大的分组，其余分组仍按路径识别，避免整包合集被
        错当成同一部剧。

        :param url: 分享链接
        :param password: 提取码
        :param platform: 平台标识
        :param account_id: 指定网盘账号
        :param identity: 调用方给定的媒体身份，None 表示全部按路径识别
        :param season: 只比对这一季，None 表示比对分享内全部季
        :return: 体检报告
        """
        resolved = await run_in_threadpool(
            self._walk, url, password, platform, account_id, max_depth=12
        )
        detail = getattr(resolved, "detail", None)
        files = list(getattr(resolved, "files", ()) or ())
        video_files = [
            item
            for item in files
            if not getattr(item, "is_dir", False)
            and is_video_file_name(item.name)
            and not is_special_name(item.name)
        ]
        truncated = len(video_files) > self._max_files
        if truncated:
            logger.warning(
                f"分享体检文件数超过上限 {self._max_files}，仅处理前 {self._max_files} 个"
            )
            video_files = video_files[: self._max_files]
        groups = group_share_files(video_files)
        target = _identity_group_index(groups, identity)
        audited: list[AuditedMedia] = []
        for index, group in enumerate(groups):
            recognized: Optional[RecognizedMedia] = None
            if identity is not None and index == target:
                recognized = identity
            elif self._recognize is not None:
                try:
                    recognized = await self._recognize(group.sample_path)
                except Exception as error:  # noqa: BLE001 - 识别失败按未识别处理
                    logger.warning(
                        f"分享体检识别失败 {group.sample_path}：{type(error).__name__}"
                    )
                    recognized = None
            audited.append(
                await audit_group(
                    group,
                    recognized,
                    probe=self._probe,
                    truncated=truncated,
                    season=season,
                )
            )
        unrecognized = sum(
            len(group.files)
            for group, media in zip(groups, audited)
            if not media.recognized
        )
        return ShareAuditReport(
            share_title=str(getattr(detail, "title", "") or ""),
            platform=str(getattr(getattr(resolved, "ref", None), "platform", "") or ""),
            file_count=len(video_files),
            total_bytes=sum(max(int(getattr(item, "size", 0) or 0), 0) for item in video_files),
            truncated=truncated,
            media=tuple(audited),
            unrecognized_files=unrecognized,
        )
