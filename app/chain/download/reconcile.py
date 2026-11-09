"""
网盘下载失败对账：回滚订阅已下载事实、冷却平台并触发换源。

失败任务本身由网盘下载器记录；本 owner 只读取下载历史与任务状态，
不直接访问模块内部存储。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional, Set

from app.application.download.failures import DownloadFailureWrite
from app.application.netdisk import selection
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.download.failure import DOWNLOAD_FAILURE_CAPACITY_TTL_SECONDS
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
            if getattr(torrent, "reconciled", False):
                continue
            state = str(getattr(torrent, "state", ""))
            partial_missing = self._partial_retry_episodes(torrent, state)
            if state != "error" and partial_missing is None:
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
            if partial_missing is not None:
                # 部分完成的订阅任务只把缺失集视为未下载，其余有效集仍算已交付。
                failed.update(partial_missing)
                continue
            failed.update(episode_rules.parse_episode_selection(
                str(getattr(history, "episodes", "") or "")
            ))
        return failed

    @staticmethod
    def _partial_retry_episodes(torrent: Any, state: Optional[str] = None) -> Optional[Set[int]]:
        """
        读取「部分完成且 AI 建议补集」任务的确定性缺失集。

        只有 ``completed + partial`` 且批次决策为 ``retry_missing`` 的任务才需要
        回滚订阅事实；AI 给出的集数不可信，一律使用本地解析的 ``missing_episodes``。

        :param torrent: 下载器任务投影
        :param state: 已读取的任务状态；缺省时从投影读取
        :return: 缺失集集合；任务不属于该形态时返回 None
        """
        task_state = str(
            state if state is not None else getattr(torrent, "state", "")
        )
        if task_state != "completed" or not bool(getattr(torrent, "partial", False)):
            return None
        decision = getattr(torrent, "ai_decision", None)
        if isinstance(decision, dict):
            decision_value = decision.get("decision")
        else:
            decision_value = getattr(decision, "decision", None)
        if str(decision_value or "") != "retry_missing":
            return None
        return {
            int(item) for item in (getattr(torrent, "missing_episodes", None) or [])
        }

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
            str(conf.download_path)
            for conf in DirectoryHelper().get_download_dirs()
            if str(getattr(conf, "download_path", "") or "").strip()
        ]
        active_paths = [
            str(torrent.path)
            for torrent in (self.list_torrents() or [])
            if getattr(torrent, "path", None)
        ]
        result = self.run_module(
            "cleanup_orphans",
            download_dirs=directories,
            active_paths=active_paths,
            is_transferred=self.is_download_file_transferred,
        )
        return result if isinstance(result, dict) else {}

    def reconcile_netdisk_failures(self) -> int:
        """
        对账网盘失败任务：回滚订阅集数、冷却账号、发送容量告警并触发重新搜索。

        :return: 本次处理的失败任务数
        """
        from app.chain.subscribe.facade import SubscribeChain

        processed = 0
        for torrent in self.list_torrents() or []:
            if not getattr(torrent, "netdisk_platform", None):
                continue
            if getattr(torrent, "reconciled", False):
                continue
            state = str(getattr(torrent, "state", ""))
            partial_missing = self._partial_retry_episodes(torrent, state)
            is_partial = partial_missing is not None
            # 失败任务与「部分完成且需要补集」的订阅任务都走同一套回滚/换源流程。
            if state != "error" and not is_partial:
                continue
            task_hash = str(getattr(torrent, "hash", "") or "")
            platform = normalize_platform(getattr(torrent, "netdisk_platform", None))
            # 容量不足只冷却实际使用的账号；解析、网络或鉴权失败不冷却任何对象，
            # 让一次代码异常演变成整个平台在冷却期内全部无法提交是不可接受的。
            account_id = int(getattr(torrent, "account_id", 0) or 0)
            if (
                not is_partial
                and platform is not None
                and account_id > 0
                and self._is_capacity_error(getattr(torrent, "error", ""))
            ):
                selection.cool_account(account_id)
            history = self.download_history_repository.get_by_hash(task_hash) if task_hash else None
            if history is not None:
                # 恢复任务可能没有内存来源，订阅标识一律以提交时写入的历史备注为准。
                note = getattr(history, "note", None)
                source = note.get("source") if isinstance(note, dict) else None
                parsed = SubscribeChain.parse_subscribe_source_keyword(str(source or ""))
                if is_partial:
                    episodes = set(partial_missing or set())
                else:
                    episodes = episode_rules.parse_episode_selection(
                        str(getattr(history, "episodes", "") or "")
                    )
                if parsed and episodes:
                    subscribe_id = parsed.get("id")
                    SubscribeChain().rollback_download_facts(subscribe_id, episodes)
                    if is_partial:
                        # 缺失集没有 error 文案，用瞬时冷却记录同一资源，
                        # 避免下一次搜索立刻再次选中这个已判定不完整的分享。
                        self._record_capacity_failure(
                            history, torrent, source,
                            self._download_failure_ttl(None),
                        )
                    else:
                        capacity_error = self._is_capacity_error(getattr(torrent, "error", ""))
                        if capacity_error:
                            self._record_capacity_failure(history, torrent, source)
                        else:
                            # 分享失效、鉴权/解析失败也要冷却，避免重搜反复选中同一失效资源。
                            self._record_capacity_failure(
                                history, torrent, source,
                                self._download_failure_ttl(getattr(torrent, "error", "")),
                            )
                        if capacity_error:
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

    def _record_capacity_failure(
        self,
        history: Any,
        torrent: Any,
        source: Any,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """
        把订阅容量失败写入下载失败台账，冷却期内跳过同一资源。

        资源指纹在提交成功时随下载历史持久化，避免对账阶段重建出的媒体身份、
        站点或资源标识与提交时不一致。

        :param history: 下载历史快照
        :param torrent: 下载器任务投影
        :param source: 下载来源关键字
        :param ttl_seconds: 冷却秒数；缺省按容量失败处理
        """
        note = getattr(history, "note", None)
        fingerprint = note.get("failure_fingerprint") if isinstance(note, dict) else None
        repository = getattr(self, "download_failure_repository", None)
        if not fingerprint or repository is None:
            logger.debug("下载历史缺少资源指纹，跳过容量失败台账写入")
            return
        now = time.time()
        failed_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        ttl = (
            ttl_seconds if ttl_seconds is not None
            else DOWNLOAD_FAILURE_CAPACITY_TTL_SECONDS
        )
        next_retry_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now + ttl),
        )
        try:
            repository.record_failure(DownloadFailureWrite(
                fingerprint=str(fingerprint),
                failed_at=failed_at,
                next_retry_at=next_retry_at,
                media_type=getattr(history, "type", None),
                title=getattr(history, "title", None),
                year=getattr(history, "year", None),
                media_source=getattr(history, "media_source", None),
                media_id=getattr(history, "media_id", None),
                seasons=getattr(history, "seasons", None),
                episodes=getattr(history, "episodes", None),
                site_name=getattr(history, "torrent_site", None),
                torrent_name=getattr(history, "torrent_name", None),
                downloader=getattr(torrent, "downloader", None),
                source=str(source)[:1000] if source else None,
                error_message=str(getattr(torrent, "error", "") or "")[:1000],
            ))
        except Exception as error:  # noqa: BLE001 - 台账写入失败不阻断对账
            logger.error(f"记录网盘容量失败台账失败：{type(error).__name__}")

    @staticmethod
    def _is_capacity_error(error: Any) -> bool:
        """
        判断失败原因是否属于容量/空间不足。

        :param error: 下载器返回的失败原因
        :return: 属于容量问题返回 True
        """
        text = str(error or "")
        return "空间不足" in text or "容量不足" in text or "SPACE_INSUFFICIENT" in text

    def _alert_netdisk_capacity(self, torrent: Any, platform: Optional[NetdiskPlatform]) -> None:
        """
        为容量不足的网盘任务发送去重告警。

        :param torrent: 下载器任务投影
        :param platform: 网盘平台
        """
        error = str(getattr(torrent, "error", "") or "")
        if (
            "空间不足" not in error
            and "容量不足" not in error
            and "SPACE_INSUFFICIENT" not in error
        ):
            return
        key = platform.value if platform is not None else "unknown"
        if not selection.should_alert(key):
            return
        self.post_message(Message(
            mtype=MessageType.Manual,
            title="网盘容量不足",
            text=(
                f"{key} 剩余空间不足，已冷却相关账号并尝试其他账号或平台资源。\n"
                f"任务：{getattr(torrent, 'title', None) or getattr(torrent, 'name', '')}\n"
                f"原因：{error}"
            ),
        ))
