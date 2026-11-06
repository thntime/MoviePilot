"""
网盘账号用例的单元测试：字段校验、主账号选择与脱敏。
"""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService, mask_credential, parse_cookie_header
from app.application.netdisk.contract import (
    NetdiskAccountRecord,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskTuning,
)
from app.adapters.external.netdisk.base import BaseNetdiskAdapter
from app.domain.netdisk import (
    AccountProbe,
    DriveCapabilities,
    DriveQuota,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
)


class _FakeAdapter(BaseNetdiskAdapter):
    """
    只用于账号用例测试的夸克适配器替身。
    """

    platform = NetdiskPlatform.QUARK
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    max_concurrency = 512
    cookie_required_keys = ("__pus", "__puus")
    cookie_domains = (".quark.cn",)
    login_url = "https://pan.quark.cn/"

    def validate_credential(self, credential):
        """
        按字段完整性返回探测结果。
        """
        return super().validate_credential(credential)

    def probe_account(self, account):
        """
        固定返回有效。
        """
        return AccountProbe(status="active")


class _NicknameAdapter(_FakeAdapter):
    """
    固定返回平台昵称的适配器替身，用于自动命名测试。
    """

    def login_account_name(self, credential):
        """
        忽略凭据，返回固定昵称。
        """
        del credential
        return "夸克用户"


class _FakeStore:
    """
    内存账号仓储替身。
    """

    def __init__(self) -> None:
        self.views: list[NetdiskAccountView] = []
        self.records: dict[int, NetdiskAccountRecord] = {}
        self._next = 1

    def list_accounts(self, platform=None):
        return [view for view in self.views if platform is None or view.platform == platform.value]

    def get_record(self, account_id):
        return self.records.get(account_id)

    def create_account(self, payload: NetdiskAccountWrite):
        if any(
            view.platform == payload.platform.value and view.name == payload.name
            for view in self.views
        ):
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "同平台下账号名称已存在",
                platform=payload.platform,
                account_name=payload.name,
            )
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

    def update_quota(self, account_id, used, total):
        """
        更新账号空间占用。
        """
        self.views = [
            replace(item, quota_used=used, quota_total=total) if item.id == account_id else item
            for item in self.views
        ]

    def update_account(self, account_id, **kwargs):
        record = self.records.get(account_id)
        if record is None:
            return None
        credential = kwargs.get("credential") or record.credential
        self.records[account_id] = NetdiskAccountRecord(
            id=record.id,
            platform=record.platform,
            name=kwargs.get("name") or record.name,
            credential=dict(credential),
            is_primary=bool(kwargs.get("is_primary", record.is_primary)),
            status=kwargs.get("status") or record.status,
            expires_at=record.expires_at,
        )
        record = self.records[account_id]
        view = NetdiskAccountView(
            id=record.id,
            platform=record.platform.value,
            name=record.name,
            is_primary=record.is_primary,
            status=record.status,
            expires_at=record.expires_at,
            masked=mask_credential(record.credential),
        )
        self.views = [view if item.id == account_id else item for item in self.views]
        return view

    def delete_account(self, account_id):
        self.records.pop(account_id, None)
        self.views = [view for view in self.views if view.id != account_id]
        return True

    def touch_account(self, account_id, probe):
        self.records[account_id] = NetdiskAccountRecord(
            id=account_id,
            platform=self.records[account_id].platform,
            name=self.records[account_id].name,
            credential=self.records[account_id].credential,
            status=probe.status,
        )


class _FakeSettings:
    """
    固定参数快照。
    """

    def load(self):
        return NetdiskTuning()


class _StatusAdapter(BaseNetdiskAdapter):
    """
    按账号名称返回不同探测结果的适配器替身。
    """

    platform = NetdiskPlatform.QUARK
    cookie_required_keys = ("__pus", "__puus")

    def probe_account(self, account):
        """
        按名称模拟有效、失效、未知与探测异常。
        """
        if account.name == "失效":
            return AccountProbe(status="expired", message="登录态已失效")
        if account.name == "未知":
            return AccountProbe(status="unknown")
        if account.name == "异常":
            raise RuntimeError("探测接口不可用")
        return AccountProbe(status="active")


@pytest.fixture()
def status_service():
    """
    装配按名称返回状态的适配器与账号用例，测试结束后清理。
    """
    store = _FakeStore()
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: _StatusAdapter()})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_FakeSettings(),
        cookie_import=None,
        login_adapter=None,
    )
    yield NetdiskAccountService(store)
    registry.reset_netdisk_services()


@pytest.fixture()
def service():
    """
    装配注册表与账号用例，测试结束后清理。
    """
    store = _FakeStore()
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: _FakeAdapter()})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_FakeSettings(),
        cookie_import=None,
        login_adapter=None,
    )
    yield NetdiskAccountService(store)
    registry.reset_netdisk_services()


@pytest.fixture()
def nickname_service():
    """
    装配返回平台昵称的适配器与账号用例，测试结束后清理。
    """
    store = _FakeStore()
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: _NicknameAdapter()})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_FakeSettings(),
        cookie_import=None,
        login_adapter=None,
    )
    yield NetdiskAccountService(store)
    registry.reset_netdisk_services()


def test_parse_cookie_header_ignores_empty_parts():
    """
    Cookie 头解析忽略空字段并保留最后一个同名值。
    """
    parsed = parse_cookie_header("__pus=1; __puus=2; ; broken; __pus=3")
    assert parsed == {"__pus": "3", "__puus": "2"}


def test_create_rejects_missing_required_keys(service):
    """
    缺少必填字段时拒绝保存并指出字段名。
    """
    with pytest.raises(NetdiskError) as error:
        service.create("quark", "主账号", {"cookie": "__pus=1"})
    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert "__puus" in error.value.message


def test_create_accepts_complete_credential(service):
    """
    字段完整且校验通过时保存账号。
    """
    view = service.create("quark", "主账号", {"cookie": "__pus=1; __puus=2"}, is_primary=True)
    assert view.name == "主账号"
    assert view.is_primary is True
    assert view.masked == {"cookie": "***"}


def test_create_without_name_uses_platform_nickname(nickname_service):
    """
    未提供名称时使用平台解析出的昵称。
    """
    view = nickname_service.create("quark", "", {"cookie": "__pus=1; __puus=2"})
    assert view.name == "夸克用户"


def test_create_without_name_appends_sequence_for_duplicate_nickname(nickname_service):
    """
    同平台昵称重复时自动追加序号，避免触发唯一约束。
    """
    first = nickname_service.create("quark", "", {"cookie": "__pus=1; __puus=2"})
    second = nickname_service.create("quark", "", {"cookie": "__pus=1; __puus=2"})
    assert first.name == "夸克用户"
    assert second.name == "夸克用户 (2)"


def test_create_explicit_duplicate_name_raises(service):
    """
    显式名称重复时保持原行为，由仓储抛出错误且不自动改名。
    """
    service.create("quark", "主账号", {"cookie": "__pus=1; __puus=2"})
    with pytest.raises(NetdiskError) as error:
        service.create("quark", "主账号", {"cookie": "__pus=1; __puus=2"})
    assert "已存在" in error.value.message


def test_select_prefers_primary_then_active(service):
    """
    选择账号时主账号优先，失效后回退到其他可用账号。
    """
    first = service.create("quark", "备用", {"cookie": "__pus=1; __puus=2"})
    primary = service.create("quark", "主账号", {"cookie": "__pus=1; __puus=2"}, is_primary=True)
    selected = service.select("quark")
    assert selected.id == primary.id
    service.store.records[primary.id] = NetdiskAccountRecord(
        id=primary.id,
        platform=NetdiskPlatform.QUARK,
        name=primary.name,
        credential={"cookie": "__pus=1; __puus=2"},
        status="expired",
    )
    fallback = service.select("quark")
    assert fallback.id == first.id


def test_select_without_account_raises_auth_expired(service):
    """
    没有可用账号时返回 AUTH_EXPIRED。
    """
    with pytest.raises(NetdiskError) as error:
        service.select("quark")
    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_probe_updates_status(service):
    """
    探测结果写回账号状态。
    """
    view = service.create("quark", "主账号", {"cookie": "__pus=1; __puus=2"})
    probe = service.probe(view.id)
    assert probe.status == "active"
    assert service.store.records[view.id].status == "active"


def test_update_credential_validates(service):
    """
    更新凭据时同样执行字段校验。
    """
    view = service.create("quark", "主账号", {"cookie": "__pus=1; __puus=2"})
    with pytest.raises(NetdiskError):
        service.update(view.id, credential={"cookie": "__pus=1"})
    updated = service.update(view.id, credential={"cookie": "__pus=1; __puus=2"}, name="新名字")
    assert updated.name == "新名字"


def test_check_all_counts_active_expired_and_failed(status_service):
    """
    批量检测按有效、失效、未知与异常分类计数。
    """
    for name in ("有效", "失效", "未知", "异常"):
        status_service.create("quark", name, {"cookie": "__pus=1; __puus=2"})

    result = status_service.check_all()

    assert result == {"checked": 4, "active": 1, "expired": 1, "failed": 2}


def test_check_all_continues_after_account_error(status_service):
    """
    单账号探测异常不中断后续账号检测。
    """
    status_service.create("quark", "异常", {"cookie": "__pus=1; __puus=2"})
    valid = status_service.create("quark", "有效", {"cookie": "__pus=1; __puus=2"})

    result = status_service.check_all()

    assert result["checked"] == 2
    assert result["active"] == 1
    assert result["failed"] == 1
    assert status_service.store.records[valid.id].status == "active"


def test_check_all_endpoint_returns_accounts_and_counts(monkeypatch):
    """
    检查全部账号接口返回账号列表与统计数量。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_active_superuser_async
    from app.api.endpoints import netdisk as netdisk_endpoints

    service = Mock()
    service.check_all.return_value = {"checked": 2, "active": 1, "expired": 1, "failed": 0}
    service.list.return_value = [
        NetdiskAccountView(id=1, platform="quark", name="主账号", is_primary=True, status="active"),
        NetdiskAccountView(id=2, platform="quark", name="备用", is_primary=False, status="expired"),
    ]
    app = FastAPI()
    app.include_router(netdisk_endpoints.router, prefix="/netdisk")
    app.dependency_overrides[get_current_active_superuser_async] = lambda: Mock()
    monkeypatch.setattr(netdisk_endpoints, "_accounts", lambda: service)

    response = TestClient(app).post("/netdisk/accounts/check")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["checked"] == 2
    assert data["active"] == 1
    assert data["expired"] == 1
    assert data["failed"] == 0
    assert [item["id"] for item in data["accounts"]] == [1, 2]
    service.check_all.assert_called_once_with()




class _QuotaAdapter(_FakeAdapter):
    """
    创建账号时返回固定空间配额的适配器替身。
    """

    drive_capabilities = DriveCapabilities(quota=True)
    cookie_required_keys = ("__puus",)

    def login_account_name(self, credential):
        """
        返回固定昵称。
        """
        del credential
        return "阿夸"

    def quota(self, account):
        """
        返回固定已用/总空间。
        """
        del account
        return DriveQuota(used=123, total=456)


def test_create_fetches_quota_immediately() -> None:
    """
    登录创建账号后立即拉取一次空间，不需要再点检测。
    """
    store = _FakeStore()
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: _QuotaAdapter()})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_FakeSettings(),
        cookie_import=None,
        login_adapter=None,
    )
    try:
        view = NetdiskAccountService(store).create(
            NetdiskPlatform.QUARK,
            "",
            {"cookie": "__puus=token"},
        )

        stored = next(item for item in store.views if item.id == view.id)
        assert view.name == "阿夸"
        assert stored.quota_used == 123
        assert stored.quota_total == 456
    finally:
        registry.reset_netdisk_services()
