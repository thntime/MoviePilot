"""
下载后内容校验的证据打分、抽帧与待处理记录端口。

打分只做本地能算的事（文件名、目录名、标题、时长、集号），据此决定是否需要
请模型介入、是否需要抽帧；抽帧用 ffmpeg 截两帧，既作为多模态证据交给模型，
也随记录存证供人工复核。待处理记录通过 `ContentVerifyRepository` 端口持久化，
具体实现由组合根注入。
"""

from __future__ import annotations

import shutil
import statistics
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from app.domain.episode import episode_numbers
from app.foundation.environment import get_config_path
from app.runtime.log import logger

# 抽帧位置：20% 大概率命中片头标题卡，60% 是正片内容。
FRAME_RATIOS = (0.2, 0.6)
# 一批最多抽几个文件，避免整季分享反复起 ffmpeg。
MAX_FRAME_FILES = 3
# 单帧超时秒数。
FRAME_TIMEOUT_SECONDS = 10.0
# 抽帧图保留天数，过期自动清理。
FRAME_TTL_DAYS = 7
# 时长容差：±20%，且至少给 ±3 分钟。
DURATION_TOLERANCE_RATIO = 0.2
DURATION_TOLERANCE_SECONDS = 180
# 时长差超过该比例直接否决，不问模型。
DIRECT_REJECT_RATIO = 0.5
# 证据得分达到该值时不再问模型。
SCORE_SKIP_MODEL = 5
# 文件名、目录名、标题、时长、集号、集数各自的分值。
SCORE_FILE_NAME = 3
SCORE_DIR_NAME = 2
SCORE_TITLE = 1
SCORE_DURATION = 2
SCORE_EPISODE_HIT = 2
SCORE_EPISODE_COUNT = 1
# 评分满分，用于列表展示 N/12。
SCORE_TOTAL = (
    SCORE_FILE_NAME + SCORE_DIR_NAME + SCORE_TITLE
    + SCORE_DURATION + SCORE_EPISODE_HIT + SCORE_EPISODE_COUNT
)
# 记录状态：待处理、人工放行、已删除。
DECISION_REJECT = "reject"
DECISION_MANUAL = "manual"
DECISION_DELETED = "deleted"
DECISION_PASS = "pass"
# 音频后缀，用于判断「音频占多数」。
_AUDIO_SUFFIXES = {
    ".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg", ".ape", ".wma", ".opus", ".dsf", ".dff",
}
_FRAME_DIR_NAME = "content_verify_frames"


@dataclass(frozen=True, slots=True)
class TargetMedia:
    """
    本次校验的目标媒体信息。

    :param title: 媒体标题
    :param original_title: 原始标题
    :param names: 别名与译名
    :param year: 年份
    :param media_type: 媒体类型
    :param season: 季号
    :param episodes: 目标集号
    :param runtime_minutes: 单集或影片时长（分钟）
    """

    title: str = ""
    original_title: str = ""
    names: Tuple[str, ...] = ()
    year: str = ""
    media_type: str = ""
    season: int = 0
    episodes: Tuple[int, ...] = ()
    runtime_minutes: int = 0

    def display(self) -> str:
        """
        拼装给模型看的媒体描述。

        :return: 描述文本
        """
        parts = [self.title or "未知"]
        if self.year:
            parts.append(f"（{self.year}）")
        if self.media_type:
            parts.append(f"类型 {self.media_type}")
        if self.season:
            parts.append(f"第 {self.season} 季")
        if self.episodes:
            parts.append(f"目标集 {_compact(self.episodes)}")
        if self.runtime_minutes:
            parts.append(f"单集时长约 {self.runtime_minutes} 分钟")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class FileEvidence:
    """
    单个下载文件的证据。

    :param name: 文件名
    :param path: 相对路径
    :param size: 大小（字节）
    :param duration: 时长（秒），0 表示未取到
    """

    name: str = ""
    path: str = ""
    size: int = 0
    duration: int = 0


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """
    本地证据打分结果。

    :param score: 总分
    :param hits: 命中的证据名称
    :param duration_ratio: 实际时长与目标时长的比值；无法比较时为 None
    :param reject_reason: 直接否决原因；不否决时为空
    """

    score: int = 0
    hits: Tuple[str, ...] = ()
    duration_ratio: Optional[float] = None
    reject_reason: str = ""


@dataclass(slots=True)
class VerifyRecord:
    """
    一条待处理的下载内容校验记录。

    :param id: 记录标识
    :param src: 源路径
    :param storage: 源存储
    :param title: 媒体标题
    :param media_type: 媒体类型
    :param season: 季号
    :param episodes: 集号文本
    :param score: 证据得分
    :param confidence: 模型置信度
    :param reason: 判否原因
    :param hits: 命中的证据
    :param frames: 抽帧图路径
    :param decision: 处理状态
    :param file_count: 文件数
    :param total_size: 总大小（字节）
    :param median_duration: 单集中位时长（秒）
    :param platform: 网盘平台
    :param share_code: 分享码
    :param download_hash: 下载任务标识
    :param created_at: 创建时间
    :param updated_at: 更新时间
    """

    id: str = ""
    src: str = ""
    storage: str = "local"
    title: str = ""
    media_type: str = ""
    season: str = ""
    episodes: str = ""
    score: int = 0
    confidence: float = 0.0
    reason: str = ""
    hits: List[str] = field(default_factory=list)
    frames: List[str] = field(default_factory=list)
    decision: str = DECISION_REJECT
    file_count: int = 0
    total_size: int = 0
    median_duration: int = 0
    platform: str = ""
    share_code: str = ""
    download_hash: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """
        转成可直接写入 JSON 的字典。

        :return: 字典
        """
        return asdict(self)


def build_target(mediainfo: Any, meta: Any = None) -> TargetMedia:
    """
    从媒体信息与元数据里取出校验目标。

    :param mediainfo: 媒体信息对象，可为空
    :param meta: 元数据对象，可为空
    :return: 目标媒体
    """
    title = str(getattr(mediainfo, "title", "") or getattr(meta, "name", "") or "")
    original = str(getattr(mediainfo, "original_title", "") or "")
    names = tuple(str(item) for item in (getattr(mediainfo, "names", None) or []) if item)
    year = str(getattr(mediainfo, "year", "") or getattr(meta, "year", "") or "")
    media_type = getattr(mediainfo, "type", None) or getattr(meta, "type", None)
    media_type = str(getattr(media_type, "value", media_type) or "")
    season = int(getattr(meta, "begin_season", 0) or getattr(mediainfo, "season", 0) or 0)
    episodes = _meta_episodes(meta)
    runtime = int(getattr(mediainfo, "runtime", 0) or 0)
    return TargetMedia(
        title=title,
        original_title=original,
        names=names,
        year=year,
        media_type=media_type,
        season=season,
        episodes=episodes,
        runtime_minutes=runtime,
    )


def build_evidence(
    file_items: Iterable[Any],
    storage: str = "local",
    *,
    probe: Optional[Any] = None,
    max_probe: int = MAX_FRAME_FILES,
) -> Tuple[FileEvidence, ...]:
    """
    把整理文件项投影成证据，本机文件额外探测时长。

    :param file_items: 整理文件项
    :param storage: 源存储
    :param probe: 时长探测函数，签名 (Path) -> Optional[float]
    :param max_probe: 最多探测几个文件的时长
    :return: 证据列表
    """
    evidence: List[FileEvidence] = []
    probed = 0
    for item in file_items or []:
        name = str(getattr(item, "name", "") or "")
        path = str(getattr(item, "path", "") or "")
        size = int(getattr(item, "size", 0) or 0)
        duration = 0
        if storage == "local" and probe is not None and probed < max_probe:
            probed += 1
            try:
                value = probe(Path(path))
            except Exception:  # noqa: BLE001 - 探测失败按未知时长处理
                value = None
            if value:
                duration = int(float(value))
        evidence.append(FileEvidence(name=name, path=path, size=size, duration=duration))
    return tuple(evidence)


def score_evidence(
    target: TargetMedia,
    evidence: Sequence[FileEvidence],
    task_name: str = "",
) -> ScoreResult:
    """
    按本地证据给候选打分，并给出是否需要直接否决。

    :param target: 目标媒体
    :param evidence: 文件证据
    :param task_name: 下载任务名（分享标题）
    :return: 打分结果
    """
    if not evidence:
        return ScoreResult()
    names = _target_names(target)
    hits: List[str] = []
    score = 0
    if names and any(_contains_name(names, item.name) for item in evidence):
        score += SCORE_FILE_NAME
        hits.append("文件名")
    if names and any(_contains_name(names, _parent_of(item.path)) for item in evidence if item.path):
        score += SCORE_DIR_NAME
        hits.append("目录名")
    if names and task_name and _contains_name(names, task_name):
        score += SCORE_TITLE
        hits.append("标题")
    ratio = _duration_ratio(target, evidence)
    if ratio is not None and _duration_close(target, evidence, ratio):
        score += SCORE_DURATION
        hits.append("时长")
    file_episodes = _evidence_episodes(evidence)
    if target.episodes and file_episodes & set(target.episodes):
        score += SCORE_EPISODE_HIT
        hits.append("集号")
    if target.episodes and len(file_episodes) == len(set(target.episodes)):
        score += SCORE_EPISODE_COUNT
        hits.append("集数")
    reject = _direct_reject(target, evidence, ratio)
    return ScoreResult(score=score, hits=tuple(hits), duration_ratio=ratio, reject_reason=reject)


def frame_policy(result: ScoreResult) -> str:
    """
    根据得分决定是否需要模型介入以及是否需要抽帧。

    只要名字证据不足以直接放行，就抽帧带图问模型：画面是判断「这到底是不是
    这部剧」最直接的证据，只给文件名和标题容易在频道文案串作品时判错。

    :param result: 打分结果
    :return: `skip`（名字证据充分，直接放行）、`frames`（抽帧带图问模型）
    """
    if result.score >= SCORE_SKIP_MODEL:
        return "skip"
    return "frames"


def extract_frames(paths: Sequence[str], limit: int = MAX_FRAME_FILES) -> Tuple[str, ...]:
    """
    用 ffmpeg 从本地视频里截帧存证。

    :param paths: 视频路径
    :param limit: 最多处理几个文件
    :return: 抽帧图路径；ffmpeg 不可用或全部失败时为空
    """
    if not paths:
        return ()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.debug("抽帧跳过：ffmpeg 不可用")
        return ()
    cleanup_expired_frames()
    output_dir = _frame_root() / uuid.uuid4().hex
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.warning(f"抽帧目录创建失败：{error}")
        return ()
    frames: List[str] = []
    for index, path in enumerate(list(paths)[:limit]):
        target = Path(path)
        if not target.is_file():
            continue
        duration = probe_duration(target)
        if not duration or duration <= 0:
            continue
        for ratio_index, ratio in enumerate(FRAME_RATIOS):
            output = output_dir / f"{index:02d}-{ratio_index}.jpg"
            if _grab_frame(ffmpeg, target, duration * ratio, output):
                frames.append(str(output))
    if not frames:
        shutil.rmtree(output_dir, ignore_errors=True)
    return tuple(frames)


def cleanup_expired_frames(now: Optional[datetime] = None) -> int:
    """
    清理超过保留期的抽帧目录。

    :param now: 当前时间，默认取系统时间
    :return: 清理的目录数
    """
    root = _frame_root()
    if not root.is_dir():
        return 0
    deadline = (now or datetime.now(timezone.utc)) - timedelta(days=FRAME_TTL_DAYS)
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            created = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if created < deadline:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


class ContentVerifyRepository(Protocol):
    """
    待处理内容校验记录的持久化端口。

    具体实现由组合根注入（当前是数据库表 `contentverify`），应用层只依赖本协议。
    """

    def save(self, record: VerifyRecord) -> VerifyRecord:
        """
        新增一条记录。

        :param record: 待写入记录
        :return: 写入后的记录
        """
        ...

    def list(self, decision: Optional[str] = None) -> List[VerifyRecord]:
        """
        按状态读取记录。

        :param decision: 处理状态；为空时返回待处理记录
        :return: 记录列表，按判定时间倒序
        """
        ...

    def get(self, record_id: str) -> Optional[VerifyRecord]:
        """
        读取单条记录。

        :param record_id: 记录标识
        :return: 记录；不存在时为空
        """
        ...

    def update_decision(self, record_id: str, decision: str) -> Optional[VerifyRecord]:
        """
        更新记录的处理状态。

        :param record_id: 记录标识
        :param decision: 新状态
        :return: 更新后的记录；不存在时为空
        """
        ...


_repository: Optional[ContentVerifyRepository] = None


def configure_content_verify_repository(
    repository: Optional[ContentVerifyRepository],
) -> None:
    """
    由组合根注册待处理记录仓库。

    :param repository: 仓库实现；传 None 表示释放
    """
    global _repository
    _repository = repository


def get_content_verify_store() -> ContentVerifyRepository:
    """
    返回已装配的待处理记录仓库。

    :return: 仓库实现
    :raises RuntimeError: 组合根尚未装配
    """
    if _repository is None:
        raise RuntimeError(
            "内容校验记录仓库未装配：请先在组合根调用 configure_content_verify_repository"
        )
    return _repository


def _frame_root() -> Path:
    """
    返回抽帧图根目录。

    :return: 目录路径
    """
    return get_config_path() / _FRAME_DIR_NAME


def _compact(numbers: Sequence[int]) -> str:
    """
    把集号压成区间文本。

    :param numbers: 集号序列
    :return: 区间文本
    """
    values = sorted(set(int(item) for item in numbers))
    if not values:
        return ""
    if len(values) == 1:
        return str(values[0])
    return f"{values[0]}-{values[-1]}"


def _meta_episodes(meta: Any) -> Tuple[int, ...]:
    """
    从元数据里取目标集号范围。

    :param meta: 元数据对象
    :return: 集号序列
    """
    begin = int(getattr(meta, "begin_episode", 0) or 0)
    end = int(getattr(meta, "end_episode", 0) or 0)
    if begin <= 0:
        return ()
    if end < begin:
        end = begin
    if end - begin > 2000:
        return ()
    return tuple(range(begin, end + 1))


def _target_names(target: TargetMedia) -> Tuple[str, ...]:
    """
    收集目标媒体的所有可匹配名字。

    :param target: 目标媒体
    :return: 归一化后的名字
    """
    raw = [target.title, target.original_title, *target.names]
    names: List[str] = []
    for item in raw:
        value = _normalize(str(item or ""))
        if len(value) >= 2 and value not in names:
            names.append(value)
    return tuple(names)


def _normalize(text: str) -> str:
    """
    归一化文本：只保留字母、数字与汉字，统一小写。

    :param text: 原始文本
    :return: 归一化文本
    """
    return "".join(
        char.lower() for char in str(text or "")
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def _contains_name(names: Sequence[str], text: str) -> bool:
    """
    判断文本里是否出现任一目标名字。

    :param names: 归一化后的名字
    :param text: 待匹配文本
    :return: 命中返回 True
    """
    normalized = _normalize(text)
    return bool(normalized) and any(name in normalized for name in names)


def _parent_of(path: str) -> str:
    """
    取相对路径的目录部分。

    :param path: 相对路径
    :return: 目录文本
    """
    value = str(path or "").replace("\\", "/")
    return value.rsplit("/", 1)[0] if "/" in value else ""


def _duration_ratio(target: TargetMedia, evidence: Sequence[FileEvidence]) -> Optional[float]:
    """
    计算实际时长与目标时长的比值。

    :param target: 目标媒体
    :param evidence: 文件证据
    :return: 比值；无法比较时返回 None
    """
    if target.runtime_minutes <= 0:
        return None
    durations = [item.duration for item in evidence if item.duration > 0]
    if not durations:
        return None
    expected = target.runtime_minutes * 60
    median = statistics.median(durations)
    return median / expected if expected else None


def _duration_close(
    target: TargetMedia, evidence: Sequence[FileEvidence], ratio: Optional[float]
) -> bool:
    """
    判断时长是否落在容差范围内。

    :param target: 目标媒体
    :param evidence: 文件证据
    :param ratio: 已算好的比值
    :return: 落在容差内返回 True
    """
    if ratio is None or target.runtime_minutes <= 0:
        return False
    expected = target.runtime_minutes * 60
    durations = [item.duration for item in evidence if item.duration > 0]
    median = statistics.median(durations)
    tolerance = max(expected * DURATION_TOLERANCE_RATIO, DURATION_TOLERANCE_SECONDS)
    return abs(median - expected) <= tolerance


def _evidence_episodes(evidence: Sequence[FileEvidence]) -> set:
    """
    收集文件证据里的集号。

    :param evidence: 文件证据
    :return: 集号集合
    """
    numbers = set()
    for item in evidence:
        numbers.update(episode_numbers(item.name))
    return numbers


def _direct_reject(
    target: TargetMedia,
    evidence: Sequence[FileEvidence],
    ratio: Optional[float],
) -> str:
    """
    判断是否可以直接否决，不必问模型。

    :param target: 目标媒体
    :param evidence: 文件证据
    :param ratio: 时长比值
    :return: 否决原因；不否决时为空
    """
    if ratio is not None and (ratio > 1 + DIRECT_REJECT_RATIO or ratio < 1 - DIRECT_REJECT_RATIO):
        return f"单集时长与目标相差过大（实际约为目标的 {ratio:.0%}）"
    audio = sum(1 for item in evidence if Path(item.name).suffix.lower() in _AUDIO_SUFFIXES)
    if audio * 2 > len(evidence):
        return "下载内容以音频为主"
    return ""


def probe_duration(path: Path) -> Optional[float]:
    """
    用 ffprobe 读取视频时长。

    :param path: 视频路径
    :return: 秒数；失败时返回 None
    """
    from app.application.netdisk.integrity import probe_video_duration

    return probe_video_duration(path, timeout=FRAME_TIMEOUT_SECONDS)


def _grab_frame(ffmpeg: str, path: Path, position: float, output: Path) -> bool:
    """
    调用 ffmpeg 在指定位置截一帧。

    :param ffmpeg: ffmpeg 可执行路径
    :param path: 视频路径
    :param position: 截帧位置（秒）
    :param output: 输出图片路径
    :return: 成功返回 True
    """
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y", "-v", "error", "-ss", f"{max(0.0, position):.2f}",
                "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=FRAME_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        logger.debug(f"抽帧失败：{path} - {type(error).__name__}")
        return False
    return result.returncode == 0 and output.is_file()


def record_from_dict(payload: Dict[str, Any]) -> VerifyRecord:
    """
    把字典还原成记录对象。

    持久化实现把数据库行投影成同名字典后，用它还原成领域记录。

    :param payload: 字典
    :return: 记录对象
    """
    fields = {field_name for field_name in VerifyRecord.__dataclass_fields__}
    values = {key: value for key, value in (payload or {}).items() if key in fields}
    values["hits"] = list(values.get("hits") or [])
    values["frames"] = list(values.get("frames") or [])
    return VerifyRecord(**values)
