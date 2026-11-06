"""
保护批量下载各阶段之间的状态传递与副作用顺序。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.selection as download_selection
from app.application.netdisk.resolve import NetdiskResolveService, ResolvedShare
from app.chain.download import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.domain.netdisk import NetdiskPlatform, ShareDetail, ShareFile, ShareRef
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType, NotificationChannel


def _context(
        name: str,
        media_id: str = "1",
        episodes: tuple[int, ...] = (),
        season: int = 1,
        media_type: MediaType = MediaType.TV,
) -> Context:
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
    return NotExistMediaInfo(
        season=season, episodes=list(episodes), total_episode=total,
        start_episode=1, require_complete_coverage=complete,
    )


@pytest.fixture
def batch(monkeypatch):
    """
    只替换事件、排序、持久化及下载边界，保留真实阶段选择和失败指纹。
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


def _submitted(chain) -> list[Context]:
    return [
        call.args[0] if call.args else call.kwargs["context"]
        for call in chain.download_single.call_args_list
    ]


def _batch_run(chain, contexts=(), missing=None):
    """
    构造单次批量状态，便于直接核对阶段内部判定。
    """
    options = {
        "save_path": None, "channel": None, "source": None, "userid": None,
        "username": None, "downloader": None, "custom_words": None, "governance": None,
    }
    return download_batch._BatchDownloadRun(chain, list(contexts), missing or {}, options, None)


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


def test_batch_preserves_phase_order_and_submission_options(batch):
    """
    批量下载保持各媒体阶段顺序，并完整传递提交选项。
    """
    partial = _context("partial", media_id="3")
    labelled = _context("labelled", media_id="2", episodes=(2,))
    whole = _context("whole")
    movie = _context("movie", media_type=MediaType.MOVIE)
    music = _context("music", media_type=MediaType.MUSIC)
    missing = {
        "tmdb:3": {1: _missing((3,))},
        "tmdb:2": {1: _missing((2,))},
        "tmdb:1": {1: _missing()},
    }
    options = {
        "save_path": "smb:/server/media",
        "channel": NotificationChannel.Telegram,
        "source": "Subscribe|42",
        "userid": "user-id",
        "username": "plugin-name",
        "downloader": "test-downloader",
        "custom_words": "first\nsecond",
        "governance": object(),
    }
    result, remaining = batch.chain.batch_download(
        [partial, labelled, whole, movie, music], missing, **options,
    )

    assert result == [movie, music, whole, labelled, partial]
    assert _submitted(batch.chain) == result
    assert remaining is missing
    assert remaining == {}
    assert batch.chain.eventmanager.send_event.call_count == 1
    assert batch.helper.sort_torrents.call_count == 1
    assert batch.chain._active_download_failure_fingerprints.call_count == 1
    for call in batch.chain.download_single.call_args_list:
        assert {key: call.kwargs[key] for key in options} == options
    calls = batch.chain.download_single.call_args_list
    assert "torrent_content" not in calls[0].kwargs
    assert calls[2].kwargs["torrent_content"] == b"torrent"
    assert "torrent_content" not in calls[3].kwargs
    assert calls[4].kwargs["episodes"] == {3}
    assert all(
        call.kwargs["custom_words"] == ["first", "second"]
        for call in batch.helper.get_torrent_episodes.call_args_list
    )


def test_incomplete_season_becomes_labelled_candidate_after_file_inspection(batch):
    """
    整季候选内容不足时按解析出的剧集转入带集数候选阶段。
    """
    candidate = _context("incomplete")
    missing = {"tmdb:1": {1: _missing()}}
    batch.chain.download_torrent.return_value = (b"torrent", None, [1, 2])

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert remaining is missing
    assert remaining["tmdb:1"][1].episodes == [3, 4]
    assert candidate.meta_info.episode_list == [1, 2]
    batch.chain.download_torrent.assert_called_once()
    batch.chain.download_single.assert_called_once()
    assert "torrent_content" not in batch.chain.download_single.call_args.kwargs
    batch.chain._record_download_failure.assert_not_called()


def test_failed_season_submission_stays_in_cooldown_after_metadata_changes(batch):
    """
    整季下载失败后，即使剧集元数据变化也继续受到失败冷却约束。
    """
    whole = _context("whole")
    fallback = _context("fallback", episodes=(1,))
    missing = {"tmdb:1": {1: _missing(total=3)}}
    batch.chain.download_torrent.return_value = (b"torrent", None, [1, 2, 3])
    batch.chain.download_single.side_effect = [None, "fallback-id"]

    result, remaining = batch.chain.batch_download([whole, fallback], missing)

    assert result == [fallback]
    assert _submitted(batch.chain) == [whole, fallback]
    assert remaining["tmdb:1"][1].episodes == [2, 3]
    assert whole.meta_info.episode_list == [1, 2, 3]
    batch.chain.download_torrent.assert_called_once()
    assert batch.chain._log_download_failure_cooldown.call_count == 2


def test_empty_torrent_is_recorded_once_and_not_retried_in_later_phases(batch):
    """
    空种子失败只记录一次，后续阶段不重复尝试。
    """
    whole = _context("empty")
    fallback = _context("fallback", episodes=(1,))
    missing = {"tmdb:1": {1: _missing(total=3)}}
    batch.chain.download_torrent.return_value = (None, "failed", [])

    result, remaining = batch.chain.batch_download(
        [whole, fallback], missing, source="Subscribe|42", downloader="test",
    )

    assert result == [fallback]
    assert remaining["tmdb:1"][1].episodes == [2, 3]
    batch.chain.download_torrent.assert_called_once()
    batch.chain._record_download_failure.assert_called_once_with(
        context=whole, error_msg="下载种子内容为空", downloader="test", source="Subscribe|42",
    )


def test_uninspectable_magnet_is_not_cooled_down_as_a_download_failure(batch):
    """
    无法检查文件的磁力链接不被误判为下载失败冷却。
    """
    whole = _context("magnet")
    fallback = _context("fallback", episodes=(1,))
    missing = {"tmdb:1": {1: _missing(total=3)}}
    batch.chain.download_torrent.return_value = ("magnet:?xt=test", None, [])

    result, remaining = batch.chain.batch_download([whole, fallback], missing)

    assert result == [fallback]
    assert remaining["tmdb:1"][1].episodes == [2, 3]
    assert batch.chain.download_torrent.call_count == 2
    batch.chain._record_download_failure.assert_not_called()
    batch.chain._log_download_failure_cooldown.assert_not_called()
    batch.helper.get_torrent_episodes.assert_not_called()


def test_netdisk_share_skips_whole_season_file_inspection(batch):
    """
    网盘分享不是种子，整季阶段直接跳过文件检查，不触发种子下载与集数解析。
    """
    netdisk = _context("netdisk")
    netdisk.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing()}}

    result, remaining = batch.chain.batch_download([netdisk], missing)

    assert result == []
    assert remaining is missing
    assert remaining == missing
    batch.chain.download_torrent.assert_not_called()
    batch.helper.get_torrent_episodes.assert_not_called()
    batch.chain._record_download_failure.assert_not_called()


def test_partial_packs_only_settle_successful_allowed_episode_selections(batch):
    """
    分集包仅结算成功提交且在允许范围内的剧集。
    """
    failed = _context("failed", episodes=(1, 4))
    first = _context("first")
    first.allowed_episodes = {1, 3}
    second = _context("second")
    missing = {"tmdb:1": {1: _missing((1, 2, 3))}}
    batch.chain.download_torrent.side_effect = [
        (b"failed", None, [1, 2, 4]),
        (b"first", None, [1, 3, 4]),
        (b"second", None, [2, 4]),
    ]
    needs_at_submission = []

    def submit(**kwargs):
        needs_at_submission.append(list(missing["tmdb:1"][1].episodes))
        return None if kwargs["context"] is failed else "id"

    batch.chain.download_single.side_effect = submit
    result, remaining = batch.chain.batch_download([failed, first, second], missing)

    assert result == [first, second]
    assert remaining is missing
    assert remaining == {}
    assert needs_at_submission == [[1, 2, 3], [1, 2, 3], [2]]
    assert [
        call.kwargs["episodes"] for call in batch.chain.download_single.call_args_list
    ] == [{1, 2}, {1, 3}, {2}]
    assert first.meta_info.episode_list == [1, 2, 3, 4]
    assert second.meta_info.episode_list == [2, 3, 4]
    assert failed.meta_info.episode_list == [1, 2, 3, 4]


@pytest.mark.parametrize("missing", [None, {}])
def test_batch_preserves_optional_missing_result_and_resets_per_call_state(batch, missing):
    """
    批量调用保留可选缺集结果的对象语义，并重置每次调用状态。
    """
    candidate = _context("movie", media_type=MediaType.MOVIE)
    batch.chain.download_single.side_effect = [None, "retry-id"]

    first, first_remaining = batch.chain.batch_download([candidate], missing)
    second, second_remaining = batch.chain.batch_download([candidate], missing)

    assert first == []
    assert second == [candidate]
    assert first_remaining is missing
    assert second_remaining is missing
    assert batch.chain.download_single.call_count == 2


def test_reentrant_batch_call_keeps_outer_candidates_and_accounting(batch):
    """
    下载回调触发另一批次时，两次调用不能共用候选、结果或缺集状态。
    """
    movie = _context("outer-movie", media_type=MediaType.MOVIE)
    nested = _context("nested-movie", media_type=MediaType.MOVIE)
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing()}}
    nested_results = []

    def submit(*args, **kwargs):
        context = args[0] if args else kwargs["context"]
        if context is movie:
            nested_results.append(batch.chain.batch_download([nested]))
        return "id"

    batch.chain.download_single.side_effect = submit
    result, remaining = batch.chain.batch_download([whole, movie], missing)

    assert result == [movie, whole]
    assert nested_results == [([nested], None)]
    assert remaining is missing
    assert remaining == {}
    assert _submitted(batch.chain) == [movie, nested, whole]


def test_stop_during_movie_submission_prevents_later_phase_side_effects(batch):
    """
    电影提交期间收到停止信号后，不再执行后续阶段的副作用。
    """
    movie = _context("movie", media_type=MediaType.MOVIE)
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing()}}

    def submit(*_args, **_kwargs):
        batch.stop.is_system_stopped = True
        return "movie-id"

    batch.chain.download_single.side_effect = submit
    result, remaining = batch.chain.batch_download([whole, movie], missing)

    assert result == [movie]
    assert remaining == {"tmdb:1": {1: _missing()}}
    batch.chain.download_single.assert_called_once()
    batch.chain.download_torrent.assert_not_called()


def test_multiseason_pack_submits_without_reading_torrent_files(batch):
    """
    多季合集直接提交，不额外读取种子文件确认覆盖范围。
    """
    candidate = _context("multiseason", season=0)
    candidate.meta_info.end_season = 1
    missing = {"tmdb:1": {0: _missing(season=0), 1: _missing()}}

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert remaining is missing
    assert remaining == {}
    batch.chain.download_torrent.assert_not_called()
    assert batch.chain.download_single.call_args.args == (candidate,)
    assert candidate.confirmed_full_coverage is False


def test_partial_failure_does_not_rewrite_candidate_episode_range(batch):
    """
    分集提交失败不得改写候选的原始剧集范围。
    """
    candidate = _context("partial")
    missing = {"tmdb:1": {1: _missing((2,))}}
    batch.chain.download_single.return_value = None
    batch.chain.download_torrent.return_value = (b"torrent", None, [1, 2, 3])

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == []
    assert remaining["tmdb:1"][1].episodes == [2]
    assert candidate.meta_info.episode_list == []
    batch.chain.download_single.assert_called_once()


def test_failed_complete_coverage_submission_does_not_confirm_or_settle(batch):
    """
    要求完整覆盖的提交失败时，不确认覆盖完成或结算缺集。
    """
    candidate = _context("complete")
    missing = {"tmdb:1": {1: _missing(complete=True)}}
    batch.chain.download_single.return_value = None

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == []
    assert remaining["tmdb:1"][1].require_complete_coverage is True
    assert remaining["tmdb:1"][1].episodes == []
    assert candidate.confirmed_full_coverage is False
    batch.chain.download_single.assert_called_once()


def test_higher_priority_labelled_full_pack_wins_whole_season_phase(batch):
    """
    标注完整集数的高优先级包与无集数整季包同轮竞争，不被后者抢先清空缺季。
    """
    labelled = _context("labelled", episodes=(1, 2, 3, 4))
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing(complete=True)}}

    result, remaining = batch.chain.batch_download([labelled, whole], missing)

    assert result == [labelled]
    assert remaining == {}
    batch.chain.download_torrent.assert_called_once()
    assert batch.chain.download_single.call_args.kwargs["torrent_content"] == b"torrent"
    assert labelled.confirmed_full_coverage is True


@pytest.mark.parametrize(
    ("episodes", "total"),
    [((1, 2), 4), ((1, 2, 3, 4), 0)],
    ids=["partial-range", "unknown-total"],
)
def test_labelled_pack_without_full_coverage_stays_out_of_whole_season_phase(batch, episodes, total):
    """
    未覆盖整季的带集数候选不得进入整季优先阶段。
    """
    labelled = _context("labelled", episodes=episodes)
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing(total=total)}}

    result, _remaining = batch.chain.batch_download([labelled, whole], missing)

    assert result == [whole]
    batch.chain.download_torrent.assert_called_once()
    assert batch.chain.download_single.call_args.kwargs["context"] is whole


def test_labelled_full_pack_respects_candidate_allowed_episodes(batch):
    """
    带集数完整合集仍必须遵守候选允许下载的剧集范围。
    """
    labelled = _context("labelled", episodes=(1, 2, 3, 4))
    labelled.allowed_episodes = {1, 2}
    missing = {"tmdb:1": {1: _missing()}}

    result, remaining = batch.chain.batch_download([labelled], missing)

    assert result == []
    assert remaining["tmdb:1"][1].episodes == []
    batch.chain.download_torrent.assert_not_called()
    batch.chain.download_single.assert_not_called()


def test_labelled_full_pack_magnet_falls_back_to_episode_pack_phase(batch):
    """
    无法检查内容的完整合集磁力链接回退到分集包阶段提交。
    """
    labelled = _context("labelled", episodes=(1, 2, 3, 4))
    missing = {"tmdb:1": {1: _missing(complete=True)}}
    batch.chain.download_torrent.return_value = ("magnet:?xt=test", None, [])

    result, remaining = batch.chain.batch_download([labelled], missing)

    assert result == [labelled]
    assert remaining == {}
    batch.chain.download_torrent.assert_called_once()
    assert "torrent_content" not in batch.chain.download_single.call_args.kwargs
    batch.chain._record_download_failure.assert_not_called()


def test_whole_season_inspection_uses_only_target_season_episodes(batch, monkeypatch):
    """
    整季核对只统计目标季的文件，其他季的集号不能补齐目标季缺集。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing(total=2, complete=True)}}
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S02/02.mp4"])
    run = _batch_run(batch.chain, [candidate], missing)

    assert run._inspect_whole_season(candidate, "tmdb:1", 1) is None


def test_multiseason_netdisk_pack_submits_target_season_episodes(batch, monkeypatch):
    """
    缺 S01 时允许包含 S02 的网盘整包，只按 S01 目标集提交。
    """
    candidate = _context("multiseason")
    candidate.meta_info.end_season = 2
    candidate.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing(total=1)}}
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S02/05.mp4"])

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert remaining == {}
    assert batch.chain.download_single.call_args.kwargs["episode_targets"] == {1: {1}}
    assert batch.chain.download_single.call_args.kwargs["episodes"] == {1}
    assert candidate.meta_info.episode_list == []
    batch.chain.download_torrent.assert_not_called()


def test_multiseason_netdisk_pack_keeps_same_number_across_seasons(batch, monkeypatch):
    """
    目标季与其他季存在同集号时，提交的季级目标不能把其他季集号并进来。
    """
    candidate = _context("multiseason")
    candidate.meta_info.end_season = 2
    candidate.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing(total=2, complete=True)}}
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S01/02.mp4", "S02/02.mp4"])

    result, _remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert batch.chain.download_single.call_args.kwargs["episode_targets"] == {1: {1, 2}}
    assert batch.chain.download_single.call_args.kwargs["episodes"] == {1, 2}
    # 多季整包不得用跨季并集 min/max 改写候选原始识别范围。
    assert candidate.meta_info.episode_list == []


def test_single_season_netdisk_pack_submits_season_targets(batch, monkeypatch):
    """
    单季候选即使分享里混有其他季，也按目标季提交季级目标。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing(total=1)}}
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S02/01.mp4"])

    result, _remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert batch.chain.download_single.call_args.kwargs["episode_targets"] == {1: {1}}


def test_whole_season_candidate_allows_netdisk_extra_season(batch):
    """
    网盘候选只要覆盖任一缺失季即可进入整季阶段。
    """
    candidate = _context("multiseason")
    candidate.meta_info.end_season = 2
    candidate.torrent_info.download_type = "netdisk"
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._whole_season_candidate(candidate, "tmdb:1", [1]) == [1, 2]


def test_whole_season_candidate_keeps_non_netdisk_subset_requirement(batch):
    """
    非网盘候选仍要求全部季都在缺失范围内。
    """
    candidate = _context("multiseason")
    candidate.meta_info.end_season = 2
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing()}})

    assert run._whole_season_candidate(candidate, "tmdb:1", [1]) == []


def test_multiseason_netdisk_pack_keeps_partial_season_accounting(batch, monkeypatch):
    """
    整季阶段只结算整缺季，候选附带的部分缺集季必须保留原缺集记录。
    """
    candidate = _context("multiseason")
    candidate.meta_info.end_season = 2
    candidate.torrent_info.download_type = "netdisk"
    missing = {
        "tmdb:1": {
            1: _missing(total=1),
            2: _missing((5,), total=10),
        },
    }
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S02/05.mp4"])

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert batch.chain.download_single.call_args.kwargs["episode_targets"] == {1: {1}}
    assert batch.chain.download_single.call_args.kwargs["episodes"] == {1}
    assert 1 not in remaining["tmdb:1"]
    assert remaining["tmdb:1"][2].episodes == [5]


def test_single_season_netdisk_pack_without_season_markers_submits(batch, monkeypatch):
    """
    根目录平铺、无季标记的分享仍按目标季提交，不能因季号为 None 被整季阶段拒绝。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing(total=2)}}
    _patch_netdisk_paths(monkeypatch, ["第01集.mp4", "第02集.mp4"])

    result, _remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert batch.chain.download_single.call_args.kwargs["episodes"] == {1, 2}
    assert batch.chain.download_single.call_args.kwargs["episode_targets"] == {1: {1, 2}}


def test_netdisk_partial_pack_without_season_markers_submits(batch, monkeypatch):
    """
    无季标记分享的缺失集走拆包阶段时，仍能按目标季兜底提交。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    missing = {"tmdb:1": {1: _missing((27, 28, 29), total=29)}}
    _patch_netdisk_paths(monkeypatch, ["第27集.mp4", "第28集.mp4", "第29集.mp4"])
    run = _batch_run(batch.chain, [candidate], missing)

    run._download_season_partial_packs("tmdb:1", 1, [27, 28, 29])  # pylint: disable=protected-access

    assert batch.chain.download_single.call_args.kwargs["episodes"] == {27, 28, 29}


def test_episode_pack_candidate_falls_back_for_flat_share(batch, monkeypatch):
    """
    无季标记分享在完整集阶段同样按目标季兜底取集，不再返回空集。
    """
    candidate = _context("flat")
    candidate.torrent_info.download_type = "netdisk"
    info = _missing((27, 28, 29), total=29)
    _patch_netdisk_paths(monkeypatch, ["第27集.mp4", "第28集.mp4", "第29集.mp4"])
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: info}})

    assert run._episode_pack_candidate(  # pylint: disable=protected-access
        candidate, "tmdb:1", 1, [27, 28, 29], info,
    ) == {27, 28, 29}


def test_episode_pack_candidate_only_counts_target_season_episodes(batch, monkeypatch):
    """
    多季分享中的完整集包只按目标季集号结算，其他季同集号不计入。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S02/02.mp4"])
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing((2,), total=2)}})

    assert run._episode_pack_candidate(  # pylint: disable=protected-access
        candidate, "tmdb:1", 1, [2], _missing((2,), total=2),
    ) == set()


def test_partial_pack_does_not_submit_other_season_episode(batch, monkeypatch):
    """
    拆包阶段只按目标季核对集号，其他季同集号不能触发提交。
    """
    candidate = _context("multiseason")
    candidate.torrent_info.download_type = "netdisk"
    _patch_netdisk_paths(monkeypatch, ["S01/01.mp4", "S02/02.mp4"])
    run = _batch_run(batch.chain, [candidate], {"tmdb:1": {1: _missing((2,), total=2)}})

    run._download_season_partial_packs("tmdb:1", 1, [2])  # pylint: disable=protected-access

    batch.chain.download_single.assert_not_called()
