"""
本地库已有完整文件检查。

失败对账时先询问媒体库是否已存在目标集数，再按预算遍历任务相关的下载目录，
用非空壳判定过滤零字节、无完成标记的文件。命中后由对账流程标记整理完成，
不触发换源。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.application.netdisk.integrity import is_complete_file
from app.domain.episode import episode_numbers
from app.runtime.log import logger

# 只把这些后缀视为可入库的视频文件。
_VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".mov", ".flv", ".wmv", ".webm",
    ".rmvb", ".rm", ".mpg", ".mpeg", ".m4v", ".iso",
})
# 单次扫描的短 TTL，避免同一失败任务在连续对账轮次内重复遍历目录。
_CACHE_TTL_SECONDS = 60.0
# (title, season, episodes, dirs) -> (expires_at, result)
_CACHE: dict[
    Tuple[str, Optional[int], Tuple[int, ...], Tuple[str, ...]],
    Tuple[float, Optional["LocalExistingHit"]],
] = {}
# 目录名或文件名中的显式季号写法。
_SEASON_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})"),
    re.compile(r"第\s*(\d{1,3})\s*季"),
    re.compile(r"[Ss]eason\s*(\d{1,3})", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class LocalExistingHit:
    """
    本地已有完整文件的命中结果。

    :param path: 下载目录中命中的首个完整文件；媒体库命中时为空
    :param in_library: 是否由媒体库存在性判定命中
    """

    path: Optional[Path] = None
    in_library: bool = False


class _Budget:
    """
    一次本地扫描共享的目录数、文件数与时间预算。

    :param max_dirs: 最多遍历的目录数
    :param max_files: 最多检查的文件数
    :param timeout_seconds: 扫描截止时间（秒）
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
        """判断是否已超过扫描截止时间。"""
        return time.monotonic() > self.deadline


def _season_number(name: str) -> Optional[int]:
    """从路径或文件名中提取显式季号。"""
    for pattern in _SEASON_PATTERNS:
        matched = pattern.search(str(name or ""))
        if matched:
            return int(matched.group(1))
    return None


def _is_video(name: str) -> bool:
    """判断文件名后缀是否为可入库视频。"""
    return Path(name).suffix.casefold() in _VIDEO_SUFFIXES


def _cache_key(
    title: str,
    season: Optional[int],
    episodes: Set[int],
    download_dirs: Sequence[str],
) -> Tuple[str, Optional[int], Tuple[int, ...], Tuple[str, ...]]:
    """构造稳定的扫描缓存键。"""
    return (title, season, tuple(sorted(episodes)), tuple(download_dirs))


def _cached(key) -> Optional[LocalExistingHit]:
    """读取未过期的缓存；未命中或已过期时返回 None。"""
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, result = entry
    if time.monotonic() > expires_at:
        _CACHE.pop(key, None)
        return None
    return result


def _scan_file(
    target: Path,
    season: Optional[int],
    episodes: Set[int],
    found: Dict[int, Path],
    probe: Optional[Callable[[Path], Optional[float]]],
) -> None:
    """检查单个视频文件并把完整文件登记的集数写入结果。"""
    if not _is_video(target.name):
        return
    detected = _season_number(target.name)
    if season is not None and detected is not None and detected != season:
        return
    numbers = set(episode_numbers(target.name)).intersection(episodes)
    if not numbers:
        return
    if not is_complete_file(target, probe=probe):
        return
    for number in numbers:
        found.setdefault(number, target)


def _scan_dir(
    root: Path,
    season: Optional[int],
    episodes: Set[int],
    found: Dict[int, Path],
    budget: _Budget,
    probe: Optional[Callable[[Path], Optional[float]]],
) -> None:
    """按预算递归扫描一个下载目录。"""
    stack: List[Path] = [root]
    while stack:
        current = stack.pop()
        if not budget.take_dir():
            logger.warning("本地已有文件查询超出预算（目录数或耗时），按未命中处理")
            return
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if not budget.take_file():
                logger.warning("本地已有文件查询超出预算（文件数或耗时），按未命中处理")
                return
            _scan_file(Path(entry.path), season, episodes, found, probe)
            if episodes.issubset(found):
                return


def find_local_complete(
    title: str,
    season: Optional[int],
    episodes: Set[int],
    download_dirs: Sequence[str],
    *,
    media_exists: Optional[Callable[[], bool]] = None,
    probe: Optional[Callable[[Path], Optional[float]]] = None,
    max_dirs: int = 200,
    max_files: int = 5000,
    timeout_seconds: float = 5.0,
) -> Optional[LocalExistingHit]:
    """
    检查媒体库与下载目录是否已有覆盖全部目标集数的完整文件。

    :param title: 媒体标题，用于日志与缓存键
    :param season: 目标季；为空时不限制季号
    :param episodes: 目标集数集合
    :param download_dirs: 需要扫描的下载目录或文件路径
    :param media_exists: 媒体库存在性判定；返回 True 表示已存在目标集数
    :param probe: 时长探测函数，供非空壳判定使用
    :param max_dirs: 单次扫描最多遍历的目录数
    :param max_files: 单次扫描最多检查的文件数
    :param timeout_seconds: 单次扫描时间预算（秒）
    :return: 命中结果；未命中时返回 None
    """
    if not episodes:
        return None
    if media_exists is not None:
        try:
            if media_exists():
                return LocalExistingHit(in_library=True)
        except Exception as error:  # noqa: BLE001 - 媒体库查询失败继续扫描下载目录
            logger.debug(f"媒体库存在性查询失败：{type(error).__name__}")
    key = _cache_key(str(title or ""), season, episodes, download_dirs)
    cached = _cached(key)
    if cached is not None:
        return cached
    found: Dict[int, Path] = {}
    budget = _Budget(max_dirs, max_files, timeout_seconds)
    for item in download_dirs:
        raw = str(item or "").strip()
        if not raw:
            continue
        root = Path(raw)
        if root.is_file():
            if not budget.take_file():
                logger.warning("本地已有文件查询超出预算（文件数或耗时），按未命中处理")
                break
            _scan_file(root, season, episodes, found, probe)
        elif root.is_dir():
            _scan_dir(root, season, episodes, found, budget, probe)
        if episodes.issubset(found):
            break
    hit = None
    if episodes.issubset(found):
        hit = LocalExistingHit(path=found[min(episodes)])
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, hit)
    return hit
