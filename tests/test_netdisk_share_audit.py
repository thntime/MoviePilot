"""
分享体检（share audit）的行为测试。

体检回答三件事：这个分享是哪个媒体、里面有多少东西、媒体库缺哪几集。
其中「媒体库缺哪几集」依赖媒体服务器缓存里的季集快照，快照缺失时必须返回
空集合交给调用方标「未确认」，不能直接判成缺失——否则用户会重复下载已有的集。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.application.netdisk.audit import (
    EPISODE_MISSING,
    EPISODE_PRESENT,
    EPISODE_UNCONFIRMED,
    LIBRARY_PRESENT,
    LIBRARY_UNKNOWN,
    AuditedMedia,
    RecognizedMedia,
    ShareAuditService,
    ShareFileGroup,
    audit_group,
    group_share_files,
)
from app.db.models.mediaserver import MediaServerItem
from app.db.oper.mediaserver import MediaServerOper
from app.db.session import async_session_scope
from app.domain.netdisk import ShareFile
from app.schemas.types import MediaSource


@pytest.fixture(autouse=True)
def _track(db):
    """
    把媒体服务器条目表纳入用例级回收。
    """
    db.watermark(MediaServerItem)


def _item(
    server: str = "emby",
    item_id: str = "it-1",
    title: str = "示例剧",
    item_type: str = "电视剧",
    year: str = "2026",
    media_id: str = "9001",
    seasoninfo=None,
) -> MediaServerItem:
    """
    构造一条带季集快照的媒体服务器缓存条目。
    """
    return MediaServerItem(
        server=server,
        library="lib",
        item_id=item_id,
        item_type=item_type,
        title=title,
        year=year,
        media_source=str(MediaSource.TMDB),
        media_id=media_id,
        seasoninfo=seasoninfo,
        lst_mod_date="2026-11-19 10:00:00",
    )


def _query(**overrides):
    """
    构造带默认身份的季集查询参数。
    """
    payload = {
        "media_source": MediaSource.TMDB,
        "media_id": "9001",
        "mtype": "电视剧",
        "season": 1,
    }
    payload.update(overrides)
    return payload


def test_season_episodes_accepts_string_and_int_season_keys(db):
    """
    JSON 往返会把季号键变成字符串，两种写法都要取到同一批集号。
    """
    db.add(_item(item_id="str-key", seasoninfo={"1": [1, 2, 3]}))
    db.add(_item(item_id="int-key", media_id="9002", seasoninfo={1: [4, 5]}))

    assert MediaServerOper(db.session).get_season_episodes(**_query()) == {1, 2, 3}
    assert MediaServerOper(db.session).get_season_episodes(
        **_query(media_id="9002")
    ) == {4, 5}


def test_season_episodes_returns_empty_set_when_snapshot_missing(db):
    """
    快照缺失、不是字典、或目标季不存在时返回空集合，交给调用方标「未确认」。
    """
    db.add(_item(item_id="no-snapshot", media_id="9003", seasoninfo=None))
    db.add(_item(item_id="bad-snapshot", media_id="9004", seasoninfo=["oops"]))
    db.add(_item(item_id="other-season", media_id="9005", seasoninfo={"2": [1]}))

    oper = MediaServerOper(db.session)
    assert oper.get_season_episodes(**_query(media_id="9003")) == set()
    assert oper.get_season_episodes(**_query(media_id="9004")) == set()
    assert oper.get_season_episodes(**_query(media_id="9005")) == set()


def test_season_episodes_returns_empty_set_when_not_found(db):
    """
    媒体库里没有这部作品时返回空集合，不抛异常。
    """
    oper = MediaServerOper(db.session)

    assert oper.get_season_episodes(**_query(media_id="missing")) == set()
    assert oper.get_season_episodes(**_query(media_id="missing", season=None)) == set()


def test_async_season_episodes_matches_sync_twin(db):
    """
    异步查询与同步查询必须返回同一批集号。
    """
    db.add(_item(item_id="async-1", media_id="9006", seasoninfo={"1": [7, 8]}))

    async def check() -> set[int]:
        """
        在显式 AsyncSession 上执行异步季集查询。
        """
        async with async_session_scope() as session:
            return await MediaServerOper(session).async_get_season_episodes(
                **_query(media_id="9006")
            )

    assert asyncio.run(check()) == {7, 8}


def _share_file(name: str, path: str, size: int = 1024) -> ShareFile:
    """
    构造一条分享内视频文件条目。
    """
    return ShareFile(file_id=name, name=name, size=size, path=path)


class _Probe:
    """
    记录媒体库探针的调用，并按预设返回集号。
    """

    def __init__(self, episodes=None, has_item: bool = False) -> None:
        """
        保存按季预设的集号与整部作品存在性。
        """
        self._episodes = dict(episodes or {})
        self._has_item = has_item
        self.calls: list[tuple[str, int | None]] = []

    async def has_item(self, media: RecognizedMedia) -> bool:
        """
        返回预设的整部作品存在性。
        """
        self.calls.append(("has_item", None))
        return self._has_item

    async def season_episodes(self, media: RecognizedMedia, season: int) -> set[int]:
        """
        返回预设的某季集号集合。
        """
        self.calls.append(("season_episodes", season))
        return set(self._episodes.get(season, ()))


def _tv_group(numbers=range(1, 13), season: int = 1) -> ShareFileGroup:
    """
    构造一个整季电视剧分组。
    """
    files = tuple(
        _share_file(f"S{season:02d}E{number:02d}.mkv", f"示例剧/Season {season}/S{season:02d}E{number:02d}.mkv")
        for number in numbers
    )
    return ShareFileGroup(
        key="示例剧|2026",
        title="示例剧",
        year="2026",
        sample_path=files[0].path,
        files=files,
        total_bytes=sum(item.size for item in files),
        seasons=((season, tuple(numbers)),),
        mtype="电视剧",
    )


def _tv_media(aired=range(1, 13), season: int = 1) -> RecognizedMedia:
    """
    构造已识别为电视剧的媒体身份。
    """
    return RecognizedMedia(
        media_source="TheMovieDb",
        media_id="9001",
        title="示例剧",
        year="2026",
        mtype="电视剧",
        aired_seasons=((season, tuple(aired)),),
    )


def test_group_share_files_splits_distinct_titles():
    """
    两个不同剧名的子目录必须分成两组，不能合并成一部剧的多集。
    """
    files = [
        _share_file("01.mp4", "剧名甲/01.mp4"),
        _share_file("02.mp4", "剧名甲/02.mp4"),
        _share_file("01.mp4", "剧名乙/01.mp4"),
    ]

    groups = group_share_files(files)

    assert len(groups) == 2
    assert sum(len(group.files) for group in groups) == 3


def test_group_share_files_skips_specials_and_non_video():
    """
    花絮、字幕与图片不进入分组，也不计入体量。
    """
    files = [
        _share_file("01.mp4", "示例剧/01.mp4"),
        _share_file("花絮.mp4", "示例剧/花絮.mp4"),
        ShareFile(file_id="srt", name="01.ass", size=10, path="示例剧/01.ass"),
    ]

    groups = group_share_files(files)

    assert len(groups) == 1
    assert [item.name for item in groups[0].files] == ["01.mp4"]
    assert groups[0].total_bytes == 1024


def test_audit_group_reports_missing_episodes():
    """
    媒体库已有 1-8 集、分享含 1-12 集时，缺失集必须是 9-12。
    """
    result = asyncio.run(
        audit_group(_tv_group(), _tv_media(), probe=_Probe({1: [1, 2, 3, 4, 5, 6, 7, 8]}))
    )

    assert result.missing_episodes == (9, 10, 11, 12)
    assert result.existing_episodes == (1, 2, 3, 4, 5, 6, 7, 8)
    assert result.library_state == LIBRARY_PRESENT


def test_audit_group_ignores_unaired_episodes():
    """
    只播到第 8 集时，未播出的 9-12 集不能算缺失。
    """
    result = asyncio.run(
        audit_group(
            _tv_group(),
            _tv_media(aired=range(1, 9)),
            probe=_Probe({1: [1, 2, 3, 4, 5, 6, 7, 8]}),
        )
    )

    assert result.missing_episodes == ()
    assert result.existing_episodes == (1, 2, 3, 4, 5, 6, 7, 8)


def test_audit_group_marks_unconfirmed_without_library_evidence():
    """
    媒体库没有可用证据时全部标未确认，不许直接判缺失。
    """
    result = asyncio.run(audit_group(_tv_group(), _tv_media(), probe=_Probe()))

    assert result.missing_episodes == ()
    assert {state for _season, _episode, state in result.episode_states} == {EPISODE_UNCONFIRMED}
    assert result.library_evidence == "none"
    assert result.library_state == LIBRARY_UNKNOWN


def test_audit_group_marks_share_absent_episode():
    """
    媒体库缺、但分享里也没有的集标为分享无，不计入可补缺失集。
    """
    group = _tv_group(numbers=range(1, 11))
    result = asyncio.run(
        audit_group(group, _tv_media(), probe=_Probe({1: [1, 2, 3, 4, 5, 6, 7, 8]}))
    )

    states = {(season, episode): state for season, episode, state in result.episode_states}
    assert states[(1, 9)] == EPISODE_MISSING
    assert states[(1, 10)] == EPISODE_MISSING
    assert states[(1, 11)] == "share_absent"
    assert result.missing_episodes == (9, 10)


def test_audit_group_handles_movie_without_episodes():
    """
    电影只判整部在不在，不产生集数状态。
    """
    group = ShareFileGroup(
        key="某电影|2026",
        title="某电影",
        year="2026",
        sample_path="某电影/某电影.2026.mkv",
        files=(_share_file("某电影.2026.mkv", "某电影/某电影.2026.mkv"),),
        total_bytes=2048,
        seasons=(),
        mtype="电影",
    )
    media = RecognizedMedia(
        media_source="TheMovieDb",
        media_id="7001",
        title="某电影",
        year="2026",
        mtype="电影",
        aired_seasons=(),
    )

    present = asyncio.run(audit_group(group, media, probe=_Probe(has_item=True)))
    absent = asyncio.run(audit_group(group, media, probe=_Probe(has_item=False)))

    assert present.library_state == LIBRARY_PRESENT
    assert absent.library_state == LIBRARY_UNKNOWN
    assert present.episode_states == ()


def test_audit_group_keeps_unrecognized_group():
    """
    识别失败的分组保留标题与体量，状态为未识别。
    """
    result = asyncio.run(audit_group(_tv_group(), None, probe=_Probe({1: [1]})))

    assert result.recognized is False
    assert result.files == 12
    assert result.library_state == LIBRARY_UNKNOWN
    assert result.missing_episodes == ()


def test_share_audit_service_marks_truncated():
    """
    视频文件数超过上限时截断并标记报告不完整。
    """

    class _Resolved:
        """
        模拟分享解析结果。
        """

        class detail:  # noqa: N801 - 与解析结果字段同名
            """
            模拟分享详情。
            """

            title = "示例分享"

        class ref:  # noqa: N801 - 与解析结果字段同名
            """
            模拟分享引用。
            """

            platform = "quark"

        files = tuple(
            _share_file(f"{index:02d}.mp4", f"示例剧/{index:02d}.mp4")
            for index in range(1, 4)
        )

    def _walk(*_args, **_kwargs):
        """
        返回固定分享解析结果。
        """
        return _Resolved()

    async def _recognize(_path: str):
        """
        返回固定识别结果。
        """
        return _tv_media(aired=range(1, 4))

    service = ShareAuditService(
        walk=_walk, recognize=_recognize, probe=_Probe({1: [1]}), max_files=2
    )
    report = asyncio.run(service.audit("https://pan.quark.cn/s/demo"))

    assert report.truncated is True
    assert report.file_count == 2
    assert report.platform == "quark"
    assert report.share_title == "示例分享"
    # 截断后只剩前两集，未扫到的集不能算成「可补」。
    assert report.media[0].missing_episodes == (2,)


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------

def _audit_files(count: int = 12) -> tuple[ShareFile, ...]:
    """
    构造一季分享文件，文件名带标准季集标记。
    """
    return tuple(
        ShareFile(
            file_id=f"f{number}",
            name=f"示例剧.S01E{number:02d}.mkv",
            size=100,
            path=f"示例剧/示例剧.S01E{number:02d}.mkv",
            token="secret-entry-token",
        )
        for number in range(1, count + 1)
    )


class _FakeLibrary:
    """
    模拟媒体服务器本地缓存查询服务。
    """

    def __init__(self, episodes=None, has_item: bool = True) -> None:
        """
        保存按季预设的集号与整部作品存在性。
        """
        self._episodes = dict(episodes or {})
        self._has_item = has_item
        self.queries: list[dict] = []

    async def find_item_id(self, **kwargs) -> str | None:
        """
        返回预设的条目标识。
        """
        self.queries.append(dict(kwargs))
        return "item-1" if self._has_item else None

    async def find_season_episodes(self, **kwargs) -> set[int]:
        """
        返回预设的某季集号集合。
        """
        self.queries.append(dict(kwargs))
        return set(self._episodes.get(kwargs.get("season"), ()))


def _season_info(aired: int = 12, future_from: int | None = None) -> list[dict]:
    """
    构造 TMDB 风格的季详情；future_from 之后的集给未来播出日期。
    """
    episodes = []
    for number in range(1, 13):
        date = "2026-01-01"
        if future_from is not None and number >= future_from:
            date = "2099-01-01"
        episodes.append({"episode_number": number, "air_date": date})
    del aired
    return [{"season_number": 1, "episodes": episodes}]


def _build_audit_app(
    monkeypatch, *, files, season_info, library, history=None, media_type=None, tmdb_info=None,
    recognize_none=False, identity=None,
):
    """
    装配只包含网盘路由的独立应用，并用桩替换解析、识别与媒体库查询。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.dependencies.history import (
        get_history_query_service,
        get_mediaserver_query_service,
    )
    from app.api.endpoints import netdisk as netdisk_module
    from app.application.netdisk.resolve import NetdiskResolveService, ResolvedShare
    from app.chain.media import MediaChain
    from app.domain.netdisk import NetdiskPlatform, ShareDetail, ShareRef
    from app.schemas.types import MediaSource, MediaType

    state = SimpleNamespace(walk_calls=[], recognize_calls=[], identity_calls=[], error=None)
    recognized_type = media_type or MediaType.TV

    def fake_walk(self, url, password=None, platform=None, account_id=None, **kwargs):
        """
        记录解析入参并返回预置文件树。
        """
        del self
        state.walk_calls.append(
            {"url": url, "password": password, "platform": platform, "account_id": account_id,
             "max_depth": kwargs.get("max_depth")}
        )
        if state.error is not None:
            raise state.error
        return ResolvedShare(
            ref=ShareRef(platform=NetdiskPlatform.QUARK, share_id="abc123", url=url),
            detail=ShareDetail(title="示例分享", files=tuple(files)),
            files=tuple(files),
        )

    async def fake_recognize(self, path, media_source=None, episode_group=None, obtain_images=False):
        """
        记录识别入参并返回固定媒体身份。
        """
        del self, media_source, episode_group, obtain_images
        state.recognize_calls.append(path)
        if recognize_none:
            return None
        return SimpleNamespace(
            media_info=SimpleNamespace(
                media_source=MediaSource.TMDB,
                media_id="9001",
                title="示例剧",
                year="2026",
                type=recognized_type,
                season_info=season_info,
                seasons={1: list(range(1, 13))},
                tmdb_info=tmdb_info or {},
            )
        )

    async def fake_recognize_media(self, meta=None, mtype=None, media_source=None, media_id=None, **kwargs):
        """
        记录按指定身份识别的入参，并返回预置的媒体身份。
        """
        del self, meta, kwargs
        state.identity_calls.append(
            {"mtype": mtype, "media_source": media_source, "media_id": media_id}
        )
        if identity is None:
            return None
        return SimpleNamespace(media_info=identity)

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)
    monkeypatch.setattr(MediaChain, "async_recognize_by_path", fake_recognize)
    monkeypatch.setattr(MediaChain, "async_recognize_media", fake_recognize_media)
    app = FastAPI()
    app.include_router(netdisk_module.router, prefix="/api/v1/netdisk")
    app.dependency_overrides[netdisk_module.get_current_active_superuser_async] = lambda: None
    app.dependency_overrides[get_mediaserver_query_service] = lambda: library
    app.dependency_overrides[get_history_query_service] = lambda: history or _FakeHistory()
    return TestClient(app), state


def test_audit_endpoint_returns_missing_episodes(monkeypatch):
    """
    接口返回缺集，且不泄露分享令牌与提取码。
    """
    library = _FakeLibrary({1: [1, 2, 3, 4, 5, 6, 7, 8]})
    client, state = _build_audit_app(
        monkeypatch, files=_audit_files(), season_info=_season_info(), library=library
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={"url": "https://pan.quark.cn/s/abc123", "password": "9527"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    data = payload["data"]["audit"]
    assert data["share_title"] == "示例分享"
    assert data["platform"] == "quark"
    assert data["file_count"] == 12
    assert data["media"][0]["missing_episodes"] == [9, 10, 11, 12]
    assert data["media"][0]["library_state"] == LIBRARY_PRESENT
    assert data["media"][0]["media_id"] == "9001"
    assert "secret-entry-token" not in response.text
    assert "9527" not in response.text
    assert state.walk_calls[0]["max_depth"] == 12


def test_audit_endpoint_ignores_unaired_episodes(monkeypatch):
    """
    只播到第 8 集时接口不报 9-12 集缺失。
    """
    library = _FakeLibrary({1: [1, 2, 3, 4, 5, 6, 7, 8]})
    client, _state = _build_audit_app(
        monkeypatch, files=_audit_files(), season_info=_season_info(future_from=9), library=library
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    data = response.json()["data"]["audit"]
    assert data["media"][0]["missing_episodes"] == []


def test_audit_endpoint_empty_tree_returns_empty_report(monkeypatch):
    """
    分享内没有视频文件时返回空报告。
    """
    client, _state = _build_audit_app(
        monkeypatch, files=(), season_info=_season_info(), library=_FakeLibrary()
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    data = response.json()["data"]["audit"]
    assert data["file_count"] == 0
    assert data["media"] == []


def test_audit_endpoint_requires_superuser():
    """
    非超级管理员访问体检接口返回 403。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_active_user_async
    from app.api.dependencies.history import (
        get_history_query_service,
        get_mediaserver_query_service,
    )
    from app.api.endpoints import netdisk as netdisk_module

    app = FastAPI()
    app.include_router(netdisk_module.router, prefix="/api/v1/netdisk")
    app.dependency_overrides[get_current_active_user_async] = lambda: SimpleNamespace(
        id=1, name="alice", is_active=True, is_superuser=False
    )
    app.dependency_overrides[get_mediaserver_query_service] = lambda: _FakeLibrary()
    app.dependency_overrides[get_history_query_service] = lambda: _FakeHistory()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
        )

    assert response.status_code == 403


def test_audit_endpoint_returns_error_envelope(monkeypatch):
    """
    分享失效时沿用统一错误响应。
    """
    from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform

    client, state = _build_audit_app(
        monkeypatch, files=(), season_info=[], library=_FakeLibrary()
    )
    state.error = NetdiskError(
        NetdiskErrorCode.SHARE_INVALID, "分享链接无效或已失效", platform=NetdiskPlatform.QUARK
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={"url": "https://pan.quark.cn/s/abc123", "password": "9527"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "分享链接无效或已失效"
    assert payload["data"]["code"] == "share_invalid"


def test_audit_endpoint_skips_unstarted_season(monkeypatch):
    """
    整季未开播（只有未来的季首播日期）时不把该季集数算成缺失。
    """
    library = _FakeLibrary({1: [1]})
    client, _state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=[{"season_number": 1, "air_date": "2099-01-01"}],
        library=library,
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    data = response.json()["data"]["audit"]
    assert data["media"][0]["missing_episodes"] == []
    assert data["media"][0]["seasons"] == [{"season": 1, "episodes": list(range(1, 13))}]


class _FakeHistory:
    """
    模拟整理历史查询服务，返回预置的成功整理记录。
    """

    def __init__(self, records=None) -> None:
        """
        保存预置记录并记录查询入参。
        """
        self._records = list(records or [])
        self.queries: list[dict] = []

    async def list_transfer(self, **kwargs):
        """
        返回预置的整理历史分页结果。
        """
        self.queries.append(dict(kwargs))
        return SimpleNamespace(list=list(self._records))


def _history_record(episodes: str = "1-6", seasons: str = "1", title: str = "示例剧"):
    """
    构造一条成功整理历史 DTO。
    """
    return SimpleNamespace(
        title=title,
        media_source="themoviedb",
        media_id="9001",
        seasons=seasons,
        episodes=episodes,
        status=True,
    )


def test_audit_endpoint_uses_transfer_history_as_fallback(monkeypatch):
    """
    媒体服务器缓存为空时，整理历史里成功的集要算进「已有」，不能重复下载。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=_season_info(),
        library=_FakeLibrary(),
        history=_FakeHistory([_history_record(episodes="1-6")]),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    data = response.json()["data"]["audit"]
    media = data["media"][0]
    assert media["existing_episodes"] == [1, 2, 3, 4, 5, 6]
    assert media["missing_episodes"] == [7, 8, 9, 10, 11, 12]
    assert media["library_evidence"] == "media_server"


def test_audit_endpoint_merges_library_and_history(monkeypatch):
    """
    媒体服务器缓存与整理历史取并集：缓存给 1-4，历史给 5-8，缺 9-12。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=_season_info(),
        library=_FakeLibrary({1: [1, 2, 3, 4]}),
        history=_FakeHistory([_history_record(episodes="5-8")]),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["existing_episodes"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert media["missing_episodes"] == [9, 10, 11, 12]


def test_audit_endpoint_ignores_history_of_other_season(monkeypatch):
    """
    其它季的整理历史不能算进本季已有集。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=_season_info(),
        library=_FakeLibrary(),
        history=_FakeHistory([_history_record(episodes="1-12", seasons="2")]),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["missing_episodes"] == []
    assert media["library_evidence"] == "none"


def test_audit_endpoint_reports_one_entry_per_group(monkeypatch):
    """
    分享里两部不同剧名时接口返回两个媒体项，不合并成一部。
    """
    files = [
        ShareFile(file_id="a1", name="剧名甲.S01E01.mkv", size=10, path="剧名甲/剧名甲.S01E01.mkv"),
        ShareFile(file_id="a2", name="剧名甲.S01E02.mkv", size=10, path="剧名甲/剧名甲.S01E02.mkv"),
        ShareFile(file_id="b1", name="剧名乙.S01E01.mkv", size=10, path="剧名乙/剧名乙.S01E01.mkv"),
    ]
    client, _state = _build_audit_app(
        monkeypatch,
        files=files,
        season_info=_season_info(),
        library=_FakeLibrary({1: [1]}),
        history=_FakeHistory(),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    data = response.json()["data"]["audit"]
    assert data["file_count"] == 3
    assert len(data["media"]) == 2
    assert [item["files"] for item in data["media"]] == [2, 1]


def test_audit_endpoint_marks_movie_present_via_history(monkeypatch):
    """
    电影只看整部在不在：媒体服务器缓存没有、整理历史有成功记录时算已入库。
    """
    from app.schemas.types import MediaType

    files = [
        ShareFile(
            file_id="m1",
            name="某电影.2026.1080p.mkv",
            size=2048,
            path="某电影/某电影.2026.1080p.mkv",
        )
    ]
    client, _state = _build_audit_app(
        monkeypatch,
        files=files,
        season_info=[],
        library=_FakeLibrary(has_item=False),
        history=_FakeHistory(
            [SimpleNamespace(
                title="示例剧", media_source="themoviedb", media_id="9001",
                seasons=None, episodes=None, status=True,
            )]
        ),
        media_type=MediaType.MOVIE,
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["type"] == "电影"
    assert media["library_state"] == "present"
    assert media["episodes"] == []
    assert media["missing_episodes"] == []


def test_audit_endpoint_uses_last_episode_to_air(monkeypatch):
    """
    连载剧按 TMDB 的 last_episode_to_air 判断已播到第 8 集，不把 9-12 算成缺失。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=[{"season_number": 1, "episode_count": 12}],
        library=_FakeLibrary({1: [1, 2, 3, 4, 5]}),
        tmdb_info={"last_episode_to_air": {"season_number": 1, "episode_number": 8}},
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["missing_episodes"] == [6, 7, 8]
    assert media["existing_episodes"] == [1, 2, 3, 4, 5]


def test_audit_endpoint_treats_earlier_season_as_finished(monkeypatch):
    """
    最近播出的集在第 2 季时，第 1 季视为整季播完，第 3 季视为未开播。
    """
    files = tuple(
        ShareFile(
            file_id=f"s{season}e{number}",
            name=f"示例剧.S{season:02d}E{number:02d}.mkv",
            size=10,
            path=f"示例剧/Season {season}/示例剧.S{season:02d}E{number:02d}.mkv",
        )
        for season in (1, 3)
        for number in range(1, 4)
    )
    client, _state = _build_audit_app(
        monkeypatch,
        files=files,
        season_info=[
            {"season_number": 1, "episode_count": 3},
            {"season_number": 2, "episode_count": 10},
            {"season_number": 3, "episode_count": 3},
        ],
        library=_FakeLibrary({1: [1], 3: [1]}),
        tmdb_info={"last_episode_to_air": {"season_number": 2, "episode_number": 5}},
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    states = {(item["season"], item["episode"]): item["state"] for item in media["episodes"]}
    assert states[(1, 2)] == "missing"
    assert states[(1, 3)] == "missing"
    assert media["missing_episodes"] == [2, 3]


def test_audit_endpoint_counts_unrecognized_files(monkeypatch):
    """
    识别失败时分组保留但标未识别，文件计入 unrecognized_files，不猜缺集。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=_season_info(),
        library=_FakeLibrary({1: [1, 2, 3]}),
        recognize_none=True,
    )

    response = client.post(
        "/api/v1/netdisk/share/audit", json={"url": "https://pan.quark.cn/s/abc123"}
    )

    assert response.status_code == 200
    data = response.json()["data"]["audit"]
    assert data["unrecognized_files"] == 12
    media = data["media"][0]
    assert media["recognized"] is False
    assert media["missing_episodes"] == []
    assert media["library_state"] == "unknown"
    assert media["episodes"] == []

def _two_season_group() -> ShareFileGroup:
    """
    构造含两季、每季 4 集的分组，路径带显式季目录。
    """
    files = tuple(
        _share_file(
            f"S{season:02d}E{number:02d}.mkv",
            f"示例剧/Season {season}/S{season:02d}E{number:02d}.mkv",
        )
        for season in (1, 2)
        for number in range(1, 5)
    )
    return ShareFileGroup(
        key="示例剧|2026",
        title="示例剧",
        year="2026",
        sample_path=files[0].path,
        files=files,
        total_bytes=sum(item.size for item in files),
        seasons=((1, (1, 2, 3, 4)), (2, (1, 2, 3, 4))),
        mtype="电视剧",
    )


def _two_season_media() -> RecognizedMedia:
    """
    构造已播出两季、每季 4 集的媒体身份。
    """
    return RecognizedMedia(
        media_source="TheMovieDb",
        media_id="9001",
        title="示例剧",
        year="2026",
        mtype="电视剧",
        aired_seasons=((1, (1, 2, 3, 4)), (2, (1, 2, 3, 4))),
    )


def _two_season_info() -> list[dict]:
    """
    构造两季、每季 4 集且都已播出的 TMDB 季详情。
    """
    return [
        {
            "season_number": season,
            "episodes": [
                {"episode_number": number, "air_date": "2026-01-01"}
                for number in range(1, 5)
            ],
        }
        for season in (1, 2)
    ]


def _identity_info(
    media_id: str = "4242",
    title: str = "示例剧",
    season_info: list[dict] | None = None,
) -> SimpleNamespace:
    """
    构造按指定身份识别返回的媒体信息。
    """
    from app.schemas.types import MediaType

    return SimpleNamespace(
        media_source=MediaSource.TMDB,
        media_id=media_id,
        title=title,
        year="2026",
        type=MediaType.TV,
        season_info=_two_season_info() if season_info is None else season_info,
        seasons={1: list(range(1, 13))},
        tmdb_info={},
    )


def _mixed_title_files() -> list[ShareFile]:
    """
    构造「12 集示例剧 + 2 集另一部剧」的分享文件树。
    """
    return list(_audit_files()) + [
        _share_file("E01.mkv", "另一部剧/E01.mkv", size=100),
        _share_file("E02.mkv", "另一部剧/E02.mkv", size=100),
    ]


def test_audit_group_reports_gap_per_season():
    """
    两季分享必须按季给出缺口，跨季合并的列表不能互相顶替。
    """
    result = asyncio.run(
        audit_group(
            _two_season_group(),
            _two_season_media(),
            probe=_Probe({1: [1, 2], 2: [1, 2]}),
        )
    )

    assert result.season_missing == ((1, (3, 4)), (2, (3, 4)))
    assert result.season_existing == ((1, (1, 2)), (2, (1, 2)))
    assert result.missing_episodes == (3, 4)


def test_audit_group_filters_requested_season():
    """
    指定只比对第二季时，逐集状态与缺口都只留第二季。
    """
    result = asyncio.run(
        audit_group(
            _two_season_group(),
            _two_season_media(),
            probe=_Probe({1: [1, 2], 2: [1, 2]}),
            season=2,
        )
    )

    assert result.season_missing == ((2, (3, 4)),)
    assert {season for season, _episode, _state in result.episode_states} == {2}
    assert result.missing_episodes == (3, 4)


def test_audit_endpoint_uses_requested_identity(monkeypatch):
    """
    请求带 media_source+media_id 时按该身份体检，不再走文件名识别。
    """
    client, state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=_season_info(),
        library=_FakeLibrary({1: [1, 2, 3, 4, 5, 6, 7, 8]}),
        identity=_identity_info(season_info=_season_info()),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={
            "url": "https://pan.quark.cn/s/abc123",
            "media_source": "themoviedb",
            "media_id": "4242",
        },
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["media_id"] == "4242"
    assert media["missing_episodes"] == [9, 10, 11, 12]
    assert state.identity_calls == [
        {"mtype": None, "media_source": MediaSource.TMDB, "media_id": "4242"}
    ]
    assert state.recognize_calls == []


def test_audit_endpoint_falls_back_when_identity_unrecognized(monkeypatch):
    """
    指定身份识别不出媒体时退回按路径识别，不把整份报告判成未识别。
    """
    client, state = _build_audit_app(
        monkeypatch,
        files=_audit_files(),
        season_info=_season_info(),
        library=_FakeLibrary({1: [1, 2, 3]}),
        identity=None,
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={
            "url": "https://pan.quark.cn/s/abc123",
            "media_source": "themoviedb",
            "media_id": "4242",
        },
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["media_id"] == "9001"
    assert state.identity_calls == [
        {"mtype": None, "media_source": MediaSource.TMDB, "media_id": "4242"}
    ]
    assert state.recognize_calls == ["示例剧/示例剧.S01E01.mkv"]


def test_audit_endpoint_identity_mismatch_uses_largest_group(monkeypatch):
    """
    身份标题对不上任何分组时只套体量最大的分组，其它分组照旧按路径识别。
    """
    client, state = _build_audit_app(
        monkeypatch,
        files=_mixed_title_files(),
        season_info=_season_info(),
        library=_FakeLibrary({1: [1, 2]}),
        identity=_identity_info(title="无关剧名", season_info=_season_info()),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={
            "url": "https://pan.quark.cn/s/abc123",
            "media_source": "themoviedb",
            "media_id": "4242",
        },
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"]
    assert media[0]["media_id"] == "4242"
    assert media[1]["media_id"] == "9001"
    assert len(state.recognize_calls) == 1


def test_audit_endpoint_reports_season_gaps(monkeypatch):
    """
    两季分享按季返回缺口，前端只取当前季即可。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=list(_two_season_group().files),
        season_info=_two_season_info(),
        library=_FakeLibrary({1: [1, 2], 2: [1, 2]}),
        identity=_identity_info(season_info=_two_season_info()),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={
            "url": "https://pan.quark.cn/s/abc123",
            "media_source": "themoviedb",
            "media_id": "4242",
        },
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["season_gaps"] == [
        {"season": 1, "missing_episodes": [3, 4], "existing_episodes": [1, 2]},
        {"season": 2, "missing_episodes": [3, 4], "existing_episodes": [1, 2]},
    ]
    assert media["missing_episodes"] == [3, 4]


def test_audit_endpoint_season_filter_limits_gaps(monkeypatch):
    """
    请求带 season 时只返回该季的缺口。
    """
    client, _state = _build_audit_app(
        monkeypatch,
        files=list(_two_season_group().files),
        season_info=_two_season_info(),
        library=_FakeLibrary({1: [1, 2], 2: [1, 2]}),
        identity=_identity_info(season_info=_two_season_info()),
    )

    response = client.post(
        "/api/v1/netdisk/share/audit",
        json={
            "url": "https://pan.quark.cn/s/abc123",
            "media_source": "themoviedb",
            "media_id": "4242",
            "season": 2,
        },
    )

    assert response.status_code == 200
    media = response.json()["data"]["audit"]["media"][0]
    assert media["season_gaps"] == [
        {"season": 2, "missing_episodes": [3, 4], "existing_episodes": [1, 2]}
    ]
