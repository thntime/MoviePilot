"""
网盘分享黑名单查询与清除接口的测试。
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import netdisk as netdisk_module
from app.application.netdisk import share_blacklist
from app.application.netdisk.share_blacklist import ShareBlacklistStore


def _build_app() -> FastAPI:
    """
    装配只包含网盘路由的独立应用。
    """
    app = FastAPI()
    app.include_router(netdisk_module.router, prefix="/api/v1/netdisk")
    return app


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    用临时文件驱动黑名单接口，并绕过管理员鉴权。
    """
    store = ShareBlacklistStore(tmp_path / "blacklist.json")
    monkeypatch.setattr(share_blacklist, "_store", store)
    app = _build_app()
    app.dependency_overrides[netdisk_module.get_current_active_superuser_async] = lambda: None
    with TestClient(app) as client:
        yield store, client


def test_list_share_blacklist(api) -> None:
    """
    查询接口返回当前有效条目的完整字段。
    """
    store, client = api
    store.add("uc", "abc", "UC 直链不完整")

    response = client.get("/api/v1/netdisk/share-blacklist")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    entries = payload["data"]["share_blacklist"]
    assert len(entries) == 1
    assert entries[0]["platform"] == "uc"
    assert entries[0]["share_code"] == "abc"
    assert entries[0]["reason"] == "UC 直链不完整"
    assert entries[0]["expires_at"]


def test_delete_share_blacklist(api) -> None:
    """
    清除接口移除指定条目，重复删除返回失败说明。
    """
    store, client = api
    store.add("uc", "abc", "UC 直链不完整")

    response = client.delete("/api/v1/netdisk/share-blacklist/uc/abc")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["deleted"] is True
    assert store.is_blacklisted("uc", "abc") is None

    again = client.delete("/api/v1/netdisk/share-blacklist/uc/abc")
    assert again.status_code == 200
    assert again.json()["success"] is False
    assert again.json()["data"]["deleted"] is False
