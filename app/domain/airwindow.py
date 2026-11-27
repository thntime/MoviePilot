"""
订阅定时搜索的播出窗口判定。

定时搜索原本只按固定小时间隔执行；本模块提供纯函数，把「现在是否处在
播出之后的可搜索窗口内」这件事算清楚，供调度层在取到期订阅时过滤。
函数不读配置、不做 I/O，时间来源全部由调用方注入。
"""

from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Sequence

# 触发点之后放开定时搜索的小时数：覆盖资源晚于播出数小时到一天才出现的常见情况，
# 又明显短于周更周期，避免整周空转
AIR_SEARCH_WINDOW_HOURS = 48

# 窗口内的默认重试间隔（分钟）：窗口只有 48 小时，按小时级间隔会把窗口耗在等下一次到期上，
# 取 15 分钟与调度器 5 分钟的扫描粒度对齐
AIR_SEARCH_RETRY_MINUTES = 15

# 播出时刻的合法形态：24 小时制两位数字
_AIR_TIME_SEPARATOR = ":"


def _local_naive(moment: datetime) -> datetime:
    """
    把带时区的时间换算到服务器本地时区，并去掉时区信息参与比较。

    播出日期只有日期语义，时刻由用户按本地时区填写，因此两侧必须落在同一本地时钟上。

    :param moment: 调用方给出的当前时间，可带时区也可不带
    :return: 服务器本地时区的朴素时间
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def parse_air_time(value: Optional[str]) -> Optional[time]:
    """
    把 HH:MM 文本解析为时刻；空值或非法值返回 None。

    :param value: 订阅配置的播出时刻文本
    :return: 解析出的时刻，无法使用时为 None
    """
    if not value:
        return None
    text = value.strip()
    if len(text) != 5 or text[2] != _AIR_TIME_SEPARATOR:
        return None
    hours_text, minutes_text = text[:2], text[3:]
    if not (hours_text.isdecimal() and minutes_text.isdecimal()):
        return None
    hours, minutes = int(hours_text), int(minutes_text)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return time(hour=hours, minute=minutes)


def parse_air_weekdays(value: Optional[str]) -> tuple[int, ...]:
    """
    把逗号分隔的 ISO 周号解析为去重升序元组；空值或非法值返回空元组。

    :param value: 订阅配置的兜底周几文本，1 为周一、7 为周日
    :return: 合法周号元组
    """
    if not value:
        return ()
    numbers: set[int] = set()
    for item in value.split(","):
        text = item.strip()
        if not text or not text.isdecimal():
            continue
        number = int(text)
        if 1 <= number <= 7:
            numbers.add(number)
    return tuple(sorted(numbers))


def season_air_dates(info: Any, season: Optional[int]) -> tuple[str, ...]:
    """
    取某一季各集的播出日期文本。

    只读取识别结果里的分集日期，不做日期比较；未来集的过滤交给窗口判定，
    这样调用方拿到的是原样事实，判定口径集中在一处。

    :param info: 识别出的媒体信息，需要带 season_info
    :param season: 目标季号；为空时返回空元组
    :return: 该季各集的 air_date 文本
    """
    if season is None or info is None:
        return ()
    for season_info in getattr(info, "season_info", None) or ():
        if not isinstance(season_info, dict):
            continue
        number = season_info.get("season_number")
        try:
            if number is None or int(number) != int(season):
                continue
        except (TypeError, ValueError):
            continue
        dates: list[str] = []
        for episode in season_info.get("episodes") or ():
            if not isinstance(episode, dict):
                continue
            air_date = episode.get("air_date")
            if air_date:
                dates.append(str(air_date))
        return tuple(dates)
    return ()


def _parse_air_date(value: str) -> Optional[date]:
    """
    把 YYYY-MM-DD 文本解析为日期；非法值返回 None。

    :param value: 播出日期文本
    :return: 解析出的日期，无法使用时为 None
    """
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        return None


def _latest_aired_date(air_dates: Optional[Sequence[str]], today: date) -> Optional[date]:
    """
    取不晚于今天的最新播出日期。

    未来日期属于尚未播出的集，不能作为触发点，因此在这里统一滤掉。

    :param air_dates: 该季各集的播出日期文本
    :param today: 本地今天
    :return: 最新已播出日期，没有可用日期时为 None
    """
    latest: Optional[date] = None
    for value in air_dates or ():
        parsed = _parse_air_date(value)
        if parsed is None or parsed > today:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _latest_weekday_trigger(
    now: datetime,
    weekdays: Sequence[int],
    air_time: time,
) -> Optional[datetime]:
    """
    取不晚于当前时间的最近一次「配置周几 + 播出时刻」触发点。

    今天就是配置的周几但还没到时刻时，触发点落在上一周的同一天。

    :param now: 本地朴素当前时间
    :param weekdays: 配置的 ISO 周号
    :param air_time: 播出时刻
    :return: 最近一次触发点，未配置周几时为 None
    """
    if not weekdays:
        return None
    today = now.date()
    for offset in range(0, 8):
        candidate_date = today - timedelta(days=offset)
        if candidate_date.isoweekday() not in weekdays:
            continue
        trigger = datetime.combine(candidate_date, air_time)
        if trigger <= now:
            return trigger
    return None


def _within_window(now: datetime, trigger: Optional[datetime], window_hours: int) -> bool:
    """
    判断当前时间是否落在触发点之后、窗口长度之内。

    :param now: 本地朴素当前时间
    :param trigger: 触发点；为空表示该来源没有证据
    :param window_hours: 窗口长度（小时）
    :return: 是否在窗口内
    """
    if trigger is None:
        return False
    return trigger <= now < trigger + timedelta(hours=window_hours)


def air_search_window_open(
    now: datetime,
    air_dates: Optional[Sequence[str]],
    weekdays: Optional[str],
    air_time: Optional[str],
    window_hours: int = AIR_SEARCH_WINDOW_HOURS,
) -> bool:
    """
    判断定时搜索此刻是否允许放行。

    规则按「宁可多搜、不可漏搜」排序：

    1. 没配播出时刻，说明订阅没启用播出窗口，直接放行。
    2. 日历来源：取已播出集里最新的播出日期，触发点为该日播出时刻。
    3. 周几来源：取最近一次「配置周几 + 播出时刻」，用于兜底日历日期不准或缺失。
    4. 两个来源取并集，任一落在窗口内即放行。
    5. 两个来源都没有可用证据时放行，避免因为拿不到播出信息而永久不搜。

    :param now: 当前时间，可带时区；带时区时按服务器本地时区换算
    :param air_dates: 该季已播出集的播出日期文本，顺序不限
    :param weekdays: 兜底周几文本，逗号分隔的 ISO 周号 1-7
    :param air_time: 播出日当天的更新时刻 HH:MM
    :param window_hours: 触发点之后放开搜索的小时数
    :return: 是否允许本轮定时搜索
    """
    parsed_time = parse_air_time(air_time)
    if parsed_time is None:
        return True

    local_now = _local_naive(now)
    today = local_now.date()
    latest_date = _latest_aired_date(air_dates, today)
    calendar_trigger = datetime.combine(latest_date, parsed_time) if latest_date else None
    weekday_trigger = _latest_weekday_trigger(local_now, parse_air_weekdays(weekdays), parsed_time)

    if calendar_trigger is None and weekday_trigger is None:
        # 没有播出日期也没有兜底周几：拿不到证据时放行，漏集代价高于多搜
        return True

    return _within_window(local_now, calendar_trigger, window_hours) or _within_window(
        local_now, weekday_trigger, window_hours
    )
