"""
从媒体信息里取出「已播出季集」的纯规则。

订阅计算缺集需要知道这部剧播到哪一集：TMDB 的季清单会按 `episode_count`
把未来集一并列出来，若直接拿它当目标范围，订阅就会一直去搜还没播的集，
最后从别的作品的集号里凑出「命中」。本模块只做投影计算，不访问网络或数据库。
"""

from __future__ import annotations

import datetime
from typing import Any, Optional, Tuple

# 已播出集号集合的返回类型：季号到该季已播出集号序列。
AiredSeasons = Tuple[Tuple[int, Tuple[int, ...]], ...]


def last_aired_point(info: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    取最近播出的那一集，用来判断连载剧播到哪。

    TMDB 的 `tv/{id}` 详情带 `last_episode_to_air`，统一媒体字段没有投影它，
    这里直接读原始字典；其它数据源没有该字段时返回两个 None。

    :param info: 识别出的媒体信息
    :return: （季号, 集号）；拿不到时两个元素都是 None
    """
    tmdb_info = getattr(info, "tmdb_info", None)
    if not isinstance(tmdb_info, dict):
        return None, None
    last = tmdb_info.get("last_episode_to_air")
    if not isinstance(last, dict):
        return None, None
    try:
        season = int(last.get("season_number"))
        episode = int(last.get("episode_number"))
    except (TypeError, ValueError):
        return None, None
    return season, episode


def aired_seasons(info: Any, today: Optional[str] = None, include_fallback: bool = True) -> AiredSeasons:
    """
    取已播出的季集，作为缺集计算的分母。

    优先级从准到粗：季内单集清单带播出日期时按日期过滤；否则用 TMDB 的
    `last_episode_to_air` 判断该季播到第几集（更早的季视为整季播完，更晚的季视为未开播）；
    再否则只有季首播日期晚于今天时整季排除；全都没有才退回该季集数清单。

    :param info: 识别出的媒体信息
    :param today: 今天的日期文本（YYYY-MM-DD），为空时取系统日期
    :param include_fallback: 没有任何播出证据时是否退回该季集数清单；
        封顶场景必须为 False，否则会把「元数据里的集数」误当成「已播出集数」
    :return: 季号到已播出集号集合的序列
    """
    current_day = today or datetime.date.today().isoformat()
    last_season, last_episode = last_aired_point(info)
    aired: dict[int, set[int]] = {}
    decided: set[int] = set()
    for season in getattr(info, "season_info", None) or []:
        if not isinstance(season, dict):
            continue
        number = season.get("season_number")
        if number is None:
            number = season.get("order")
        if number is None:
            continue
        try:
            season_number = int(number)
        except (TypeError, ValueError):
            continue
        episodes = [item for item in (season.get("episodes") or []) if isinstance(item, dict)]
        dated = [item for item in episodes if item.get("air_date")]
        if dated:
            picked: set[int] = set()
            for episode in episodes:
                value = episode.get("episode_number")
                if value is None:
                    continue
                date = str(episode.get("air_date") or "")
                if not date or date > current_day:
                    continue
                try:
                    picked.add(int(value))
                except (TypeError, ValueError):
                    continue
            if picked:
                aired[season_number] = picked
            decided.add(season_number)
            continue
        if last_season is not None and last_episode is not None:
            if season_number < last_season:
                count = season.get("episode_count")
                if isinstance(count, int) and count > 0:
                    aired[season_number] = set(range(1, count + 1))
            elif season_number == last_season:
                aired[season_number] = set(range(1, last_episode + 1))
            decided.add(season_number)
            continue
        season_air_date = str(season.get("air_date") or "")
        if season_air_date and season_air_date > current_day:
            decided.add(season_number)
    for number, numbers in (getattr(info, "seasons", None) or {}).items():
        try:
            season_number = int(number)
        except (TypeError, ValueError):
            continue
        if season_number in decided or season_number in aired:
            continue
        if not include_fallback:
            continue
        try:
            aired[season_number] = {int(value) for value in numbers or []}
        except (TypeError, ValueError):
            continue
    return tuple(
        (number, tuple(sorted(numbers))) for number, numbers in sorted(aired.items())
    )


def aired_episode_cap(info: Any, season: Optional[int], today: Optional[str] = None) -> Optional[int]:
    """
    取某一季已播出的最大集号，用于给订阅目标范围封顶。

    :param info: 识别出的媒体信息
    :param season: 目标季号；为空时返回 None
    :param today: 今天的日期文本（YYYY-MM-DD），为空时取系统日期
    :return: 已播出最大集号；拿不到播出信息时返回 None
    """
    if season is None:
        return None
    for number, episodes in aired_seasons(info, today=today, include_fallback=False):
        if number != int(season):
            continue
        if not episodes:
            return None
        return max(episodes)
    return None
