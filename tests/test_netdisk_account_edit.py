"""
网盘账号编辑与重新授权的接口测试。

覆盖按平台解析凭据文本、状态恢复、明文详情与脱敏列表，以及
CookieCloud/账号密码重新授权不新增账号。
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.external.netdisk.base import BaseNetdiskAdapter
from app.api.endpoints import netdisk as netdisk_module
from app.application.netdisk import registry, selection
from app.application.netdisk.accounts import mask_credential
from app.application.netdisk.contract import (
    LoginSession,
    NetdiskAccountRecord,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskTuning,
)
from app.application.netdisk.credentials import FernetCredentialCipher
from app.db.adapters.netdiskaccount import TransactionalNetdiskAccountRepository
from app.db.base import Base
from app.db.models.netdiskaccount import NetdiskAccount
from app.domain.netdisk import (
    AccountProbe,
    LinkPolicy,
    NetdiskPlatform,
)


class _Pan123Adapter(BaseNetdiskAdapter):
    """
    123 云盘适配器替身：令牌非空即视为有效。
    """

    platform = NetdiskPlatform.PAN123
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    max_concurrency = 512
    cookie_required_keys = ("token",)
    cookie_domains = (".123pan.com",)
    login_url = "https://www.123pan.com/"
    supports_password_login = True

    def password_login(self, username, password):
        """
        账号密码换令牌。
        """
        del password
        return {"token": f"jwt-{username}"}


class _CookieImport:
    """
    CookieCloud 导入替身。
    """

    def import_cookies(self, domains):
        """
        返回固定令牌。
        """
        del domains
        return {"token": "jwt-cookiecloud"}


class _LoginAdapter:
    """
    登录端口替身；本用例不启动扫码会话。
    """

    def start(self, platform, login_url, account_id=None):
        """
        返回占位会话。
        """
        del login_url, account_id
        return LoginSession(session_id="unused", platform=platform)

    def poll(self, session_id):
        """
        未使用。
        """
        raise AssertionError(session_id)

    def cancel(self, session_id):
        """
        未使用。
        """
        del session_id


class _Settings:
    """
    固定参数快照。
    """

    def load(self):
        """
        返回默认参数。
        """
        return NetdiskTuning()


class _FakeStore:
    """
    内存账号仓储替身。
    """

    def __init__(self) -> None:
        self.views: list[NetdiskAccountView] = []
        self.records: dict[int, NetdiskAccountRecord] = {}
        self._next = 1

    def seed(self, credential: dict[str, str], status: str = "active") -> NetdiskAccountView:
        """
        预置一个账号。
        """
        view = self.create_account(
            NetdiskAccountWrite(
                platform=NetdiskPlatform.PAN123,
                name="测试账号",
                credential=credential,
            )
        )
        self.records[view.id] = replace(self.records[view.id], status=status)
        self.views = [
            replace(item, status=status) if item.id == view.id else item for item in self.views
        ]
        return view

    def list_accounts(self, platform=None):
        """
        返回账号脱敏投影。
        """
        return [
            view for view in self.views if platform is None or view.platform == platform.value
        ]

    def get_record(self, account_id):
        """
        按主键返回完整账号记录。
        """
        return self.records.get(account_id)

    def create_account(self, payload: NetdiskAccountWrite):
        """
        创建账号并返回脱敏投影。
        """
        account_id = self._next
        self._next += 1
        view = NetdiskAccountView(
            id=account_id,
            platform=payload.platform.value,
            name=payload.name,
            is_primary=payload.is_primary,
            status="active",
            masked=mask_credential(payload.credential),
        )
        self.views.append(view)
        self.records[account_id] = NetdiskAccountRecord(
            id=account_id,
            platform=payload.platform,
            name=payload.name,
            credential=dict(payload.credential),
            is_primary=payload.is_primary,
        )
        return view

    def update_account(self, account_id, **kwargs):
        """
        更新账号字段并返回脱敏投影。
        """
        record = self.records.get(account_id)
        if record is None:
            return None
        credential = kwargs.get("credential") or record.credential
        updated = replace(
            record,
            name=kwargs.get("name") or record.name,
            credential=dict(credential),
            is_primary=bool(kwargs.get("is_primary", record.is_primary)),
            status=kwargs.get("status") or record.status,
        )
        self.records[account_id] = updated
        view = NetdiskAccountView(
            id=updated.id,
            platform=updated.platform.value,
            name=updated.name,
            is_primary=updated.is_primary,
            status=updated.status,
            masked=mask_credential(updated.credential),
        )
        self.views = [view if item.id == account_id else item for item in self.views]
        return view

    def delete_account(self, account_id):
        """
        删除账号。
        """
        self.records.pop(account_id, None)
        self.views = [view for view in self.views if view.id != account_id]
        return True

    def touch_account(self, account_id, probe):
        """
        记录探测结果。
        """
        del account_id, probe

    def update_quota(self, account_id, used, total):
        """
        空间刷新未在本用例使用。
        """
        del account_id, used, total


@pytest.fixture()
def api(monkeypatch):
    """
    装配独立路由与内存账号仓储。
    """
    store = _FakeStore()
    monkeypatch.setattr(registry, "get_adapter", lambda platform: _Pan123Adapter())
    registry.configure_netdisk_services(
        account_store=store,
        settings=_Settings(),
        cookie_import=_CookieImport(),
        login_adapter=_LoginAdapter(),
    )
    app = FastAPI()
    app.include_router(netdisk_module.router, prefix="/api/v1/netdisk")
    app.dependency_overrides[netdisk_module.get_current_active_superuser_async] = lambda: None
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store)
    registry.reset_netdisk_services()


def test_update_parses_bare_jwt_as_token(api):
    """
    编辑 123 账号时裸 JWT 被识别为 token 凭据，而不是报缺字段。
    """
    view = api.store.seed({"token": "old-token"})
    bare_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"

    response = api.client.put(f"/api/v1/netdisk/accounts/{view.id}", json={"cookie": bare_jwt})

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert api.store.records[view.id].credential == {"token": bare_jwt}


def test_update_credential_restores_active_status(api):
    """
    修改凭据成功后账号状态恢复为 active。
    """
    view = api.store.seed({"token": "expired-token"}, status="expired")
    bare_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIyIn0.signature"

    response = api.client.put(f"/api/v1/netdisk/accounts/{view.id}", json={"cookie": bare_jwt})

    assert response.status_code == 200
    assert api.store.records[view.id].status == "active"


def test_update_name_is_visible_in_list_and_detail(api):
    """
    编辑保存后名称立即在列表与详情生效。
    """
    view = api.store.seed({"token": "secret-token"})

    response = api.client.put(f"/api/v1/netdisk/accounts/{view.id}", json={"name": "新名字"})

    assert response.status_code == 200
    listing = api.client.get("/api/v1/netdisk/accounts")
    detail = api.client.get(f"/api/v1/netdisk/accounts/{view.id}")
    assert listing.json()["data"]["accounts"][0]["name"] == "新名字"
    assert detail.json()["data"]["account"]["name"] == "新名字"


def test_detail_returns_plaintext_while_list_masks(api):
    """
    详情接口返回明文凭据，列表接口继续脱敏。
    """
    view = api.store.seed({"token": "secret-token"})

    detail = api.client.get(f"/api/v1/netdisk/accounts/{view.id}")
    listing = api.client.get("/api/v1/netdisk/accounts")

    assert detail.status_code == 200
    assert detail.json()["data"]["account"]["credential"] == {"token": "secret-token"}
    account = listing.json()["data"]["accounts"][0]
    assert account["masked"] == {"token": "***"}
    assert "credential" not in account


def test_listing_and_detail_include_average_speed(api):
    """
    列表与详情响应都返回账号平均下载速度。
    """
    view = api.store.seed({"token": "secret-token"})
    selection.clear_selection_state()
    selection.record_account_speed(view.id, 10 * 1024 * 1024, 1)
    try:
        listing = api.client.get("/api/v1/netdisk/accounts")
        detail = api.client.get(f"/api/v1/netdisk/accounts/{view.id}")

        assert listing.json()["data"]["accounts"][0]["speed"] == pytest.approx(10 * 1024 * 1024)
        assert detail.json()["data"]["account"]["speed"] == pytest.approx(10 * 1024 * 1024)
    finally:
        selection.clear_selection_state()


def test_cookiecloud_with_account_id_updates_existing(api):
    """
    带 account_id 的 CookieCloud 导入更新已有账号且不新增记录。
    """
    view = api.store.seed({"token": "old-token"})

    response = api.client.post(
        "/api/v1/netdisk/accounts/cookiecloud",
        json={"account_id": view.id},
    )

    assert response.status_code == 200
    assert len(api.store.records) == 1
    assert api.store.records[view.id].credential == {"token": "jwt-cookiecloud"}
    assert api.store.records[view.id].status == "active"


def test_password_with_account_id_updates_existing(api):
    """
    带 account_id 的账号密码登录更新已有账号且不新增记录。
    """
    view = api.store.seed({"token": "old-token"})

    response = api.client.post(
        "/api/v1/netdisk/accounts/password",
        json={"account_id": view.id, "username": "alice", "password": "secret"},
    )

    assert response.status_code == 200
    assert len(api.store.records) == 1
    assert api.store.records[view.id].credential == {"token": "jwt-alice"}
    assert api.store.records[view.id].status == "active"


def test_unknown_probe_does_not_downgrade_active():
    """
    仓储层把 unknown 探测写回时保持已有 active 状态。
    """
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[NetdiskAccount.__table__])
    repository = TransactionalNetdiskAccountRepository(
        sync_session=sessionmaker(bind=engine),
        cipher=FernetCredentialCipher("unit-test-secret"),
    )
    view = repository.create_account(
        NetdiskAccountWrite(
            platform=NetdiskPlatform.QUARK,
            name="主账号",
            credential={"cookie": "__pus=1; __puus=2"},
        )
    )
    try:
        repository.touch_account(view.id, AccountProbe(status="unknown", message="网络抖动"))
        record = repository.get_record(view.id)
        assert record is not None and record.status == "active"

        repository.touch_account(view.id, AccountProbe(status="expired", message="已失效"))
        record = repository.get_record(view.id)
        assert record is not None and record.status == "expired"
    finally:
        engine.dispose()
