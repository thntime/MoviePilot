"""
订阅候选按网盘平台启用集合排除的回归测试。
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest

import app.chain.subscribe.search as subscribe_search
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.domain.netdisk import NetdiskPlatform
from app.schemas.types import MediaSource, MediaType


def _subscribe() -> SubscriptionSnapshot:
    """
    构造不启用洗版的电视剧订阅快照。

    :return: 订阅快照
    """
    return SubscriptionSnapshot(
        id=7,
        name="Example Show",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="1",
        season=1,
        start_episode=1,
        total_episode=1,
    )


def _media() -> MediaInfo:
    """
    构造与订阅对应的电视剧媒体信息。

    :return: 媒体信息
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1",
        tmdb_id=1,
        type=MediaType.TV,
        title="Example Show",
        original_title="Example Show",
        names=["Example Show"],
        season=1,
        seasons={1: [1]},
    )


def _context(
    title: str,
    *,
    download_type: str = "torrent",
    platform: Optional[str] = None,
) -> Context:
    """
    构造一个订阅候选上下文。

    :param title: 候选标题
    :param download_type: 资源类型
    :param platform: 网盘平台标识
    :return: 候选上下文
    """
    return Context(
        meta_info=MetaInfo(title),
        media_info=_media(),
        torrent_info=TorrentInfo(
            site=1,
            site_name="A",
            title=title,
            download_type=download_type,
            netdisk_platform=platform,
        ),
    )


def _patch_enabled_platforms(monkeypatch: Any, enabled: frozenset[NetdiskPlatform]) -> None:
    """
    固定当前启用的网盘平台集合。

    :param monkeypatch: pytest 补丁工具
    :param enabled: 启用的平台集合
    """
    monkeypatch.setattr(
        subscribe_search,
        "enabled_platforms",
        lambda: enabled,
        raising=False,
    )


def test_disabled_netdisk_platform_candidates_are_excluded(monkeypatch: Any) -> None:
    """
    停用平台的网盘候选被排除，启用平台与种子候选仍进入后续匹配。
    """
    _patch_enabled_platforms(monkeypatch, frozenset({NetdiskPlatform.PAN115}))
    chain = object.__new__(SubscribeChain)
    disabled = _context("Example Show S01E01 夸克", download_type="netdisk", platform="quark")
    enabled = _context("Example Show S01E01 115", download_type="netdisk", platform="pan115")
    torrent = _context("Example Show S01E01 WEB-DL")
    contexts = [disabled, enabled, torrent]

    matched = chain._filter_search_contexts(_subscribe(), contexts)

    assert matched == [enabled, torrent]
    assert contexts == []


def test_all_candidates_disabled_platform_returns_empty_without_failure(monkeypatch: Any) -> None:
    """
    候选全部来自停用平台时返回空结果，只记录拒绝判定，不产生失败状态。
    """
    _patch_enabled_platforms(monkeypatch, frozenset({NetdiskPlatform.QUARK}))
    decisions: list[tuple[str, Optional[str]]] = []

    class _Activity:
        """
        记录候选判定结果的最小活动对象。
        """

        @staticmethod
        def resource(_context: Any, decision: str, reason: Optional[str] = None) -> None:
            """
            记录一次候选判定。

            :param _context: 被判定的候选
            :param decision: 判定结果
            :param reason: 判定原因
            """
            decisions.append((decision, reason))

    monkeypatch.setattr(subscribe_search, "current_activity_run", lambda: _Activity())
    chain = object.__new__(SubscribeChain)
    contexts = [
        _context("Example Show S01E01 115", download_type="netdisk", platform="pan115"),
        _context("Example Show S01E01 百度", download_type="netdisk", platform="baidu"),
    ]

    matched = chain._filter_search_contexts(_subscribe(), contexts)

    assert matched == []
    assert contexts == []
    assert {decision for decision, _reason in decisions} == {"rejected"}
