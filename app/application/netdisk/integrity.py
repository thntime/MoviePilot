"""
非空壳文件判定。

文件必须存在且大小大于 0；可用探测函数时要求读出正时长，探测不可用或
返回 None 时退化为同名 `.done` 完成标记。宁可判为不完整，也不把空壳文件
交给整理链路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional


def is_complete_file(
    path: Path,
    *,
    probe: Optional[Callable[[Path], Optional[float]]] = None,
) -> bool:
    """
    判断文件是否已完整下载。

    :param path: 待检查文件
    :param probe: 时长探测函数，返回秒数；不可用或返回 None 时使用完成标记
    :return: 文件存在、大小大于 0 且时长有效或存在完成标记时返回 True
    """
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size <= 0:
            return False
    except OSError:
        return False
    if probe is not None:
        try:
            duration = probe(target)
        except Exception:  # noqa: BLE001 - 探测失败退化为完成标记
            duration = None
        if duration is not None:
            return float(duration) > 0
    return target.with_name(target.name + ".done").exists()
