"""
网盘账号选择与平台冷却的进程内状态。

速度与冷却只保存在当前进程：它们用于短周期调度决策，不需要跨重启持久化。
真正的失败事实仍写入下载失败台账，由下载链统一管理。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from app.domain.netdisk import NetdiskPlatform

_LOCK = threading.RLock()
_SPEED_EWMA: Dict[int, float] = {}
_COOLDOWN_UNTIL: Dict[NetdiskPlatform, float] = {}
_LAST_ALERT: Dict[object, float] = {}
_SPEED_ALPHA = 0.3


def record_account_speed(account_id: int, bytes_count: int, seconds: float) -> None:
    """
    记录一次账号下载速度，按指数滑动平均更新。

    :param account_id: 账号主键
    :param bytes_count: 本次下载字节数
    :param seconds: 本次耗时，单位秒
    """
    if account_id <= 0 or bytes_count <= 0 or seconds <= 0:
        return
    sample = float(bytes_count) / float(seconds)
    with _LOCK:
        previous = _SPEED_EWMA.get(account_id)
        _SPEED_EWMA[account_id] = (
            sample if previous is None else previous * (1 - _SPEED_ALPHA) + sample * _SPEED_ALPHA
        )


def account_speed(account_id: int) -> float:
    """
    返回账号近期下载速度；没有样本时返回 0。

    :param account_id: 账号主键
    :return: 字节/秒
    """
    with _LOCK:
        return float(_SPEED_EWMA.get(account_id) or 0.0)


def cool_platform(platform: NetdiskPlatform, seconds: float = 30 * 60) -> None:
    """
    冷却一个平台，冷却期内新的提交直接跳过该平台。

    :param platform: 网盘平台
    :param seconds: 冷却时长，单位秒
    """
    with _LOCK:
        _COOLDOWN_UNTIL[platform] = max(
            _COOLDOWN_UNTIL.get(platform, 0.0),
            time.monotonic() + max(float(seconds), 0.0),
        )


def is_platform_cooled(platform: NetdiskPlatform) -> bool:
    """
    判断平台是否仍在冷却期。

    :param platform: 网盘平台
    :return: 冷却中返回 True
    """
    with _LOCK:
        until = _COOLDOWN_UNTIL.get(platform, 0.0)
        if until <= time.monotonic():
            _COOLDOWN_UNTIL.pop(platform, None)
            return False
        return True


def platform_cooldown_remaining(platform: NetdiskPlatform) -> float:
    """
    返回平台剩余冷却秒数；未冷却时返回 0。

    :param platform: 网盘平台
    :return: 剩余秒数
    """
    with _LOCK:
        return max(_COOLDOWN_UNTIL.get(platform, 0.0) - time.monotonic(), 0.0)


def should_alert(account_id: object, window_seconds: float = 6 * 60 * 60) -> bool:
    """
    判断是否应为账号/平台发送容量告警，并在返回 True 时记录本次告警时间。

    :param account_id: 账号主键或平台标识
    :param window_seconds: 告警去重窗口，单位秒
    :return: 需要发送告警时返回 True
    """
    now = time.monotonic()
    with _LOCK:
        last = _LAST_ALERT.get(account_id, 0.0)
        if now - last < max(float(window_seconds), 0.0):
            return False
        _LAST_ALERT[account_id] = now
        return True


def clear_selection_state() -> None:
    """
    清空进程内速度、冷却与告警状态，供测试隔离使用。
    """
    with _LOCK:
        _SPEED_EWMA.clear()
        _COOLDOWN_UNTIL.clear()
        _LAST_ALERT.clear()
