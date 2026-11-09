"""网盘候选排序：夸克平台优先于其他网盘平台。"""

from types import SimpleNamespace

import pytest

import app.application.torrent.download as download_module
from app.application.torrent.download import TorrentHelper
from app.domain.context import Context, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import SystemConfigKey


def test_quark_netdisk_ranks_before_other_netdisk() -> None:
    """夸克网盘候选的优先级值小于其他候选，便于排序键统一处理。"""
    quark = TorrentInfo(download_type="netdisk", netdisk_platform="quark")
    other = TorrentInfo(download_type="netdisk", netdisk_platform="pan123")
    torrent = TorrentInfo(download_type="torrent")
    assert TorrentHelper._netdisk_platform_rank(quark) == "0"
    assert TorrentHelper._netdisk_platform_rank(other) == "1"
    assert TorrentHelper._netdisk_platform_rank(torrent) == "1"


def _patch_sort_config(monkeypatch: pytest.MonkeyPatch, priority: list[str]) -> None:
    """把排序依赖的系统配置与站点数据替换为固定值。"""
    monkeypatch.setattr(
        download_module,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: priority if key == SystemConfigKey.TorrentsPriority else None),
    )
    monkeypatch.setattr(
        download_module,
        "get_configured_site_query_service",
        lambda: SimpleNamespace(userdata_latest_sync=lambda: []),
    )


def _build_context(platform: str, pri_order: int = 0) -> Context:
    """构造标题相同、仅网盘平台不同的候选。"""
    return Context(
        meta_info=MetaInfo("Example Show S01E01"),
        torrent_info=TorrentInfo(
            download_type="netdisk",
            netdisk_platform=platform,
            pri_order=pri_order,
        ),
    )


def test_sort_torrents_prefers_quark_netdisk(monkeypatch: pytest.MonkeyPatch) -> None:
    """同标题下夸克网盘候选排在其他网盘候选之前。"""
    _patch_sort_config(monkeypatch, ["torrent"])
    quark = _build_context("quark")
    other = _build_context("pan123")

    assert TorrentHelper.sort_torrents([other, quark])[0] is quark


def test_sort_torrents_prefers_quark_even_when_title_sorts_lower(monkeypatch: pytest.MonkeyPatch) -> None:
    """标题不同的情况下，夸克候选仍必须排在非夸克候选之前。"""
    _patch_sort_config(monkeypatch, ["torrent"])
    quark = Context(
        meta_info=MetaInfo("仙逆 (2023) S01E166"),
        torrent_info=TorrentInfo(download_type="netdisk", netdisk_platform="quark"),
    )
    other = Context(
        meta_info=MetaInfo("仙逆 4K臻彩MAX [附合集篇+剧场版] S01E01-E166"),
        torrent_info=TorrentInfo(download_type="netdisk", netdisk_platform="xunlei"),
    )

    assert TorrentHelper.sort_torrents([other, quark])[0] is quark


def test_sort_torrents_keeps_non_quark_relative_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """非夸克候选之间仍按既有优先级规则排序，不被平台排序键打乱。"""
    _patch_sort_config(monkeypatch, ["torrent"])
    lower = _build_context("pan123", pri_order=1)
    higher = _build_context("uc", pri_order=5)

    assert TorrentHelper.sort_torrents([lower, higher])[0] is higher
