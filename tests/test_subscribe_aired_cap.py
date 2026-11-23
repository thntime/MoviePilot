"""
验证订阅有效总集数按已播出集号封顶。

TMDB 会把还没播出的集也列进季清单（`episode_count` 200，实际只播到 168），
订阅若直接拿季清单长度当目标范围，就会一直去搜未播出的集。本文件锁住封顶
口径与拿不到播出信息时的回退口径。
"""

from typing import Any, Dict, List

from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo
from app.domain.projection.aired import aired_episode_cap
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


def test_effective_total_episode_is_capped_by_aired() -> None:
    """TMDB 声明 200 集、实际播到 168 集时，有效总集数是 168。"""
    assert _effective(_subscribe(), _mediainfo(aired=_AIRED)) == _AIRED


def test_effective_total_episode_keeps_metadata_when_aired_unknown() -> None:
    """拿不到播出信息时保持原有口径，仍用元数据季集数。"""
    assert _effective(_subscribe(), _mediainfo(aired=0)) == _TOTAL


def test_effective_total_episode_respects_manual_total() -> None:
    """手工指定总集数时不被播出信息改写。"""
    assert _effective(_subscribe(manual=1), _mediainfo(aired=_AIRED)) == _TOTAL
