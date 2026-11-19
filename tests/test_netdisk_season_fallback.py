"""
网盘分享季号与订阅季号对不上时的集号对齐测试。

站点常把年番整季标成 S01（文件名为 `S01E180.mp4` 或目录 `S01`），而订阅可能是
S05；分享只声明一个季键时按集号对齐到目标季，多季分享仍严格按季隔离。
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

NEEDED = list(range(180, 220))


def _batch_run(chain):
    """
    构造只用于直接核对阶段内部判定的批量状态。
    """
    options = {
        "save_path": None, "channel": None, "source": None, "userid": None,
        "username": None, "downloader": None, "custom_words": None, "governance": None,
    }
    return download_batch._BatchDownloadRun(chain, [], {}, options, None)


def test_single_declared_season_aligns_to_target_season():
    """
    分享只声明第 1 季时，集号按目标季 5 对齐。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(  # pylint: disable=protected-access
        {1: set(NEEDED)}, 5,
    ) == set(NEEDED)


def test_other_season_key_is_not_aligned_to_first_season():
    """
    分享只声明第 2 季时不许对齐到第 1 季，S02 的集号不能被当成 S01。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(  # pylint: disable=protected-access
        {2: {1, 2}}, 1,
    ) == set()


def test_multi_season_share_keeps_isolation():
    """
    分享声明两个季时不对齐，避免把其他季的集号算进目标季。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(  # pylint: disable=protected-access
        {1: {1, 2}, 2: set(NEEDED)}, 5,
    ) == set()


def test_target_season_wins_when_present():
    """
    分享里本来就有目标季时直接用目标季集号。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(  # pylint: disable=protected-access
        {1: {1}, 5: {180, 181}}, 5,
    ) == {180, 181}


def test_unlabelled_files_still_fall_back():
    """
    分享里没有任何季号时仍按无季号文件的集号兜底。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(  # pylint: disable=protected-access
        {None: {180, 181}}, 5,
    ) == {180, 181}


def test_fallback_can_be_disabled_for_multi_season_packs():
    """
    多季整包核对显式关闭对齐，保持严格按季过滤。
    """
    assert download_batch._BatchDownloadRun._netdisk_target_season_episodes(  # pylint: disable=protected-access
        {1: set(NEEDED)}, 5, allow_single_season_fallback=False,
    ) == set()


def _context() -> Context:
    """
    构造一个订阅目标是第 5 季的网盘候选上下文。
    """
    name = "斗破苍穹"
    meta = MetaInfo(f"{name} S05")
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title=name,
            type=MediaType.TV,
            media_source=MediaSource.TMDB,
            media_id="1",
        ),
        torrent_info=TorrentInfo(
            title=f"{name} 年番 S01E180-E219",
            site_name="CloudSaver",
            site=1,
            enclosure="https://pan.quark.cn/s/abc123",
            download_type="netdisk",
            netdisk_platform="quark",
        ),
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
            detail=ShareDetail(title="斗破苍穹 年番", size=len(files) * 100, files=files),
            files=files,
        )

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)


def test_subscription_season_five_accepts_season_one_labelled_share(batch, monkeypatch):
    """
    端到端：分享文件名标 S01、订阅要第 5 季时仍按集号提交下载。
    """
    _patch_walk_paths(monkeypatch, [f"S01E{number}.mp4" for number in NEEDED])
    missing = {
        "tmdb:1": {
            5: NotExistMediaInfo(
                season=5, episodes=list(NEEDED), total_episode=219,
                start_episode=1, require_complete_coverage=True,
            ),
        },
    }

    result, remaining = batch.chain.batch_download([_context()], missing)

    assert result
    assert remaining == {}
    assert batch.chain.download_single.call_args.kwargs["episodes"] == set(NEEDED)
