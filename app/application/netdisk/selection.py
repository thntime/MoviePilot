"""
网盘账号选择与平台冷却的进程内状态。

账号平均速度通过持久化端口跨重启保留，并继续参与账号排序；平台/账号冷却与告警窗口
仍只保存在当前进程，用于短周期调度决策。真正的失败事实仍写入下载失败台账，
由下载链统一管理。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from app.application.netdisk.contract import NetdiskSpeedStore
from app.domain.netdisk import NetdiskPlatform
from app.runtime.log import logger

_LOCK = threading.RLock()
_SPEED_EWMA: Dict[int, float] = {}
_SPEED_STORE: Optional[NetdiskSpeedStore] = None
_SPEED_LOADED = False
_COOLDOWN_UNTIL: Dict[NetdiskPlatform, float] = {}
_ACCOUNT_COOLDOWN_UNTIL: Dict[int, float] = {}
_LAST_ALERT: Dict[object, float] = {}
_SPEED_ALPHA = 0.3
# 容量类失败默认冷却账号 30 分钟；期间换用同平台其他账号仍可继续。
ACCOUNT_COOLDOWN_SECONDS = 30 * 60


def configure_speed_store(store: Optional[NetdiskSpeedStore]) -> None:
    """
    装配账号速度的持久化端口并重置内存缓存。

    :param store: 速度存储端口；None 表示解除装配
    """
    global _SPEED_STORE, _SPEED_LOADED
    with _LOCK:
        _SPEED_STORE = store
        _SPEED_EWMA.clear()
        _SPEED_LOADED = False


def _ensure_loaded_locked() -> None:
    """
    首次访问速度时从持久化端口加载，失败按空映射降级。

    调用方必须持有 ``_LOCK``；加载失败不重试，避免每次访问都触碰损坏文件。
    """
    global _SPEED_LOADED
    if _SPEED_LOADED:
        return
    _SPEED_LOADED = True
    if _SPEED_STORE is None:
        return
    try:
        loaded = _SPEED_STORE.load()
    except Exception as error:  # noqa: BLE001 - 辅助指标不能影响下载
        logger.warning(f"网盘账号速度加载失败：error={type(error).__name__}")
        return
    for account_id, speed in loaded.items():
        if account_id > 0 and speed > 0:
            _SPEED_EWMA[account_id] = float(speed)


def _persist_speeds_locked() -> None:
    """
    把内存速度写回持久化端口，失败只记录告警。

    调用方必须持有 ``_LOCK``。
    """
    if _SPEED_STORE is None:
        return
    try:
        _SPEED_STORE.save(dict(_SPEED_EWMA))
    except Exception as error:  # noqa: BLE001 - 辅助指标不能影响下载
        logger.warning(f"网盘账号速度保存失败：error={type(error).__name__}")


def record_account_speed(account_id: int, bytes_count: int, seconds: float) -> None:
    """
    记录一次账号下载速度，按指数滑动平均更新并写回持久化端口。

    :param account_id: 账号主键
    :param bytes_count: 本次下载字节数
    :param seconds: 本次耗时，单位秒
    """
    if account_id <= 0 or bytes_count <= 0 or seconds <= 0:
        return
    sample = float(bytes_count) / float(seconds)
    with _LOCK:
        _ensure_loaded_locked()
        previous = _SPEED_EWMA.get(account_id)
        _SPEED_EWMA[account_id] = (
            sample if previous is None else previous * (1 - _SPEED_ALPHA) + sample * _SPEED_ALPHA
        )
        _persist_speeds_locked()


def account_speed(account_id: int) -> float:
    """
    返回账号近期下载速度；没有样本时返回 0。

    :param account_id: 账号主键
    :return: 字节/秒
    """
    with _LOCK:
        _ensure_loaded_locked()
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


def account_cooldown_remaining(account_id: int) -> float:
    """
    返回账号剩余冷却秒数；未冷却时返回 0。

    :param account_id: 账号主键
    :return: 剩余秒数
    """
    with _LOCK:
        return max(_ACCOUNT_COOLDOWN_UNTIL.get(account_id, 0.0) - time.monotonic(), 0.0)


def cool_account(account_id: int, seconds: float = ACCOUNT_COOLDOWN_SECONDS) -> None:
    """
    冷却单个账号；容量类失败只影响该账号，不牵连整个平台。

    :param account_id: 账号主键
    :param seconds: 冷却时长，单位秒
    """
    if account_id <= 0:
        return
    with _LOCK:
        _ACCOUNT_COOLDOWN_UNTIL[account_id] = max(
            _ACCOUNT_COOLDOWN_UNTIL.get(account_id, 0.0),
            time.monotonic() + max(float(seconds), 0.0),
        )


def is_account_cooled(account_id: int) -> bool:
    """
    判断账号是否仍在冷却期。

    :param account_id: 账号主键
    :return: 冷却中返回 True
    """
    with _LOCK:
        until = _ACCOUNT_COOLDOWN_UNTIL.get(account_id, 0.0)
        if until <= time.monotonic():
            _ACCOUNT_COOLDOWN_UNTIL.pop(account_id, None)
            return False
        return True


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
    清空进程内速度、持久化端口、平台/账号冷却与告警状态，供测试隔离使用。
    """
    global _SPEED_STORE, _SPEED_LOADED
    with _LOCK:
        _SPEED_EWMA.clear()
        _SPEED_STORE = None
        _SPEED_LOADED = False
        _COOLDOWN_UNTIL.clear()
        _ACCOUNT_COOLDOWN_UNTIL.clear()
        _LAST_ALERT.clear()
