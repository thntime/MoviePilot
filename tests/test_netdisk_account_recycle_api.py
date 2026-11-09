"""
网盘回收站清理接口测试。

覆盖成功、能力不支持、账号不存在、安全密钥限制与限流降级；
全部依赖内存替身，不访问真实网络。
"""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import netdisk as netdisk_module
from app.application.netdisk import registry
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import (
    DriveCapabilities,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
)


class _FakeStore:
    """
    内存账号仓储替身。
    """

    def __init__(self, records: list[NetdiskAccountRecord]) -> None:
        """
        保存按主键索引的账号记录。
        """
        self.records = {record.id: record for record in records}

    def get_record(self, account_id: int):
        """
        按主键返回完整账号记录。
        """
        return self.records.get(account_id)

    def list_accounts(self, platform=None):
        """
        返回账号列表，可按平台过滤。
        """
        values = list(self.records.values())
        if platform is None:
            return values
        return [item for item in values if item.platform is platform]


class _RecycleAdapter:
    """
    回收站能力适配器替身。
    """

    def __init__(
        self,
        *,
        recycle_bin: bool = True,
        result: dict | None = None,
        error: NetdiskError | None = None,
    ) -> None:
        """
        配置能力声明、返回结果或待抛异常。
        """
        self.drive_capabilities = DriveCapabilities(recycle_bin=recycle_bin)
        self.result = result or {"cleared": True, "count": 1}
        self.error = error
        self.calls: list[int] = []

    def clear_recycle_bin(self, account: NetdiskAccountRecord) -> dict:
        """
        记录调用并返回预设结果。
        """
        self.calls.append(account.id)
        if self.error is not None:
            raise self.error
        return self.result


def _record(account_id: int = 1, platform: NetdiskPlatform = NetdiskPlatform.PAN115):
    """
    构造账号记录。
    """
    return NetdiskAccountRecord(
        id=account_id,
        platform=platform,
        name=f"账号{account_id}",
        credential={"cookie": "UID=fake; SEID=fake"},
    )


@contextmanager
def _client(monkeypatch, adapter, records):
    """
    装配独立路由、内存仓储与适配器替身。
    """
    store = _FakeStore(records)
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapter)
    registry.configure_netdisk_services(
        account_store=store,
        settings=SimpleNamespace(),
        cookie_import=SimpleNamespace(),
        login_adapter=SimpleNamespace(),
    )
    app = FastAPI()
    app.include_router(netdisk_module.router, prefix="/api/v1/netdisk")
    app.dependency_overrides[netdisk_module.get_current_active_superuser_async] = lambda: None
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, store=store)
    finally:
        registry.reset_netdisk_services()


def test_clear_recycle_bin_success(monkeypatch) -> None:
    """
    115 账号清空成功时返回 cleared 与提示。
    """
    adapter = _RecycleAdapter(result={"cleared": True, "count": 3})

    with _client(monkeypatch, adapter, [_record()]) as api:
        response = api.client.post("/api/v1/netdisk/accounts/1/recycle-bin/clear")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["cleared"] is True
    assert body["data"]["empty"] is False
    assert datetime.fromisoformat(body["data"]["cleared_at"])
    assert body["message"] == "回收站已清空"
    assert adapter.calls == [1]


def test_clear_recycle_bin_reports_empty_when_already_empty(monkeypatch) -> None:
    """
    回收站本就为空时返回 empty=true、cleared=false，便于前端区分成功与空结果。
    """
    adapter = _RecycleAdapter(result={"cleared": False, "count": 0})

    with _client(monkeypatch, adapter, [_record()]) as api:
        response = api.client.post("/api/v1/netdisk/accounts/1/recycle-bin/clear")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["cleared"] is False
    assert body["data"]["empty"] is True
    assert body["message"] == "回收站已为空"
    assert adapter.calls == [1]


def test_clear_recycle_bin_rejects_platform_without_capability(monkeypatch) -> None:
    """
    未声明回收站能力的平台返回 unsupported，且不调用适配器。
    """
    adapter = _RecycleAdapter(recycle_bin=False)

    with _client(monkeypatch, adapter, [_record(platform=NetdiskPlatform.QUARK)]) as api:
        response = api.client.post("/api/v1/netdisk/accounts/1/recycle-bin/clear")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["data"]["code"] == "unsupported"
    assert adapter.calls == []


def test_clear_recycle_bin_reports_missing_account(monkeypatch) -> None:
    """
    账号不存在时返回失败提示，不调用适配器。
    """
    adapter = _RecycleAdapter()

    with _client(monkeypatch, adapter, []) as api:
        response = api.client.post("/api/v1/netdisk/accounts/99/recycle-bin/clear")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "账号不存在"
    assert adapter.calls == []


def test_clear_recycle_bin_surfaces_safe_password_hint(monkeypatch) -> None:
    """
    安全密钥开启时返回未支持提示，不猜密码。
    """
    adapter = _RecycleAdapter(
        error=NetdiskError(
            NetdiskErrorCode.UNSUPPORTED,
            "账号开启回收站安全密钥，暂不支持自动清空",
            platform=NetdiskPlatform.PAN115,
        )
    )

    with _client(monkeypatch, adapter, [_record()]) as api:
        response = api.client.post("/api/v1/netdisk/accounts/1/recycle-bin/clear")

    body = response.json()
    assert body["success"] is False
    assert body["data"]["code"] == "unsupported"
    assert "安全密钥" in body["message"]


def test_clear_recycle_bin_swallows_rate_limit(monkeypatch) -> None:
    """
    平台限流时返回未清空而不是失败，避免定时任务反复告警。
    """
    adapter = _RecycleAdapter(
        error=NetdiskError(
            NetdiskErrorCode.RATE_LIMITED,
            "115 操作太频繁，请稍后重试",
            platform=NetdiskPlatform.PAN115,
        )
    )

    with _client(monkeypatch, adapter, [_record()]) as api:
        response = api.client.post("/api/v1/netdisk/accounts/1/recycle-bin/clear")

    body = response.json()
    assert body["success"] is True
    assert body["data"]["cleared"] is False
    assert body["data"]["empty"] is False
    assert "太频繁" in body["message"]
