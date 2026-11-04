"""
网盘账号惰性续期的进程内节流策略。

本模块只依赖 `app.domain.netdisk`，不访问网络、数据库或运行时配置；
真正的刷新由调用方通过适配器 `refresh_account` 执行。
"""

from __future__ import annotations

import time
from typing import Mapping, Optional

from app.domain.netdisk import NetdiskPlatform

# 各平台距上次刷新超过该秒数后才需要再次续期；未列出的平台不支持惰性续期。
REFRESH_TTL_SECONDS: dict[NetdiskPlatform, float] = {
    NetdiskPlatform.QUARK: 5400.0,
    NetdiskPlatform.UC: 5400.0,
}


class RefreshThrottle:
    """
    进程内按账号记录最近刷新时间，避免在 TTL 窗口内重复刷新。

    :param ttl_seconds: 平台到刷新窗口秒数的映射；为空时使用 `REFRESH_TTL_SECONDS`
    """

    def __init__(self, ttl_seconds: Optional[Mapping[NetdiskPlatform, float]] = None) -> None:
        """
        保存刷新窗口并初始化空记录。
        """
        self._ttl: dict[NetdiskPlatform, float] = dict(
            REFRESH_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        )
        self._last: dict[tuple[int, NetdiskPlatform], float] = {}

    def due(self, account_id: int, platform: NetdiskPlatform, now: Optional[float] = None) -> bool:
        """
        判断账号是否已超出刷新窗口。

        :param account_id: 账号主键
        :param platform: 平台标识
        :param now: 当前单调时钟；省略时取 `time.monotonic()`
        :return: 需要刷新返回 True；平台未配置窗口时返回 False
        """
        ttl = self._ttl.get(platform)
        if ttl is None:
            return False
        last = self._last.get((account_id, platform))
        if last is None:
            return True
        current = time.monotonic() if now is None else now
        return current - last >= ttl

    def mark(self, account_id: int, platform: NetdiskPlatform, now: Optional[float] = None) -> None:
        """
        记录账号本次刷新时间。

        :param account_id: 账号主键
        :param platform: 平台标识
        :param now: 当前单调时钟；省略时取 `time.monotonic()`
        """
        self._last[(account_id, platform)] = time.monotonic() if now is None else now

    def reset(self) -> None:
        """
        清空全部刷新记录。
        """
        self._last.clear()
