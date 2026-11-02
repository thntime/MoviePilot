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
from app.application.netdisk.login import NetdiskLoginService, reset_pending_sessions
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
    reset_pending_sessions()
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


def test_qr_pending_session_survives_new_service_instance(env):
    """
    接口每次请求都会新建用例实例，扫码会话必须跨实例仍可轮询。
    """
    _, store, _, _ = env
    reset_pending_sessions()
    first = NetdiskLoginService(NetdiskAccountService(store))
    second = NetdiskLoginService(NetdiskAccountService(store))

    first.start_qr("pan123", name="扫码账号")
    result = second.poll_qr("s-1")

    assert result.status == "success"
    assert store.records[1].credential == {"token": "jwt-qr"}


class _FakeQrPage:
    """
    模拟登录页与二维码元素，用于验证截图裁剪。
    """

    def __init__(self, box: dict[str, float], element_image: bytes = b"qr-png") -> None:
        """
        保存二维码元素尺寸与截图。
        """
        self._box = box
        self._element_image = element_image
        self.selectors: list[str] = []

    def goto(self, url: str, **kwargs: object) -> None:
        """
        模拟打开页面。
        """
        del url, kwargs

    def wait_for_load_state(self, state: str, timeout: int) -> None:
        """
        模拟等待加载。
        """
        del state, timeout

    def wait_for_timeout(self, timeout: int) -> None:
        """
        模拟等待渲染。
        """
        del timeout

    def locator(self, selector: str) -> "_FakeQrLocator":
        """
        返回二维码元素定位器。
        """
        self.selectors.append(selector)
        return _FakeQrLocator(self._box, self._element_image)

    def screenshot(self) -> bytes:
        """
        返回整页截图。
        """
        return b"full-page"


class _FakeQrLocator:
    """
    模拟 Playwright 定位器。
    """

    def __init__(self, box: dict[str, float], image: bytes) -> None:
        """
        保存元素尺寸与截图。
        """
        self._box = box
        self._image = image

    def count(self) -> int:
        """
        返回匹配数量。
        """
        return 1

    def nth(self, index: int) -> "_FakeQrLocator":
        """
        返回指定元素。
        """
        del index
        return self

    def is_visible(self) -> bool:
        """
        元素可见。
        """
        return True

    def bounding_box(self) -> dict[str, float]:
        """
        返回元素尺寸。
        """
        return self._box

    def screenshot(self) -> bytes:
        """
        返回元素截图。
        """
        return self._image


def test_qr_capture_crops_to_qr_element() -> None:
    """
    二维码元素足够大时应截取元素本身，而不是整页。
    """
    from types import SimpleNamespace
    from app.adapters.external.netdisk.login.browser import BrowserQrLoginAdapter

    page = _FakeQrPage({"width": 220, "height": 220})
    adapter = BrowserQrLoginAdapter()

    image = adapter._capture(SimpleNamespace(active_page=page), "https://example.com")  # pylint: disable=protected-access

    assert image == b"qr-png"
    assert page.selectors


def test_qr_capture_falls_back_to_full_page() -> None:
    """
    找不到足够大的二维码元素时回退整页截图。
    """
    from types import SimpleNamespace
    from app.adapters.external.netdisk.login.browser import BrowserQrLoginAdapter

    page = _FakeQrPage({"width": 20, "height": 20})
    adapter = BrowserQrLoginAdapter()

    image = adapter._capture(SimpleNamespace(active_page=page), "https://example.com")  # pylint: disable=protected-access

    assert image == b"full-page"


def test_qr_missing_cookie_fields_keeps_pending(env, monkeypatch):
    """
    扫码后会话字段尚未下发时保持 pending，下一次轮询再落库。
    """
    service, store, _, _ = env
    calls = {"count": 0}
    original = _Adapter.validate_credential

    def fake_validate(self, credential):
        """
        第一次返回缺少字段，第二次走正常校验。
        """
        calls["count"] += 1
        if calls["count"] == 1:
            return AccountProbe(status="expired", message="凭据缺少必填字段：__pus、__puus")
        return original(self, credential)

    monkeypatch.setattr(_Adapter, "validate_credential", fake_validate)
    service.start_qr("pan123", name="扫码账号")

    first = service.poll_qr("s-1")
    assert first.status == "pending"
    assert store.records == {}

    second = service.poll_qr("s-1")
    assert second.status == "success"
    assert store.records[1].credential == {"token": "jwt-qr"}


class _FakeCookieContext:
    """
    模拟浏览器上下文中的 Cookie 列表。
    """

    def __init__(self, cookies: list[dict[str, str]]) -> None:
        """
        保存 Cookie 列表。
        """
        self.cookies_list = cookies

    def cookies(self) -> list[dict[str, str]]:
        """
        返回 Cookie 列表。
        """
        return list(self.cookies_list)


class _FakeNavPage:
    """
    模拟登录后跳转：进入网盘页时补发 __puus。
    """

    def __init__(self, context: _FakeCookieContext) -> None:
        """
        保存上下文引用。
        """
        self._context = context
        self.urls: list[str] = []

    def goto(self, url: str, **kwargs: object) -> None:
        """
        记录跳转并在进入网盘页后补发 __puus。
        """
        del kwargs
        self.urls.append(url)
        if url.endswith("/list"):
            self._context.cookies_list.append(
                {"name": "__puus", "value": "new-token", "domain": ".quark.cn"}
            )

    def wait_for_timeout(self, timeout: int) -> None:
        """
        模拟等待。
        """
        del timeout


def test_quark_qr_login_navigates_to_drive_for_puus() -> None:
    """
    夸克扫码后停留在登录页时，读取 Cookie 应进入网盘页触发 __puus 下发。
    """
    from types import SimpleNamespace
    from app.adapters.external.netdisk.login.browser import BrowserQrLoginAdapter
    from app.domain.netdisk import NetdiskPlatform

    context = _FakeCookieContext(
        [
            {"name": "b-user-id", "value": "u-1", "domain": ".quark.cn"},
            {"name": "__pus", "value": "old", "domain": ".quark.cn"},
        ]
    )
    page = _FakeNavPage(context)
    state = SimpleNamespace(context=context, active_page=page)
    session = SimpleNamespace(post_login_navigated=False)
    adapter = BrowserQrLoginAdapter()

    cookie = adapter._read_cookie(state, NetdiskPlatform.QUARK, session)  # pylint: disable=protected-access

    assert page.urls == ["https://pan.quark.cn/list"]
    assert "__puus=new-token" in cookie
    assert session.post_login_navigated is True
