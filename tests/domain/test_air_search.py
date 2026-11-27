"""
播出窗口纯函数的边界测试。

时间基准：2026-11-27 是周五，2026-11-28 是周六（ISO 周号 6）。
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.domain.air_search import (
    AIR_SEARCH_WINDOW_HOURS,
    air_search_window_open,
    parse_air_time,
    parse_air_weekdays,
)

SATURDAY = date(2026, 11, 28)
FRIDAY = date(2026, 11, 27)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """
    构造本地朴素时间。
    """
    return datetime.combine(day, time(hour=hour, minute=minute))


def test_week_baseline_matches_expected_weekday() -> None:
    """
    基准日期本身的周号必须与用例假设一致，否则整套边界数字都会错位。
    """
    assert FRIDAY.isoweekday() == 5
    assert SATURDAY.isoweekday() == 6


def test_window_hours_constant_is_48() -> None:
    """
    窗口长度常量是任务裁决的 48 小时，改动需要同步文档。
    """
    assert AIR_SEARCH_WINDOW_HOURS == 48


@pytest.mark.parametrize(
    "air_time,expected",
    [("00:00", time(0, 0)), ("11:00", time(11, 0)), ("23:59", time(23, 59)), ("7:00", None), ("24:00", None), ("", None), (None, None)],
)
def test_parse_air_time(air_time, expected) -> None:
    """
    时刻解析只接受两位数字的 24 小时制 HH:MM。
    """
    assert parse_air_time(air_time) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("6", (6,)), ("7,6", (6, 7)), ("1,1,3", (1, 3)), ("0,8", ()), ("a", ()), ("", ()), (None, ())],
)
def test_parse_air_weekdays(value, expected) -> None:
    """
    兜底周几解析去重升序，并丢掉越界与非数字项。
    """
    assert parse_air_weekdays(value) == expected


def test_no_air_time_disables_window() -> None:
    """
    没配播出时刻等于没启用播出窗口，任何时间都放行。
    """
    assert air_search_window_open(_at(SATURDAY, 3), ["2026-11-28"], None, None)
    assert air_search_window_open(_at(SATURDAY, 3), None, "6", "")


def test_calendar_window_boundaries() -> None:
    """
    日历来源的窗口是左闭右开：起点当刻放行，起点前 1 分钟与满 48 小时都拦截。
    """
    air_dates = ["2026-11-28"]
    assert not air_search_window_open(_at(SATURDAY, 10, 59), air_dates, None, "11:00")
    assert air_search_window_open(_at(SATURDAY, 11), air_dates, None, "11:00")
    assert air_search_window_open(_at(SATURDAY, 23, 59), air_dates, None, "11:00")
    assert air_search_window_open(
        _at(SATURDAY, 11) + timedelta(hours=47, minutes=59), air_dates, None, "11:00"
    )
    assert not air_search_window_open(
        _at(SATURDAY, 11) + timedelta(hours=48), air_dates, None, "11:00"
    )


def test_calendar_uses_latest_aired_date() -> None:
    """
    多集播出日期并存时按最新一集算触发点，且未来日期不参与。
    """
    air_dates = ["2026-11-14", "2026-11-21", "2026-11-28", "2026-12-05"]
    # 11-28 11:00 之后窗口开；12-05 是未来集，不能把触发点推到它上面
    assert air_search_window_open(_at(SATURDAY, 12), air_dates, None, "11:00")
    assert not air_search_window_open(_at(SATURDAY, 10), air_dates, None, "11:00")


def test_weekday_window_boundaries() -> None:
    """
    周几来源在配置周几的播出时刻之后放行，未到时刻时回落到上一周。
    """
    assert air_search_window_open(_at(SATURDAY, 12), [], "6", "11:00")
    assert not air_search_window_open(_at(SATURDAY, 10), [], "6", "11:00")
    # 周五 12:00 距上一个周六 11:00 已超 48 小时
    assert not air_search_window_open(_at(FRIDAY, 12), [], "6", "11:00")
    # 周日 12:00 距周六 11:00 只过 25 小时，仍在窗口内
    assert air_search_window_open(_at(date(2026, 11, 29), 12), [], "6", "11:00")


def test_sources_take_union() -> None:
    """
    日历与周几取并集：日历已过窗口时周几仍可放行，周几不命中时日历经命中即放行。
    """
    stale_calendar = ["2026-11-20"]
    assert air_search_window_open(_at(SATURDAY, 12), stale_calendar, "6", "11:00")
    assert air_search_window_open(_at(SATURDAY, 12), ["2026-11-28"], "1", "11:00")


def test_without_any_evidence_allow_search() -> None:
    """
    既没有可用播出日期也没有兜底周几时放行，宁可多搜不可漏搜。
    """
    assert air_search_window_open(_at(SATURDAY, 3), None, None, "11:00")
    assert air_search_window_open(_at(SATURDAY, 3), [], "", "11:00")
    assert air_search_window_open(_at(SATURDAY, 3), ["not-a-date"], "9", "11:00")


def test_aware_now_is_converted_to_local_clock() -> None:
    """
    带时区的当前时间按服务器本地时钟参与比较。
    """
    local_tz = datetime.now().astimezone().tzinfo
    assert local_tz is not None
    aware = datetime.combine(SATURDAY, time(12, 0), tzinfo=local_tz)
    assert air_search_window_open(aware, ["2026-11-28"], None, "11:00")
    earlier = datetime.combine(SATURDAY, time(10, 0), tzinfo=local_tz)
    assert not air_search_window_open(earlier, ["2026-11-28"], None, "11:00")
