"""
验证订阅有效总集数按已播出集号封顶。

TMDB 会把还没播出的集也列进季清单（`episode_count` 200，实际只播到 168），
订阅若直接拿季清单长度当目标范围，就会一直去搜未播出的集。本文件锁住封顶
口径与拿不到播出信息时的回退口径。
"""

import dataclasses
from typing import Any, Dict

import pytest

from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo
from app.domain.projection.aired import aired_episode_cap
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaType

_TOTAL = 200
_AIRED = 168


def _mediainfo(total: int = _TOTAL, aired: int = 0) -> MediaInfo:
    """
    构造《仙逆》的媒体信息。

    :param total: TMDB 声明的季集数
    :param aired: 最近播出的集号，0 表示拿不到播出信息
    :return: 媒体信息
    """
    tmdb_info: Dict[str, Any] = {}
    if aired:
        tmdb_info["last_episode_to_air"] = {"season_number": 1, "episode_number": aired}
    return MediaInfo(
        title="仙逆",
        year="2023",
        type=MediaType.TV,
        seasons={1: list(range(1, total + 1))},
        season_info=[{"season_number": 1, "episode_count": total}],
        tmdb_info=tmdb_info,
    )


def _subscribe(total: int = _TOTAL, manual: int = 0) -> SubscriptionSnapshot:
    """
    构造订阅快照。

    :param total: 订阅当前总集数
    :param manual: 是否手工指定总集数
    :return: 订阅快照
    """
    return SubscriptionSnapshot(
        id=23,
        name="仙逆",
        type=MediaType.TV.value,
        season=1,
        total_episode=total,
        start_episode=0,
        manual_total_episode=manual,
    )


def _effective(subscribe: SubscriptionSnapshot, mediainfo: MediaInfo) -> int:
    """
    调订阅只读口径计算有效总集数。

    :param subscribe: 订阅快照
    :param mediainfo: 媒体信息
    :return: 有效总集数
    """
    resolver = SubscribeChain._SubscribeChain__resolve_effective_total_episode
    return resolver(subscribe, mediainfo)


def test_aired_episode_cap_reads_last_aired_point() -> None:
    """已播出最大集号取自 TMDB 的 last_episode_to_air；拿不到播出信息时不封顶。"""
    assert aired_episode_cap(_mediainfo(aired=_AIRED), 1) == _AIRED
    assert aired_episode_cap(_mediainfo(aired=0), 1) is None
    assert aired_episode_cap(_mediainfo(aired=_AIRED), None) is None


def test_effective_total_episode_ignores_aired_cap() -> None:
    """有效总集数不能按已播出集号压小：它同时决定整季是否下完。"""
    assert _effective(_subscribe(), _mediainfo(aired=_AIRED)) == _TOTAL


def test_effective_total_episode_keeps_metadata_when_aired_unknown() -> None:
    """拿不到播出信息时保持原有口径，仍用元数据季集数。"""
    assert _effective(_subscribe(), _mediainfo(aired=0)) == _TOTAL


def test_effective_total_episode_respects_manual_total() -> None:
    """手工指定总集数时不被播出信息改写。"""
    assert _effective(_subscribe(manual=1), _mediainfo(aired=_AIRED)) == _TOTAL


def test_clip_no_exists_drops_unaired_episodes() -> None:
    """缺集列表裁到已播出范围内，未播出的集不进搜索目标。"""
    from app.chain.subscribe.metadata import _clip_missing_to_aired as clip
    no_exists = {
        "203483": {
            1: NotExistMediaInfo(
                season=1, episodes=[100, 169, 170], total_episode=_TOTAL, start_episode=1,
            )
        }
    }
    clipped = clip(no_exists, _mediainfo(aired=_AIRED), _subscribe())
    assert clipped["203483"][1].episodes == [100]
    assert clipped["203483"][1].total_episode == _TOTAL


def test_resolve_subscribe_missing_keeps_series_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """媒体库只覆盖已播出的集时，订阅不得被判成整季完成（否则会被删掉）。"""
    from app.chain.subscribe import refresh as refresh_module

    captured: Dict[str, Any] = {}

    class _FakeDownloadChain:
        """替换下载链，返回「只缺未播出集」的结果。"""

        def get_no_exists_info(self, meta, mediainfo, totals, totals_are_episode_numbers=False, raise_on_query_failure=False):
            """返回整季目标下的缺失集。"""
            captured["totals"] = dict(totals)
            missing = list(range(_AIRED + 1, _TOTAL + 1))
            return False, {
                "203483": {
                    1: NotExistMediaInfo(
                        season=1,
                        episodes=missing,
                        total_episode=totals.get(1, 0),
                        start_episode=1,
                    )
                }
            }

    monkeypatch.setattr(refresh_module, "DownloadChain", _FakeDownloadChain)
    exist_flag, no_exists = SubscribeChain().resolve_subscribe_missing(
        subscribe=_subscribe(),
        meta=None,
        mediainfo=_mediainfo(aired=_AIRED),
        mediakey="203483",
    )
    assert captured["totals"] == {1: _TOTAL}
    assert exist_flag is False
    # resolve 不再裁剪：完整缺集留给进度与完成判定，裁剪只发生在搜索目标
    assert no_exists["203483"][1].episodes == list(range(_AIRED + 1, _TOTAL + 1))


def test_clip_does_not_mark_series_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """媒体库缺的集被 note 抵消、未播出的集被裁掉时，仍不得判整季完成。"""
    from app.chain.subscribe import refresh as refresh_module

    missing_now = [100, 259] + list(range(_AIRED + 1, _TOTAL + 1))

    class _FakeDownloadChain:
        """返回「媒体库缺两集 + 未播出的集」的缺集结果。"""

        def get_no_exists_info(self, meta, mediainfo, totals, totals_are_episode_numbers=False, raise_on_query_failure=False):
            """返回未裁剪的缺集。"""
            return False, {
                "203483": {
                    1: NotExistMediaInfo(
                        season=1,
                        episodes=list(missing_now),
                        total_episode=_TOTAL,
                        start_episode=1,
                    )
                }
            }

    monkeypatch.setattr(refresh_module, "DownloadChain", _FakeDownloadChain)
    subscribe = dataclasses.replace(_subscribe(), note=[100, 259])
    exist_flag, no_exists = SubscribeChain().resolve_subscribe_missing(
        subscribe=subscribe,
        meta=None,
        mediainfo=_mediainfo(aired=_AIRED),
        mediakey="203483",
    )
    assert exist_flag is False
    # note 里记账过的 100、259 被抵消，只剩未播出的 169-200 仍然缺失
    assert no_exists["203483"][1].episodes == list(range(_AIRED + 1, _TOTAL + 1))


def test_lack_episode_uses_full_missing() -> None:
    """缺失数按完整缺集算；空缺集在既有语义里等于整季未找到。"""
    from app.application.subscription.contract import subscribe_media_keys
    from app.application.subscription.priority import compute_lack_episode

    subscribe = dataclasses.replace(_subscribe(), media_source="themoviedb", media_id="203483")
    key = (subscribe_media_keys(subscribe) or ["203483"])[0]
    aired_missing = list(range(_AIRED + 1, _TOTAL + 1))
    no_exists = {
        key: {
            1: NotExistMediaInfo(
                season=1, episodes=aired_missing, total_episode=_TOTAL, start_episode=1,
            )
        }
    }
    assert compute_lack_episode(subscribe, no_exists=no_exists) == _TOTAL - _AIRED

    empty = {
        key: {1: NotExistMediaInfo(season=1, episodes=[], total_episode=_TOTAL, start_episode=1)}
    }
    assert compute_lack_episode(subscribe, no_exists=empty) == _TOTAL
