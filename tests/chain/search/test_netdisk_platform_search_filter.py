"""
搜索候选按网盘平台启用集合过滤的回归测试。
"""

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from app.application.netdisk import settings as netdisk_settings
from app.chain.search import SearchChain
from app.chain.search import result as result_module
from app.domain.context import MediaInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType, SystemConfigKey


def _patch_enabled_platforms(monkeypatch: pytest.MonkeyPatch, platforms: Optional[List[str]]) -> None:
    """
    把网盘平台启用集合替换为固定配置。

    :param monkeypatch: pytest 补丁工具
    :param platforms: `NetdiskEnabledPlatforms` 的配置值
    """
    def _get(key: SystemConfigKey) -> Any:
        """
        按配置键返回平台启用值。

        :param key: 配置键
        :return: 仅对平台启用键返回配置值
        """
        return platforms if key == SystemConfigKey.NetdiskEnabledPlatforms else None

    monkeypatch.setattr(
        netdisk_settings,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=_get),
    )


def _media() -> MediaInfo:
    """
    构造带有可匹配标题的目标媒体。
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1",
        tmdb_id=1,
        title="测试电影",
        original_title="Test Movie",
        names=["测试别名"],
        type=MediaType.MOVIE,
        year="2024",
    )


def _netdisk_torrent(title: str, platform: Optional[str]) -> TorrentInfo:
    """
    构造一条网盘候选。

    :param title: 资源标题
    :param platform: 网盘平台标识
    :return: 网盘资源
    """
    return TorrentInfo(
        site=1,
        site_name="CD站点",
        title=title,
        description="描述",
        category=MediaType.MOVIE.value,
        download_type="netdisk",
        netdisk_platform=platform,
    )


def _pt_torrent(title: str) -> TorrentInfo:
    """
    构造一条 PT 候选。

    :param title: 资源标题
    :return: 种子资源
    """
    return TorrentInfo(
        site=2,
        site_name="PT站点",
        title=title,
        description="描述",
        category=MediaType.MOVIE.value,
    )


def _patch_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    将匹配和排序边界替换为确定性的离线实现。
    """
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "match_torrent",
        staticmethod(lambda **_kwargs: True),
    )
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "requires_identity_disambiguation",
        staticmethod(lambda **_kwargs: False),
    )
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )


def test_parse_result_drops_disabled_netdisk_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    未启用平台的网盘候选被丢弃，启用平台的候选保留。
    """
    _patch_enabled_platforms(monkeypatch, ["quark"])
    _patch_match(monkeypatch)
    torrents = [
        _netdisk_torrent("测试电影 2024 1080p", "quark"),
        _netdisk_torrent("测试电影 2024 2160p", "pan115"),
    ]

    contexts = object.__new__(SearchChain)._parse_result(
        torrents=torrents,
        mediainfo=_media(),
        rule_groups=[],
    )

    assert [context.torrent_info.netdisk_platform for context in contexts] == ["quark"]


def test_parse_result_keeps_pt_and_unlabelled_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    平台全部禁用时 PT 候选与未标注平台的网盘候选仍保留。
    """
    _patch_enabled_platforms(monkeypatch, [])
    _patch_match(monkeypatch)
    torrents = [
        _pt_torrent("测试电影 2024 1080p"),
        _netdisk_torrent("测试电影 2024 2160p", None),
    ]

    contexts = object.__new__(SearchChain)._parse_result(
        torrents=torrents,
        mediainfo=_media(),
        rule_groups=[],
    )

    assert [context.torrent_info.download_type for context in contexts] == ["torrent", "netdisk"]
