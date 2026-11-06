"""
网盘下载与取链参数的系统设置实现。

参数来自 `SystemConfigKey` 注册的系统设置，未配置时使用默认值。
"""

from __future__ import annotations

from typing import Any, Optional

from app.application.configuration import get_configured_system_config
from app.application.netdisk.contract import NetdiskTuning
from app.schemas.types import SystemConfigKey

# 参数边界：线程数允许到 512，其余参数按服务端资源与风控保守收敛。
_THREADS_MIN = 1
_THREADS_MAX = 512
_MAX_TASKS_MIN = 1
_MAX_TASKS_MAX = 64
_RETRIES_MIN = 1
_RETRIES_MAX = 10
_BUFFER_MIN = 16 * 1024
_BUFFER_MAX = 1024 * 1024


def _clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    """
    把配置值夹紧到合法区间。

    :param value: 原始配置值
    :param default: 缺省值
    :param minimum: 下界
    :param maximum: 上界
    :return: 合法整数
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _flag(value: Any, default: bool) -> bool:
    """
    把配置值转换为布尔。

    :param value: 原始配置值
    :param default: 缺省值
    :return: 布尔值
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


class SystemNetdiskSettings:
    """
    从系统设置读取网盘参数快照。
    """

    def load(self) -> NetdiskTuning:
        """
        读取并校验参数。

        :return: 不可变参数快照
        """
        config = get_configured_system_config()

        def value(key: SystemConfigKey) -> Optional[Any]:
            """
            读取单个配置项。
            """
            return config.get(key)

        return NetdiskTuning(
            threads=_clamp(value(SystemConfigKey.NetdiskDownloadThreads), 32, _THREADS_MIN, _THREADS_MAX),
            max_tasks=_clamp(
                value(SystemConfigKey.NetdiskMaxConcurrentFiles),
                3,
                _MAX_TASKS_MIN,
                _MAX_TASKS_MAX,
            ),
            rate_limit=max(0, _clamp(value(SystemConfigKey.NetdiskDownloadSpeedLimit), 0, 0, 1 << 40)),
            retries=_clamp(value(SystemConfigKey.NetdiskDownloadRetries), 3, _RETRIES_MIN, _RETRIES_MAX),
            buffer_size=_clamp(
                value(SystemConfigKey.NetdiskDownloadBufferSize),
                64 * 1024,
                _BUFFER_MIN,
                _BUFFER_MAX,
            ),
            preempt_enabled=_flag(value(SystemConfigKey.NetdiskSlowPreemptEnabled), True),
            preempt_bps=_clamp(value(SystemConfigKey.NetdiskSlowPreemptMinBps), 12 * 1024, 1024, 1 << 30),
            preempt_age_seconds=_clamp(value(SystemConfigKey.NetdiskSlowPreemptMinAge), 15, 1, 300),
            multi_source=_flag(value(SystemConfigKey.NetdiskMultiSourceEnabled), False),
            auto_transfer=_flag(value(SystemConfigKey.NetdiskAutoTransferEnabled), True),
            # 转存优先是下载器实例级配置，系统设置只提供默认值。
            transfer_first=False,
            refresh_seconds=_clamp(value(SystemConfigKey.NetdiskLinkRefreshSeconds), 30, 5, 3600),
            probe_concurrency=_clamp(value(SystemConfigKey.NetdiskProbeConcurrency), 16, 1, 64),
            probe_budget_seconds=_clamp(value(SystemConfigKey.NetdiskProbeBudgetSeconds), 30, 5, 300),
            probe_limit=_clamp(value(SystemConfigKey.NetdiskProbeLimit), 200, 1, 2000),
        )
