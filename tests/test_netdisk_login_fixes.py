"""
网盘登录修复的定向测试：账号昵称提取、光鸭网页令牌与滑块拖拽定位。

全部使用替身，不访问真实网络或浏览器。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.external.netdisk import baidu as baidu_module
from app.adapters.external.netdisk import guangya as guangya_module
from app.adapters.external.netdisk import pan115 as pan115_module
from app.adapters.external.netdisk import pan123 as pan123_module
from app.adapters.external.netdisk import quark as quark_module
from app.adapters.external.netdisk import uc as uc_module
from app.adapters.external.netdisk import xunlei as xunlei_module
from app.adapters.external.netdisk.base import NetdiskHttp
from app.adapters.external.netdisk.login import browser as browser_module
from app.domain.netdisk import NetdiskPlatform


def _patch_get_json(monkeypatch, payload: dict[str, Any], expected_url: str) -> None:
    """
    用固定响应替换 HTTP GET，并校验请求地址。

    :param monkeypatch: pytest 打桩夹具
    :param payload: 假响应
    :param expected_url: 期望请求地址
    """

    def fake_get_json(self, url, *, account=None, params=None, headers=None):
        del self, account, params, headers
        assert url == expected_url
        return payload

    monkeypatch.setattr(NetdiskHttp, "get_json", fake_get_json)


def test_quark_login_account_name(monkeypatch) -> None:
    """
    夸克从账号信息接口读取昵称。
    """
    _patch_get_json(monkeypatch, {"status": 200, "data": {"nickname": "小明"}}, quark_module._ACCOUNT_INFO_URL)

    adapter = quark_module.QuarkAdapter()
    assert adapter.login_account_name({"cookie": "__puus=fake"}) == "小明"


def test_uc_login_account_name(monkeypatch) -> None:
    """
    UC 从账号信息接口读取昵称。
    """
    _patch_get_json(monkeypatch, {"success": True, "data": {"nickname": "小U"}}, uc_module.UC_ACCOUNT_INFO_URL)

    adapter = uc_module.UCAdapter()
    assert adapter.login_account_name({"cookie": "__puus=fake"}) == "小U"


def test_pan123_login_account_name(monkeypatch) -> None:
    """
    123 云盘从 user/info 读取 Nickname。
    """
    _patch_get_json(monkeypatch, {"code": 0, "data": {"Nickname": "123用户"}}, pan123_module._USER_INFO_URL)

    adapter = pan123_module.Pan123Adapter()
    assert adapter.login_account_name({"token": "jwt"}) == "123用户"


def test_pan115_login_account_name(monkeypatch) -> None:
    """
    115 从 user/info 的 data 字段读取账号名。
    """
    _patch_get_json(monkeypatch, {"state": True, "data": "137*****542"}, pan115_module._USER_INFO_URL)

    adapter = pan115_module.Pan115Adapter()
    assert adapter.login_account_name({"cookie": "UID=1; CID=2; SEID=3; KID=4"}) == "137*****542"


def test_guangya_login_account_name(monkeypatch) -> None:
    """
    光鸭从 user/me 读取昵称。
    """
    _patch_get_json(monkeypatch, {"success": True, "data": {"nickname": "鸭子"}}, guangya_module._USER_ME_URL)

    adapter = guangya_module.GuangYaAdapter()
    assert adapter.login_account_name({"token": "web-token"}) == "鸭子"


def test_baidu_login_account_name(monkeypatch) -> None:
    """
    百度从模板变量接口读取用户名。
    """
    _patch_get_json(
        monkeypatch,
        {"errno": 0, "result": {"username": "百度用户"}},
        baidu_module._TEMPLATE_URL,
    )

    adapter = baidu_module.BaiduAdapter()
    assert adapter.login_account_name({"cookie": "BDUSS=fake"}) == "百度用户"


def test_xunlei_login_account_name_prefers_credential() -> None:
    """
    迅雷凭据里带昵称时直接返回，不额外请求接口。
    """
    adapter = xunlei_module.XunleiAdapter()
    credential = {"access_token": "token", "deviceid": "device", "nickname": "雷友"}
    assert adapter.login_account_name(credential) == "雷友"


def test_xunlei_login_account_name_falls_back_to_about(monkeypatch) -> None:
    """
    迅雷凭据没有昵称时从 about 接口读取。
    """
    _patch_get_json(
        monkeypatch,
        {"kind": "drive#about", "nickname": "雷友"},
        xunlei_module._ABOUT_URL,
    )

    adapter = xunlei_module.XunleiAdapter()
    assert adapter.login_account_name({"access_token": "token", "deviceid": "device"}) == "雷友"


class _FakePage:
    """
    记录 evaluate 返回值的页面替身。
    """

    def __init__(self, url: str, result: str) -> None:
        """
        保存页面地址与脚本返回值。
        """
        self.url = url
        self._result = result

    def evaluate(self, script: str) -> str:
        """
        返回预置的脚本结果。
        """
        del script
        return self._result


class _FakeContext:
    """
    只提供 cookies 的浏览器上下文替身。
    """

    def __init__(self, pages: list[_FakePage]) -> None:
        """
        保存页面列表。
        """
        self.pages = pages

    def cookies(self) -> list[dict[str, str]]:
        """
        返回空 Cookie 列表。
        """
        return []


def test_guangya_read_credential_reads_local_storage_token() -> None:
    """
    光鸭网页登录从 localStorage 读取 token，而不是只返回 Cookie。
    """
    page = _FakePage(
        "https://www.guangyapan.com/",
        '{"token":"web-token","refresh_token":"refresh","expires_at":"2026-11-03T16:00:00Z","sub":"u1"}',
    )
    state = SimpleNamespace(context=_FakeContext([page]), active_page=page)
    adapter = browser_module.BrowserQrLoginAdapter()

    credential = adapter._read_credential(state, NetdiskPlatform.GUANGYA)  # pylint: disable=protected-access

    assert credential == {
        "token": "web-token",
        "refresh_token": "refresh",
        "expires_at": "2026-11-03T16:00:00Z",
    }


def test_guangya_read_credential_without_token_stays_pending() -> None:
    """
    光鸭页面没有写入 token 时不返回凭据，继续等待登录。
    """
    page = _FakePage("https://www.guangyapan.com/", "")
    state = SimpleNamespace(context=_FakeContext([page]), active_page=page)
    adapter = browser_module.BrowserQrLoginAdapter()

    assert adapter._read_credential(state, NetdiskPlatform.GUANGYA) == {}  # pylint: disable=protected-access


class _FakeMouse:
    """
    记录鼠标动作的替身。
    """

    def __init__(self) -> None:
        """
        初始化动作记录。
        """
        self.moves: list[tuple[float, float]] = []
        self.down_count = 0
        self.up_count = 0

    def move(self, x: float, y: float) -> None:
        """
        记录移动。
        """
        self.moves.append((x, y))

    def down(self) -> None:
        """
        记录按下。
        """
        self.down_count += 1

    def up(self) -> None:
        """
        记录抬起。
        """
        self.up_count += 1


class _FakeLocator:
    """
    返回固定包围盒的定位器替身。
    """

    def __init__(self, box: dict[str, float]) -> None:
        """
        保存包围盒。
        """
        self._box = box
        self.first = self

    def count(self) -> int:
        """
        始终存在一个元素。
        """
        return 1

    def nth(self, index: int) -> "_FakeLocator":
        """
        返回自身，忽略索引。
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
        返回包围盒。
        """
        return self._box

    def evaluate(self, script: str, arg: object = None) -> bool:
        """
        模拟命中检查，始终认为元素可命中。
        """
        del script, arg
        return True

    def hover(self, timeout: int = 0) -> None:
        """
        模拟 hover，不执行任何动作。
        """
        del timeout


class _FakeDragPage:
    """
    记录拖拽动作的页面替身。
    """

    def __init__(self, box: dict[str, float] | None) -> None:
        """
        保存滑块包围盒。
        """
        self.mouse = _FakeMouse()
        self._box = box
        self.frames = [self]

    def locator(self, selector: str) -> _FakeLocator:
        """
        返回固定定位器；没有句柄时返回空定位器。
        """
        del selector
        if self._box is None:
            raise RuntimeError("no handle")
        return _FakeLocator(self._box)

    def evaluate(self, script: str, arg: object = None) -> dict[str, object]:
        """
        模拟状态读取，返回空状态。
        """
        del script, arg
        return {}

    def wait_for_timeout(self, timeout: int) -> None:
        """
        忽略等待。
        """
        del timeout


def test_drag_centers_on_139_slider_handle() -> None:
    """
    139 拖拽起点校正到滑块中心，并保留水平位移。
    """
    page = _FakeDragPage({"x": 550.0, "y": 531.0, "width": 54.0, "height": 28.0})

    browser_module._drag(  # pylint: disable=protected-access
        page, 588.58, 532.88, 757.07, 522.78, NetdiskPlatform.PAN139
    )

    assert page.mouse.moves[0] == (577.0, 545.0)
    assert page.mouse.moves[-1][0] == 577.0 + (757.07 - 588.58)
    assert page.mouse.moves[-1][1] == 545.0
    assert page.mouse.down_count == 1
    assert page.mouse.up_count == 1


def test_drag_keeps_original_coordinates_for_other_platforms() -> None:
    """
    非 139 平台保持前端传入的坐标。
    """
    page = _FakeDragPage({"x": 550.0, "y": 531.0, "width": 54.0, "height": 28.0})

    browser_module._drag(  # pylint: disable=protected-access
        page, 100.0, 200.0, 300.0, 210.0, NetdiskPlatform.QUARK
    )

    assert page.mouse.moves[0] == (100.0, 200.0)
    assert page.mouse.moves[-1] == (300.0, 210.0)


class _FakeItem:
    """
    滑块候选元素替身。
    """

    def __init__(self, box: dict[str, float], top_ok: bool = True) -> None:
        """
        保存包围盒与命中结果。
        """
        self._box = box
        self._top_ok = top_ok

    def is_visible(self) -> bool:
        """
        元素可见。
        """
        return True

    def bounding_box(self) -> dict[str, float]:
        """
        返回包围盒。
        """
        return self._box

    def evaluate(self, script: str, arg: object = None) -> bool:
        """
        返回预置的命中结果。
        """
        del script, arg
        return self._top_ok

    def hover(self, timeout: int = 0) -> None:
        """
        模拟 hover。
        """
        del timeout


class _FakeLocatorList:
    """
    固定元素列表的定位器替身。
    """

    def __init__(self, items: list[_FakeItem]) -> None:
        """
        保存元素列表。
        """
        self._items = items

    def count(self) -> int:
        """
        返回元素数量。
        """
        return len(self._items)

    def nth(self, index: int) -> _FakeItem:
        """
        返回指定元素。
        """
        return self._items[index]


class _FakeFrame:
    """
    只提供 locator 的 frame 替身。
    """

    def __init__(self, items: list[_FakeItem]) -> None:
        """
        保存候选元素。
        """
        self._items = items
        self.url = "https://yun.139.com/"

    def locator(self, selector: str) -> _FakeLocatorList:
        """
        返回同一组候选元素。
        """
        del selector
        return _FakeLocatorList(self._items)


def test_slider_candidate_prefers_visible_handle_containing_point() -> None:
    """
    多个句柄时优先选择包含原始点且可命中的窄句柄。
    """
    frame = _FakeFrame(
        [
            _FakeItem({"x": 100.0, "y": 100.0, "width": 54.0, "height": 28.0}),
            _FakeItem({"x": 550.0, "y": 531.0, "width": 54.0, "height": 28.0}),
        ]
    )
    page = SimpleNamespace(frames=[frame])

    candidate = browser_module._slider_candidate(page, 588.0, 532.0)  # pylint: disable=protected-access

    assert candidate is not None
    assert candidate["box"]["x"] == 550.0
    assert candidate["contains"] is True


def test_slider_candidate_skips_covered_handle() -> None:
    """
    被遮挡的句柄即使包含原始点也不应优先于可命中的句柄。
    """
    frame = _FakeFrame(
        [
            _FakeItem({"x": 550.0, "y": 531.0, "width": 54.0, "height": 28.0}, top_ok=False),
            _FakeItem({"x": 700.0, "y": 531.0, "width": 54.0, "height": 28.0}, top_ok=True),
        ]
    )
    page = SimpleNamespace(frames=[frame])

    candidate = browser_module._slider_candidate(page, 588.0, 532.0)  # pylint: disable=protected-access

    assert candidate is not None
    assert candidate["box"]["x"] == 700.0
    assert candidate["top_ok"] is True


class _FakeFallbackPage:
    """
    支持状态采样与合成拖拽记录的页面替身。
    """

    def __init__(self, moves_after_drag: bool) -> None:
        """
        保存拼图是否在拖拽后移动。
        """
        self.mouse = _FakeMouse()
        self.frames = [self]
        self.url = "https://yun.139.com/"
        self._moves_after_drag = moves_after_drag
        self._state_calls = 0
        self.synthetic_calls: list[dict[str, object]] = []

    def locator(self, selector: str) -> _FakeLocatorList:
        """
        返回一个窄句柄。
        """
        del selector
        return _FakeLocatorList([_FakeItem({"x": 550.0, "y": 531.0, "width": 54.0, "height": 28.0})])

    def evaluate(self, script: str, arg: object = None) -> dict[str, object]:
        """
        按脚本类型返回状态或记录合成拖拽。
        """
        if "verify-puzzle" in script:
            self._state_calls += 1
            if self._moves_after_drag and self._state_calls >= 2:
                return {"puzzle": 170.0, "handle": 720.0}
            return {"puzzle": 0.0, "handle": 550.0}
        if "MouseEvent" in script:
            self.synthetic_calls.append(dict(arg or {}))
            return {"ok": True}
        return {}

    def wait_for_timeout(self, timeout: int) -> None:
        """
        忽略等待。
        """
        del timeout


def test_drag_falls_back_to_synthetic_when_mouse_does_not_move() -> None:
    """
    真实鼠标拖拽未产生位移时回退到合成 DOM 事件。
    """
    page = _FakeFallbackPage(moves_after_drag=False)

    browser_module._drag(  # pylint: disable=protected-access
        page, 588.0, 532.0, 757.0, 522.0, NetdiskPlatform.PAN139, "session-1"
    )

    assert page.mouse.down_count == 2
    assert len(page.synthetic_calls) == 1
    assert page.synthetic_calls[0]["dx"] == pytest.approx(169.0)


def test_drag_skips_synthetic_when_mouse_moves() -> None:
    """
    真实鼠标拖拽已产生位移时不再触发合成回退。
    """
    page = _FakeFallbackPage(moves_after_drag=True)

    browser_module._drag(  # pylint: disable=protected-access
        page, 588.0, 532.0, 757.0, 522.0, NetdiskPlatform.PAN139, "session-1"
    )

    assert page.mouse.down_count == 1
    assert page.synthetic_calls == []
