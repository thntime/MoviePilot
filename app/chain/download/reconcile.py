"""
网盘下载失败对账：回滚订阅已下载事实、冷却平台并触发换源。

失败任务本身由网盘下载器记录；本 owner 只读取下载历史与任务状态，
不直接访问模块内部存储。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence, Set

from app.application.download.failures import DownloadFailureWrite
from app.application.netdisk import selection
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.existing import find_complete_media
from app.application.netdisk.integrity import probe_video_duration
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.download.failure import DOWNLOAD_FAILURE_CAPACITY_TTL_SECONDS
from app.chain.download.local_existing import LocalExistingHit, find_local_complete
from app.domain import episode as episode_rules
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.netdisk import NetdiskPlatform, normalize_platform
from app.runtime.log import logger
from app.schemas.media import normalize_media_source
from app.schemas.message import Message
from app.schemas.types import MediaType, MessageType

# 暂停任务超过该时长仍未恢复时，其覆盖集数视为未交付，交给订阅重新搜索。
PAUSED_UNDELIVERED_AFTER_SECONDS = 1800


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
        返回当前媒体在网盘失败或暂停超时任务中覆盖的集数。

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
            paused_undelivered = self._paused_undelivered(torrent, state)
            if state != "error" and partial_missing is None and not paused_undelivered:
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

    @staticmethod
    def _paused_undelivered(torrent: Any, state: Optional[str] = None) -> bool:
        """
        判断暂停任务是否已超过交付等待窗口。

        重启中断、用户暂停与不可恢复都写 ``paused``；只有超过等待窗口且不是
        不可恢复的任务才把覆盖集数视为未交付，避免重启瞬间的暂停触发重复搜索。

        :param torrent: 下载器任务投影
        :param state: 已读取的任务状态；缺省时从投影读取
        :return: 应按未交付处理返回 True
        """
        task_state = str(
            state if state is not None else getattr(torrent, "state", "")
        )
        if task_state != "paused":
            return False
        if str(getattr(torrent, "paused_reason", "") or "") == "unrecoverable":
            return False
        paused_at = getattr(torrent, "paused_at", None)
        if not paused_at:
            # 旧记录没有暂停时间，按超时处理，符合「暂停即未交付」的修复目标。
            return True
        try:
            paused_moment = datetime.fromisoformat(str(paused_at))
        except ValueError:
            return True
        if paused_moment.tzinfo is None:
            paused_moment = paused_moment.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - paused_moment).total_seconds()
        return elapsed >= PAUSED_UNDELIVERED_AFTER_SECONDS

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
            account_id = int(getattr(torrent, "account_id", 0) or 0)
            history = self.download_history_repository.get_by_hash(task_hash) if task_hash else None
            source = None
            parsed = None
            episodes: Set[int] = set()
            if history is not None:
                # 恢复任务可能没有内存来源，订阅标识一律以提交时写入的历史备注为准。
                note = getattr(history, "note", None)
                source = note.get("source") if isinstance(note, dict) else None
                parsed = SubscribeChain.parse_subscribe_source_keyword(str(source or ""))
                if is_partial:
                    episodes = set(partial_missing or set())
                else:
                    episodes = set(episode_rules.parse_episode_selection(
                        str(getattr(history, "episodes", "") or "")
                    ))
                # 已有完整文件优先于回滚与换源：网盘个人盘命中后重新取链下载，
                # 本地命中后直接标记整理完成，两种情况都不冷却账号。
                if parsed and episodes and self._try_existing_files(
                    history, torrent, platform, account_id, episodes, source, parsed,
                ):
                    processed += 1
                    continue
            # 容量不足只冷却实际使用的账号；解析、网络或鉴权失败不冷却任何对象，
            # 让一次代码异常演变成整个平台在冷却期内全部无法提交是不可接受的。
            if (
                not is_partial
                and platform is not None
                and account_id > 0
                and self._is_capacity_error(getattr(torrent, "error", ""))
            ):
                selection.cool_account(account_id)
            if history is not None and parsed and episodes:
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

    def _try_existing_files(
        self,
        history: Any,
        torrent: Any,
        platform: Optional[NetdiskPlatform],
        account_id: int,
        episodes: Set[int],
        source: Any,
        parsed: Any,
    ) -> bool:
        """
        尝试复用网盘个人盘或本地已有的完整文件。

        先按平台查询个人盘，命中后重新提交下载；再查媒体库与下载目录，
        命中后标记整理完成。任一命中都返回 True，调用方不再回滚换源。

        :param history: 下载历史快照
        :param torrent: 下载器任务投影
        :param platform: 任务所属网盘平台
        :param account_id: 任务使用的账号主键
        :param episodes: 需要覆盖的集数
        :param source: 原下载来源
        :param parsed: 订阅来源解析结果
        :return: 命中并处理成功返回 True
        """
        if (isinstance(parsed, dict) and bool(parsed.get("existing"))) \
                or str(source or "") == "existing":
            # 复用任务只允许执行一次：再次失败必须回到回滚换源，避免死循环。
            return False
        title = str(getattr(history, "title", "") or "").strip()
        season = episode_rules.parse_season_number(
            str(getattr(history, "seasons", "") or "")
        )
        if not title or not episodes:
            return False
        if platform is not None:
            accounts = self._existing_accounts(platform, account_id)
            if accounts:
                try:
                    hit = find_complete_media(
                        accounts, title, season, set(episodes),
                    )
                except Exception as error:  # noqa: BLE001 - 查询失败继续查本地
                    logger.debug(f"网盘个人盘已有文件查询失败：{type(error).__name__}")
                    hit = None
                if hit is not None and self._resubmit_existing(
                    torrent, history, episodes, season, hit, source,
                ):
                    logger.info(
                        f"网盘个人盘已有完整文件，重新提交下载：{title} "
                        f"{sorted(episodes)}（账号 {hit.account_id}）"
                    )
                    return True
        local = self._find_local_existing(history, torrent, season, episodes)
        if local is None:
            return False
        paths = local.paths or ((local.path,) if local.path is not None else ())
        for path in paths:
            if not self._transfer_local_path(path, history, torrent):
                return False
        task_hash = str(getattr(torrent, "hash", "") or "")
        if task_hash:
            self.run_module("transfer_completed", hashs=[task_hash])
            self.run_module("mark_reconciled", hashs=[task_hash])
        logger.info(
            f"本地已有完整文件，标记整理完成：{title} {sorted(episodes)}"
            + ("（媒体库）" if local.in_library else "")
        )
        return True

    def _transfer_local_path(
        self,
        path: Path,
        history: Any,
        torrent: Any,
    ) -> bool:
        """
        触发真实整理入口；整理失败时不标记完成，让流程回落到换源。

        :param path: 下载目录中命中的完整文件
        :param history: 下载历史快照
        :param torrent: 下载器任务投影
        :return: 整理成功返回 True
        """
        from app.chain.transfer.facade import TransferChain
        from app.schemas.file import FileItem

        target = Path(path)
        try:
            stat = target.stat()
        except OSError as error:
            logger.debug(f"本地命中文件不可读，跳过整理：{target} - {type(error).__name__}")
            return False
        fileitem = FileItem(
            path=str(target),
            storage="local",
            type="file",
            name=target.name,
            basename=target.stem,
            extension=target.suffix[1:],
            size=stat.st_size,
            modify_time=stat.st_mtime,
        )
        mtype = MediaType.from_agent(str(getattr(history, "type", "") or ""))
        try:
            success, message = TransferChain().do_transfer(
                fileitem=fileitem,
                mtype=mtype,
                downloader=getattr(torrent, "downloader", None),
                download_hash=str(getattr(torrent, "hash", "") or ""),
                background=False,
            )
        except Exception as error:  # noqa: BLE001 - 整理失败按未命中处理
            logger.warning(f"本地命中文件整理失败：{target} - {type(error).__name__}")
            return False
        if not success:
            logger.warning(f"本地命中文件整理未成功：{target} - {message}")
            return False
        return True

    def _existing_accounts(
        self,
        platform: NetdiskPlatform,
        account_id: int,
    ) -> list[tuple[int, str]]:
        """
        按任务账号优先返回同平台可用账号。

        :param platform: 网盘平台
        :param account_id: 任务使用的账号主键
        :return: 账号主键与展示名列表；查询失败时返回空列表
        """
        try:
            views = NetdiskAccountService().list(platform)
        except Exception as error:  # noqa: BLE001 - 账号查询失败按未命中处理
            logger.debug(f"查询网盘账号失败：{type(error).__name__}")
            return []
        accounts = [
            (int(view.id), str(view.name))
            for view in views
            if str(getattr(view, "status", "active") or "active")
            not in {"expired", "disabled"}
        ]
        accounts.sort(key=lambda item: item[0] != account_id)
        return accounts

    def _find_local_existing(
        self,
        history: Any,
        torrent: Any,
        season: Optional[int],
        episodes: Set[int],
    ) -> Optional[LocalExistingHit]:
        """
        检查媒体库与任务相关下载目录是否已有完整文件。

        :param history: 下载历史快照
        :param torrent: 下载器任务投影
        :param season: 目标季
        :param episodes: 需要覆盖的集数
        :return: 命中结果；未命中或查询失败时返回 None
        """
        try:
            return find_local_complete(
                str(getattr(history, "title", "") or ""),
                season,
                set(episodes),
                self._download_dirs(history, torrent),
                media_exists=lambda: self._media_library_exists(
                    history, season, episodes,
                ),
                probe=probe_video_duration,
            )
        except Exception as error:  # noqa: BLE001 - 本地扫描失败按未命中处理
            logger.debug(f"本地已有文件查询失败：{type(error).__name__}")
            return None

    def _media_library_exists(
        self,
        history: Any,
        season: Optional[int],
        episodes: Set[int],
    ) -> bool:
        """
        用下载历史构造最小媒体身份，查询媒体库是否已有目标集数。

        :param history: 下载历史快照
        :param season: 目标季
        :param episodes: 需要覆盖的集数
        :return: 媒体库已存在目标集数返回 True
        """
        getter = getattr(self, "get_no_exists_info", None)
        if getter is None or season is None:
            return False
        media_source = normalize_media_source(getattr(history, "media_source", None))
        media_id = str(getattr(history, "media_id", "") or "").strip()
        title = str(getattr(history, "title", "") or "").strip()
        if media_source is None or not media_id or not title:
            return False
        try:
            meta = MetaBase(title=title)
            meta.type = MediaType.TV
            meta.begin_season = int(season)
            meta.end_season = int(season)
            meta.begin_episode = min(episodes)
            meta.end_episode = max(episodes)
            mediainfo = MediaInfo(
                type=MediaType.TV,
                title=title,
                media_source=media_source,
                media_id=media_id,
                season=int(season),
                seasons={int(season): sorted(episodes)},
            )
            exists, _ = getter(meta, mediainfo)
            return bool(exists)
        except Exception as error:  # noqa: BLE001 - 媒体库查询失败按未命中处理
            logger.debug(f"媒体库存在性查询失败：{type(error).__name__}")
            return False

    @staticmethod
    def _download_dirs(history: Any, torrent: Any) -> list[str]:
        """
        收集任务相关的下载目录。

        :param history: 下载历史快照
        :param torrent: 下载器任务投影
        :return: 去重后的目录路径列表
        """
        result: list[str] = []
        for value in (
            getattr(torrent, "save_path", None),
            getattr(torrent, "path", None),
            getattr(history, "path", None),
        ):
            raw = str(value or "").strip()
            if not raw:
                continue
            path = Path(raw)
            if path.is_file():
                path = path.parent
            text = str(path)
            if text not in result:
                result.append(text)
        return result

    @staticmethod
    def _existing_download_dir(torrent: Any, history: Any) -> Optional[Path]:
        """
        确定复用任务的下载根目录，缺少任务路径时回退到配置的下载目录。

        :param torrent: 下载器任务投影
        :param history: 下载历史快照
        :return: 下载目录；无法确定时返回 None
        """
        base = (
            getattr(torrent, "save_path", None)
            or getattr(torrent, "path", None)
            or getattr(history, "path", None)
        )
        if base:
            return Path(str(base)).parent
        from app.application.directory import DirectoryHelper

        for conf in DirectoryHelper().get_download_dirs():
            raw = str(getattr(conf, "download_path", "") or "").strip()
            if raw:
                return Path(raw)
        return None

    def _resubmit_existing(
        self,
        torrent: Any,
        history: Any,
        episodes: Set[int],
        season: Optional[int],
        hit: Any,
        source: Any,
    ) -> bool:
        """
        用个人盘已有文件重新提交下载任务。

        先删除旧任务记录以清理残留转存句柄，再调用网盘模块的个人盘下载入口，
        不解析分享链接、不转存。

        :param torrent: 下载器任务投影
        :param history: 下载历史快照
        :param episodes: 需要覆盖的集数
        :param season: 目标季
        :param hit: 已有文件命中结果
        :param source: 原下载来源
        :return: 已提交返回 True；缺少必要信息时返回 False
        """
        files = list(getattr(hit, "files", None) or [])
        if not files:
            return False
        download_dir = self._existing_download_dir(torrent, history)
        if download_dir is None:
            logger.warning("无法确定复用任务下载目录，回落到换源")
            return False
        source_marker = self._existing_source(source)
        try:
            result = self.run_module(
                "download_existing",
                account_id=int(getattr(hit, "account_id", 0) or 0),
                files=files,
                download_dir=download_dir,
                category=getattr(history, "media_category", None),
                episodes=set(episodes),
                season=season,
                source=source_marker,
                media_runtime=self._history_media_runtime(history),
                downloader=getattr(torrent, "downloader", None),
                title=getattr(history, "title", None),
            )
        except Exception as error:  # noqa: BLE001 - 提交失败回落到换源
            logger.warning(f"复用已有文件提交失败：{type(error).__name__}")
            return False
        new_hash = ""
        if isinstance(result, tuple) and len(result) > 1:
            new_hash = str(result[1] or "")
        if not new_hash:
            logger.warning("复用已有文件提交未返回任务标识，回落到换源")
            return False
        task_hash = str(getattr(torrent, "hash", "") or "")
        if task_hash and task_hash != new_hash:
            removed = False
            try:
                removed = bool(self.run_module(
                    "remove_torrents", hashs=[task_hash], delete_file=False,
                ))
            except Exception as error:  # noqa: BLE001 - 旧任务清理失败不阻断新任务
                logger.warning(f"清理旧网盘任务失败：{type(error).__name__}")
            if not removed:
                # 旧任务记录仍在时标记已对账，避免下一轮再次复用同一失败任务。
                logger.warning(f"清理旧网盘任务未成功，标记已对账：{task_hash}")
                try:
                    self.run_module("mark_reconciled", hashs=[task_hash])
                except Exception as error:  # noqa: BLE001 - 标记失败不影响新任务
                    logger.warning(f"标记旧网盘任务已对账失败：{type(error).__name__}")
        self._record_existing_history(
            history, torrent, new_hash, source_marker,
            download_dir, int(getattr(hit, "account_id", 0) or 0), files,
            episodes,
        )
        return True

    @staticmethod
    def _history_media_runtime(history: Any) -> Optional[int]:
        """
        从下载历史备注中读取单集时长，避免复用任务跳过时长校验。

        :param history: 下载历史快照
        :return: 单集时长（分钟）；未知时返回 None
        """
        note = getattr(history, "note", None)
        if not isinstance(note, dict):
            return None
        value = note.get("media_runtime")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def _record_existing_history(
        self,
        history: Any,
        torrent: Any,
        download_hash: str,
        source_marker: str,
        download_dir: Path,
        account_id: int,
        files: Sequence[Any],
        episodes: Set[int],
    ) -> None:
        """
        为复用任务补写下载历史与文件行，供下一次失败对账读取 `existing` 标记。

        :param history: 原下载历史快照
        :param torrent: 原下载器任务投影
        :param download_hash: 新任务标识
        :param source_marker: 带 `existing` 标记的来源
        :param download_dir: 新任务下载根目录
        :param account_id: 文件所在账号主键
        :param files: 复用任务的文件条目
        :param episodes: 本次实际重新提交的集数
        """
        from app.application.history import DownloadFileWrite, DownloadHistoryWrite

        repository = getattr(self, "download_history_repository", None)
        add = getattr(repository, "add", None)
        if add is None:
            return
        note = getattr(history, "note", None)
        payload = dict(note) if isinstance(note, dict) else {}
        payload["source"] = source_marker
        task_dir = download_dir / f"existing-{account_id}-{download_hash[:6]}"
        file_rows = tuple(
            DownloadFileWrite(
                downloader=getattr(torrent, "downloader", None),
                download_hash=download_hash,
                fullpath=str(task_dir / str(getattr(item, "name", "") or "")),
                savepath=str(task_dir),
                filepath=str(getattr(item, "name", "") or ""),
                torrentname=str(getattr(item, "name", "") or ""),
            )
            for item in files
            if str(getattr(item, "name", "") or "").strip()
        )
        try:
            add(DownloadHistoryWrite(
                path=str(task_dir),
                type=str(getattr(history, "type", "") or ""),
                title=str(getattr(history, "title", "") or ""),
                year=getattr(history, "year", None),
                media_source=getattr(history, "media_source", None),
                media_id=getattr(history, "media_id", None),
                music_type=getattr(history, "music_type", None),
                seasons=getattr(history, "seasons", None),
                episodes=episode_rules.format_ranges(sorted(episodes)),
                image=getattr(history, "image", None),
                poster=getattr(history, "poster", None),
                downloader=getattr(torrent, "downloader", None),
                download_hash=download_hash,
                torrent_name=getattr(history, "torrent_name", None),
                torrent_description=getattr(history, "torrent_description", None),
                torrent_site=getattr(history, "torrent_site", None),
                userid=getattr(history, "userid", None),
                username=getattr(history, "username", None),
                channel=getattr(history, "channel", None),
                date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                note=payload,
                media_category_id=getattr(history, "media_category_id", None),
                media_category=getattr(history, "media_category", None),
                classification_rule_id=getattr(history, "classification_rule_id", None),
                classification_policy_revision=getattr(
                    history, "classification_policy_revision", None,
                ),
                classification_source=getattr(history, "classification_source", None),
                episode_group=getattr(history, "episode_group", None),
                custom_words=getattr(history, "custom_words", None),
            ), file_rows)
        except Exception as error:  # noqa: BLE001 - 历史补写失败不影响下载提交
            logger.debug(f"补写复用任务下载历史失败：{type(error).__name__}")

    @staticmethod
    def _existing_source(source: Any) -> str:
        """
        构造复用任务来源标记，供下一次失败对账识别并避免重复复用。

        :param source: 原下载来源
        :return: 订阅来源追加 `existing` 字段；其他来源使用 `existing`
        """
        from app.chain.subscribe.facade import SubscribeChain

        text = str(source or "").strip()
        parsed = SubscribeChain.parse_subscribe_source_keyword(text)
        if parsed:
            payload = dict(parsed)
            payload["existing"] = True
            return "Subscribe|" + json.dumps(payload, ensure_ascii=False)
        return "existing"

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
