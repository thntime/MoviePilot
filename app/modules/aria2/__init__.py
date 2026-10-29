"""
aria2 下载模块，负责任务提交、本地归属登记与任务投影。

aria2 无原生标签与做种策略，归属与标签由本地登记承担。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union, cast

from torrentool.torrent import Torrent

from app.domain import torrent as torrent_rules
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.modules._base.downloader import _DownloaderModuleBase
from app.modules.aria2.aria2 import Aria2
from app.runtime.cache import CacheBackend, FileCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.transfer import DownloaderFile, DownloaderTorrent
from app.schemas.types import (
    DownloaderType,
    DownloadTaskState,
    ModuleType,
    TorrentQueryStatus,
    TorrentStatus,
)

_ARIA2_TASK_REGION = "aria2_tasks"
_ARIA2_TASK_KEY = "tasks"

_ARIA2_PAUSED_STATES = {"paused"}
_ARIA2_DOWNLOADING_STATES = {"active", "waiting"}
_ARIA2_COMPLETED_STATES = {"complete"}
_ARIA2_FAILED_STATE = "failed"
# aria2 重启后不保留已完成任务，终态任务依赖本地快照继续对外投影。
_ARIA2_TERMINAL_FAILURE_STATES = {"error", "removed"}
_ARIA2_TERMINAL_STATES = _ARIA2_COMPLETED_STATES | _ARIA2_TERMINAL_FAILURE_STATES


def _task_cache() -> CacheBackend:
    """
    返回 aria2 本地任务登记使用的文件缓存后端。

    :return: 始终落在本地文件系统的缓存后端
    """
    return FileCache(local_only=True)


class Aria2Module(_DownloaderModuleBase[Aria2]):
    """
    aria2 下载器模块，负责任务添加、本地标签与文件选择。

    aria2 无原生标签与做种策略，归属与标签由本地登记承担。
    """

    def init_module(self) -> None:
        """
        根据系统下载器配置创建并注册本类型的客户端实例。
        """
        super().init_service(service_name=Aria2.__name__.lower(),
                             service_type=Aria2)

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "Aria2"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        """
        获取模块子类型。
        """
        return DownloaderType.Aria2

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 3

    def stop(self) -> None:
        """
        保留模块停止入口；本实现没有额外的停止动作。
        """
        pass

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        由下载器实例配置控制启用状态，不声明独立的模块开关。
        """
        pass

    def download(self, content: Union[Path, str, bytes], download_dir: Path, cookie: str,
                 episodes: Optional[Set[int]] = None, category: Optional[str] = None, label: Optional[str] = None,
                 downloader: Optional[str] = None,
                 custom_words: Optional[str] = None) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        提交直链、磁力或种子任务，并按需选择剧集文件。

        :param content: 本地种子路径、种子文件字节内容、磁力链接，或用换行连接的多条 http(s) 直链
        :param download_dir: MoviePilot 侧的下载目录，会映射为下载器路径
        :param cookie: 传给下载器的站点 Cookie
        :param episodes: 目标集数集合；种子任务非空时暂停提交并按文件选择，磁力任务降级为整包
        :param category: 下载器任务分类，作为下载目录的子路径
        :param label: 逗号分隔的任务标签；省略时使用系统种子标签
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param custom_words: 来源识别词，按行参与文件名识别，使选集结果与订阅一致
        :return: 下载器名称、任务标识、内容布局和结果说明；无适用实例时返回 None，
            提交失败时前三项为空，说明包含失败原因；相同标识的任务已存在时说明为
            ``DOWNLOAD_TASK_ALREADY_EXISTS``
        """
        if not content:
            return None, None, None, "下载内容为空"

        is_url, uris = self._split_direct_urls(content)
        is_magnet = False if isinstance(content, Path) else torrent_rules.is_magnet_link(content)
        _torrent_info, torrent_content, task_id = self._resolve_task_identity(content, label)
        if not torrent_content:
            return None, None, None, "添加下载任务失败：无法读取种子文件"

        server = self.get_instance(downloader)
        if not server:
            return None

        resolved_name = downloader or self.get_default_config_name()
        registry = self._read_registry()
        if self._has_alive_task(server, registry, task_id, resolved_name):
            return resolved_name, task_id, "Original", DOWNLOAD_TASK_ALREADY_EXISTS

        options, tags = self._build_submit_options(download_dir, category, cookie, label, downloader)
        # 种子任务需要选集时先暂停提交，拿到 gid 后再按实际文件索引设置选择范围。
        if bool(episodes) and not is_url and not is_magnet:
            options["pause"] = "true"

        gids, submit_error = self._submit_task(
            server, content, torrent_content, options, is_url, is_magnet, uris,
        )
        if submit_error:
            return None, None, None, submit_error

        gid = gids[0]
        self._apply_episode_selection(server, gid, episodes, custom_words, is_url, is_magnet)

        part_total = len(gids)
        for part, part_gid in enumerate(gids):
            self._register_task(
                gid=part_gid, task_id=task_id, tags=tags,
                downloader_name=resolved_name, part=part, part_total=part_total,
            )
        server.save_session()
        return resolved_name, task_id, "Original", "添加下载任务成功"

    @staticmethod
    def _split_direct_urls(content: Union[Path, str, bytes]) -> Tuple[bool, List[str]]:
        """
        判断内容是否为多行 http(s) 直链并拆分为逐条地址。

        :param content: 待提交的下载内容
        :return: 是否为直链内容，以及拆分后的地址列表；非直链时列表为空
        """
        if not isinstance(content, str):
            return False, []
        # 分享链接可能由多行直链组成，按行拆分并去空行后统一提交。
        candidates = [line.strip() for line in content.splitlines() if line.strip()]
        if candidates and all(
                line.lower().startswith(("http://", "https://")) for line in candidates
        ):
            return True, candidates
        return False, []

    def _resolve_task_identity(self, content: Union[Path, str, bytes],
                               label: Optional[str]) -> Tuple[Optional[Torrent], Optional[Union[str, bytes]], str]:
        """
        解析种子内容并推导稳定任务标识。

        网盘提交链通过 label 的 ``netdisk:<40位标识>`` 传入稳定任务标识，
        避免签名直链变化导致历史分裂。

        :param content: 本地种子路径、种子文件字节内容、磁力链接或多行直链
        :param label: 逗号分隔的任务标签，可能携带网盘稳定标识
        :return: 种子对象、原始种子内容与任务标识；无法读取种子内容时内容与标识为空
        """
        is_url, _uris = self._split_direct_urls(content)
        torrent_info: Optional[Torrent]
        torrent_content: Optional[Union[str, bytes]]
        if is_url:
            torrent_info, torrent_content = None, cast(str, content)
        else:
            torrent_info, torrent_content = self._get_torrent_info(content)
        if not torrent_content:
            return None, None, ""
        explicit_task_id = next(
            (item.split(":", 1)[1] for item in str(label or "").split(",")
             if item.strip().startswith("netdisk:") and len(item.strip()) == 48),
            None,
        )
        task_id: Optional[str]
        if is_url:
            # shortcut: 未提供显式标识的直链只能按整段内容摘要，调用方需保证直链稳定
            task_id = explicit_task_id or hashlib.sha1(str(content).encode("utf-8")).hexdigest()
        else:
            hashes = torrent_rules.download_hashes(torrent_content)
            task_id = next((item for item in hashes if len(item) == 40), None)
            if not task_id and torrent_info:
                task_id = torrent_info.info_hash
            if not task_id:
                task_id = hashlib.sha1(
                    torrent_content.encode("utf-8") if isinstance(torrent_content, str) else torrent_content
                ).hexdigest()
        return torrent_info, torrent_content, task_id or ""

    def _has_alive_task(self, server: Aria2, registry: Dict[str, Dict[str, Any]], task_id: str,
                        resolved_name: Optional[str]) -> bool:
        """
        检查同一任务标识是否仍有存活任务，并清理已失效的本地登记。

        只有明确报告 error/removed 才视为旧任务失效；无法识别的响应按仍在处理。

        :param server: 已配置的 aria2 客户端
        :param registry: 本地登记内容，清理失效项时原地修改
        :param task_id: 种子 infohash 或直链内容摘要
        :param resolved_name: 下载器配置名称
        :return: 存在存活任务时返回 True；失效登记已清理或没有登记时返回 False
        """
        existing_gids = self._find_gids(registry, task_id, resolved_name)
        if not existing_gids:
            return False
        for existing_gid in existing_gids:
            existing_entry = registry.get(existing_gid) or {}
            existing_info, _ = self._resolve_task_info(server, existing_gid, existing_entry)
            if not existing_info:
                continue
            raw_status = str(existing_info.get("status") or "").strip().lower()
            if raw_status not in _ARIA2_TERMINAL_FAILURE_STATES:
                logger.info(f"aria2 已存在相同任务 {task_id}，跳过重复提交")
                return True
        # 该标识下的全部 gid 都已失效且没有终态快照，清理后按新任务重新提交。
        logger.info(f"aria2 本地登记的任务 {task_id} 已失效，清理后重新提交")
        for existing_gid in existing_gids:
            registry.pop(existing_gid, None)
        self._write_registry(registry)
        return False

    def _build_submit_options(self, download_dir: Path, category: Optional[str], cookie: str,
                              label: Optional[str],
                              downloader: Optional[str]) -> Tuple[Dict[str, object], List[str]]:
        """
        构造 addUri/addTorrent 的提交选项与本地任务标签。

        系统种子标签始终参与归属过滤，网盘任务通过 label 传入稳定标识时同样需要保留。

        :param download_dir: MoviePilot 侧的下载目录，会映射为下载器路径
        :param category: 下载器任务分类，作为下载目录的子路径
        :param cookie: 传给下载器的站点 Cookie
        :param label: 逗号分隔的任务标签，``netdisk:`` 前缀项不进入标签
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 提交选项字典与去重后的标签列表
        """
        download_path = self.normalize_path(download_dir, downloader)
        if category:
            download_path = (Path(download_path) / category).as_posix()
        options: Dict[str, object] = {"dir": download_path}
        if cookie:
            options["header"] = [f"Cookie: {cookie}"]

        tags: List[str] = []
        if label:
            tags = [tag.strip() for tag in label.split(",")
                    if tag.strip() and not tag.strip().startswith("netdisk:")]
        system_tag = get_runtime_setting("TORRENT_TAG")
        if system_tag:
            tags.extend(
                tag.strip() for tag in system_tag.split(",")
                if tag.strip() and tag.strip() not in tags
            )
        return options, tags

    @staticmethod
    def _submit_task(server: Aria2, content: Union[Path, str, bytes],
                     torrent_content: Union[str, bytes], options: Dict[str, object],
                     is_url: bool, is_magnet: bool,
                     uris: List[str]) -> Tuple[List[str], Optional[str]]:
        """
        按内容类型提交任务并返回全部 gid。

        直链分享可能包含多条地址，提交失败时会回滚已添加的分片。

        :param server: 已配置的 aria2 客户端
        :param content: 原始下载内容，磁力任务需要还原为地址文本
        :param torrent_content: 种子文件内容
        :param options: 提交选项字典
        :param is_url: 内容是否为多行 http(s) 直链
        :param is_magnet: 内容是否为磁力链接
        :param uris: 直链拆分结果，仅在 is_url 为真时使用
        :return: 已提交的 gid 列表与失败说明；成功时说明为 None
        """
        gids: List[str] = []
        if is_url:
            # addUri 的 uris 参数表示同一资源的镜像地址，多文件分享必须逐条提交。
            for uri in uris:
                part_gid = server.add_uri([uri], options)
                if not part_gid:
                    for added_gid in gids:
                        server.remove(added_gid)
                    return [], "添加下载任务失败：aria2 未返回任务标识"
                gids.append(part_gid)
            return gids, None
        if is_magnet:
            uri = content.decode("utf-8") if isinstance(content, bytes) else str(content)
            part_gid = server.add_uri([uri], options)
        else:
            part_gid = server.add_torrent(cast(bytes, torrent_content), options)
        if not part_gid:
            return [], "添加下载任务失败：aria2 未返回任务标识"
        return [part_gid], None

    def _apply_episode_selection(self, server: Aria2, gid: str, episodes: Optional[Set[int]],
                                 custom_words: Optional[str], is_url: bool, is_magnet: bool) -> None:
        """
        按需为已提交的种子任务设置选集，并恢复任务运行。

        磁力任务缺少元数据时降级为整包下载，直链任务不参与选集。

        :param server: 已配置的 aria2 客户端
        :param gid: 首个分片的任务 gid
        :param episodes: 目标集数集合；为空时不做选集
        :param custom_words: 来源识别词，按行参与物理文件名识别
        :param is_url: 内容是否为多行 http(s) 直链
        :param is_magnet: 内容是否为磁力链接
        """
        if not is_url and not is_magnet and episodes:
            if not self._select_episodes(server, gid, set(episodes), custom_words):
                logger.warn(f"aria2 任务 {gid} 无法按集数选择文件，降级为整包下载")
            server.unpause(gid)
        elif is_magnet and episodes:
            logger.warn(f"aria2 磁力任务 {gid} 缺少元数据，无法按集数选择文件，降级为整包下载")

    @staticmethod
    def _select_episodes(server: Aria2, gid: str, episodes: Set[int],
                         custom_words: Optional[str]) -> bool:
        """
        按下载器返回的文件索引与来源集数对齐并设置 select-file。

        :param server: 已配置的 aria2 客户端
        :param gid: 需要选集的已暂停任务 gid
        :param episodes: 需要完整匹配的目标集数
        :param custom_words: 来源识别词，按行参与物理文件名识别
        :return: 选集选项写入成功时返回 True；无法可靠对齐时返回 False
        """
        torrent_files = server.get_files(gid)
        if not torrent_files:
            return False
        wanted: List[int] = []
        matched: Set[int] = set()
        for item in torrent_files:
            raw_index = item.get("index")
            if raw_index is None:
                return False
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                return False
            meta = MetaInfo(Path(str(item.get("path") or "")).name,
                            custom_words=custom_words.splitlines() if custom_words else [])
            file_episodes = set(meta.episode_list)
            if file_episodes and file_episodes.issubset(episodes):
                wanted.append(index)
                matched.update(file_episodes)
        if not wanted or not episodes.issubset(matched):
            return False
        return server.change_option(gid, {"select-file": ",".join(str(index) for index in sorted(wanted))})

    @staticmethod
    def _read_registry() -> Dict[str, Dict[str, Any]]:
        """
        读取本地任务登记。

        :return: gid 到归属信息的映射；文件不存在、损坏或读取失败时返回空登记
        """
        try:
            raw = _task_cache().get(_ARIA2_TASK_KEY, region=_ARIA2_TASK_REGION)
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
        except Exception as err:
            logger.warn(f"读取 aria2 本地任务登记失败，按空登记处理：{err}")
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_registry(registry: Dict[str, Dict[str, Any]]) -> bool:
        """
        写入本地任务登记。

        :param registry: 完整登记内容
        :return: 写入成功时返回 True；失败时记录中文说明并返回 False
        """
        try:
            _task_cache().set(
                _ARIA2_TASK_KEY,
                json.dumps(registry, ensure_ascii=False).encode("utf-8"),
                region=_ARIA2_TASK_REGION,
            )
            return True
        except Exception as err:
            logger.warn(f"写入 aria2 本地任务登记失败：{err}")
            return False

    def _register_task(self, gid: str, task_id: str, tags: List[str],
                       downloader_name: Optional[str], part: int = 0,
                       part_total: int = 1) -> None:
        """
        把新任务的 gid、任务标识、标签和下载器名称写入本地登记。

        :param gid: aria2 返回的任务 gid
        :param task_id: 种子 infohash 或直链内容摘要
        :param tags: 任务标签，仅保存在本地登记
        :param downloader_name: 下载器配置名称
        :param part: 同一任务标识下的分片序号，从 0 开始
        :param part_total: 同一任务标识下的分片总数
        """
        registry = self._read_registry()
        registry[gid] = {
            "hash": task_id,
            "tags": list(tags),
            "downloader": downloader_name,
            "part": part,
            "part_total": part_total,
        }
        self._write_registry(registry)

    @staticmethod
    def _find_gids(registry: Dict[str, Dict[str, Any]], task_hash: str,
                   downloader_name: Optional[str] = None) -> List[str]:
        """
        在本地登记中按任务标识反查全部 gid。

        :param registry: 本地登记内容
        :param task_hash: 种子 infohash 或直链内容摘要
        :param downloader_name: 下载器配置名称，省略时不限实例
        :return: 匹配的 gid 列表；未登记时返回空列表
        """
        return [
            gid for gid, entry in registry.items()
            if entry.get("hash") == task_hash
            and (not downloader_name or entry.get("downloader") == downloader_name)
        ]

    @staticmethod
    def _find_gid(registry: Dict[str, Dict[str, Any]], task_hash: str,
                  downloader_name: Optional[str] = None) -> Optional[str]:
        """
        在本地登记中按任务标识反查首个 gid。

        :param registry: 本地登记内容
        :param task_hash: 种子 infohash 或直链内容摘要
        :param downloader_name: 下载器配置名称，省略时不限实例
        :return: 匹配的 gid；未登记时返回 None
        """
        gids = Aria2Module._find_gids(registry, task_hash, downloader_name)
        return gids[0] if gids else None

    @staticmethod
    def _resolve_task_info(server: Aria2, gid: str,
                           entry: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        依次从活动任务、已停止任务和本地终态快照中解析任务信息。

        :param server: 已配置的 aria2 客户端
        :param gid: 需要查询的任务 gid
        :param entry: 本地登记中的归属信息
        :return: 任务字典与来源标识（status/stopped/snapshot）；都查不到时返回 (None, None)
        """
        info = server.tell_status(gid)
        if info:
            return info, "status"
        info = next((item for item in (server.tell_stopped() or []) if item.get("gid") == gid), None)
        if info:
            return info, "stopped"
        info = Aria2Module._snapshot_to_info(gid, entry)
        return (info, "snapshot") if info else (None, None)

    @staticmethod
    def _snapshot_to_info(gid: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        把本地终态快照还原为 tellStatus 形状的任务字典。

        :param gid: 任务 gid
        :param entry: 本地登记中的归属信息
        :return: 可交给任务投影的快照字典；没有有效终态快照时返回 None
        """
        snapshot = entry.get("snapshot")
        if not isinstance(snapshot, dict):
            return None
        status_text = str(snapshot.get("status") or "").strip().lower()
        if status_text not in _ARIA2_TERMINAL_STATES:
            return None
        save_dir = str(snapshot.get("dir") or "")
        files: List[Dict[str, Any]] = []
        for name in snapshot.get("files") or []:
            file_name = str(name.get("path") or "") if isinstance(name, dict) else str(name or "")
            if not file_name:
                continue
            file_path = (Path(save_dir) / file_name).as_posix() if save_dir else file_name
            files.append({"path": file_path})
        return {
            "gid": gid,
            "status": status_text,
            "dir": save_dir,
            "files": files,
            "totalLength": snapshot.get("totalLength") or 0,
            "completedLength": snapshot.get("completedLength") or 0,
        }

    @staticmethod
    def _store_snapshot(entry: Dict[str, Any], info: Dict[str, Any]) -> bool:
        """
        任务进入终态时把可离线复用的字段写入本地登记项。

        :param entry: 本地登记中的归属信息，命中终态时原地补写快照
        :param info: tellStatus 或 tellStopped 返回的任务字典
        :return: 快照发生变化、需要持久化登记时返回 True
        """
        status_text = str(info.get("status") or "").strip().lower()
        if status_text not in _ARIA2_TERMINAL_STATES:
            return False
        save_dir = str(info.get("dir") or "")
        prefix = f"{save_dir.rstrip('/')}/" if save_dir else ""
        names: List[str] = []
        for item in info.get("files") or []:
            file_path = str(item.get("path") or "")
            if not file_path:
                continue
            if prefix and file_path.startswith(prefix):
                file_path = file_path[len(prefix):]
            names.append(file_path)
        snapshot = {
            "status": status_text,
            "dir": save_dir,
            "files": names,
            "totalLength": int(info.get("totalLength") or 0),
            "completedLength": int(info.get("completedLength") or 0),
        }
        if entry.get("snapshot") == snapshot:
            return False
        entry["snapshot"] = snapshot
        return True

    @staticmethod
    def _normalize_task_state(status: Optional[str]) -> str:
        """
        归一 aria2 原始任务状态。

        :param status: aria2 的 active/waiting/paused/complete/error/removed 状态
        :return: 统一任务状态；error、removed 等终态归为失败
        """
        status_text = str(status or "").strip().lower()
        if status_text in _ARIA2_PAUSED_STATES:
            return DownloadTaskState.PAUSED.value
        if status_text in _ARIA2_DOWNLOADING_STATES:
            return DownloadTaskState.DOWNLOADING.value
        if status_text in _ARIA2_COMPLETED_STATES:
            return DownloadTaskState.COMPLETED.value
        return _ARIA2_FAILED_STATE

    def _build_torrent(self, downloader_name: Optional[str], info: Dict[str, Any],
                       entry: Dict[str, Any]) -> DownloaderTorrent:
        """
        把 aria2 任务状态投影为统一下载器任务对象。

        :param downloader_name: 下载器配置名称
        :param info: tellStatus 或 tellStopped 返回的任务字典
        :param entry: 本地登记中的归属信息
        :return: 统一任务对象
        """
        files = info.get("files") or []
        bittorrent_info = (info.get("bittorrent") or {}).get("info") or {}
        title = str(bittorrent_info.get("name") or "")
        if not title and files:
            title = Path(str(files[0].get("path") or "")).name
        if not title:
            title = str(entry.get("hash") or info.get("gid") or "")
        meta = MetaInfo(title)
        total_length = int(info.get("totalLength") or 0)
        completed_length = int(info.get("completedLength") or 0)
        state = self._normalize_task_state(info.get("status"))
        progress = completed_length / total_length * 100 if total_length else (
            100.0 if state == DownloadTaskState.COMPLETED.value else 0.0
        )
        download_speed = int(info.get("downloadSpeed") or 0)
        upload_speed = int(info.get("uploadSpeed") or 0)
        save_dir = str(info.get("dir") or "")
        if bittorrent_info.get("name"):
            content_path = Path(save_dir) / title if save_dir else Path(title)
        elif save_dir:
            # 直链任务以下载目录作为整理单元，路径不落到具体文件。
            content_path = Path(save_dir)
        else:
            content_path = Path(title)
        return DownloaderTorrent(
            downloader=downloader_name,
            hash=entry.get("hash"),
            title=title,
            name=meta.name,
            year=str(meta.year) if meta.year is not None else None,
            season_episode=meta.season_episode,
            path=Path(self.normalize_return_path(content_path, downloader_name)),
            save_path=self.normalize_return_path(Path(save_dir), downloader_name) if save_dir else None,
            content_path=self.normalize_return_path(content_path, downloader_name),
            progress=progress,
            size=total_length,
            state=state,
            dlspeed=size_tools.format_compact_size(download_speed),
            upspeed=size_tools.format_compact_size(upload_speed),
            tags=",".join(str(tag) for tag in entry.get("tags") or []),
            left_time=time_tools.format_duration(
                (total_length - completed_length) / download_speed
            ) if download_speed > 0 and total_length > completed_length else "",
        )

    @staticmethod
    def _merge_tags(members: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
        """
        合并同一任务标识下各分片的本地标签。

        :param members: (gid, 登记项) 列表
        :return: 去重后的标签列表，保持首次出现顺序
        """
        merged: List[str] = []
        for _gid, entry in members:
            for tag in entry.get("tags") or []:
                text = str(tag)
                if text and text not in merged:
                    merged.append(text)
        return merged

    @staticmethod
    def _match_query_status(state: Optional[str], query_status: TorrentQueryStatus) -> bool:
        """
        判断合并后的任务状态是否命中查询状态。

        :param state: 合并后的统一任务状态
        :param query_status: 归一后的查询状态
        :return: 命中时返回 True
        """
        if query_status == TorrentQueryStatus.TRANSFER:
            return state == DownloadTaskState.COMPLETED.value
        if query_status == TorrentQueryStatus.DOWNLOADING:
            return state == DownloadTaskState.DOWNLOADING.value
        if query_status == TorrentQueryStatus.PAUSED:
            return state == DownloadTaskState.PAUSED.value
        if query_status == TorrentQueryStatus.COMPLETED:
            return state == DownloadTaskState.COMPLETED.value
        return True

    def _build_group_torrent(self, downloader_name: Optional[str], task_hash: str,
                             parts: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]],
                             tags: List[str]) -> Optional[DownloaderTorrent]:
        """
        把同一任务标识下的多个分片合并投影为一个下载器任务。

        :param downloader_name: 下载器配置名称
        :param task_hash: 稳定任务标识
        :param parts: (gid, 登记项, 任务字典) 列表，任务字典不可用时为 None
        :param tags: 合并后的任务标签
        :return: 合并任务对象；全部分片都无法解析时返回 None
        """
        if not any(info for _gid, _entry, info in parts):
            return None
        part_total = max(
            (int(entry.get("part_total") or 1) for _gid, entry, _info in parts),
            default=1,
        )
        if part_total <= 1 and len(parts) == 1:
            _gid, entry, info = parts[0]
            return self._build_torrent(downloader_name, info, entry) if info else None

        states: List[str] = []
        total_length = 0
        completed_length = 0
        download_speed = 0
        upload_speed = 0
        save_dir = ""
        for _gid, _entry, info in parts:
            if not info:
                # 分片既不在活动/停止列表也没有终态快照，按失败处理。
                states.append(_ARIA2_FAILED_STATE)
                continue
            states.append(self._normalize_task_state(info.get("status")))
            total_length += int(info.get("totalLength") or 0)
            completed_length += int(info.get("completedLength") or 0)
            download_speed += int(info.get("downloadSpeed") or 0)
            upload_speed += int(info.get("uploadSpeed") or 0)
            if not save_dir:
                save_dir = str(info.get("dir") or "")

        if _ARIA2_FAILED_STATE in states:
            state = _ARIA2_FAILED_STATE
        elif all(item == DownloadTaskState.COMPLETED.value for item in states):
            state = DownloadTaskState.COMPLETED.value
        else:
            # 取最落后的非终态：downloading > paused > waiting。
            state = next(
                (candidate for candidate in (
                    DownloadTaskState.DOWNLOADING.value,
                    DownloadTaskState.PAUSED.value,
                    "waiting",
                ) if candidate in states),
                DownloadTaskState.DOWNLOADING.value,
            )
        progress = completed_length / total_length * 100 if total_length else (
            100.0 if state == DownloadTaskState.COMPLETED.value else 0.0
        )
        title = Path(save_dir).name if save_dir else ""
        if not title:
            title = task_hash
        meta = MetaInfo(title)
        content_path = Path(save_dir) if save_dir else Path(title)
        return DownloaderTorrent(
            downloader=downloader_name,
            hash=task_hash,
            title=title,
            name=meta.name,
            year=str(meta.year) if meta.year is not None else None,
            season_episode=meta.season_episode,
            path=Path(self.normalize_return_path(content_path, downloader_name)),
            save_path=self.normalize_return_path(Path(save_dir), downloader_name) if save_dir else None,
            content_path=self.normalize_return_path(content_path, downloader_name),
            progress=progress,
            size=total_length,
            state=state,
            dlspeed=size_tools.format_compact_size(download_speed),
            upspeed=size_tools.format_compact_size(upload_speed),
            tags=",".join(tags),
            left_time=time_tools.format_duration(
                (total_length - completed_length) / download_speed
            ) if download_speed > 0 and total_length > completed_length else "",
        )

    def list_torrents(self, status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]] = None,
                      hashs: Optional[Union[list[str], str]] = None,
                      downloader: Optional[str] = None,
                      include_all_tags: bool = False,
                      ) -> Optional[List[DownloaderTorrent]]:
        """
        将本地登记中的任务转换为统一列表，并按状态和标签筛选。

        只返回本地登记过的 gid，aria2 中的陌生任务一律忽略；同一任务标识下的多个
        分片合并为一条记录，按各分片总量汇总大小与进度。

        :param status: 任务查询状态；空值或未知值归一为查询全部
        :param hashs: 指定任务标识或列表；提供时优先于状态筛选
        :param downloader: 下载器配置名称；省略时汇总本类型的所有实例
        :param include_all_tags: 是否包含未设置系统种子标签的任务，默认只查询系统标签范围
        :return: 匹配任务列表；指定实例不可用时返回 None，查询失败的实例不贡献结果
        """
        servers: Dict[Optional[str], Aria2]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers = self.get_instances()
        query_status = self._normalize_query_status(status)
        query_tags = None if include_all_tags else get_runtime_setting("TORRENT_TAG")
        query_tag_set = {tag.strip() for tag in query_tags.split(",") if tag.strip()} if query_tags else set()
        hash_filter = {hashs} if isinstance(hashs, str) else set(hashs or [])
        registry = self._read_registry()
        ret_torrents: List[DownloaderTorrent] = []
        snapshot_dirty = False
        for name, server in servers.items():
            server_torrents, server_dirty = self._collect_server_torrents(
                name, server, registry, hash_filter, query_tag_set, query_status,
            )
            ret_torrents.extend(server_torrents)
            snapshot_dirty = snapshot_dirty or server_dirty
        if snapshot_dirty:
            self._write_registry(registry)
        return ret_torrents

    def _collect_server_torrents(self, name: Optional[str], server: Aria2, registry: Dict[str, Dict[str, Any]],
                                 hash_filter: Set[str], query_tag_set: Set[str],
                                 query_status: TorrentQueryStatus) -> Tuple[List[DownloaderTorrent], bool]:
        """
        汇总单个下载器实例下匹配的本地登记任务。

        :param name: 下载器配置名称
        :param server: 已配置的 aria2 客户端
        :param registry: 本地登记内容
        :param hash_filter: 指定任务标识集合；为空时不限标识
        :param query_tag_set: 查询标签集合；为空时不限标签
        :param query_status: 归一后的查询状态
        :return: 匹配任务列表与登记是否需要回写
        """
        stopped_cache: List[Optional[List[Dict[str, Any]]]] = [None]
        ret_torrents: List[DownloaderTorrent] = []
        snapshot_dirty = False
        for task_hash, members in self._group_registry_entries(registry, name).items():
            if hash_filter and task_hash not in hash_filter:
                continue
            tags = self._merge_tags(members)
            if query_tag_set and not query_tag_set.issubset(set(tags)):
                continue
            parts, parts_dirty = self._build_task_parts(server, members, stopped_cache)
            snapshot_dirty = snapshot_dirty or parts_dirty
            torrent = self._build_group_torrent(name, task_hash, parts, tags)
            if not torrent:
                continue
            if not self._matches_query_status(torrent.state, query_status, tags):
                continue
            ret_torrents.append(torrent)
        return ret_torrents, snapshot_dirty

    @staticmethod
    def _group_registry_entries(registry: Dict[str, Dict[str, Any]],
                                name: Optional[str]) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
        """
        把本地登记按下载器实例和任务标识分组。

        :param registry: 本地登记内容
        :param name: 下载器配置名称
        :return: 任务标识到 (gid, 登记项) 列表的映射
        """
        groups: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        for gid, entry in registry.items():
            if entry.get("downloader") != name:
                continue
            task_hash = str(entry.get("hash") or "")
            if not task_hash:
                continue
            groups.setdefault(task_hash, []).append((gid, entry))
        return groups

    def _build_task_parts(self, server: Aria2, members: List[Tuple[str, Dict[str, Any]]],
                          stopped_cache: List[Optional[List[Dict[str, Any]]]]
                          ) -> Tuple[List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]], bool]:
        """
        按分片顺序解析各 gid 的任务信息，并在终态时补写本地快照。

        :param server: 已配置的 aria2 客户端
        :param members: 同一任务标识下的 (gid, 登记项) 列表
        :param stopped_cache: 单元素可变缓存，首次需要时惰性拉取并复用 tellStopped 结果
        :return: (gid, 登记项, 任务字典) 列表与登记是否需要回写
        """
        parts: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]] = []
        snapshot_dirty = False
        for gid, entry in sorted(members, key=lambda item: int(item[1].get("part") or 0)):
            info, source = self._resolve_gid_info(server, gid, entry, stopped_cache)
            if info and source != "snapshot" and self._store_snapshot(entry, info):
                snapshot_dirty = True
            parts.append((gid, entry, info))
        return parts, snapshot_dirty

    def _resolve_gid_info(self, server: Aria2, gid: str, entry: Dict[str, Any],
                          stopped_cache: List[Optional[List[Dict[str, Any]]]]
                          ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        依次从活动任务、已停止任务和本地终态快照中解析任务信息。

        :param server: 已配置的 aria2 客户端
        :param gid: 需要查询的任务 gid
        :param entry: 本地登记中的归属信息
        :param stopped_cache: 单元素可变缓存，首次需要时惰性拉取并复用 tellStopped 结果
        :return: 任务字典与来源标识（status/stopped/snapshot）；都查不到时返回 (None, "")
        """
        info = server.tell_status(gid)
        if info:
            return info, "status"
        if stopped_cache[0] is None:
            stopped_cache[0] = server.tell_stopped() or []
        info = next((item for item in (stopped_cache[0] or []) if item.get("gid") == gid), None)
        if info:
            return info, "stopped"
        # aria2 重启后活动与已停止列表都可能查不到，改用本地终态快照投影。
        info = self._snapshot_to_info(gid, entry)
        return (info, "snapshot") if info else (None, "")

    @classmethod
    def _matches_query_status(cls, state: Optional[str], query_status: TorrentQueryStatus,
                              tags: List[str]) -> bool:
        """
        判断合并后的任务状态与标签是否命中查询状态。

        :param state: 合并后的统一任务状态
        :param query_status: 归一后的查询状态
        :param tags: 合并后的任务标签
        :return: 命中时返回 True
        """
        if query_status == TorrentQueryStatus.TRANSFER and "已整理" in tags:
            return False
        return cls._match_query_status(state, query_status)

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        完成状态由轮询提供，无需额外回调。

        :param hashs: 已完成整理的种子哈希
        :param downloader: 下载器配置名称，省略时使用默认实例
        """
        pass

    def remove_torrents(self, hashs: Union[str, list[str]], delete_file: Optional[bool] = True,
                        downloader: Optional[str] = None) -> Optional[bool]:
        """
        删除指定下载任务，并清理本地登记。

        aria2 RPC 不提供删除下载文件的选项，delete_file 仅保留签名兼容。

        :param hashs: 种子哈希或哈希列表
        :param delete_file: 是否同时删除下载文件；aria2 不支持，忽略该参数
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 全部任务删除成功时返回 True；找不到适用实例时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        registry = self._read_registry()
        targets = [hashs] if isinstance(hashs, str) else list(hashs)
        results: List[bool] = []
        for task_hash in targets:
            gids = self._find_gids(registry, task_hash, downloader)
            if not gids:
                results.append(False)
                continue
            task_results = [server.remove(gid) for gid in gids]
            for gid in gids:
                registry.pop(gid, None)
            results.append(all(task_results))
        self._write_registry(registry)
        return all(results) if results else False

    def _change_pause(self, hashs: Union[str, list[str]], downloader: Optional[str],
                      unpause: bool) -> Optional[bool]:
        """
        按任务标识批量暂停或恢复任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param unpause: 为真时恢复任务，否则暂停任务
        :return: 全部请求成功时返回 True；找不到适用实例时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        registry = self._read_registry()
        targets = [hashs] if isinstance(hashs, str) else list(hashs)
        results: List[bool] = []
        for task_hash in targets:
            gids = self._find_gids(registry, task_hash, downloader)
            if not gids:
                results.append(False)
                continue
            task_results = [
                server.unpause(gid) if unpause else server.pause(gid) for gid in gids
            ]
            results.append(all(task_results))
        return all(results) if results else False

    def start_torrents(self, hashs: Union[list[str], str],
                       downloader: Optional[str] = None) -> Optional[bool]:
        """
        启动指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 全部任务恢复成功时返回 True；找不到适用实例时返回 None
        """
        return self._change_pause(hashs, downloader, unpause=True)

    def stop_torrents(self, hashs: Union[list[str], str],
                      downloader: Optional[str] = None) -> Optional[bool]:
        """
        暂停指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 全部任务暂停成功时返回 True；找不到适用实例时返回 None
        """
        return self._change_pause(hashs, downloader, unpause=False)

    def set_torrents_tag(self, hashs: Union[str, list[str]], tags: list[str],
                         downloader: Optional[str] = None) -> Optional[bool]:
        """
        为指定下载任务追加标签。

        aria2 无原生标签，标签只写入本地登记。

        :param hashs: 种子哈希或哈希列表
        :param tags: 需要追加的标签列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 全部任务登记更新成功时返回 True；找不到适用实例时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        registry = self._read_registry()
        targets = [hashs] if isinstance(hashs, str) else list(hashs)
        results: List[bool] = []
        for task_hash in targets:
            gids = self._find_gids(registry, task_hash, downloader)
            if not gids:
                results.append(False)
                continue
            for gid in gids:
                entry = registry.get(gid) or {}
                org_tags = [str(tag) for tag in entry.get("tags") or []]
                entry["tags"] = list(dict.fromkeys(org_tags + [str(tag) for tag in tags]))
                registry[gid] = entry
            results.append(True)
        self._write_registry(registry)
        return all(results) if results else False

    @staticmethod
    def _build_file(item: Dict[str, Any], base_dir: str = "") -> DownloaderFile:
        """
        把 aria2 文件项转换为宿主文件对象。

        :param item: getFiles 返回的单个文件字典
        :param base_dir: 任务下载目录，用于把绝对路径还原为任务内相对路径
        :return: 统一文件对象
        """
        length = int(item.get("length") or 0)
        completed_length = int(item.get("completedLength") or 0)
        file_path = str(item.get("path") or "")
        prefix = f"{base_dir.rstrip('/')}/" if base_dir else ""
        if prefix and file_path.startswith(prefix):
            file_path = file_path[len(prefix):]
        return DownloaderFile(
            id=item.get("index"),
            name=file_path,
            size=length,
            priority=1 if item.get("selected") == "true" else 0,
            progress=completed_length / length * 100 if length else 0,
        )

    def torrent_files(
        self, tid: str, downloader: Optional[str] = None
    ) -> Optional[List[DownloaderFile]]:
        """
        将下载器文件列表转换为统一的文件模型。

        :param tid: 种子哈希或直链内容摘要
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 统一文件列表；找不到登记任务或客户端没有返回文件清单时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        gids = self._find_gids(self._read_registry(), tid, downloader)
        if not gids:
            return None
        ret_files: List[DownloaderFile] = []
        for gid in gids:
            status = server.tell_status(gid, keys=["dir"]) or {}
            base_dir = str(status.get("dir") or "")
            part_files = self._normalize_torrent_files(
                server.get_files(gid),
                lambda item: self._build_file(item, base_dir),
            )
            if part_files:
                ret_files.extend(part_files)
        return ret_files

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        汇总下载器的上下行速度。

        aria2 全局统计不提供累计传输量，下载与上传总量保持默认值。

        :param downloader: 下载器配置名称；省略时查询本类型的所有实例
        :return: 各可用实例的统计列表，速度单位为字节/秒；指定实例不可用时返回 None
        """
        servers: Iterable[Aria2]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = self.get_instances().values()
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            ret_info.append(_SchemaDownloaderInfo(
                download_speed=info.get("downloadSpeed"),
                upload_speed=info.get("uploadSpeed"),
            ))
        return ret_info
