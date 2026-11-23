"""
验证整季包提交时按订阅目标范围裁剪。

订阅只到 168 集、分享里却有 185 集时，越界的 169-185 不能跟着一起下走，
否则会把别的作品（或还没播出的集）当成目标集。
"""

from types import SimpleNamespace
from typing import Set

from app.chain.download.batch import _BatchDownloadRun

_TOTAL = 168
_SHARE = set(range(1, 186))


def _info(total: int = _TOTAL, start: int = 0) -> SimpleNamespace:
    """
    构造某季的缺集信息替身。

    :param total: 订阅目标总集数
    :param start: 订阅起始集号
    :return: 缺集信息替身
    """
    return SimpleNamespace(total_episode=total, start_episode=start, episodes=list(_SHARE))


def test_target_excludes_episodes_beyond_subscribe_scope() -> None:
    """普通订阅（不要求完整覆盖）也要与订阅目标范围求交。"""
    target: Set[int] = _BatchDownloadRun._resolve_season_target(set(), _SHARE, _info())

    assert target == set(range(1, _TOTAL + 1))
    assert 169 not in target and 185 not in target


def test_target_keeps_all_when_scope_unknown() -> None:
    """拿不到总集数时不裁剪，保持原有整包行为。"""
    target: Set[int] = _BatchDownloadRun._resolve_season_target(set(), {1, 2, 3}, _info(total=0))

    assert target == {1, 2, 3}


def test_target_uses_intersection_when_complete_coverage() -> None:
    """要求完整覆盖时仍按目标范围求交。"""
    target: Set[int] = _BatchDownloadRun._resolve_season_target({1, 2, 3}, {2, 3, 4}, _info())

    assert target == {2, 3}


def test_target_respects_start_episode() -> None:
    """起始集号之后的集才算目标集。"""
    target: Set[int] = _BatchDownloadRun._resolve_season_target(set(), {1, 2, 3}, _info(start=2))

    assert target == {2, 3}
