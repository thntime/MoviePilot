"""
网盘网页登录弹窗生命周期测试。

覆盖百度匿名 Cookie 不关闭微信弹窗、切回主页面默认只置前、
只关闭明确登录域名，以及百度 login_ready 过滤匿名 Cookie。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Optional

from app.adapters.external.netdisk import baidu as baidu_module
from app.adapters.external.netdisk.login import browser as browser_module
from app.domain.netdisk import NetdiskPlatform


class _FakePage:
    """
    记录关闭与置前调用的页面替身。
    """

    def __init__(self, url: str, *, opener: Optional[Any] = None) -> None:
        self.url = url
        self._opener = opener
        self.closed = False
        self.close_calls = 0
        self.front_calls = 0

    def is_closed(self) -> bool:
        """
        返回页面是否已关闭。
        """
        return self.closed

    def close(self) -> None:
        """
        记录关闭调用。
        """
        self.closed = True
        self.close_calls += 1

    def bring_to_front(self) -> None:
        """
        记录置前调用。
        """
        self.front_calls += 1

    def opener(self) -> Optional[Any]:
        """
        返回打开该页面的 opener。
        """
        return self._opener


class _FakeContext:
    """
    只提供 pages 与 cookies 的浏览器上下文替身。
    """

    def __init__(self, pages: list[_FakePage], cookies: list[dict[str, str]] | None = None) -> None:
        self.pages = pages
        self._cookies = cookies or []

    def cookies(self) -> list[dict[str, str]]:
        """
        返回预设 Cookie。
        """
        return list(self._cookies)


class _FakeHelper:
    """
    直接执行回调的浏览器会话替身。
    """

    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state

    def with_session(self, session_key: str, callback, timeout: float | None = None):
        """
        在当前状态上同步执行回调。
        """
        del session_key, timeout
        return callback(self._state)


def _session(
    adapter: browser_module.BrowserQrLoginAdapter,
    state: SimpleNamespace,
    *,
    platform: NetdiskPlatform = NetdiskPlatform.BAIDU,
    main_page: Optional[_FakePage] = None,
) -> Any:
    """
    构造并登记一个扫码会话。

    :param adapter: 登录适配器
    :param state: 浏览器会话状态
    :param platform: 平台标识
    :param main_page: 主页面引用
    :return: 扫码会话
    """
    session = browser_module._QrSession(  # pylint: disable=protected-access
        session_id="s-1",
        session_key="k-1",
        platform=platform,
        created_at=0.0,
        expires_at=time.monotonic() + 600,
        helper=_FakeHelper(state),
        main_page=main_page,
    )
    adapter._sessions["s-1"] = session  # pylint: disable=protected-access
    return session


def test_baidu_anonymous_cookie_does_not_close_wechat_popup() -> None:
    """
    只有匿名 BAIDUID 时，轮询不得关闭微信扫码弹窗。
    """
    main = _FakePage("https://pan.baidu.com/")
    popup = _FakePage("https://open.weixin.qq.com/connect/qrconnect?appid=wx1")
    state = SimpleNamespace(
        context=_FakeContext(
            [main, popup],
            [{"name": "BAIDUID", "value": "anonymous", "domain": ".baidu.com"}],
        ),
        active_page=main,
    )
    adapter = browser_module.BrowserQrLoginAdapter()
    _session(adapter, state, main_page=main)

    result = adapter.poll("s-1")

    assert result.status == "success"
    assert popup.close_calls == 0
    assert popup.closed is False
    assert main.front_calls == 0


def test_focus_main_page_default_only_brings_to_front() -> None:
    """
    默认只把主页面置前，不关闭弹窗。
    """
    main = _FakePage("https://pan.baidu.com/")
    popup = _FakePage("https://open.weixin.qq.com/connect/qrconnect")
    state = SimpleNamespace(context=_FakeContext([main, popup]), active_page=main)
    adapter = browser_module.BrowserQrLoginAdapter()
    _session(adapter, state, main_page=main)

    adapter.focus_main_page("s-1")

    assert main.front_calls == 1
    assert popup.close_calls == 0


def test_focus_main_page_close_popups_only_auth_domains() -> None:
    """
    显式关闭弹窗时只关闭微信登录域名，保留主页面与用户标签页。
    """
    main = _FakePage("https://pan.baidu.com/")
    popup = _FakePage("https://open.weixin.qq.com/connect/qrconnect")
    other = _FakePage("https://example.com/")
    state = SimpleNamespace(context=_FakeContext([main, popup, other]), active_page=main)
    adapter = browser_module.BrowserQrLoginAdapter()
    _session(adapter, state, main_page=main)

    adapter.focus_main_page("s-1", close_popups=True)

    assert popup.close_calls == 1
    assert main.close_calls == 0
    assert other.close_calls == 0
    assert main.front_calls == 1


def test_focus_main_page_uses_recorded_main_page() -> None:
    """
    页面顺序变化时仍使用启动时记录的主页面。
    """
    popup = _FakePage("https://open.weixin.qq.com/connect/qrconnect")
    main = _FakePage("https://pan.baidu.com/")
    state = SimpleNamespace(context=_FakeContext([popup, main]), active_page=main)
    adapter = browser_module.BrowserQrLoginAdapter()
    _session(adapter, state, main_page=main)

    adapter.focus_main_page("s-1")

    assert main.front_calls == 1
    assert popup.front_calls == 0


def test_focus_main_page_ignores_non_baidu() -> None:
    """
    非百度平台不执行切页，保持既有登录流程。
    """
    main = _FakePage("https://pan.quark.cn/")
    popup = _FakePage("https://open.weixin.qq.com/connect/qrconnect")
    state = SimpleNamespace(context=_FakeContext([main, popup]), active_page=main)
    adapter = browser_module.BrowserQrLoginAdapter()
    _session(adapter, state, platform=NetdiskPlatform.QUARK, main_page=main)

    adapter.focus_main_page("s-1", close_popups=True)

    assert main.front_calls == 0
    assert popup.close_calls == 0


def test_baidu_login_ready_rejects_anonymous_cookie() -> None:
    """
    匿名 Cookie 或只有 BDUSS 的半截会话不算登录完成，必须同时有 STOKEN。
    """
    adapter = baidu_module.BaiduAdapter()

    assert adapter.login_ready({"cookie": "BAIDUID=anonymous"}) is False
    assert adapter.login_ready({"cookie": "BDUSS=token"}) is False
    assert adapter.login_ready({"BDUSS": "token"}) is False
    assert adapter.login_ready({"cookie": "BDUSS=token; STOKEN=web"}) is True
