"""
订阅链路对网盘整包按缺失集拆包的测试。
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
from app.schemas.types import MediaSource, MediaType


def _context(name: str = "小城良方") -> Context:
    """
    构造一个网盘候选上下文。
    """
    meta = MetaInfo(f"{name} S01")
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title=name,
            type=MediaType.TV,
            media_source=MediaSource.TMDB,
            media_id="1",
        ),
        torrent_info=TorrentInfo(
            title=f"{name} S01E01-E29",
            site_name="cloud saver",
            site=1,
            enclosure="https://pan.quark.cn/s/abc123",
            download_type="netdisk",
            netdisk_platform="quark",
        ),
    )


def _missing(episodes: tuple[int, ...], total: int = 29) -> NotExistMediaInfo:
    """
    构造要求完整覆盖的缺失集信息。
    """
    return NotExistMediaInfo(
        season=1,
        episodes=list(episodes),
        total_episode=total,
        start_episode=1,
        require_complete_coverage=True,
    )


@pytest.fixture()
def batch(monkeypatch):
    """
    保留真实阶段选择，只替换下载、事件与持久化边界。
    """
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_single = MagicMock(return_value="download-id")
    chain.download_torrent = MagicMock()
    helper = SimpleNamespace(
        sort_torrents=MagicMock(side_effect=lambda contexts: contexts),
        get_torrent_episodes=MagicMock(return_value=[]),
    )
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)
    return SimpleNamespace(chain=chain, helper=helper)


def _patch_walk(monkeypatch, episode_numbers):
    """
    让网盘取链服务返回指定集数的文件清单。
    """
    files = tuple(
        ShareFile(file_id=f"f{number}", name=f"小城良方.S01E{number:02d}.mp4", size=100)
        for number in episode_numbers
    )

    def fake_walk(self, url, password=None, platform=None, account_id=None, **kwargs):
        return ResolvedShare(
            ref=ShareRef(platform=NetdiskPlatform.QUARK, share_id="abc123", url=url),
            detail=ShareDetail(title="小城良方", size=len(files) * 100, files=files),
            files=files,
        )

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)


def _patch_walk_paths(monkeypatch, paths):
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
            detail=ShareDetail(title="小城良方", size=len(files) * 100, files=files),
            files=files,
        )

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)


def _batch_run(chain):
    """
    构造只用于直接核对阶段内部判定的批量状态。
    """
    options = {
        "save_path": None, "channel": None, "source": None, "userid": None,
        "username": None, "downloader": None, "custom_words": None, "governance": None,
    }
    return download_batch._BatchDownloadRun(chain, [], {}, options, None)


def test_netdisk_season_episodes_groups_files_by_season(batch, monkeypatch):
    """
    同一分享内按季归集集号，无法解析季号的文件归入 None。
    """
    _patch_walk_paths(monkeypatch, ["S01/01.mp4", "S01/02.mp4", "S02/05.mp4", "03.mp4"])
    run = _batch_run(batch.chain)

    assert run._netdisk_season_episodes(_context().torrent_info) == {
        1: {1, 2}, 2: {5}, None: {3},
    }
    assert run._netdisk_episodes(_context().torrent_info) == [1, 2, 3, 5]


def test_netdisk_season_episodes_ignores_non_video_files(batch, monkeypatch):
    """
    字幕和图片不参与季集统计，没有可解析集数时返回 None。
    """
    _patch_walk_paths(monkeypatch, ["S01/cover.jpg", "S01/01.ass"])
    run = _batch_run(batch.chain)

    assert run._netdisk_season_episodes(_context().torrent_info) is None


def test_netdisk_full_pack_submits_only_missing_episodes(batch, monkeypatch):
    """
    整包包含已入库集时只提交缺失集。
    """
    _patch_walk(monkeypatch, range(1, 30))
    missing = {"tmdb:1": {1: _missing((27, 28, 29))}}

    result, remaining = batch.chain.batch_download([_context()], missing)

    assert result
    assert remaining == {}
    batch.chain.download_torrent.assert_not_called()
    call = batch.chain.download_single.call_args
    assert call.kwargs["episodes"] == {27, 28, 29}


def test_netdisk_empty_intersection_is_not_submitted(batch, monkeypatch):
    """
    资源不覆盖任何缺失集时不提交，并记录原因。
    """
    _patch_walk(monkeypatch, range(1, 27))
    governance = MagicMock()
    missing = {"tmdb:1": {1: _missing((27, 28, 29))}}

    result, _ = batch.chain.batch_download([_context()], missing, governance=governance)

    assert not result
    batch.chain.download_single.assert_not_called()
    governance.report.assert_called()


def test_netdisk_share_without_target_reports_failure(batch, monkeypatch):
    """
    分享内没有目标集时给出明确失败原因，不静默成功。
    """
    _patch_walk(monkeypatch, range(1, 27))
    governance = MagicMock()
    missing = {"tmdb:1": {1: _missing((27, 28, 29))}}

    batch.chain.batch_download([_context()], missing, governance=governance)

    messages = [call.args[2] for call in governance.report.call_args_list]
    assert any("没有目标集" in message for message in messages)
