"""
网盘账号平均速度的 JSON 持久化适配器。

速度文件位于配置目录，写入采用同目录临时文件替换；文件缺失或损坏时按空映射降级，
避免辅助指标影响下载主流程。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional

from app.foundation.environment import get_config_path
from app.runtime.log import logger

_SPEED_FILENAME = "netdisk_account_speeds.json"


class JsonNetdiskSpeedStore:
    """
    用配置目录下的 JSON 文件保存账号平均速度。

    :param path: 速度文件路径；None 表示使用配置目录下的默认文件
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        """
        绑定速度文件路径。

        :param path: 速度文件路径；None 表示使用配置目录下的默认文件
        """
        self._path = Path(path) if path is not None else get_config_path() / _SPEED_FILENAME

    def load(self) -> Dict[int, float]:
        """
        读取全部账号的平均速度。

        文件缺失、内容损坏或顶层不是对象时返回空映射；只接受正整数账号主键与正数速度。

        :return: 账号主键到字节每秒的映射
        """
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as error:  # noqa: BLE001 - 辅助指标不能影响启动
            logger.warning(f"网盘账号速度文件读取失败：error={type(error).__name__}")
            return {}
        if not isinstance(payload, dict):
            return {}
        speeds: Dict[int, float] = {}
        for key, value in payload.items():
            try:
                account_id = int(key)
                speed = float(value)
            except (TypeError, ValueError):
                continue
            if account_id > 0 and speed > 0:
                speeds[account_id] = speed
        return speeds

    def save(self, speeds: Mapping[int, float]) -> None:
        """
        原子写回全部账号的平均速度。

        先写同目录临时文件再替换目标文件；写入失败只记录告警，不向上抛出。

        :param speeds: 账号主键到字节每秒的映射
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(dict(speeds), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self._path)
        except Exception as error:  # noqa: BLE001 - 辅助指标不能影响下载
            logger.warning(f"网盘账号速度文件写入失败：error={type(error).__name__}")
