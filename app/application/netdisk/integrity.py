"""
非空壳文件判定。

文件必须存在且大小大于 0；可用探测函数时要求读出正时长，探测不可用或
返回 None 时退化为同名 `.done` 完成标记。宁可判为不完整，也不把空壳文件
交给整理链路。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

# 分片/ HLS 未完成的计划文件；存在任一即说明文件可能仍是预分配空壳。
_PLAN_SUFFIXES = (".plan.json", ".hls.json", ".hls.part")


def probe_video_duration(target: Path, timeout: float = 30.0) -> Optional[float]:
    """
    用 ffprobe 读取视频容器时长。

    :param target: 视频文件
    :param timeout: 探测超时秒数
    :return: 时长（秒）；探测失败或结果不可解析时返回 None
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(target),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _has_pending_plan(target: Path) -> bool:
    """
    判断目标文件是否仍有未完成的分片/HLS 计划文件。

    :param target: 视频文件
    :return: 存在任一计划文件返回 True
    """
    return any(
        target.with_suffix(target.suffix + suffix).exists()
        for suffix in _PLAN_SUFFIXES
    )


def is_complete_file(
    path: Path,
    *,
    probe: Optional[Callable[[Path], Optional[float]]] = None,
) -> bool:
    """
    判断文件是否已完整下载。

    :param path: 待检查文件
    :param probe: 时长探测函数，返回秒数；不可用或返回 None 时使用完成标记
    :return: 文件存在、大小大于 0、没有未完成计划文件，且时长有效或存在完成标记时返回 True
    """
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size <= 0:
            return False
    except OSError:
        return False
    if _has_pending_plan(target):
        return False
    if probe is not None:
        try:
            duration = probe(target)
        except Exception:  # noqa: BLE001 - 探测失败退化为完成标记
            duration = None
        if duration is not None:
            return float(duration) > 0
    return target.with_name(target.name + ".done").exists()
