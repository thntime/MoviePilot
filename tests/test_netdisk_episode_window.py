"""
网盘分享「只标集数」文件名的窗口识别测试。

站点为了不被网盘封号，常把分享内文件写成 `140_179.mp4`、`140@xx.mp4`、
`140.4K.mp4` 这类只有集数、没有片名季号的形态。窗口识别用候选标题声明的
集数范围当白名单，把落在范围内的数字认成集数，范围外的噪音数字忽略。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.selection as download_selection
from app.application.netdisk.candidates import is_episode_clear
from app.application.netdisk.resolve import NetdiskResolveService, ResolvedShare
from app.chain.download import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.episode import declared_episode_window, windowed_episode_numbers
from app.domain.metainfo import MetaInfo
from app.domain.netdisk import NetdiskPlatform, ShareDetail, ShareFile, ShareRef
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType

# 目标季的集数窗口：标题声明的 S01E140-E179。
WINDOW = set(range(140, 180))


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("140.mp4", [140]),
        ("140_179.mp4", [140, 179]),
        ("140@xx.mp4", [140]),
        ("140.4K.mp4", [140]),
        ("140(1).mp4", [140]),
        ("140_60帧.mp4", [140]),
        ("140 2.0.mp4", [140]),
        ("179.4K.mp4", [179]),
    ],
)
def test_windowed_episode_numbers_accepts_episode_only_names(file_name, expected):
    """
    窗口内的数字一律认作集数，版本号与混淆符号不再一票否决。
    """
    assert windowed_episode_numbers(file_name, WINDOW) == expected


@pytest.mark.parametrize(
    "file_name",
    ["1917 2019 1080p.mp4", "60.mp4", "1080.mp4", "花絮 140.mp4", "cover.jpg"],
)
def test_windowed_episode_numbers_rejects_out_of_window_and_specials(file_name):
    """
    窗口外的年份、帧率、清晰度与花絮不参与集数识别。
    """
    assert windowed_episode_numbers(file_name, WINDOW) == []


def test_declared_episode_window_reads_title_range():
    """
    标题里的标准季集标记解析成季号到集号窗口。
    """
    assert declared_episode_window("斗罗大陆Ⅱ绝世唐门 S01E140-E179") == {1: WINDOW}
    assert declared_episode_window("斗罗大陆Ⅱ绝世唐门 S01E140") == {1: {140}}
    assert declared_episode_window("斗罗大陆Ⅱ绝世唐门") == {}


def test_is_episode_clear_relaxes_only_with_window():
    """
    没有窗口时集数不清晰，给出窗口后同一批文件通过清晰度判定。
    """
    videos = [{"name": "140.4K.mp4"}, {"name": "141.4K.mp4"}]
    assert is_episode_clear(videos) is False
    assert is_episode_clear(videos, window=WINDOW) is True


def _context(title: str = "斗罗大陆Ⅱ绝世唐门", torrent_title: str = "斗罗大陆Ⅱ绝世唐门 S01E140-E179") -> Context:
    """
    构造一个网盘候选上下文。
    """
    meta = MetaInfo(f"{title} S01")
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title=title,
            type=MediaType.TV,
            media_source=MediaSource.TMDB,
            media_id="1",
        ),
        torrent_info=TorrentInfo(
            title=torrent_title,
            site_name="PanSou",
            site=2,
            enclosure="https://pan.quark.cn/s/abc123",
            download_type="netdisk",
            netdisk_platform="quark",
        ),
    )


def _missing(episodes, total: int = 179) -> NotExistMediaInfo:
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
            detail=ShareDetail(title="斗罗大陆Ⅱ绝世唐门", size=len(files) * 100, files=files),
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


def test_netdisk_season_episodes_uses_title_window(batch, monkeypatch):
    """
    只标集数的文件名按标题窗口归入目标季。
    """
    _patch_walk_paths(monkeypatch, [f"{number}.4K.mp4" for number in range(140, 180)])
    run = _batch_run(batch.chain)

    season_episodes = run._netdisk_season_episodes(_context().torrent_info)

    assert season_episodes == {None: WINDOW}
    assert run._netdisk_target_season_episodes(season_episodes, 1) == WINDOW


def test_netdisk_window_keeps_season_isolation(batch, monkeypatch):
    """
    多季分享里其他季的文件不会被算进目标季。
    """
    _patch_walk_paths(monkeypatch, ["S01/140.4K.mp4", "S02/5.mp4"])
    run = _batch_run(batch.chain)

    season_episodes = run._netdisk_season_episodes(_context().torrent_info)

    assert run._netdisk_target_season_episodes(season_episodes, 1) == {140}


def test_netdisk_full_pack_accepts_episode_only_file_names(batch, monkeypatch):
    """
    端到端：文件名只有集数时仍按缺集提交下载。
    """
    _patch_walk_paths(monkeypatch, [f"{number}.4K.mp4" for number in range(141, 180)])
    missing = {"tmdb:1": {1: _missing(range(141, 180))}}

    result, remaining = batch.chain.batch_download([_context()], missing)

    assert result
    assert remaining == {}
    call = batch.chain.download_single.call_args
    assert call.kwargs["episodes"] == set(range(141, 180))
