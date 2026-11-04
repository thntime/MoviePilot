"""
网盘下载失败对账：回滚订阅已下载事实、冷却平台并触发换源。

失败任务本身由网盘下载器记录；本 owner 只读取下载历史与任务状态，
不直接访问模块内部存储。
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Set

from app.application.netdisk import selection
from app.chain.download.contract import _DownloadOwnerBase
from app.domain import episode as episode_rules
from app.domain.netdisk import NetdiskPlatform, normalize_platform
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import MessageType


class DownloadFailureReconcileOwner(_DownloadOwnerBase):
    """
    网盘后台失败的对账与换源编排。
    """

    def failed_netdisk_episodes(
        self,
        media_source: Any,
        media_id: Any,
        season: Optional[int] = None,
    ) -> Set[int]:
        """
        返回当前媒体在网盘失败任务中覆盖的集数。

        :param media_source: 标准媒体来源
        :param media_id: 媒体标识
        :param season: 目标季；None 表示不限季
        :return: 失败任务覆盖的集号集合
        """
        failed: Set[int] = set()
        try:
            torrents = self.list_torrents() or []
        except Exception as error:  # noqa: BLE001 - 下载器不可用时不影响订阅缺集计算
            logger.debug(f"查询网盘失败任务失败，跳过失败集数过滤：{type(error).__name__}")
            return failed
        for torrent in torrents:
            if not getattr(torrent, "netdisk_platform", None):
                continue
            if str(getattr(torrent, "state", "")) != "error":
                continue
            history = self.download_history_repository.get_by_hash(str(torrent.hash))
            if history is None:
                continue
            if str(getattr(history, "media_source", "")) != str(media_source):
                continue
            if str(getattr(history, "media_id", "")) != str(media_id):
                continue
            history_season = episode_rules.parse_season_number(
                str(getattr(history, "seasons", "") or "")
            )
            if season is not None and history_season is not None and history_season != int(season):
                continue
            failed.update(episode_rules.parse_episode_selection(
                str(getattr(history, "episodes", "") or "")
            ))
        return failed

    def is_download_file_transferred(self, fullpath: str) -> bool:
        """
        判断下载文件是否已经完成整理并被标记为删除。

        :param fullpath: 下载文件完整路径
        :return: 已整理返回 True；没有记录或仍有效时返回 False
        """
        try:
            snapshot = self.download_history_repository.get_file_by_fullpath(fullpath)
        except Exception as error:  # noqa: BLE001 - 查询失败时保守保留文件
            logger.debug(f"查询下载文件整理状态失败：{fullpath} - {type(error).__name__}")
            return False
        return bool(snapshot is not None and int(getattr(snapshot, "state", 1) or 0) == 0)

    def cleanup_netdisk_artifacts(self) -> dict[str, Any]:
        """
        清理孤儿网盘下载目录、残留转存句柄与过期任务记录。

        :return: 各类清理数量统计
        """
        from app.application.directory import DirectoryHelper

        directories = [
            str(path) for path in DirectoryHelper().get_download_dirs()
            if str(path).strip()
        ]
        result = self.run_module(
            "cleanup_orphans",
            download_dirs=directories,
            is_transferred=self.is_download_file_transferred,
        )
        return result if isinstance(result, dict) else {}

    def reconcile_netdisk_failures(self) -> int:
        """
        对账网盘失败任务：回滚订阅集数、冷却平台、发送容量告警并触发重新搜索。

        :return: 本次处理的失败任务数
        """
        from app.chain.subscribe.facade import SubscribeChain

        processed = 0
        for torrent in self.list_torrents() or []:
            if not getattr(torrent, "netdisk_platform", None):
                continue
            if str(getattr(torrent, "state", "")) != "error":
                continue
            if getattr(torrent, "reconciled", False):
                continue
            task_hash = str(getattr(torrent, "hash", "") or "")
            platform = normalize_platform(getattr(torrent, "netdisk_platform", None))
            if platform is not None:
                selection.cool_platform(platform)
            history = self.download_history_repository.get_by_hash(task_hash) if task_hash else None
            if history is not None:
                note = getattr(history, "note", None)
                source = note.get("source") if isinstance(note, dict) else None
                parsed = SubscribeChain.parse_subscribe_source_keyword(str(source or ""))
                episodes = episode_rules.parse_episode_selection(
                    str(getattr(history, "episodes", "") or "")
                )
                if parsed and episodes:
                    subscribe_id = parsed.get("id")
                    SubscribeChain().rollback_download_facts(subscribe_id, episodes)
                    self._alert_netdisk_capacity(torrent, platform)
                    threading.Thread(
                        target=SubscribeChain().search,
                        kwargs={"sid": subscribe_id},
                        daemon=True,
                        name=f"netdisk-reconcile-search-{subscribe_id}",
                    ).start()
            if task_hash:
                self.run_module("mark_reconciled", hashs=[task_hash])
            processed += 1
        if processed:
            logger.info(f"网盘失败任务对账完成：{processed} 个")
        return processed

    def _alert_netdisk_capacity(self, torrent: Any, platform: Optional[NetdiskPlatform]) -> None:
        """
        为容量不足的网盘任务发送去重告警。

        :param torrent: 下载器任务投影
        :param platform: 网盘平台
        """
        error = str(getattr(torrent, "error", "") or "")
        if "空间不足" not in error and "SPACE_INSUFFICIENT" not in error:
            return
        key = platform.value if platform is not None else "unknown"
        if not selection.should_alert(key):
            return
        self.post_message(Message(
            mtype=MessageType.Manual,
            title="网盘容量不足",
            text=(
                f"{key} 剩余空间不足，已冷却该平台并尝试其他平台资源。\n"
                f"任务：{getattr(torrent, 'title', None) or getattr(torrent, 'name', '')}\n"
                f"原因：{error}"
            ),
        ))
