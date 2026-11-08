"""
网盘分享 AI 预选预览接口的测试。

覆盖成功预选、空文件树不调用 AI、AI 降级为空预选、管理员鉴权与分类异常响应。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_active_user_async
from app.api.endpoints import netdisk as netdisk_module
from app.application.netdisk.ai_selection import ShareSelectionResult
from app.application.netdisk.resolve import NetdiskResolveService, ResolvedShare
from app.domain.netdisk import (
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
)


def _files() -> tuple[ShareFile, ...]:
    """
    构造两集分享文件；token 字段用于断言不会出现在响应里。
    """
    return (
        ShareFile(
            file_id="f1",
            name="小城良方.S01E01.mp4",
            size=100,
            path="小城良方/小城良方.S01E01.mp4",
            token="secret-entry-token",
        ),
        ShareFile(
            file_id="f2",
            name="小城良方.S01E02.mp4",
            size=200,
            path="小城良方/小城良方.S01E02.mp4",
        ),
    )


def _resolved(files: tuple[ShareFile, ...]) -> ResolvedShare:
    """
    构造分享解析结果。
    """
    return ResolvedShare(
        ref=ShareRef(
            platform=NetdiskPlatform.QUARK,
            share_id="abc123",
            url="https://pan.quark.cn/s/abc123",
        ),
        detail=ShareDetail(
            title="小城良方",
            size=sum(item.size for item in files),
            files=files,
        ),
        files=files,
    )


def _build_app() -> FastAPI:
    """
    装配只包含网盘路由的独立应用。
    """
    app = FastAPI()
    app.include_router(netdisk_module.router, prefix="/api/v1/netdisk")
    return app


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch):
    """
    装配独立路由，并用桩替换分享解析与 AI 预选。
    """
    state = SimpleNamespace(
        walk_calls=[],
        select_calls=[],
        files=_files(),
        result=ShareSelectionResult(("f1",), "1"),
        error=None,
    )

    def fake_walk(self, url, password=None, platform=None, account_id=None, **kwargs):
        """
        记录解析入参并返回预置文件树。
        """
        del self, kwargs
        state.walk_calls.append(
            {"url": url, "password": password, "platform": platform, "account_id": account_id}
        )
        if state.error is not None:
            raise state.error
        return _resolved(state.files)

    async def fake_select(files, *, season=None, episodes=None, user_intent="", max_files=200):
        """
        记录预选入参并返回预置结果。
        """
        del max_files
        state.select_calls.append(
            {
                "files": list(files),
                "season": season,
                "episodes": episodes,
                "user_intent": user_intent,
            }
        )
        return state.result

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)
    monkeypatch.setattr(netdisk_module, "select_share_files", fake_select)
    app = _build_app()
    app.dependency_overrides[netdisk_module.get_current_active_superuser_async] = lambda: None
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state)


def test_preview_returns_files_and_ai_selection(api):
    """
    解析成功时返回安全文件字段、选中标识与建议集数。
    """
    response = api.client.post(
        "/api/v1/netdisk/share/preview",
        json={
            "url": "https://pan.quark.cn/s/abc123",
            "password": "9527",
            "season": 1,
            "episodes": "E01-E02",
            "user_intent": "只要前两集",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    data = payload["data"]
    assert [item["file_id"] for item in data["files"]] == ["f1", "f2"]
    assert data["files"][0]["path"] == "小城良方/小城良方.S01E01.mp4"
    assert data["selected_file_ids"] == ["f1"]
    assert data["suggested_episode_selection"] == "1"
    assert "secret-entry-token" not in response.text
    assert "9527" not in response.text
    assert api.state.walk_calls == [
        {
            "url": "https://pan.quark.cn/s/abc123",
            "password": "9527",
            "platform": None,
            "account_id": None,
        }
    ]
    assert api.state.select_calls[0]["season"] == 1
    assert api.state.select_calls[0]["episodes"] == "E01-E02"
    assert api.state.select_calls[0]["user_intent"] == "只要前两集"


def test_preview_empty_tree_skips_ai(api):
    """
    文件树为空时直接返回空预选且不调用 AI。
    """
    api.state.files = ()

    response = api.client.post(
        "/api/v1/netdisk/share/preview",
        json={"url": "https://pan.quark.cn/s/abc123"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["files"] == []
    assert data["selected_file_ids"] == []
    assert data["suggested_episode_selection"] == ""
    assert api.state.select_calls == []


def test_preview_degrades_when_ai_unavailable(api):
    """
    AI 降级为空预选时仍返回成功与文件列表，供用户手动选择。
    """
    api.state.result = ShareSelectionResult()

    response = api.client.post(
        "/api/v1/netdisk/share/preview",
        json={"url": "https://pan.quark.cn/s/abc123"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    data = payload["data"]
    assert [item["file_id"] for item in data["files"]] == ["f1", "f2"]
    assert data["selected_file_ids"] == []
    assert data["suggested_episode_selection"] == ""


def test_preview_requires_superuser():
    """
    非超级管理员访问预览接口返回 403。
    """
    app = _build_app()
    app.dependency_overrides[get_current_active_user_async] = lambda: SimpleNamespace(
        id=1, name="alice", is_active=True, is_superuser=False
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/netdisk/share/preview",
            json={"url": "https://pan.quark.cn/s/abc123"},
        )

    assert response.status_code == 403


def test_preview_returns_error_envelope_on_netdisk_error(api):
    """
    解析失败沿用统一错误响应，不泄露凭据。
    """
    api.state.error = NetdiskError(
        NetdiskErrorCode.SHARE_INVALID,
        "分享链接无效或已失效",
        platform=NetdiskPlatform.QUARK,
    )

    response = api.client.post(
        "/api/v1/netdisk/share/preview",
        json={"url": "https://pan.quark.cn/s/abc123", "password": "9527"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "分享链接无效或已失效"
    assert payload["data"]["code"] == "share_invalid"
    assert payload["data"]["platform"] == "quark"
    assert api.state.select_calls == []
