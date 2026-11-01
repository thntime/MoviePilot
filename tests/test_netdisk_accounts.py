"""
网盘账号用例的单元测试：字段校验、主账号选择与脱敏。
"""

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
from app.domain.netdisk import AccountProbe, LinkPolicy, NetdiskError, NetdiskErrorCode, NetdiskPlatform


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
