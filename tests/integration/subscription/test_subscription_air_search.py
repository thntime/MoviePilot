"""
定时搜索的播出窗口门控测试。

覆盖三件事：窗口未开时不进入搜索、窗口开时正常进入、拿不到播出信息时放行。
"""

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.query import subscription_search_due
from app.chain.media import MediaChain
from app.chain.subscribe.facade import SubscribeChain
from app.db.adapters.subscription import TransactionalSubscriptionRepository
from app.db.models.subscribe import Subscribe
from app.db.session import SessionFactory, async_session_scope
from app.schemas.subscribe import Subscribe as SubscribeSchema
from app.schemas.types import MediaSource, MediaType

TV = MediaType.TV.value
MOVIE = MediaType.MOVIE.value


def _recent(hours: int = 2) -> str:
    """
    构造一个已经超过默认间隔的最近搜索时间。
    """
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _subscribe(**overrides) -> SubscriptionSnapshot:
    """
    构造启用播出窗口的电视剧订阅，未指定的字段取默认值。
    """
    values = {
        "id": 1,
        "name": "凡人修仙传",
        "state": "R",
        "type": TV,
        "season": 1,
        "search_interval": 1,
        "last_search": _recent(),
        "air_search_time": "11:00",
    }
    values.update(overrides)
    return SubscriptionSnapshot(**values)


def _mediainfo(*air_dates: str) -> SimpleNamespace:
    """
    构造带指定播出日期的识别结果。
    """
    return SimpleNamespace(
        season_info=[
            {
                "season_number": 1,
                "episodes": [{"air_date": value} for value in air_dates],
            }
        ]
    )


def _chain(subscribes: list[SubscriptionSnapshot]) -> SubscribeChain:
    """
    构造只带仓储依赖的订阅链，供准入过滤调用。
    """
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(
        list=Mock(return_value=subscribes),
        get=Mock(side_effect=lambda sid: next((item for item in subscribes if item.id == sid), None)),
    )
    return chain


def _patch_recognize(monkeypatch, result) -> Mock:
    """
    替换媒体识别入口并返回调用计数桩。
    """
    calls = Mock()

    def _recognize(_self, **_kwargs):
        calls()
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(MediaChain, "recognize_media", _recognize)
    return calls


def test_window_closed_keeps_due_subscription_out_of_search(monkeypatch) -> None:
    """
    到期订阅若未进入播出窗口，本轮不进入搜索。
    """
    subscribe = _subscribe()
    calls = _patch_recognize(monkeypatch, _mediainfo("2020-01-01"))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == []
    assert calls.call_count == 1


def test_window_open_keeps_due_subscription(monkeypatch) -> None:
    """
    播出时刻已过的到期订阅正常进入搜索。
    """
    subscribe = _subscribe(air_search_time="00:00")
    _patch_recognize(monkeypatch, _mediainfo(date.today().isoformat()))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == [subscribe]


def test_recognition_failure_allows_search(monkeypatch) -> None:
    """
    播出日期读取失败时放行，避免外部数据不可用导致永久不搜。
    """
    subscribe = _subscribe()
    _patch_recognize(monkeypatch, RuntimeError("tmdb 不可用"))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == [subscribe]


def test_recognition_returning_nothing_allows_search(monkeypatch) -> None:
    """
    识别不到媒体信息时同样放行。
    """
    subscribe = _subscribe()
    _patch_recognize(monkeypatch, None)
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == [subscribe]


def test_weekday_fallback_opens_window_without_calendar(monkeypatch) -> None:
    """
    日历没有可用日期时，配置的兜底周几仍可打开窗口。
    """
    weekday = date.today().isoweekday()
    subscribe = _subscribe(air_search_time="00:00", air_search_weekdays=str(weekday))
    _patch_recognize(monkeypatch, _mediainfo())
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == [subscribe]


def test_disabled_window_skips_recognition(monkeypatch) -> None:
    """
    未配置播出时刻的订阅完全不走识别，保持原有行为与开销。
    """
    subscribe = _subscribe(air_search_time=None)
    calls = _patch_recognize(monkeypatch, _mediainfo("2020-01-01"))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == [subscribe]
    assert calls.call_count == 0


def test_movie_subscription_skips_recognition(monkeypatch) -> None:
    """
    电影没有分集播出日期，不参与播出窗口判定。
    """
    subscribe = _subscribe(type=MOVIE)
    calls = _patch_recognize(monkeypatch, _mediainfo("2020-01-01"))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=1) == [subscribe]
    assert calls.call_count == 0


def test_manual_search_bypasses_window(monkeypatch) -> None:
    """
    手动搜索不传定时间隔，因此不经过播出窗口判定。
    """
    subscribe = _subscribe()
    calls = _patch_recognize(monkeypatch, _mediainfo("2020-01-01"))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R") == [subscribe]
    assert calls.call_count == 0


def test_targeted_search_bypasses_window(monkeypatch) -> None:
    """
    指定订阅标识的补搜同样不受播出窗口限制。
    """
    subscribe = _subscribe()
    calls = _patch_recognize(monkeypatch, _mediainfo("2020-01-01"))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(1, None, "R", scheduled_interval=1) == [subscribe]
    assert chain._load_search_subscriptions(None, (1,), "R", scheduled_interval=1) == [subscribe]
    assert calls.call_count == 0


def test_not_due_subscription_skips_recognition(monkeypatch) -> None:
    """
    尚未到期的订阅在窗口判定之前就被过滤，不产生识别请求。
    """
    subscribe = _subscribe(last_search=datetime.now(timezone.utc).isoformat())
    calls = _patch_recognize(monkeypatch, _mediainfo(date.today().isoformat()))
    chain = _chain([subscribe])

    assert chain._load_search_subscriptions(None, None, "R", scheduled_interval=24) == []
    assert calls.call_count == 0


def test_repository_round_trip_keeps_air_search_fields(db) -> None:
    """
    播出窗口配置随订阅落库，仓储投影原样读回。
    """
    record = db.add(
        Subscribe(
            name="播出窗口往返",
            type=TV,
            media_source=MediaSource.TMDB.value,
            media_id="air-search-round-trip",
            search_interval=6,
            air_search_time="11:00",
            air_search_weekdays="6,7",
        )
    )
    repository = TransactionalSubscriptionRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )

    snapshot = repository.get(record.id)

    assert snapshot is not None
    assert snapshot.air_search_time == "11:00"
    assert snapshot.air_search_weekdays == "6,7"
    assert snapshot.search_interval == 6


def test_schema_accepts_and_normalizes_air_search_fields() -> None:
    """
    接口模型接受合法播出时刻与周几，并把周几规范化成去重升序文本。
    """
    schema = SubscribeSchema.model_validate({
        "name": "播出窗口校验",
        "air_search_time": "11:00",
        "air_search_weekdays": "7,6,6",
    })

    assert schema.air_search_time == "11:00"
    assert schema.air_search_weekdays == "6,7"


@pytest.mark.parametrize("payload", [
    {"air_search_time": "24:00"},
    {"air_search_time": "11:60"},
    {"air_search_time": "11"},
    {"air_search_weekdays": "0"},
    {"air_search_weekdays": "8"},
    {"air_search_weekdays": "sat"},
])
def test_schema_rejects_invalid_air_search_fields(payload) -> None:
    """
    非法时刻与越界周几在接口层就被拒绝，不会静默存成未启用。
    """
    with pytest.raises(ValidationError):
        SubscribeSchema.model_validate({"name": "非法配置", **payload})


def test_schema_treats_empty_string_as_disabled() -> None:
    """
    表单清空提交的空字符串等价于关闭播出窗口。
    """
    schema = SubscribeSchema.model_validate({
        "name": "清空配置",
        "air_search_time": "",
        "air_search_weekdays": "",
    })

    assert schema.air_search_time is None
    assert schema.air_search_weekdays is None


def test_schema_treats_whitespace_only_as_disabled() -> None:
    """
    只含空白的播出配置同样等价于关闭，覆盖空字符串被前置规范化拦不到的情形。
    """
    schema = SubscribeSchema.model_validate({
        "name": "空白配置",
        "air_search_time": "   ",
        "air_search_weekdays": "  ",
    })

    assert schema.air_search_time is None
    assert schema.air_search_weekdays is None


def test_schema_accepts_weekday_list_and_skips_blank_items() -> None:
    """
    兜底周几接受列表输入，空项被跳过并按去重升序输出。
    """
    schema = SubscribeSchema.model_validate({
        "name": "列表配置",
        "air_search_weekdays": ["7", " 6 ", ""],
    })

    assert schema.air_search_weekdays == "6,7"


def test_schema_weekdays_with_only_blank_items_is_disabled() -> None:
    """
    列表里全是空项时视为未配置兜底周几。
    """
    schema = SubscribeSchema.model_validate({"name": "空项配置", "air_search_weekdays": " , "})

    assert schema.air_search_weekdays is None


@pytest.mark.parametrize("payload", [
    {"air_search_time": 1100},
    {"air_search_weekdays": 6},
])
def test_schema_rejects_non_text_air_search_fields(payload) -> None:
    """
    非文本输入直接拒绝，不做隐式转换。
    """
    with pytest.raises(ValidationError):
        SubscribeSchema.model_validate({"name": "非文本配置", **payload})


@pytest.mark.parametrize("air_time,weekdays,expected", [
    ("11:00", None, False),
    (None, None, True),
    ("11:00", "1,2,3,4,5,6,7", True),
])
def test_air_search_allowed_matrix(monkeypatch, air_time, weekdays, expected) -> None:
    """
    门控入口对窗口关、未启用与周几兜底三种形态给出确定结果。

    第三条覆盖兜底周几：配置了全部周几时，最近一次触发点必定在一天之内，窗口必然打开。
    """
    subscribe = _subscribe(air_search_time=air_time, air_search_weekdays=weekdays)
    _patch_recognize(monkeypatch, _mediainfo("2020-01-01"))
    chain = _chain([subscribe])

    assert chain._air_search_allowed(subscribe) is expected


DUE_NOW = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)


def _searched_ago(**delta) -> str:
    """
    构造距 DUE_NOW 指定时长的上次搜索时间。
    """
    return (DUE_NOW - timedelta(**delta)).isoformat()


def test_due_uses_default_15_minutes_when_air_window_enabled() -> None:
    """
    启用播出窗口的订阅按分钟级间隔到期，未配置时默认 15 分钟。
    """
    subscribe = _subscribe(air_search_time="11:00")

    assert not subscription_search_due(
        replace(subscribe, last_search=_searched_ago(minutes=14)), 24, DUE_NOW
    )
    assert subscription_search_due(
        replace(subscribe, last_search=_searched_ago(minutes=15)), 24, DUE_NOW
    )


def test_due_honors_custom_air_retry_minutes() -> None:
    """
    自定义分钟间隔覆盖默认 15 分钟。
    """
    subscribe = _subscribe(air_search_time="11:00", air_search_interval_minutes=30)

    assert not subscription_search_due(
        replace(subscribe, last_search=_searched_ago(minutes=29)), 24, DUE_NOW
    )
    assert subscription_search_due(
        replace(subscribe, last_search=_searched_ago(minutes=30)), 24, DUE_NOW
    )


def test_due_keeps_hour_interval_without_air_window() -> None:
    """
    未启用播出窗口的订阅仍按小时间隔判定，不受分钟逻辑影响。
    """
    subscribe = _subscribe(air_search_time=None, search_interval=2)

    assert not subscription_search_due(
        replace(subscribe, last_search=_searched_ago(minutes=119)), 24, DUE_NOW
    )
    assert subscription_search_due(
        replace(subscribe, last_search=_searched_ago(minutes=120)), 24, DUE_NOW
    )
