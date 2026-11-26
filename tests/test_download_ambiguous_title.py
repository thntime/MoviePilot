"""
下载入口的同名多版本守卫：无身份下载不允许静默挑一个版本。

凡人修仙传（动漫 2020 / 真人剧 2025）这类同名多版本标题，按标题识别会静默
归到搜索排序第一的版本，下载与整理全程跟着错。守卫要求用户显式选择版本。
"""

from types import SimpleNamespace
from typing import Any, List
from unittest.mock import Mock

import pytest

from app.api.endpoints import download as endpoint
from app.domain.context import MediaInfo
from app.schemas.context import TorrentInfo as _SchemaTorrentInfo
from app.schemas.types import MediaSource, MediaType


def _torrent(title: str = "凡人修仙传 年番 更200集") -> _SchemaTorrentInfo:
    """构造一个网盘分享式种子。"""
    return _SchemaTorrentInfo(
        title=title,
        site_name="PanSou 主站",
        enclosure="https://pan.quark.cn/s/abc123",
    )


def _media(media_id: str, year: str, title: str = "凡人修仙传") -> MediaInfo:
    """构造一个 TMDB 版本。"""
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id=media_id,
        title=title,
        year=year,
        type=MediaType.TV,
    )


@pytest.fixture()
def _media_chain(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """替换 MediaChain，识别与标题搜索都走替身。"""
    chain = Mock()
    chain.recognize_by_meta.return_value = _media("243224", "2025")
    chain.recognize_media.return_value = _media("106449", "2020")
    chain.search_medias.return_value = [
        _media("243224", "2025"),
        _media("106449", "2020"),
    ]
    monkeypatch.setattr(endpoint, "MediaChain", lambda: chain)
    return chain


@pytest.fixture()
def _download_chain(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """替换 DownloadChain，记录提交上下文。"""
    chain = Mock()
    chain.download_single.return_value = "did-1"
    monkeypatch.setattr(endpoint, "DownloadChain", lambda: chain)
    return chain


def _current_user() -> SimpleNamespace:
    """构造一个已认证用户。"""
    return SimpleNamespace(name="admin")


def test_ambiguous_title_requires_confirmation(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """同名多版本时不下单，返回候选让用户选。"""
    response = endpoint.add(
        torrent_in=_torrent(),
        current_user=_current_user(),
    )

    assert response.success is False
    assert "同名版本" in response.message
    assert response.data.requires_confirmation is True
    alternatives: List[Any] = response.data.alternatives
    assert [item.media_id for item in alternatives] == ["243224", "106449"]
    assert all(item.title == "凡人修仙传" for item in alternatives)
    assert _media_chain.search_medias.call_args.args[0].name == "凡人修仙传"
    assert _download_chain.download_single.call_count == 0


def test_resubmit_with_picked_identity_downloads_that_version(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """带选定 media_source/media_id 复提时不做歧义检查，按选定版本下单。"""
    response = endpoint.add(
        torrent_in=_torrent(),
        media_source=MediaSource.TMDB,
        media_id="106449",
        current_user=_current_user(),
    )

    assert response.success is True
    assert response.data["download_id"] == "did-1"
    context = _download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info.media_id == "106449"
    assert context.media_info.year == "2020"
    # 显式身份走 recognize_media，不再触发标题搜索。
    assert _media_chain.search_medias.call_count == 0


def test_single_version_downloads_directly(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """唯一版本没有歧义，直接下单。"""
    _media_chain.search_medias.return_value = [_media("243224", "2025")]

    response = endpoint.add(
        torrent_in=_torrent(),
        current_user=_current_user(),
    )

    assert response.success is True
    assert response.data["download_id"] == "did-1"


def test_english_alias_versions_trigger_guard(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """TMDB 返回英文原名时，也通过识别结果的英文名识别同名版本。"""
    recognized = _media("79481", "2017", title="斗破苍穹")
    recognized.en_title = "Fights Break Sphere"
    _media_chain.recognize_by_meta.return_value = recognized
    _media_chain.search_medias.return_value = [
        _media("79481", "2017", title="Fights Break Sphere"),
        _media("81500", "2018", title="Fights Break Sphere"),
    ]

    response = endpoint.add(
        torrent_in=_torrent(title="斗破苍穹 年番"),
        current_user=_current_user(),
    )

    assert response.success is False
    assert [item.media_id for item in response.data.alternatives] == ["81500", "79481"]
    assert _download_chain.download_single.call_count == 0


def test_similar_but_different_titles_do_not_trigger(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """标题不同（衍生作、翻拍改名）不算同名版本，直接下单。"""
    _media_chain.search_medias.return_value = [
        _media("243224", "2025"),
        _media("999001", "2024", title="凡人修仙传之仙界篇"),
    ]

    response = endpoint.add(
        torrent_in=_torrent(),
        current_user=_current_user(),
    )

    assert response.success is True


def test_allow_unrecognized_skips_ambiguity_guard(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """用户显式确认继续时保持旧行为，按识别结果下单。"""
    response = endpoint.add(
        torrent_in=_torrent(),
        allow_unrecognized=True,
        current_user=_current_user(),
    )

    assert response.success is True
    context = _download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info.media_id == "243224"


def test_recognition_failure_keeps_existing_confirmation_flow(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """识别不到媒体时仍走原有的 requires_confirmation 流程。"""
    _media_chain.recognize_by_meta.return_value = None

    response = endpoint.add(
        torrent_in=_torrent(),
        current_user=_current_user(),
    )

    assert response.success is False
    assert response.data.requires_confirmation is True
    assert response.data.alternatives == []


def test_music_download_skips_ambiguity_guard(
    _media_chain: Mock, _download_chain: Mock
) -> None:
    """音乐下载不做影视同名版本检查。"""
    _media_chain.recognize_by_meta.return_value = _media("243224", "2025")

    response = endpoint.add(
        torrent_in=_torrent(),
        music_type="recording",
        current_user=_current_user(),
    )

    assert response.success is True
    assert _media_chain.search_medias.call_count == 0
