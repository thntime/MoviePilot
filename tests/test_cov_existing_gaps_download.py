"""
补齐存量文件未覆盖分支：批量下载阶段、调度器兼容链与站点认证规范化。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.selection as download_selection
import app.scheduler.chain as scheduler_chain
from app.application.netdisk.resolve import NetdiskResolveService, ResolvedShare
from app.application.site.auth import (
    normalize_site_auth_config,
    normalize_site_auth_params,
)
from app.chain.download import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform, ShareDetail, ShareFile, ShareRef
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


def _context(
        name: str,
        media_id: str = "1",
        episodes: tuple[int, ...] = (),
        season: int = 1,
        media_type: MediaType = MediaType.TV,
) -> Context:
    """
    构造指定媒体身份、季和集号的批量下载候选。
    """
    meta = MetaInfo(f"{name} S{season:02d}")
    if episodes:
        meta.set_episodes(begin=min(episodes), end=max(episodes))
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title=name, type=media_type, media_source=MediaSource.TMDB, media_id=media_id,
        ),
        torrent_info=TorrentInfo(title=name, site_name="test", site=1),
    )


def _missing(
        episodes: tuple[int, ...] = (),
        total: int = 4,
        season: int = 1,
        complete: bool = False,
) -> NotExistMediaInfo:
    """
    构造单季缺集或完整覆盖要求。
    """
    return NotExistMediaInfo(
        season=season, episodes=list(episodes), total_episode=total,
        start_episode=1, require_complete_coverage=complete,
    )


@pytest.fixture(name="batch")
def fixture_batch(monkeypatch):
    """
    只替换事件、排序、持久化及下载边界，保留真实阶段判定。
    """
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_single = MagicMock(return_value="download-id")
    chain.download_torrent = MagicMock(return_value=(b"torrent", None, [1, 2, 3, 4]))
    helper = SimpleNamespace(
        sort_torrents=MagicMock(side_effect=lambda contexts: contexts),
        get_torrent_episodes=MagicMock(side_effect=lambda files, **_kwargs: files),
    )
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)
    return SimpleNamespace(chain=chain, helper=helper, stop=stop)


def _batch_run(chain, contexts=(), missing=None, governance=None):
    """
    构造单次批量状态，便于直接核对阶段内部判定。
    """
    options = {
        "save_path": None, "channel": None, "source": None, "userid": None,
        "username": None, "downloader": None, "custom_words": None,
        "governance": governance,
    }
    return download_batch._BatchDownloadRun(  # pylint: disable=protected-access
        chain, list(contexts), missing or {}, options, None,
    )


def _patch_netdisk_paths(monkeypatch, paths):
    """
    让网盘取链服务返回带相对路径的文件清单。
    """
    files = tuple(
        ShareFile(file_id=f"f{index}", name=Path(path).name, size=100, path=path)
        for index, path in enumerate(paths)
    )

    def fake_walk(self, url, password=None, platform=None, account_id=None, **kwargs):
        return ResolvedShare(
            ref=ShareRef(platform=NetdiskPlatform.QUARK, share_id="abc123", url=url),
            detail=ShareDetail(title="multiseason", size=len(files) * 100, files=files),
            files=files,
        )

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)


def _governance_stub() -> tuple[SimpleNamespace, list[tuple[object, str, str | None]]]:
    """
    记录治理回调的替身，便于断言失败原因。
    """
    calls: list[tuple[object, str, str | None]] = []

    def report(context, decision, reason=None):
        calls.append((context, decision, reason))

    return SimpleNamespace(report=report), calls


def test_netdisk_episodes_returns_none_without_parseable_numbers(batch, monkeypatch):
    """
    分享内只有无法解析集号的视频时，兼容接口返回 None 而不是空列表。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    _patch_netdisk_paths(monkeypatch, ["trailer.mp4"])
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._netdisk_episodes(candidate.torrent_info) is None  # pylint: disable=protected-access


def test_netdisk_season_episodes_treats_unexpected_error_as_unreadable(batch, monkeypatch):
    """
    非网盘分类异常的读取失败按不可读处理，不向上抛出。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(NetdiskResolveService, "walk", boom)
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._netdisk_season_episodes(candidate.torrent_info) is None  # pylint: disable=protected-access


def test_netdisk_season_of_falls_back_to_numeric_directory():
    """
    目录名为 1-99 的裸数字时按季号兜底。
    """
    assert download_batch._BatchDownloadRun._netdisk_season_of("3/01.mp4") == 3


def test_target_season_episodes_ignores_other_explicit_seasons():
    """
    分享里存在显式季键时，无季号文件不再兜底到目标季。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(
        {2: {1, 2}}, 1,
    ) == set()


def test_read_torrent_returns_netdisk_episode_union(batch, monkeypatch):
    """
    网盘候选读取时返回解析到的集数并跳过种子下载。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    _patch_netdisk_paths(monkeypatch, ["第01集.mp4", "第02集.mp4"])
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._read_torrent(candidate) == (b"", [1, 2])  # pylint: disable=protected-access
    batch.chain.download_torrent.assert_not_called()


def test_read_torrent_returns_none_when_netdisk_share_has_no_episodes(batch, monkeypatch):
    """
    网盘分享未解析到集数时按不可用处理。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    _patch_netdisk_paths(monkeypatch, ["trailer.mp4"])
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._read_torrent(candidate) is None  # pylint: disable=protected-access


def test_read_torrent_reports_governance_when_content_missing(batch):
    """
    种子内容为空时把失败原因写入治理记录。
    """
    candidate = _context("show")
    batch.chain.download_torrent.return_value = (None, None, [])
    governance, calls = _governance_stub()
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}}, governance)

    assert run._read_torrent(candidate) is None  # pylint: disable=protected-access
    assert calls == [(candidate, "failed", "种子内容获取失败，未提交下载")]


def test_whole_season_candidate_rejects_context_without_media(batch):
    """
    缺少媒体或元数据的上下文不进入整季阶段。
    """
    run = _batch_run(batch.chain, [], {"tmdb:1": {1: _missing()}})
    stub = SimpleNamespace(media_info=None, meta_info=None)

    assert run._whole_season_candidate(stub, "tmdb:1", [1]) == []  # pylint: disable=protected-access


def test_whole_season_candidate_skips_already_downloaded(batch):
    """
    本轮已提交过的候选不再重复进入整季阶段。
    """
    candidate = _context("show")
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})
    run.downloaded.append(candidate)

    assert run._whole_season_candidate(candidate, "tmdb:1", [1]) == []  # pylint: disable=protected-access


def test_whole_season_candidate_rejects_netdisk_without_needed_season(batch):
    """
    网盘候选与缺失季没有交集时仍不进入整季阶段。
    """
    candidate = _context("other", season=2)
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._whole_season_candidate(candidate, "tmdb:1", [1]) == []  # pylint: disable=protected-access


def test_inspect_netdisk_season_reports_missing_target_season(batch):
    """
    目标季在分享内不存在时记录治理失败原因。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    governance, calls = _governance_stub()
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}}, governance)

    assert run._inspect_netdisk_season(  # pylint: disable=protected-access
        candidate, "tmdb:1", 1, {2: {1}},
    ) is None
    assert calls == [(candidate, "failed", "网盘分享内没有目标季 1 的集数")]


def test_inspect_netdisk_season_respects_candidate_allowed_episodes(batch):
    """
    分享集数超出候选允许范围时整包不放行。
    """
    candidate = _context("multiseason")
    candidate.allowed_episodes = {9}
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._inspect_netdisk_season(  # pylint: disable=protected-access
        candidate, "tmdb:1", 1, {1: {1, 2}},
    ) is None


def test_inspect_netdisk_season_reports_no_target_episode_in_share(batch):
    """
    要求完整覆盖但分享内没有目标集时，记录具体原因并放弃整季。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    info = NotExistMediaInfo(
        season=1, episodes=[], total_episode=5, start_episode=5,
        require_complete_coverage=True,
    )
    governance, calls = _governance_stub()
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: info}}, governance)

    assert run._inspect_netdisk_season(  # pylint: disable=protected-access
        candidate, "tmdb:1", 1, {1: {1, 2}},
    ) is None
    assert calls == [(candidate, "failed", "网盘分享内没有目标集：分享内没有目标集")]


def test_inspect_whole_season_pack_requires_parseable_share(batch, monkeypatch):
    """
    多季整包核对时分享不可解析则直接放弃。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})
    monkeypatch.setattr(run, "_netdisk_season_episodes", lambda _torrent: None)

    assert run._inspect_whole_season_pack(  # pylint: disable=protected-access
        candidate, "tmdb:1", [1, 2], [1],
    ) is None


def test_inspect_whole_season_pack_returns_none_without_target_season(batch, monkeypatch):
    """
    候选季与需要整季补齐的季没有交集时返回 None。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})
    monkeypatch.setattr(run, "_netdisk_season_episodes", lambda _torrent: {1: {1}})

    assert run._inspect_whole_season_pack(  # pylint: disable=protected-access
        candidate, "tmdb:1", [2], [1],
    ) is None


def test_inspect_whole_season_pack_stops_when_target_season_unusable(batch, monkeypatch):
    """
    任一目标季核对失败时整包放弃，不提交部分季。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})
    monkeypatch.setattr(run, "_netdisk_season_episodes", lambda _torrent: {2: {1}})

    assert run._inspect_whole_season_pack(  # pylint: disable=protected-access
        candidate, "tmdb:1", [1], [1],
    ) is None


def test_submit_whole_season_stops_when_multiseason_pack_uninspectable(batch, monkeypatch):
    """
    多季网盘整包无法核对目标季时不提交下载。
    """
    candidate = _context("multiseason")
    candidate.meta_info.end_season = 2
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})
    monkeypatch.setattr(run, "_inspect_whole_season_pack", lambda *_args, **_kwargs: None)

    assert run._submit_whole_season(  # pylint: disable=protected-access
        candidate, "tmdb:1", [1, 2], [1],
    ) is False
    batch.chain.download_single.assert_not_called()


def test_episode_pack_phase_skips_media_without_seasons(batch):
    """
    缺失记录里没有季信息时完整集阶段直接跳过该媒体。
    """
    run = _batch_run(batch.chain, [], {"tmdb:1": {}})

    run._download_episode_packs()  # pylint: disable=protected-access

    batch.chain.download_single.assert_not_called()


def test_partial_pack_phase_skips_media_without_seasons(batch):
    """
    缺失记录里没有季信息时拆包阶段直接跳过该媒体。
    """
    run = _batch_run(batch.chain, [], {"tmdb:1": {}})

    run._download_partial_packs()  # pylint: disable=protected-access

    batch.chain.download_single.assert_not_called()


def test_partial_pack_phase_breaks_on_system_stop(batch):
    """
    拆包阶段收到停止信号后不再遍历后续候选。
    """
    candidate = _context("show")
    batch.stop.is_system_stopped = True
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing((1,))}})

    run._download_season_partial_packs("tmdb:1", 1, [1])  # pylint: disable=protected-access

    batch.chain.download_single.assert_not_called()


def test_partial_pack_phase_breaks_when_nothing_left(batch):
    """
    目标集数已全部补齐时拆包阶段提前结束。
    """
    candidate = _context("show")
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing((1,))}})

    run._download_season_partial_packs("tmdb:1", 1, [])  # pylint: disable=protected-access

    batch.chain.download_single.assert_not_called()


def test_partial_pack_skip_reason_reports_identity_mismatch(batch):
    """
    媒体身份与订阅目标不一致时给出跳过原因。
    """
    candidate = _context("other", media_id="2")
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._partial_pack_skip_reason(  # pylint: disable=protected-access
        candidate, "tmdb:1", 1, {1},
    ) == "媒体身份与订阅目标不一致"


def test_submit_partial_pack_without_season_uses_share_union(batch, monkeypatch):
    """
    未指定目标季时按分享全部集号的并集提交。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing((1,))}})
    monkeypatch.setattr(run, "_netdisk_episodes", lambda _torrent: [1, 2])

    assert run._submit_partial_pack(candidate, {1}) == {1}  # pylint: disable=protected-access
    batch.chain.download_single.assert_called_once()
    assert candidate.meta_info.episode_list == [1, 2]


def test_submit_partial_pack_reports_unparseable_target_season(batch, monkeypatch):
    """
    目标季在分享内没有可解析集数时不提交下载并记录跳过原因。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    governance, calls = _governance_stub()
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing((1,))}}, governance)
    monkeypatch.setattr(run, "_netdisk_season_episodes", lambda _torrent: {})

    assert run._submit_partial_pack(candidate, {1}, 1) == set()  # pylint: disable=protected-access
    batch.chain.download_single.assert_not_called()
    assert calls == [(candidate, "rejected", "分享内没有可解析的目标季 S1 集数")]


def test_refresh_netdisk_quotas_returns_service_counts(monkeypatch):
    """
    空间刷新入口原样返回账号服务的统计结果。
    """
    class _Service:
        """
        账号服务替身：返回固定统计。
        """

        def refresh_quotas(self):
            """
            返回固定的检查、更新与失败数量。
            """
            return {"checked": 2, "updated": 1, "failed": 0}

    monkeypatch.setattr(scheduler_chain, "NetdiskAccountService", lambda: _Service())
    chain = object.__new__(scheduler_chain.SchedulerChain)

    assert chain.refresh_netdisk_quotas() == {"checked": 2, "updated": 1, "failed": 0}


def test_clear_recycle_bins_counts_classified_netdisk_error_as_failure(monkeypatch):
    """
    非安全密钥的网盘分类异常计入失败且不中断后续账号。
    """
    views = [
        SimpleNamespace(id=1, platform="pan115", name="甲", status="active"),
        SimpleNamespace(id=2, platform="pan115", name="乙", status="active"),
    ]

    class _Store:
        """
        账号仓储替身：返回固定账号列表。
        """

        def list_accounts(self, _platform=None):
            """
            返回固定账号列表。
            """
            return views

    class _Service:
        """
        账号服务替身：第一个账号抛出分类异常。
        """

        def __init__(self) -> None:
            """
            绑定仓储替身。
            """
            self.store = _Store()

        def clear_recycle_bin(self, account_id: int) -> dict:
            """
            首个账号抛出不可恢复的分类错误。
            """
            if account_id == 1:
                raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "登录态已失效")
            return {"cleared": True}

    monkeypatch.setattr(scheduler_chain, "NetdiskAccountService", lambda: _Service())
    chain = object.__new__(scheduler_chain.SchedulerChain)

    assert chain.clear_netdisk_recycle_bins() == {
        "checked": 2, "cleared": 1, "skipped": 0, "failed": 1,
    }


def test_refresh_netdisk_accounts_reports_counts_without_failures(monkeypatch):
    """
    全部账号探测成功时只返回统计，不发送重新登录提醒。
    """
    class _Store:
        """
        账号仓储替身：返回固定数量的账号。
        """

        def list_accounts(self):
            """
            返回两个占位账号。
            """
            return [object(), object()]

    class _Service:
        """
        账号服务替身：探测无异常账号。
        """

        def __init__(self) -> None:
            """
            绑定仓储替身。
            """
            self.store = _Store()

        def refresh_all(self) -> list:
            """
            返回空的异常账号列表。
            """
            return []

    monkeypatch.setattr(scheduler_chain, "NetdiskAccountService", lambda: _Service())
    chain = object.__new__(scheduler_chain.SchedulerChain)
    posted: list = []
    chain.post_message = posted.append

    assert chain.refresh_netdisk_accounts() == {"checked": 2, "failed": 0}
    assert posted == []


def test_clear_recycle_bins_continues_after_unchanged_account(monkeypatch):
    """
    未产生清空结果的账号不计入已清空，循环继续处理后续账号。
    """
    views = [
        SimpleNamespace(id=1, platform="pan115", name="甲", status="active"),
        SimpleNamespace(id=2, platform="pan115", name="乙", status="active"),
    ]

    class _Store:
        """
        账号仓储替身：返回固定账号列表。
        """

        def list_accounts(self, _platform=None):
            """
            返回固定账号列表。
            """
            return views

    class _Service:
        """
        账号服务替身：首个账号报告无需清空。
        """

        def __init__(self) -> None:
            """
            绑定仓储替身。
            """
            self.store = _Store()

        def clear_recycle_bin(self, account_id: int) -> dict:
            """
            首个账号返回空结果，第二个账号正常清空。
            """
            return {"cleared": account_id == 2}

    monkeypatch.setattr(scheduler_chain, "NetdiskAccountService", lambda: _Service())
    chain = object.__new__(scheduler_chain.SchedulerChain)

    assert chain.clear_netdisk_recycle_bins() == {
        "checked": 2, "cleared": 1, "skipped": 0, "failed": 0,
    }


def test_normalize_site_auth_params_keeps_params_without_site():
    """
    缺少站点或参数时原样返回参数副本。
    """
    assert normalize_site_auth_params(None, {"id": 1}, {}) == {"id": 1}
    assert normalize_site_auth_params("haidan", None, {}) == {}
    assert normalize_site_auth_params("haidan", {"id": 1}, []) == {"id": 1}


def test_normalize_site_auth_params_keeps_params_without_resource_definition():
    """
    站点资源缺失或参数清单形状不符时不做规范化。
    """
    assert normalize_site_auth_params("haidan", {"id": 1}, {"haidan": "broken"}) == {"id": 1}
    assert normalize_site_auth_params(
        "haidan", {"id": 1}, {"haidan": {"params": ["id"]}},
    ) == {"id": 1}


def test_normalize_site_auth_params_skips_non_string_field_names():
    """
    资源清单里的非字符串字段名被跳过，其余字段照常转换。
    """
    auth_sites = {"haidan": {"params": {1: {"name": "序号"}, "id": {"name": "用户ID"}}}}

    assert normalize_site_auth_params("haidan", {"id": 1}, auth_sites) == {"HAIDAN_ID": 1}


def test_normalize_site_auth_params_prefers_existing_prefixed_key():
    """
    调用方已提交前缀键名时保留其值并丢弃原始字段名。
    """
    auth_sites = {"haidan": {"params": {"id": {"name": "用户ID"}}}}

    assert normalize_site_auth_params(
        "haidan", {"id": 1, "HAIDAN_ID": 2}, auth_sites,
    ) == {"HAIDAN_ID": 2}


def test_normalize_site_auth_config_rejects_empty_or_malformed_config():
    """
    空配置与非字符串站点、非映射参数都返回 None。
    """
    assert normalize_site_auth_config(None, {}) is None
    assert normalize_site_auth_config({}, {}) is None
    assert normalize_site_auth_config({"site": 1, "params": {}}, {}) is None
    assert normalize_site_auth_config({"site": "haidan", "params": []}, {}) is None
