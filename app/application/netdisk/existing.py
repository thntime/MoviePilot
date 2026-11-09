"""
网盘个人盘已有完整文件查询。

失败对账时按任务账号优先、同平台其他账号其次的顺序，有界遍历个人盘目录，
只要目标集数全部能由文件名解析到即视为命中；超预算按未命中处理。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from app.application.netdisk.files import NetdiskFilesService
from app.domain.episode import episode_numbers
from app.domain.netdisk import DriveFile
from app.runtime.log import logger

# 单次查询最多下钻的目录层级，避免误入极深的相册/备份目录。
_MAX_DEPTH = 4
# 查询结果短 TTL，避免同一失败任务在连续对账轮次内反复扫盘。
_CACHE_TTL_SECONDS = 60.0
# 缓存条目上限，超出时优先清理已过期项，避免进程内无限增长。
_CACHE_MAX_ENTRIES = 512
# 未命中缓存的哨兵，区分「没有缓存」与「缓存了负结果」。
_MISS = object()
# (account_id, title, season, episodes) -> (expires_at, result)
_CACHE: dict[
    Tuple[int, str, Optional[int], Tuple[int, ...]],
    Tuple[float, Optional["ExistingHit"]],
] = {}
# 目录名或文件名中的显式季号写法。
_SEASON_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})"),
    re.compile(r"第\s*(\d{1,3})\s*季"),
    re.compile(r"[Ss]eason\s*(\d{1,3})", re.IGNORECASE),
)
# 归一化标题时只保留字母、数字与汉字，兼容空格、点号与分隔符差异。
_TITLE_NOISE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class ExistingHit:
    """
    网盘个人盘已有完整文件的命中结果。

    :param account_id: 命中文件所在的账号主键
    :param files: 覆盖目标集数的文件条目
    """

    account_id: int
    files: List[DriveFile]


class _Budget:
    """
    一次查询共享的目录数、文件数与时间预算。

    :param max_dirs: 最多列出的目录数
    :param max_files: 最多检查的文件数
    :param timeout_seconds: 查询截止时间（秒）
    """

    def __init__(self, max_dirs: int, max_files: int, timeout_seconds: float) -> None:
        """保存预算上限并记录起始时间。"""
        self.max_dirs = max_dirs
        self.max_files = max_files
        self.deadline = time.monotonic() + timeout_seconds
        self.dirs = 0
        self.files = 0

    def take_dir(self) -> bool:
        """占用一次目录额度；超出预算返回 False。"""
        self.dirs += 1
        return self.dirs <= self.max_dirs and not self.timed_out()

    def take_file(self) -> bool:
        """占用一次文件额度；超出预算返回 False。"""
        self.files += 1
        return self.files <= self.max_files and not self.timed_out()

    def timed_out(self) -> bool:
        """判断是否已超过查询截止时间。"""
        return time.monotonic() > self.deadline


def _normalize_title(value: str) -> str:
    """把标题归一化为可比较的紧凑文本。"""
    return _TITLE_NOISE.sub("", str(value or "").casefold())


def _season_number(name: str) -> Optional[int]:
    """从目录名或文件名中提取显式季号。"""
    for pattern in _SEASON_PATTERNS:
        matched = pattern.search(str(name or ""))
        if matched:
            return int(matched.group(1))
    return None


def _dir_matches(name: str, title: str, season: Optional[int]) -> bool:
    """判断目录是否值得继续下钻。"""
    if title and _normalize_title(title) in _normalize_title(name):
        return True
    if season is None:
        return False
    detected = _season_number(name)
    return detected is not None and detected == season


def _file_matches(name: str, season: Optional[int]) -> bool:
    """判断文件是否属于目标季。"""
    if season is None:
        return True
    detected = _season_number(name)
    return detected is None or detected == season


def _cache_key(
    account_id: int,
    title: str,
    season: Optional[int],
    episodes: Set[int],
) -> Tuple[int, str, Optional[int], Tuple[int, ...]]:
    """构造包含目标集数的稳定缓存键。"""
    return (account_id, title, season, tuple(sorted(episodes)))


def _cached(
    key: Tuple[int, str, Optional[int], Tuple[int, ...]],
) -> object:
    """读取未过期的缓存；没有缓存时返回 `_MISS`，负结果返回 None。"""
    entry = _CACHE.get(key)
    if entry is None:
        return _MISS
    expires_at, result = entry
    if time.monotonic() > expires_at:
        _CACHE.pop(key, None)
        return _MISS
    return result


def _prune_cache() -> None:
    """在缓存达到上限时清理过期项，仍超限则丢弃最早过期的条目。"""
    if len(_CACHE) < _CACHE_MAX_ENTRIES:
        return
    now = time.monotonic()
    expired = [key for key, (expires_at, _) in _CACHE.items() if expires_at <= now]
    for key in expired:
        _CACHE.pop(key, None)
    if len(_CACHE) < _CACHE_MAX_ENTRIES:
        return
    oldest = sorted(_CACHE.items(), key=lambda item: item[1][0])
    for key, _ in oldest[:len(_CACHE) - _CACHE_MAX_ENTRIES + 1]:
        _CACHE.pop(key, None)


def _remember(
    key: Tuple[int, str, Optional[int], Tuple[int, ...]],
    result: Optional[ExistingHit],
) -> None:
    """写入账号级短 TTL 缓存，包括未命中的负结果。"""
    _prune_cache()
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, result)


def _warn_budget(reason: str) -> None:
    """记录一次预算超限日志。"""
    logger.warning(f"网盘已有文件查询超出预算（{reason}），按未命中处理")


def _search_account(
    service: NetdiskFilesService,
    account_id: int,
    title: str,
    season: Optional[int],
    episodes: Set[int],
    budget: _Budget,
) -> Optional[ExistingHit]:
    """在单个账号内广度遍历，返回覆盖全部目标集数的文件集合。"""
    queue: List[Tuple[str, int]] = [("", 0)]
    matched: Set[int] = set()
    files: List[DriveFile] = []
    while queue:
        parent_id, depth = queue.pop(0)
        if not budget.take_dir():
            _warn_budget("目录数或耗时")
            return None
        try:
            entries = service.list_dir(account_id, parent_id)
        except Exception as error:  # noqa: BLE001 - 单账号浏览失败按未命中处理
            logger.debug(
                f"网盘已有文件查询跳过目录 {account_id}:{parent_id}：{type(error).__name__}"
            )
            continue
        for entry in entries:
            if entry.is_dir:
                if depth < _MAX_DEPTH and _dir_matches(entry.name, title, season):
                    queue.append((entry.file_id, depth + 1))
                continue
            if not budget.take_file():
                _warn_budget("文件数或耗时")
                return None
            if not _file_matches(entry.name, season):
                continue
            numbers = set(episode_numbers(entry.name))
            if not numbers.intersection(episodes):
                continue
            files.append(entry)
            matched.update(numbers)
            if episodes.issubset(matched):
                return ExistingHit(account_id=account_id, files=files)
    return None


def find_complete_media(
    accounts: Sequence[Tuple[int, str]],
    title: str,
    season: Optional[int],
    episodes: Set[int],
    *,
    max_dirs: int = 50,
    max_files: int = 2000,
    timeout_seconds: float = 5.0,
) -> Optional[ExistingHit]:
    """
    按账号顺序查询个人盘是否已有覆盖全部目标集数的文件。

    :param accounts: 账号主键与展示名，调用方应把任务账号排在首位
    :param title: 媒体标题，用于筛选值得下钻的目录
    :param season: 目标季；为空时不限制季号
    :param episodes: 目标集数集合
    :param max_dirs: 单次查询最多列出的目录数
    :param max_files: 单次查询最多检查的文件数
    :param timeout_seconds: 单次查询时间预算（秒）
    :return: 命中结果；无账号、无目标集数或超预算时返回 None
    """
    if not accounts or not str(title or "").strip() or not episodes:
        return None
    service = NetdiskFilesService()
    budget = _Budget(max_dirs, max_files, timeout_seconds)
    target_episodes = tuple(sorted(episodes))
    for account_id, _name in accounts:
        key = _cache_key(account_id, title, season, set(target_episodes))
        cached = _cached(key)
        if cached is not _MISS:
            if cached is not None:
                return cached
            continue
        if budget.timed_out():
            _warn_budget("耗时")
            return None
        hit = _search_account(service, account_id, title, season, set(episodes), budget)
        _remember(key, hit)
        if hit is not None:
            return hit
    return None
