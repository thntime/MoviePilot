"""
网盘登录用例的单元测试：粘贴、CookieCloud、账号密码与扫码。
"""

import pytest

from app.adapters.external.netdisk.base import BaseNetdiskAdapter
from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService, mask_credential
from app.application.netdisk.contract import (
    LoginResult,
    LoginSession,
    NetdiskAccountRecord,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskTuning,
)
from app.application.netdisk.login import NetdiskLoginService
from app.domain.netdisk import AccountProbe, LinkPolicy, NetdiskError, NetdiskErrorCode, NetdiskPlatform


class _Adapter(BaseNetdiskAdapter):
    """
    登录用例测试用的平台适配器替身。
    """

    platform = NetdiskPlatform.PAN123
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    max_concurrency = 512
    cookie_required_keys = ("token",)
    cookie_domains = (".123pan.com",)
    login_url = "https://www.123pan.com/"
    supports_password_login = True

    def validate_credential(self, credential):
        """
        令牌非空即视为有效。
        """
        token = str(credential.get("token") or credential.get("cookie") or "")
        if "bad" in token:
            return AccountProbe(status="expired", message="令牌无效")
        return AccountProbe(status="active")

    def password_login(self, username, password):
        """
        账号密码换令牌。
        """
        if password != "ok":
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "账号或密码错误", platform=self.platform)
        return {"token": f"jwt-{username}"}


class _Store:
    """
    内存账号仓储替身。
    """

    def __init__(self) -> None:
        self.views: list[NetdiskAccountView] = []
        self.records: dict[int, NetdiskAccountRecord] = {}
        self._next = 1
        self.updated: list[tuple[int, dict]] = []

    def list_accounts(self, platform=None):
        return list(self.views)

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
        self.updated.append((account_id, kwargs))
        return self.views[0]

    def delete_account(self, account_id):
        return True

    def touch_account(self, account_id, probe):
        return None


class _LoginAdapter:
    """
    扫码登录端口替身。
    """

    def __init__(self) -> None:
        self.started: list[tuple] = []
        self.cancelled: list[str] = []
        self.result = LoginResult(status="success", credential={"token": "jwt-qr"})

    def start(self, platform, login_url, account_id=None):
        self.started.append((platform, login_url, account_id))
        return LoginSession(
            session_id="s-1",
            platform=platform,
            image=b"png-bytes",
            status="pending",
        )

    def poll(self, session_id):
        return self.result

    def cancel(self, session_id):
        self.cancelled.append(session_id)


class _CookieImport:
    """
    CookieCloud 导入端口替身。
    """

    def __init__(self, cookies):
        self.cookies = cookies

    def import_cookies(self, domains):
        return self.cookies


class _Settings:
    """
    固定参数快照。
    """

    def load(self):
        return NetdiskTuning()


@pytest.fixture()
def env():
    """
    装配注册表与登录服务。
    """
    store = _Store()
    login_adapter = _LoginAdapter()
    cookie_import = _CookieImport({"token": "jwt-cookiecloud"})
    registry.configure_netdisk_adapters({NetdiskPlatform.PAN123: _Adapter()})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_Settings(),
        cookie_import=cookie_import,
        login_adapter=login_adapter,
    )
    yield NetdiskLoginService(NetdiskAccountService(store)), store, login_adapter, cookie_import
    registry.reset_netdisk_services()


def test_paste_cookie_saves_account(env):
    """
    粘贴令牌后校验并保存账号。
    """
    service, store, _, _ = env
    view = service.paste_cookie("pan123", "eyJhbGciOiJIUzI1NiJ9.token", name="主账号")
    assert view.name == "主账号"
    assert store.records[1].credential == {"token": "eyJhbGciOiJIUzI1NiJ9.token"}


def test_paste_cookie_rejects_invalid(env):
    """
    校验失败时拒绝保存。
    """
    service, store, _, _ = env
    with pytest.raises(NetdiskError) as error:
        service.paste_cookie("pan123", "bad-token")
    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert store.records == {}


def test_cookiecloud_import(env):
    """
    CookieCloud 返回的字段被保存为凭据。
    """
    service, store, _, _ = env
    view = service.import_cookiecloud("pan123", name="云端账号")
    assert view.name == "云端账号"
    assert store.records[1].credential == {"token": "jwt-cookiecloud"}


def test_cookiecloud_missing_raises(env):
    """
    CookieCloud 无数据时返回明确错误。
    """
    service, store, _, cookie_import = env
    cookie_import.cookies = None
    with pytest.raises(NetdiskError) as error:
        service.import_cookiecloud("pan123")
    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_password_login(env):
    """
    账号密码换取令牌并保存。
    """
    service, store, _, _ = env
    view = service.password_login("pan123", "user", "ok", name="密码账号")
    assert view.name == "密码账号"
    assert store.records[1].credential == {"token": "jwt-user"}


def test_password_login_rejects_wrong_password(env):
    """
    密码错误时不落库。
    """
    service, store, _, _ = env
    with pytest.raises(NetdiskError) as error:
        service.password_login("pan123", "user", "bad")
    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert store.records == {}


def test_qr_login_success_saves_and_cancels(env):
    """
    扫码成功后保存账号并关闭会话。
    """
    service, store, login_adapter, _ = env
    session = service.start_qr("pan123", name="扫码账号")
    assert session.session_id == "s-1"
    result = service.poll_qr("s-1")
    assert result.status == "success"
    assert store.records[1].credential == {"token": "jwt-qr"}
    assert login_adapter.cancelled == ["s-1"]


def test_qr_login_expired(env):
    """
    会话过期时返回 expired 且不落库。
    """
    service, store, login_adapter, _ = env
    service.start_qr("pan123")
    login_adapter.result = LoginResult(status="expired", message="二维码已过期")
    result = service.poll_qr("s-1")
    assert result.status == "expired"
    assert store.records == {}
