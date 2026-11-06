"""
按缺集范围选择资源并编排批量下载。
"""

import copy
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple, TypedDict, Union, cast

from app.application.download import selection as _selection
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.torrent.download import TorrentHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.domain import episode as episode_rules
from app.domain.context import (
    Context,
    TorrentInfo,
)
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import (
    MediaType,
    NotificationChannel,
)

if TYPE_CHECKING:
    from app.application.download.failures import DownloadFailureSnapshot


# 只有视频文件参与集数统计，字幕/图片/归档不能制造「已下载」假象。
_VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb", ".webm", ".mpg", ".mpeg",
}

# 文件名或目录名中的显式季号；裸数字只用于目录名兜底，避免把 03.mp4 当成第 3 季。
_EXPLICIT_SEASON_PATTERN = re.compile(
    r"[Ss]\d{1,3}|第\s*\d{1,3}\s*季|[Ss]eason\s*\d{1,3}", re.IGNORECASE,
)


def _new_torrent_helper() -> TorrentHelper:
    """
    构造保留动态初始化行为但具有静态返回类型的种子助手。
    """
    factory = cast(Callable[[], TorrentHelper], TorrentHelper)
    return factory()


class DownloadBatchOwner(_DownloadOwnerBase):
    """
    按缺集范围选择资源并编排批量下载。
    """

    def batch_download(
            self,
            contexts: List[Context],
            no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
            save_path: Optional[str] = None,
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            userid: Optional[str] = None,
            username: Optional[str] = None,
            downloader: Optional[str] = None,
            custom_words: Optional[str] = None, governance: Optional[SubscriptionDownloadGovernance] = None,
            allowed_episodes: Optional[Set[int]] = None,
    ) -> Tuple[
        List[Context],
        Optional[Dict[str, Dict[int, NotExistMediaInfo]]],
    ]:
        """
        兼容批量下载公开入口，委托给内部候选匹配阶段。

        该签名被订阅链、消息入口和插件调用；内部策略拆分不改变候选排序、失败冷却、
        完整覆盖判断或剩余缺集的返回结构。
        allowed_episodes 限制本次调用的电视剧集数，在插件替换候选后仍生效；None 沿用
        既有行为，空集合拒绝所有剧集，候选自身的限制只会进一步收窄范围。
        """
        return self._execute_batch_download(
            contexts=contexts,
            no_exists=no_exists,
            save_path=save_path,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            downloader=downloader,
            custom_words=custom_words, governance=governance, allowed_episodes=allowed_episodes,
        )

    def _execute_batch_download(self,
                                contexts: List[Context],
                                no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
                                save_path: Optional[str] = None,
                                channel: Optional[NotificationChannel] = None,
                                source: Optional[str] = None,
                                userid: Optional[str] = None,
                                username: Optional[str] = None,
                                downloader: Optional[str] = None,
                                custom_words: Optional[str] = None,
                                governance: Optional[SubscriptionDownloadGovernance] = None,
                                allowed_episodes: Optional[Set[int]] = None,
                                ) -> Tuple[
        List[Context],
        Optional[Dict[str, Dict[int, NotExistMediaInfo]]],
    ]:
        """
        根据缺失数据，自动种子列表中组合择优下载

        :param contexts:  资源上下文列表
        :param no_exists:  缺失的剧集信息
        :param save_path:  保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param channel:  通知渠道
        :param source:  来源（消息通知、订阅、手工下载等）
        :param userid:  用户ID
        :param username: 调用下载的用户名/插件名
        :param downloader: 下载器
        :param custom_words: 下载来源自定义词
        :param governance: 订阅取消与下载器副作用边界
        :param allowed_episodes: 本批电视剧允许集数，在资源选择事件之后与候选限制取交集
        :return: 已下载资源列表及剩余缺集，键格式为 no_exists[source:id]
        """
        missing = no_exists if no_exists is not None else {}
        options = _BatchDownloadOptions(
            save_path=save_path, channel=channel, source=source, userid=userid,
            username=username, downloader=downloader, custom_words=custom_words,
            governance=governance,
        )
        batch = _BatchDownloadRun(self, contexts, missing, options, allowed_episodes)
        batch.run()
        return batch.downloaded, None if no_exists is None else missing


class _BatchDownloadOptions(TypedDict):
    """
    各阶段原样传递给单资源下载的调用参数，包括订阅副作用治理边界。

    :ivar save_path: 指定保存路径，可含 storage:path 前缀；None 时按目录规则选择
    :ivar channel: 用于反馈下载结果的通知渠道，None 表示未指定
    :ivar source: 下载请求的业务或消息入口来源标识
    :ivar userid: 接收下载结果通知的用户标识，未指定时为 None
    :ivar username: 发起下载的用户名或插件名称，用于历史归属和诊断
    :ivar downloader: 指定下载器配置名称，None 时按站点或系统配置选择
    :ivar custom_words: 附加的下载来源识别词，None 表示不附加
    :ivar governance: 订阅取消与下载器副作用准入边界，None 表示普通下载调用
    """

    save_path: Optional[str]
    channel: Optional[NotificationChannel]
    source: Optional[str]
    userid: Optional[str]
    username: Optional[str]
    downloader: Optional[str]
    custom_words: Optional[str]
    governance: Optional[SubscriptionDownloadGovernance]


class _BatchDownloadRun:
    """
    单次批量调用的状态；候选、失败冷却与缺集记账不保存在共享 Chain 上。
    """

    def __init__(
            self, owner: _DownloadOwnerBase, contexts: List[Context],
            no_exists: Dict[str, Dict[int, NotExistMediaInfo]], options: _BatchDownloadOptions,
            allowed_episodes: Optional[Set[int]],
    ) -> None:
        """
        隔离本批候选、集数限制及失败记账，避免共享下载链串扰。
        """
        self.owner = owner
        self.contexts = contexts
        self.no_exists = no_exists
        self.options = options
        self.allowed_episodes = set(allowed_episodes) if allowed_episodes is not None else None
        self.downloaded: List[Context] = []
        self.failures: Dict[str, Optional[DownloadFailureSnapshot]] = {}
        words = options["custom_words"]
        self.custom_words = words.splitlines() if words else None

    def run(self) -> None:
        """
        只准备一次候选，依次匹配电影/音乐、整季、完整集和拆包补集。
        """
        self.contexts, self.failures = self.owner._prepare_batch_download_contexts(
            contexts=self.contexts,
            downloader=self.options["downloader"], source=self.options["source"],
        )
        # 插件可替换整个候选列表，批次范围必须独立保留并作用于最终候选。
        if self.allowed_episodes is not None:
            for context in self.contexts:
                if context.media_info and context.media_info.type == MediaType.TV:
                    context.allowed_episodes = _selection.apply_allowed_episodes(self.allowed_episodes, context)
        self.owner._download_movie_music_candidates(
            contexts=self.contexts, downloaded_list=self.downloaded,
            active_failure_records=self.failures, **self.options,
        )
        if self.no_exists:
            self._download_whole_seasons()
        if self.no_exists:
            self._download_episode_packs()
        if self.no_exists:
            self._download_partial_packs()
        logger.info(f"成功下载种子数：{len(self.downloaded)}，剩余未下载的剧集：{self.no_exists}")

    def _in_failure_cooldown(self, context: Context) -> bool:
        """
        按持久化或本批失败指纹拦截候选，并保留原冷却日志。
        """
        fingerprint = self.owner._build_download_failure_fingerprint(context)
        if fingerprint and fingerprint in self.failures:
            self.owner._log_download_failure_cooldown(context, self.failures[fingerprint])
            return True
        return False

    def _remember_failure(self, context: Context) -> None:
        """
        按当前识别集数记录失败，后续阶段不得重复提交本轮失败候选。
        """
        fingerprint = self.owner._build_download_failure_fingerprint(context)
        if fingerprint:
            self.failures[fingerprint] = None

    def _netdisk_episodes(self, torrent: TorrentInfo) -> Optional[List[int]]:
        """
        从网盘分享的文件清单解析覆盖集数（兼容接口，返回所有季集号的并集）。

        :param torrent: 网盘候选
        :return: 升序集数列表；分享不可读或没有可解析集数时返回 None
        """
        season_episodes = self._netdisk_season_episodes(torrent)
        if not season_episodes:
            return None
        episodes: Set[int] = set()
        for numbers in season_episodes.values():
            episodes.update(numbers)
        return sorted(episodes) or None

    def _netdisk_season_episodes(
            self, torrent: TorrentInfo,
    ) -> Optional[Dict[Optional[int], Set[int]]]:
        """
        从网盘分享的文件清单按季归集覆盖集数。

        季号优先取文件名中的 `Sxx`/`第N季`，其次取相对路径目录名；无法解析季号的
        文件归入 None 键。只有视频文件参与统计。

        :param torrent: 网盘候选
        :return: 季号到集号集合的映射；分享不可读或没有可解析集数时返回 None
        """
        from app.application.netdisk.resolve import NetdiskResolveService
        from app.domain.netdisk import NetdiskError

        try:
            walked = NetdiskResolveService().walk(
                str(getattr(torrent, "enclosure", "") or ""),
                getattr(torrent, "share_password", None),
                getattr(torrent, "netdisk_platform", None),
                share_token=str(getattr(torrent, "netdisk_share_token", "") or ""),
            )
        except NetdiskError as error:
            logger.warn(f"网盘分享读取失败：{error.message}")
            return None
        except Exception as error:  # noqa: BLE001 - 网络异常按不可读处理
            logger.warn(f"网盘分享读取异常：{type(error).__name__}")
            return None
        seasons: Dict[Optional[int], Set[int]] = {}
        for item in walked.files:
            relative = str(getattr(item, "path", "") or getattr(item, "name", "") or "")
            name = Path(relative).name
            if Path(name).suffix.lower() not in _VIDEO_SUFFIXES:
                continue
            episodes = episode_rules.episode_numbers(name)
            if not episodes:
                continue
            seasons.setdefault(self._netdisk_season_of(relative), set()).update(episodes)
        return seasons or None

    @staticmethod
    def _netdisk_season_of(relative: str) -> Optional[int]:
        """
        从相对路径解析季号，文件名中的显式季号优先于目录名。

        :param relative: 分享内的相对路径（含文件名）
        :return: 解析出的季号；无法确定时返回 None
        """
        path = Path(relative)
        matched = _EXPLICIT_SEASON_PATTERN.search(path.name)
        if matched:
            return episode_rules.parse_season_number(matched.group(0))
        for part in reversed(path.parent.parts):
            matched = _EXPLICIT_SEASON_PATTERN.search(part)
            if matched:
                return episode_rules.parse_season_number(matched.group(0))
            if part.isdigit() and 0 < int(part) <= 99:
                return int(part)
        return None

    @staticmethod
    def _netdisk_target_season_episodes(
            season_episodes: Dict[Optional[int], Set[int]], season: int,
    ) -> Set[int]:
        """
        取目标季集数；整棵分享树没有任何显式季号时，用无季号文件的集号兜底。

        平铺根目录的 `第01集.mp4`/`01.mp4` 会被归入 None 键。仅当不存在任何显式
        季键时才兜底，避免把真实多季分享里的无季号文件误算进目标季。

        :param season_episodes: 季号到集号的映射
        :param season: 目标季
        :return: 目标季集号；无法确定时为空集合
        """
        episodes = set(season_episodes.get(season, set()))
        if episodes:
            return episodes
        if set(season_episodes) - {None}:
            return set()
        return set(season_episodes.get(None, set()))

    def _read_torrent(
            self, context: Context, *, partial: bool = False,
    ) -> Optional[Tuple[bytes, List[int]]]:
        """
        读取真实文件集数；保留站点失败原因用于冷却，磁力链只跳过当前阶段。
        """
        assert context.torrent_info is not None
        torrent = context.torrent_info
        meta = context.meta_info
        assert meta is not None
        if str(getattr(torrent, "download_type", "torrent") or "torrent") == "netdisk":
            episodes = self._netdisk_episodes(torrent)
            if not episodes:
                logger.info(f"{meta.org_string} 网盘分享内未解析到可用集数")
                return None
            return b"", episodes
        logger.info(f"开始下载种子 {torrent.title} ...")
        errors: List[str] = []
        content, _, files = self.owner.download_torrent(torrent, on_failure=errors.append)
        if not content:
            log_failure = logger.info if partial else logger.warn
            log_failure(f"{torrent.title} 种子下载失败！")
            self.owner._record_download_failure(
                context=context, error_msg=errors[-1] if errors else "下载种子内容为空",
                downloader=self.options["downloader"], source=self.options["source"],
            )
            self._remember_failure(context)
            governance = self.options["governance"]
            if governance:
                governance.report(context, "failed", "种子内容获取失败，未提交下载")
            return None
        if isinstance(content, str):
            reason = "无法解析" if partial else "无法确定"
            logger.warn(f"{meta.org_string} 下载地址是磁力链，{reason}种子文件集数")
            return None
        episodes = _new_torrent_helper().get_torrent_episodes(
            files, custom_words=self.custom_words,
        )
        if partial:
            logger.info(f"{torrent.site_name} - {meta.org_string} 解析种子文件集数：{episodes}")
        else:
            logger.info(f"{meta.org_string} 解析种子文件集数为 {episodes}")
        return content, episodes

    def _download_whole_seasons(self) -> None:
        """
        按候选优先级补缺失整季，仅在成功提交后扣减缺季。
        """
        logger.info(f"开始匹配电视剧整季：{self.no_exists}")
        needed: Dict[str, List[int]] = {}
        for media_key, missing_seasons in self.no_exists.items():
            for info in missing_seasons.values():
                if info and not info.episodes:
                    needed.setdefault(media_key, []).append(info.season if info.season is not None else 1)
        logger.info(f"缺失整季：{needed}")
        for media_key, need_seasons in needed.items():
            for context in self.contexts:
                if runtime_stop_state.is_system_stopped:
                    break
                seasons = self._whole_season_candidate(context, media_key, need_seasons)
                if not seasons or not self._submit_whole_season(
                        context, media_key, seasons, need_seasons,
                ):
                    continue
                # 只结算本阶段实际提交的整缺季，候选附带的部分缺集季不能一并清空。
                submitted = [season for season in seasons if season in set(need_seasons)]
                # 该 helper 会删除不在剩余 need 中的季，因此把当前全部缺失季一并传入，
                # 避免候选附带的部分缺集季被误删。
                remaining = _selection.update_no_exists_seasons(
                    self.no_exists, media_key,
                    list(self.no_exists.get(media_key, {})), submitted,
                )
                need_seasons = [season for season in need_seasons if season in set(remaining)]
                logger.info(f"{media_key} 剩余需要季：{need_seasons}")
                if not need_seasons:
                    break

    def _whole_season_candidate(
            self, context: Context, media_key: str, needed: List[int],
    ) -> List[int]:
        """
        筛选身份、季范围及完整覆盖要求都满足的整季候选。

        网盘分享可以包含多余季，只要覆盖任一缺失季即可由真实文件按目标季裁剪；
        非网盘种子仍要求候选季全部位于缺失范围内。
        """
        media, meta = context.media_info, context.meta_info
        if media is None or meta is None or media.type != MediaType.TV:
            return []
        seasons = meta.season_list or [1]
        if not self.owner._matches_media_identity(media, media_key):
            return []
        if context in self.downloaded or self._in_failure_cooldown(context):
            return []
        torrent = context.torrent_info
        is_netdisk = str(getattr(torrent, "download_type", "torrent") or "torrent") == "netdisk"
        if is_netdisk:
            if not set(seasons).intersection(needed):
                return []
        elif not set(seasons).issubset(set(needed)):
            return []
        required = _selection.get_required_episodes(self.no_exists, media_key, seasons[0])
        # 标注集数的单季包只在完整覆盖目标范围时与无集数整季包同轮按优先级竞争，
        # 否则低优先级整季包会先清空缺季，使高优先级完整包失去下载机会。
        # 准入后同样解析种子文件核对真实集数；未知总集数或部分范围仍交给后续阶段。
        # 网盘多季分享由真实文件核对覆盖范围，不依赖标题里的集数标注。
        if meta.episode_list and (
                len(seasons) != 1 or not required or not required.issubset(set(meta.episode_list))
        ) and not (is_netdisk and len(seasons) > 1):
            return []
        if not _selection.allows_full_season(seasons, required, context):
            return []
        return seasons

    def _inspect_whole_season(
            self, context: Context, media_key: str, season: int,
    ) -> Optional[Tuple[bytes, bool, Optional[Set[int]]]]:
        """
        整季不合格也保留识别集数，供后续完整集阶段继续判断该候选。

        必须按种子内真实文件写入离散集号；若用起止集连续填洞，断档中间集会被
        误记为已覆盖，后续阶段不再补下。

        :return: (下载内容, 是否完整覆盖, 网盘目标集数)；非网盘资源目标集数为 None
        """
        torrent = context.torrent_info
        assert torrent is not None
        is_netdisk = str(getattr(torrent, "download_type", "torrent") or "torrent") == "netdisk"
        if is_netdisk:
            season_episodes = self._netdisk_season_episodes(torrent)
            if not season_episodes:
                meta = context.meta_info
                assert meta is not None
                logger.info(f"{meta.org_string} 网盘分享内未解析到可用集数")
                return None
            return self._inspect_netdisk_season(context, media_key, season, season_episodes)
        parsed = self._read_torrent(context)
        if parsed is None:
            return None
        content, episodes = parsed
        if not episodes or not _selection.allows_full_pack(set(episodes), context):
            return None
        meta = context.meta_info
        assert meta is not None
        meta.set_episode_list(list(episodes))
        info = _selection.get_no_exist_media(self.no_exists, media_key, season)
        required = _selection.get_required_episodes(self.no_exists, media_key, season) \
            if _selection.requires_complete_coverage(info) else set()
        total = _selection.get_season_episodes(self.no_exists, media_key, season)
        complete = bool(required) and required.issubset(set(episodes))
        if complete:
            logger.info(
                f"{meta.org_string} 解析文件集数已完整覆盖目标范围："
                f"{episode_rules.format_ranges(sorted(required))}")
        if required and not complete:
            missing = sorted(required.difference(episodes))
            logger.info(
                f"{meta.org_string} 解析文件集数未覆盖目标范围，"
                f"缺少 {episode_rules.format_ranges(missing)}，先放弃这个种子")
            return None
        if not required and total and len(episodes) < total:
            logger.info(f"{meta.org_string} 解析文件集数发现不是完整合集，先放弃这个种子")
            return None
        return content, complete, None

    def _inspect_netdisk_season(
            self, context: Context, media_key: str, season: int,
            season_episodes: Dict[Optional[int], Set[int]], *,
            update_meta: bool = True,
    ) -> Optional[Tuple[bytes, bool, Set[int]]]:
        """
        核对网盘分享中目标季的覆盖范围，不把其他季的集号计入目标季。

        :param season_episodes: 季号到集号的映射
        :param update_meta: 是否按目标季集数改写元数据；多季整包需要保留原始识别范围
        :return: (下载内容, 是否完整覆盖, 目标集数)；目标季没有目标集时返回 None
        """
        meta = context.meta_info
        assert meta is not None
        episodes = self._netdisk_target_season_episodes(season_episodes, season)
        if not episodes:
            logger.info(f"{meta.org_string} 网盘分享内没有目标季 {season} 的集数")
            governance = self.options.get("governance")
            if governance:
                governance.report(context, "failed", f"网盘分享内没有目标季 {season} 的集数")
            return None
        if not _selection.allows_full_pack(episodes, context):
            return None
        if update_meta:
            meta.set_episodes(begin=min(episodes), end=max(episodes))
        info = _selection.get_no_exist_media(self.no_exists, media_key, season)
        required = _selection.get_required_episodes(self.no_exists, media_key, season) \
            if _selection.requires_complete_coverage(info) else set()
        target = set(required).intersection(episodes) if required else set(episodes)
        if not target:
            reason = "分享内没有目标集" if required else "分享内没有可下载集数"
            logger.info(
                f"{meta.org_string} {reason}，目标 "
                f"{episode_rules.format_ranges(sorted(required)) or '全部'}")
            governance = self.options.get("governance")
            if governance:
                governance.report(context, "failed", f"网盘分享内没有目标集：{reason}")
            return None
        logger.info(
            f"{meta.org_string} 目标集数 {episode_rules.format_ranges(sorted(target))}，"
            f"分享内 {len(episodes)} 个文件")
        complete = bool(required) and required.issubset(episodes)
        if required and not complete:
            # 整季缺集不能按整季提交并清空缺季，否则缺失集数永远不会再补。
            missing = sorted(required.difference(episodes))
            logger.info(
                f"{meta.org_string} 网盘分享未完整覆盖目标范围，"
                f"缺少 {episode_rules.format_ranges(missing)}，交给后续拆包阶段")
            return None
        return b"", complete, target

    def _inspect_whole_season_pack(
            self, context: Context, media_key: str, seasons: List[int], needed: List[int],
    ) -> Optional[Tuple[bytes, bool, Dict[int, Set[int]]]]:
        """
        逐季核对网盘整包对缺失季的覆盖，返回各目标季可提交的集数。

        :param context: 候选上下文
        :param media_key: 缺失记账的媒体键
        :param seasons: 候选覆盖的季列表
        :param needed: 本阶段需要整季补齐的季列表，部分缺集季不在其中
        :return: (下载内容, 是否完整覆盖全部目标季, 目标季到集数的映射)；
            任一目标季没有目标集时返回 None
        """
        torrent = context.torrent_info
        assert torrent is not None
        meta = context.meta_info
        assert meta is not None
        season_episodes = self._netdisk_season_episodes(torrent)
        if not season_episodes:
            logger.info(f"{meta.org_string} 网盘分享内未解析到可用集数")
            return None
        needed_seasons = set(needed)
        target_seasons = [season for season in seasons if season in needed_seasons]
        if not target_seasons:
            return None
        targets: Dict[int, Set[int]] = {}
        complete = True
        for season in target_seasons:
            inspected = self._inspect_netdisk_season(
                context, media_key, season, season_episodes, update_meta=False,
            )
            if inspected is None:
                return None
            _, season_complete, target = inspected
            targets[season] = target
            complete = complete and season_complete
        return b"", complete, targets

    def _submit_whole_season(
            self, context: Context, media_key: str, seasons: List[int], needed: List[int],
    ) -> bool:
        """
        单季核对真实文件后提交；网盘多季按目标季核对并裁剪后提交；成功后标记下载。

        :param context: 候选上下文
        :param media_key: 缺失记账的媒体键
        :param seasons: 候选覆盖的季列表
        :param needed: 本阶段需要整季补齐的季列表
        """
        torrent = context.torrent_info
        assert torrent is not None
        is_netdisk = str(getattr(torrent, "download_type", "torrent") or "torrent") == "netdisk"
        complete = False
        if len(seasons) == 1:
            single = self._inspect_whole_season(context, media_key, seasons[0])
            if single is None:
                return False
            content, complete, target_episodes = single
            logger.info(f"开始下载 {torrent.title} ...")
            options = dict(self.options)
            if target_episodes:
                options["episodes"] = set(target_episodes)
                options["episode_targets"] = {seasons[0]: set(target_episodes)}
            download_id = self.owner.download_single(
                context=context, torrent_content=content, **options,
            )
        elif is_netdisk:
            pack = self._inspect_whole_season_pack(context, media_key, seasons, needed)
            if pack is None:
                return False
            content, complete, targets = pack
            logger.info(f"开始下载 {torrent.title} ...")
            options = dict(self.options)
            if targets:
                # 多季分享按季提交目标集，扁平并集会让其他季的同集号被误下。
                options["episode_targets"] = targets
                options["episodes"] = set().union(*targets.values())
            download_id = self.owner.download_single(
                context=context, torrent_content=content, **options,
            )
        else:
            logger.info(f"开始下载 {torrent.title} ...")
            download_id = self.owner.download_single(context, **self.options)
        if not download_id:
            self._remember_failure(context)
            return False
        if complete:
            context.confirmed_full_coverage = True
        logger.info(f"{torrent.title} 添加下载成功")
        self.downloaded.append(context)
        return True

    def _download_episode_packs(self) -> None:
        """
        按季快照遍历完整集包，避免成功记账重写缺集字典时漏处理。
        """
        logger.info(f"开始电视剧完整集匹配：{self.no_exists}")
        for media_key in list(self.no_exists):
            seasons = self.no_exists.get(media_key)
            if not seasons:
                continue
            # 单季成功会重写缺集字典；每个媒体仍按阶段开始时的季快照遍历。
            for season, info in copy.deepcopy(seasons).items():
                self._download_season_episode_packs(media_key, season, info)

    def _download_season_episode_packs(
            self, media_key: str, season: int, info: NotExistMediaInfo,
    ) -> None:
        """
        优先提交能完整匹配缺集的包，逐次更新该季剩余缺集。
        """
        needed = info.episodes or list(range(info.start_episode or 1, (info.total_episode or 0) + 1))
        for context in self.contexts:
            if runtime_stop_state.is_system_stopped:
                break
            episodes = self._episode_pack_candidate(context, media_key, season, needed, info)
            if not episodes:
                continue
            meta = context.meta_info
            assert meta is not None
            logger.info(f"开始下载 {meta.title} ...")
            options = dict(self.options)
            options["episodes"] = set(episodes)
            if not self.owner.download_single(context, **options):
                self._remember_failure(context)
                continue
            if _selection.requires_complete_coverage(info):
                effective_needed = _selection.apply_allowed_episodes(set(needed), context)
                is_netdisk = str(getattr(context.torrent_info, "download_type", "torrent") or "torrent") == "netdisk"
                if not is_netdisk or set(episodes) == effective_needed:
                    context.confirmed_full_coverage = True
            logger.info(f"{meta.title} 添加下载成功")
            self.downloaded.append(context)
            needed = _selection.update_no_exists_episodes(
                self.no_exists, media_key, season, needed, episodes,
            )
            logger.info(f"季 {season} 剩余需要集：{needed}")

    def _episode_pack_candidate(
            self, context: Context, media_key: str, season: int,
            needed: List[int], info: NotExistMediaInfo,
    ) -> Set[int]:
        """
        区分完整覆盖与普通补集，返回符合本批集数限制的候选集数。
        """
        media, meta = context.media_info, context.meta_info
        if media is None or meta is None or media.type != MediaType.TV:
            return set()
        if not self.owner._matches_media_identity(media, media_key):
            return set()
        if context in self.downloaded or self._in_failure_cooldown(context):
            return set()
        if len(meta.season_list) != 1 or meta.season_list[0] != season:
            return set()
        torrent = context.torrent_info
        is_netdisk = str(getattr(torrent, "download_type", "torrent") or "torrent") == "netdisk"
        effective = _selection.apply_allowed_episodes(set(needed), context)
        if not effective:
            return set()
        if is_netdisk and torrent is not None:
            season_episodes = self._netdisk_season_episodes(torrent) or {}
            share_episodes = self._netdisk_target_season_episodes(season_episodes, season)
            if not share_episodes:
                logger.info(f"{meta.org_string} 网盘分享内没有目标季 {season} 的集数")
                return set()
            target = share_episodes.intersection(effective)
            if not target:
                logger.info(
                    f"{meta.org_string} 分享内没有目标集，"
                    f"目标 {episode_rules.format_ranges(sorted(effective))}")
                governance = self.options.get("governance")
                if governance:
                    governance.report(context, "failed", "网盘分享内没有目标集")
                return set()
            logger.info(
                f"{meta.org_string} 目标集数 {episode_rules.format_ranges(sorted(target))}，"
                f"分享内 {len(share_episodes)} 个文件")
            return target
        episodes = set(meta.episode_list)
        if not episodes:
            return set()
        if _selection.requires_complete_coverage(info):
            required = _selection.get_required_episodes(self.no_exists, media_key, season)
            matches = bool(required) and required.issubset(episodes) \
                and _selection.allows_full_pack(episodes, context)
        else:
            matches = episodes.issubset(effective)
        return episodes if matches else set()

    def _download_partial_packs(self) -> None:
        """
        只为允许部分补集且仍有明确缺集的季进入拆包阶段。
        """
        logger.info(f"开始电视剧多集拆包匹配：{self.no_exists}")
        for media_key in list(self.no_exists):
            seasons = self.no_exists.get(media_key)
            if not seasons:
                continue
            for season in list(seasons):
                info = seasons.get(season)
                if info is None or _selection.requires_complete_coverage(info) or not info.episodes:
                    continue
                self._download_season_partial_packs(media_key, season, info.episodes)

    def _download_season_partial_packs(
            self, media_key: str, season: int, needed: List[int],
    ) -> None:
        """
        按优先级解析多集包，只扣减已成功提交的选中集数。
        """
        for context in self.contexts:
            if runtime_stop_state.is_system_stopped:
                break
            media, meta = context.media_info, context.meta_info
            if media is None or meta is None or media.type != MediaType.TV:
                continue
            # 拆包阶段沿用先检查重复/冷却、再检查缺集与媒体身份的顺序。
            if context in self.downloaded or self._in_failure_cooldown(context):
                continue
            if not needed:
                break
            effective = _selection.apply_allowed_episodes(set(needed), context)
            if not effective or not self._matches_partial_pack(context, media_key, season, effective):
                continue
            selected = self._submit_partial_pack(context, effective, season)
            if selected:
                needed = _selection.update_no_exists_episodes(
                    self.no_exists, media_key, season, needed, selected,
                )
                logger.info(f"季 {season} 剩余需要集：{needed}")

    def _matches_partial_pack(
            self, context: Context, media_key: str, season: int, effective: Set[int],
    ) -> bool:
        """
        按媒体身份和单季范围预筛拆包候选，真实集数随后由种子核对。
        """
        meta = context.meta_info
        assert meta is not None
        return self.owner._matches_media_identity(context.media_info, media_key) \
            and (not meta.episode_list or bool(set(meta.episode_list).intersection(effective))) \
            and len(meta.season_list) == 1 and meta.season_list[0] == season

    def _submit_partial_pack(
            self, context: Context, effective: Set[int], season: Optional[int] = None,
    ) -> Set[int]:
        """
        只在成功提交选中文件后更新识别集数，失败时保留原候选供换源记账。

        :param context: 候选上下文
        :param effective: 本批允许下载的目标集数
        :param season: 目标季；网盘分享按该季集号核对，避免其他季同集号被误选
        """
        assert context.torrent_info is not None
        torrent = context.torrent_info
        is_netdisk = str(getattr(torrent, "download_type", "torrent") or "torrent") == "netdisk"
        if is_netdisk:
            if season is None:
                episodes = set(self._netdisk_episodes(torrent) or [])
            else:
                season_episodes = self._netdisk_season_episodes(torrent) or {}
                episodes = self._netdisk_target_season_episodes(season_episodes, season)
            if not episodes:
                logger.info(
                    f"{torrent.site_name} - {torrent.title} 网盘分享内没有目标季 {season} 的集数"
                    if season is not None
                    else f"{torrent.site_name} - {torrent.title} 网盘分享内未解析到可用集数"
                )
                return set()
            content: Union[bytes, str] = b""
        else:
            parsed = self._read_torrent(context, partial=True)
            if parsed is None:
                return set()
            content, parsed_episodes = parsed
            episodes = set(parsed_episodes)
        selected = set(episodes).intersection(effective)
        if not selected:
            logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
            return set()
        logger.info(f"{torrent.site_name} - {torrent.title} 选中集数：{selected}")
        logger.info(f"开始下载 {torrent.title} ...")
        if not self.owner.download_single(
                context=context, torrent_content=content, episodes=selected, **self.options,
        ):
            self._remember_failure(context)
            return set()
        logger.info(f"{torrent.title} 添加下载成功")
        self.downloaded.append(context)
        meta = context.meta_info
        assert meta is not None
        # 保留种子内真实文件集号，避免断档被连续区间吞掉。
        meta.set_episode_list(list(episodes))
        return selected
