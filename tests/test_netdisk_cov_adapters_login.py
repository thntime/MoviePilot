"""
网盘网页登录与 CookieCloud 导入的覆盖率补充测试。

只使用替身对象验证分支与异常路径：不启动真实浏览器、VNC 进程，也不访问网络。
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.external.netdisk.login import browser as browser_module
from app.adapters.external.netdisk.login import cookiecloud as cookiecloud_module
from app.adapters.external.netdisk.login import vnc as vnc_module
from app.domain.netdisk import LoginResult, NetdiskError, NetdiskErrorCode, NetdiskPlatform

# ---- 通用替身 ----


class _FakeProcess:
    """
    模拟 subprocess.Popen 句柄。

    :param alive: poll() 是否返回 None（进程仍在运行）
    """

    def __init__(self, alive: bool = True, terminate_error: bool = False) -> None:
        """
        记录进程存活状态与 terminate 行为。

        :param alive: 是否仍在运行
        :param terminate_error: terminate 是否抛异常
        """
        self._alive = alive
        self._terminate_error = terminate_error
        self.terminated = 0

    def poll(self) -> int | None:
        """
        返回进程状态。

        :return: 运行中返回 None，否则返回退出码
        """
        return None if self._alive else 0

    def terminate(self) -> None:
        """
        记录终止调用，必要时抛异常。
        """
        self.terminated += 1
        if self._terminate_error:
            raise OSError("terminate failed")


class _FakePage:
    """
    模拟 Playwright 页面/Frame 的最小接口。

    :param url: 页面地址
    """

    def __init__(
        self,
        url: str = "https://pan.example.com/login",
        *,
        frames_error: bool = False,
        url_error: bool = False,
        locator_error: bool = False,
        evaluate_result: Any = None,
        evaluate_error: bool = False,
        closed: bool = False,
        closed_error: bool = False,
    ) -> None:
        """
        保存页面行为开关。

        :param url: 页面地址
        :param frames_error: 读取 frames 是否抛异常
        :param url_error: 读取 url 是否抛异常
        :param locator_error: locator 是否抛异常
        :param evaluate_result: evaluate 返回值
        :param evaluate_error: evaluate 是否抛异常
        :param closed: is_closed 返回值
        :param closed_error: is_closed 是否抛异常
        """
        self._url = url
        self._frames: list[Any] = []
        self._frames_error = frames_error
        self._url_error = url_error
        self._locator_error = locator_error
        self._evaluate_result = evaluate_result
        self._evaluate_error = evaluate_error
        self._closed = closed
        self._closed_error = closed_error
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.waited: list[int] = []
        self.screencast = _FakeScreencast()
        self.viewport_size: dict[str, int] | None = {"width": 1280, "height": 720}
        self.brought_to_front = 0
        self.closed_calls = 0

    @property
    def url(self) -> str:
        """
        返回页面地址，可模拟读取失败。

        :return: 页面地址
        """
        if self._url_error:
            raise RuntimeError("page closed")
        return self._url

    @property
    def frames(self) -> list[Any]:
        """
        返回 frame 列表，可模拟读取失败。

        :return: frame 列表
        """
        if self._frames_error:
            raise RuntimeError("frames unavailable")
        return list(self._frames)

    @frames.setter
    def frames(self, value: list[Any]) -> None:
        """
        替换 frame 列表。

        :param value: frame 列表
        """
        self._frames = list(value)

    def goto(self, url: str, **kwargs: Any) -> None:
        """
        模拟打开页面。

        :param url: 地址
        :param kwargs: 其他参数
        """
        del url, kwargs

    def locator(self, selector: str) -> Any:
        """
        返回元素定位器。

        :param selector: CSS 选择器
        :return: 定位器
        """
        del selector
        if self._locator_error:
            raise RuntimeError("bad selector")
        return _FakeLocator([])

    def evaluate(self, script: Any, arg: Any = None) -> Any:
        """
        模拟页面脚本执行。

        :param script: 脚本
        :param arg: 参数
        :return: 预设结果
        """
        del script, arg
        if self._evaluate_error:
            raise RuntimeError("evaluate failed")
        return self._evaluate_result

    def wait_for_timeout(self, timeout: int) -> None:
        """
        记录等待时长。

        :param timeout: 毫秒
        """
        self.waited.append(timeout)

    def is_closed(self) -> bool:
        """
        返回页面是否关闭。

        :return: 是否关闭
        """
        if self._closed_error:
            raise RuntimeError("closed state unavailable")
        return self._closed

    def bring_to_front(self) -> None:
        """
        记录置前调用。
        """
        self.brought_to_front += 1

    def close(self) -> None:
        """
        记录关闭调用。
        """
        self.closed_calls += 1

    def opener(self) -> Any:
        """
        返回打开者；默认无。

        :return: None
        """
        return None

    def screenshot(self, **kwargs: Any) -> bytes:
        """
        返回整页截图。

        :param kwargs: 截图参数
        :return: 图片字节
        """
        del kwargs
        return b"page-jpeg"


class _FakeMouse:
    """
    记录鼠标动作。
    """

    def __init__(self) -> None:
        """
        初始化动作记录。
        """
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float]] = []
        self.wheels: list[tuple[int, float]] = []
        self.down_calls = 0
        self.up_calls = 0

    def move(self, x: float, y: float) -> None:
        """
        记录移动。

        :param x: 横坐标
        :param y: 纵坐标
        """
        self.moves.append((x, y))

    def down(self) -> None:
        """
        记录按下。
        """
        self.down_calls += 1

    def up(self) -> None:
        """
        记录抬起。
        """
        self.up_calls += 1

    def click(self, x: float, y: float) -> None:
        """
        记录点击。

        :param x: 横坐标
        :param y: 纵坐标
        """
        self.clicks.append((x, y))

    def wheel(self, dx: int, dy: float) -> None:
        """
        记录滚轮。

        :param dx: 横向距离
        :param dy: 纵向距离
        """
        self.wheels.append((dx, dy))


class _FakeKeyboard:
    """
    记录键盘动作。
    """

    def __init__(self) -> None:
        """
        初始化动作记录。
        """
        self.typed: list[str] = []
        self.pressed: list[str] = []

    def type(self, text: str) -> None:
        """
        记录输入。

        :param text: 文本
        """
        self.typed.append(text)

    def press(self, key: str) -> None:
        """
        记录按键。

        :param key: 键名
        """
        self.pressed.append(key)


class _FakeScreencast:
    """
    记录 screencast 启停。
    """

    def __init__(self, start_error: bool = False, stop_error: bool = False) -> None:
        """
        保存异常开关。

        :param start_error: start 是否抛异常
        :param stop_error: stop 是否抛异常
        """
        self._start_error = start_error
        self._stop_error = stop_error
        self.callback: Any = None
        self.started = 0
        self.stopped = 0

    def start(self, *, on_frame: Any, quality: int, size: Any) -> None:
        """
        记录启动参数。

        :param on_frame: 帧回调
        :param quality: 质量
        :param size: 尺寸
        """
        del quality, size
        if self._start_error:
            raise RuntimeError("screencast unavailable")
        self.started += 1
        self.callback = on_frame

    def stop(self) -> None:
        """
        记录停止调用。
        """
        self.stopped += 1
        if self._stop_error:
            raise RuntimeError("already closed")


class _FakeLocator:
    """
    模拟 Playwright 定位器。

    :param items: 元素列表
    """

    def __init__(self, items: list[Any], count_error: bool = False) -> None:
        """
        保存元素列表。

        :param items: 元素列表
        :param count_error: count 是否抛异常
        """
        self._items = items
        self._count_error = count_error

    def count(self) -> int:
        """
        返回元素数量。

        :return: 数量
        """
        if self._count_error:
            raise RuntimeError("count failed")
        return len(self._items)

    def nth(self, index: int) -> Any:
        """
        返回指定元素。

        :param index: 下标
        :return: 元素
        """
        return self._items[index]


class _FakeItem:
    """
    模拟滑块句柄元素。

    :param box: 元素矩形
    """

    def __init__(
        self,
        box: dict[str, float] | None,
        *,
        visible: bool = True,
        box_error: bool = False,
        evaluate_result: Any = True,
        evaluate_error: bool = False,
        hover_error: bool = False,
    ) -> None:
        """
        保存元素行为开关。

        :param box: 元素矩形
        :param visible: 是否可见
        :param box_error: bounding_box 是否抛异常
        :param evaluate_result: evaluate 返回值
        :param evaluate_error: evaluate 是否抛异常
        :param hover_error: hover 是否抛异常
        """
        self._box = box
        self._visible = visible
        self._box_error = box_error
        self._evaluate_result = evaluate_result
        self._evaluate_error = evaluate_error
        self._hover_error = hover_error

    def is_visible(self) -> bool:
        """
        返回可见性。

        :return: 是否可见
        """
        return self._visible

    def bounding_box(self) -> dict[str, float] | None:
        """
        返回元素矩形。

        :return: 矩形或 None
        """
        if self._box_error:
            raise RuntimeError("detached")
        return self._box

    def evaluate(self, script: Any) -> Any:
        """
        返回遮挡检查结果。

        :param script: 脚本
        :return: 预设结果
        """
        del script
        if self._evaluate_error:
            raise RuntimeError("evaluate failed")
        return self._evaluate_result

    def hover(self, timeout: int) -> None:
        """
        模拟悬停。

        :param timeout: 超时毫秒
        """
        del timeout
        if self._hover_error:
            raise RuntimeError("covered")


class _FakeState:
    """
    模拟浏览器会话状态。

    :param pages: 页面列表
    """

    def __init__(
        self,
        pages: list[Any] | None = None,
        *,
        pages_error: bool = False,
        active_page: Any = None,
    ) -> None:
        """
        保存上下文替身。

        :param pages: 页面列表
        :param pages_error: 读取 pages 是否抛异常
        :param active_page: 活动页面
        """
        self.context = _FakeContext(pages or [], pages_error=pages_error)
        self.active_page = active_page


class _FakeContext:
    """
    模拟浏览器上下文。

    :param pages: 页面列表
    """

    def __init__(self, pages: list[Any], *, pages_error: bool = False) -> None:
        """
        保存页面列表与监听器。

        :param pages: 页面列表
        :param pages_error: 读取 pages 是否抛异常
        """
        self._pages = pages
        self._pages_error = pages_error
        self.listeners: dict[str, Any] = {}
        self.remove_error = False

    def cookies(self) -> list[dict[str, str]]:
        """
        返回空 Cookie 列表。

        :return: Cookie 列表
        """
        return []

    @property
    def pages(self) -> list[Any]:
        """
        返回页面列表。

        :return: 页面列表
        """
        if self._pages_error:
            raise RuntimeError("context closed")
        return list(self._pages)

    def on(self, event: str, callback: Any) -> None:
        """
        记录事件监听器。

        :param event: 事件名
        :param callback: 回调
        """
        self.listeners[event] = callback

    def remove_listener(self, event: str, callback: Any) -> None:
        """
        移除监听器，可模拟失败。

        :param event: 事件名
        :param callback: 回调
        """
        del event, callback
        if self.remove_error:
            raise RuntimeError("listener gone")
        self.listeners.clear()


class _FakeHelper:
    """
    模拟 BrowserSessionHelper。

    :param result: with_session 返回值
    """

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        """
        保存返回结果或异常。

        :param result: 正常返回值
        :param error: 需要抛出的异常
        """
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.close_calls: list[str] = []

    def with_session(self, session_key: str, action: Any, timeout: int) -> Any:
        """
        直接调用回调并返回预设结果。

        :param session_key: 会话键
        :param action: 回调
        :param timeout: 超时秒数
        :return: 回调结果或预设结果
        """
        del timeout
        self.calls.append(session_key)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return action(SimpleNamespace())

    def close_session(self, session_key: str) -> None:
        """
        记录关闭调用。

        :param session_key: 会话键
        """
        self.close_calls.append(session_key)


class _FakeBridge:
    """
    模拟 VNC 桥接。

    :param error: acquire 需要抛出的异常
    """

    def __init__(self, error: Exception | None = None) -> None:
        """
        保存异常开关。

        :param error: acquire 异常
        """
        self.error = error
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, session_id: str) -> None:
        """
        记录占用，必要时抛异常。

        :param session_id: 会话标识
        """
        self.acquired.append(session_id)
        if self.error is not None:
            raise self.error

    def release(self, session_id: str) -> None:
        """
        记录释放。

        :param session_id: 会话标识
        """
        self.released.append(session_id)


def _session(platform: NetdiskPlatform = NetdiskPlatform.QUARK, *, helper: Any = None, expires_at: float = 1e12) -> Any:
    """
    构造扫码会话对象。

    :param platform: 平台
    :param helper: 浏览器辅助替身
    :param expires_at: 过期时间（monotonic 秒）
    :return: 扫码会话
    """
    return browser_module._QrSession(  # pylint: disable=protected-access
        session_id="session-1",
        session_key="netdisk-qr-session-1",
        platform=platform,
        created_at=0.0,
        expires_at=expires_at,
        helper=helper if helper is not None else _FakeHelper(),
        main_page=None,
    )


# ---- CookieCloud 导入 ----


def test_cookiecloud_import_filters_by_domain(monkeypatch) -> None:
    """
    按域名后缀筛选 CookieCloud 数据，并跳过无等号的片段。
    """
    cookies = {
        ".quark.cn": "__pus=a; __puus=b; broken; =noname",
        "pan.baidu.com": "BDUSS=x",
    }
    monkeypatch.setattr(
        cookiecloud_module,
        "CookieCloudHelper",
        lambda: SimpleNamespace(download=lambda: (cookies, "")),
    )

    result = cookiecloud_module.CookieCloudImportAdapter().import_cookies(["quark.cn"])

    assert result == {"__pus": "a", "__puus": "b"}


def test_cookiecloud_import_without_domains_keeps_all(monkeypatch) -> None:
    """
    未指定域名时不筛选，全部 Cookie 都返回。
    """
    monkeypatch.setattr(
        cookiecloud_module,
        "CookieCloudHelper",
        lambda: SimpleNamespace(download=lambda: ({"a.com": "k=v"}, "")),
    )

    assert cookiecloud_module.CookieCloudImportAdapter().import_cookies([]) == {"k": "v"}


def test_cookiecloud_import_returns_none_without_data(monkeypatch) -> None:
    """
    无数据时返回 None，并记录服务端说明。
    """
    monkeypatch.setattr(
        cookiecloud_module,
        "CookieCloudHelper",
        lambda: SimpleNamespace(download=lambda: (None, "未配置")),
    )

    assert cookiecloud_module.CookieCloudImportAdapter().import_cookies(["quark.cn"]) is None

    monkeypatch.setattr(
        cookiecloud_module,
        "CookieCloudHelper",
        lambda: SimpleNamespace(download=lambda: (None, "")),
    )
    assert cookiecloud_module.CookieCloudImportAdapter().import_cookies(["quark.cn"]) is None


def test_cookiecloud_import_swallows_helper_error(monkeypatch) -> None:
    """
    下载异常只记录，导入结果为 None。
    """
    def boom() -> Any:
        """
        模拟下载失败。
        """
        raise RuntimeError("network down")

    monkeypatch.setattr(cookiecloud_module, "CookieCloudHelper", lambda: SimpleNamespace(download=boom))

    assert cookiecloud_module.CookieCloudImportAdapter().import_cookies(["quark.cn"]) is None


def test_cookiecloud_import_returns_none_when_no_field_matches(monkeypatch) -> None:
    """
    域名不匹配或没有有效字段时返回 None。
    """
    monkeypatch.setattr(
        cookiecloud_module,
        "CookieCloudHelper",
        lambda: SimpleNamespace(download=lambda: ({"other.com": "k=v"}, "")),
    )

    assert cookiecloud_module.CookieCloudImportAdapter().import_cookies(["quark.cn"]) is None


# ---- VNC 桥接 ----


def test_vnc_bridge_singleton_and_accessor() -> None:
    """
    instance() 返回进程级单例，vnc_bridge() 转发到该单例。
    """
    vnc_module.VncBridge._instance = None  # pylint: disable=protected-access

    first = vnc_module.VncBridge.instance()
    second = vnc_module.VncBridge.instance()

    assert first is second
    assert vnc_module.vnc_bridge() is first
    vnc_module.VncBridge._instance = None  # pylint: disable=protected-access


def test_vnc_acquire_releases_channel_on_start_failure(monkeypatch) -> None:
    """
    启动失败时清空活动会话，允许下一次重试。
    """
    bridge = vnc_module.VncBridge()

    def fail() -> None:
        """
        模拟启动失败。
        """
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "缺少组件")

    monkeypatch.setattr(bridge, "_ensure_started", fail)

    with pytest.raises(NetdiskError):
        bridge.acquire("session-1")

    assert bridge._active_session is None  # pylint: disable=protected-access


def test_vnc_ensure_started_requires_binaries(monkeypatch) -> None:
    """
    缺少 noVNC 组件时抛出明确的未支持错误。
    """
    bridge = vnc_module.VncBridge()
    monkeypatch.setattr(vnc_module.shutil, "which", lambda name: None if name == "websockify" else "/usr/bin/" + name)

    with pytest.raises(NetdiskError) as error:
        bridge._ensure_started()  # pylint: disable=protected-access

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert "websockify" in error.value.message


def test_vnc_ensure_started_rejects_missing_display(monkeypatch) -> None:
    """
    托管显示资源没有显示号时抛出未知错误。
    """
    bridge = vnc_module.VncBridge()
    monkeypatch.setattr(vnc_module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(vnc_module, "acquire_managed_resource", lambda *a, **k: SimpleNamespace(display=None))
    monkeypatch.setenv("DISPLAY", "")

    with pytest.raises(NetdiskError) as error:
        bridge._ensure_started()  # pylint: disable=protected-access

    assert error.value.code is NetdiskErrorCode.UNKNOWN


def test_vnc_ensure_started_spawns_processes(monkeypatch) -> None:
    """
    组件齐全时启动 x11vnc、openbox 与 websockify，并等待端口就绪。
    """
    bridge = vnc_module.VncBridge()
    spawned: list[list[str]] = []
    monkeypatch.setattr(vnc_module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        vnc_module,
        "acquire_managed_resource",
        lambda *a, **k: SimpleNamespace(display=SimpleNamespace(new_display_var=":99")),
    )

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeProcess:
        """
        记录启动命令并返回存活句柄。

        :param args: 命令参数
        :param kwargs: 其他参数
        :return: 进程替身
        """
        del kwargs
        spawned.append(args)
        return _FakeProcess()

    monkeypatch.setattr(vnc_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bridge, "_wait_port", lambda port, timeout: None)

    bridge._ensure_started()  # pylint: disable=protected-access

    assert [args[0] for args in spawned] == ["x11vnc", "openbox", "websockify"]
    assert spawned[0][2] == ":99"


def test_vnc_ensure_started_reuses_running_processes(monkeypatch) -> None:
    """
    已有存活进程时不重复启动。
    """
    bridge = vnc_module.VncBridge()
    bridge._x11vnc = _FakeProcess()  # pylint: disable=protected-access
    bridge._websockify = _FakeProcess()  # pylint: disable=protected-access
    spawned: list[str] = []
    monkeypatch.setattr(vnc_module.shutil, "which", lambda name: "" if name == "openbox" else "/usr/bin/" + name)
    monkeypatch.setattr(
        vnc_module,
        "acquire_managed_resource",
        lambda *a, **k: SimpleNamespace(display=SimpleNamespace(new_display_var=":99")),
    )

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeProcess:
        """
        记录启动命令。

        :param args: 命令参数
        :param kwargs: 其他参数
        :return: 进程替身
        """
        del kwargs
        spawned.append(args[0])
        return _FakeProcess()

    monkeypatch.setattr(vnc_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bridge, "_wait_port", lambda port, timeout: None)

    bridge._ensure_started()  # pylint: disable=protected-access

    assert spawned == []


def test_vnc_display_value_fallbacks(monkeypatch) -> None:
    """
    显示号读取支持资源字段缺失与 DISPLAY 环境变量回退。
    """
    monkeypatch.setenv("DISPLAY", ":7")

    assert vnc_module.VncBridge._display_value(SimpleNamespace(display=None)) == ":7"  # pylint: disable=protected-access
    assert vnc_module.VncBridge._display_value(SimpleNamespace(display=SimpleNamespace(new_display_var=""))) == ":7"  # pylint: disable=protected-access


def test_vnc_wait_port_returns_when_listening(monkeypatch) -> None:
    """
    端口可连接时立即返回。
    """
    class _FakeSocket:
        """
        模拟可连接的 socket。
        """

        def __enter__(self) -> "_FakeSocket":
            """
            进入上下文。

            :return: 自身
            """
            return self

        def __exit__(self, *args: Any) -> None:
            """
            退出上下文。

            :param args: 异常信息
            """
            return None

        def settimeout(self, timeout: float) -> None:
            """
            设置超时。

            :param timeout: 超时秒数
            """
            del timeout

        def connect_ex(self, address: tuple[str, int]) -> int:
            """
            模拟连接成功。

            :param address: 地址
            :return: 0
            """
            del address
            return 0

    monkeypatch.setattr(vnc_module.socket, "socket", lambda *a, **k: _FakeSocket())

    vnc_module.VncBridge._wait_port(6080, timeout=1.0)  # pylint: disable=protected-access


def test_vnc_wait_port_raises_on_timeout(monkeypatch) -> None:
    """
    端口始终不可连接时抛出未知错误。
    """
    class _RefusingSocket:
        """
        模拟连接被拒绝的 socket。
        """

        def __enter__(self) -> "_RefusingSocket":
            """
            进入上下文。

            :return: 自身
            """
            return self

        def __exit__(self, *args: Any) -> None:
            """
            退出上下文。

            :param args: 异常信息
            """
            return None

        def settimeout(self, timeout: float) -> None:
            """
            设置超时。

            :param timeout: 超时秒数
            """
            del timeout

        def connect_ex(self, address: tuple[str, int]) -> int:
            """
            模拟连接失败。

            :param address: 地址
            :return: 非零错误码
            """
            del address
            return 111

    monkeypatch.setattr(vnc_module.socket, "socket", lambda *a, **k: _RefusingSocket())
    monkeypatch.setattr(vnc_module.time, "sleep", lambda seconds: None)

    with pytest.raises(NetdiskError) as error:
        vnc_module.VncBridge._wait_port(6080, timeout=0.0)  # pylint: disable=protected-access

    assert error.value.code is NetdiskErrorCode.UNKNOWN


def test_vnc_stop_terminates_live_processes(monkeypatch) -> None:
    """
    停止时终止存活进程，单个 terminate 失败不影响其他进程。
    """
    bridge = vnc_module.VncBridge()
    bridge._websockify = _FakeProcess()  # pylint: disable=protected-access
    bridge._x11vnc = _FakeProcess(terminate_error=True)  # pylint: disable=protected-access
    bridge._openbox = _FakeProcess(alive=False)  # pylint: disable=protected-access
    bridge._active_session = "session-1"  # pylint: disable=protected-access

    bridge.stop()

    assert bridge._websockify is None  # pylint: disable=protected-access
    assert bridge._x11vnc is None  # pylint: disable=protected-access
    assert bridge._active_session is None  # pylint: disable=protected-access


# ---- 浏览器扫码登录：URL 与滑块工具 ----


def test_safe_url_handles_invalid_and_relative_urls() -> None:
    """
    非法或相对地址退化为去掉查询串的截断文本。
    """
    assert browser_module._safe_url("http://[::1") == "http://[::1"  # pylint: disable=protected-access
    assert browser_module._safe_url("/login?next=1#frag") == "/login"  # pylint: disable=protected-access
    assert browser_module._safe_url(None) == ""  # pylint: disable=protected-access
    assert browser_module._safe_url("https://a.com/x?y=1") == "https://a.com/x"  # pylint: disable=protected-access


def test_host_of_swallows_unreadable_page() -> None:
    """
    页面地址不可读时按未知主机处理。
    """
    assert browser_module._host_of(_FakePage(url_error=True)) == ""  # pylint: disable=protected-access
    assert browser_module._host_of(_FakePage(url="https://PAN.baidu.com/x")) == "pan.baidu.com"  # pylint: disable=protected-access


def test_slider_candidate_collects_and_ranks_candidates() -> None:
    """
    遍历全部 frame，跳过不可见/无效元素并按排序键选取最合适的句柄。
    """
    good = _FakeItem({"x": 10.0, "y": 10.0, "width": 40.0, "height": 40.0})
    frame = _FakePage()
    frame.locator = lambda selector: (  # type: ignore[method-assign]
        _FakeLocator([good]) if selector == browser_module._SLIDER_HANDLE_SELECTORS[0] else _FakeLocator([], count_error=True)
    )
    page = _FakePage()
    page.frames = [frame]  # type: ignore[assignment]

    candidate = browser_module._slider_candidate(page, 20.0, 20.0)  # pylint: disable=protected-access

    assert candidate is not None
    assert candidate["contains"] is True
    assert candidate["top_ok"] is True


def test_slider_candidate_skips_unusable_elements() -> None:
    """
    跳过不可见、无矩形、零尺寸、重复与遮挡检查失败的元素。
    """
    items = [
        _FakeItem({"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}, visible=False),
        _FakeItem(None),
        _FakeItem({"x": 0.0, "y": 0.0, "width": 0.0, "height": 10.0}),
        _FakeItem({"x": 1.0, "y": 1.0, "width": 20.0, "height": 20.0}, evaluate_error=True),
        _FakeItem({"x": 1.0, "y": 1.0, "width": 20.0, "height": 20.0}, evaluate_error=True),
    ]
    frame = _FakePage()
    frame.locator = lambda selector: _FakeLocator(items) if selector == browser_module._SLIDER_HANDLE_SELECTORS[0] else _FakeLocator([])  # type: ignore[method-assign]
    page = _FakePage()
    page.frames = [frame]  # type: ignore[assignment]

    candidate = browser_module._slider_candidate(page, 0.0, 0.0)  # pylint: disable=protected-access

    assert candidate is not None
    assert candidate["top_ok"] is False


def test_slider_candidate_returns_none_without_frames_or_candidates() -> None:
    """
    frames 不可读且没有候选元素时返回 None。
    """
    page = _FakePage(frames_error=True, locator_error=True)

    assert browser_module._slider_candidate(page, 0.0, 0.0) is None  # pylint: disable=protected-access


def test_slider_candidate_ignores_detached_elements() -> None:
    """
    元素读取失败（已卸载）时跳过。
    """
    frame = _FakePage()
    frame.locator = lambda selector: (  # type: ignore[method-assign]
        _FakeLocator([_FakeItem(None, box_error=True)]) if selector == browser_module._SLIDER_HANDLE_SELECTORS[0] else _FakeLocator([])
    )
    page = _FakePage()
    page.frames = [frame]  # type: ignore[assignment]

    assert browser_module._slider_candidate(page, 0.0, 0.0) is None  # pylint: disable=protected-access


def test_slider_state_reads_first_available_frame() -> None:
    """
    跳过异常与非映射结果，取第一个有效状态。
    """
    page = _FakePage()
    page.frames = [
        _FakePage(evaluate_error=True),
        _FakePage(evaluate_result="not-a-mapping"),
        _FakePage(evaluate_result={"puzzle": 12.5, "handle": None}),
    ]

    state = browser_module._slider_state(page)  # pylint: disable=protected-access

    assert state == {"puzzle": 12.5, "handle": None}


def test_slider_state_falls_back_to_page_when_frames_unreadable() -> None:
    """
    frames 不可读时退回页面本身读取状态。
    """
    page = _FakePage(frames_error=True, evaluate_result={})

    state = browser_module._slider_state(page)  # pylint: disable=protected-access

    assert state == {"puzzle": None, "handle": None}


def test_slider_state_prefers_given_frame() -> None:
    """
    传入 frame 时优先读取该 frame。
    """
    frame = _FakePage(evaluate_result={"puzzle": 1.0, "handle": 2.0})

    state = browser_module._slider_state(_FakePage(), frame)  # pylint: disable=protected-access

    assert state == {"puzzle": 1.0, "handle": 2.0}


def test_probe_helpers_swallow_script_errors() -> None:
    """
    探针安装与读取失败时静默返回。
    """
    browser_module._install_probe(_FakePage(evaluate_error=True))  # pylint: disable=protected-access

    assert browser_module._read_probe(_FakePage(evaluate_error=True)) is None  # pylint: disable=protected-access


def test_synthetic_drag_reports_failure() -> None:
    """
    合成拖拽脚本异常时记录并返回 None。
    """
    candidate = {
        "frame": _FakePage(evaluate_error=True),
        "selector": ".slide-block",
        "index": 0,
    }

    assert browser_module._synthetic_drag(candidate, 10.0, 0.0) is None  # pylint: disable=protected-access


def test_drag_non_pan139_uses_plain_mouse(monkeypatch) -> None:
    """
    非 139 平台直接使用真实鼠标拖拽。
    """
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(browser_module, "_mouse_drag", lambda page, *args: calls.append(args))

    page = _FakePage()
    browser_module._drag(page, 1.0, 2.0, 30.0, 4.0, NetdiskPlatform.QUARK, "session-1")  # pylint: disable=protected-access

    assert calls == [(1.0, 2.0, 30.0, 4.0)]


def test_drag_pan139_corrects_wide_handle_and_retries(monkeypatch) -> None:
    """
    139 宽轨道按边界校正起点；未移动时用原始坐标重试。
    """
    page = _FakePage(evaluate_result={"puzzle": 0.0, "handle": 0.0})
    candidate = {
        "frame": _FakePage(evaluate_result={"puzzle": 0.0, "handle": 0.0}),
        "locator": _FakeItem({"x": 10.0, "y": 20.0, "width": 200.0, "height": 40.0}),
        "selector": ".slide-block",
        "index": 0,
        "box": {"x": 10.0, "y": 20.0, "width": 200.0, "height": 40.0},
        "contains": True,
        "top_ok": True,
        "wide": True,
    }
    monkeypatch.setattr(browser_module, "_slider_candidate", lambda page, x, y: candidate)
    drags: list[tuple[float, ...]] = []
    monkeypatch.setattr(browser_module, "_mouse_drag", lambda page, *args: drags.append(args))

    browser_module._drag(page, 5.0, 25.0, 105.0, 25.0, NetdiskPlatform.PAN139, "session-abcdef")  # pylint: disable=protected-access

    assert drags[0][:2] == (18.0, 28.0)
    assert len(drags) == 2


def test_drag_pan139_uses_synthetic_events_when_mouse_fails(monkeypatch) -> None:
    """
    真实鼠标两次都未产生位移时，退回合成 DOM 事件。
    """
    page = _FakePage(evaluate_result={"puzzle": 5.0, "handle": 5.0})
    candidate = {
        "frame": _FakePage(evaluate_result={"puzzle": 5.0, "handle": 5.0}),
        "locator": _FakeItem({"x": 1.0, "y": 1.0, "width": 20.0, "height": 20.0}, hover_error=True),
        "selector": ".slide-block",
        "index": 0,
        "box": {"x": 1.0, "y": 1.0, "width": 20.0, "height": 20.0},
        "contains": True,
        "top_ok": True,
        "wide": False,
    }
    monkeypatch.setattr(browser_module, "_slider_candidate", lambda page, x, y: candidate)
    monkeypatch.setattr(browser_module, "_mouse_drag", lambda page, *args: None)
    synthetic: list[tuple[float, float]] = []
    monkeypatch.setattr(
        browser_module,
        "_synthetic_drag",
        lambda candidate, dx, dy: synthetic.append((dx, dy)),
    )

    browser_module._drag(page, 1.0, 1.0, 51.0, 1.0, NetdiskPlatform.PAN139, "session-1")  # pylint: disable=protected-access

    assert synthetic == [(50.0, 0.0)]


# ---- 浏览器扫码登录：会话生命周期 ----


def test_start_returns_session_with_base64_screenshots(monkeypatch) -> None:
    """
    启动登录时占用 VNC 通道、截图并登记会话。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    bridge = _FakeBridge()
    helper = _FakeHelper()
    calls: list[str] = []

    def with_session(session_key: str, action: Any, timeout: int) -> Any:
        """
        第一次返回截图元组，第二次返回主页面。

        :param session_key: 会话键
        :param action: 回调
        :param timeout: 超时秒数
        :return: 预设结果
        """
        del session_key, timeout
        calls.append("call")
        return (b"qr", b"page") if len(calls) == 1 else _FakePage()

    helper.with_session = with_session  # type: ignore[method-assign]
    adapter._vnc_helper = helper  # pylint: disable=protected-access
    monkeypatch.setattr(browser_module, "vnc_bridge", lambda: bridge)

    session = adapter.start(NetdiskPlatform.QUARK, "https://pan.quark.cn/")

    assert bridge.acquired == [session.session_id]
    assert base64.b64decode(session.image) == b"qr"
    assert base64.b64decode(session.page_image) == b"page"
    assert adapter._sessions[session.session_id].main_page is not None  # pylint: disable=protected-access


def test_start_releases_channel_when_browser_fails(monkeypatch) -> None:
    """
    浏览器启动失败时释放 VNC 通道并抛出统一错误。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    bridge = _FakeBridge()
    adapter._vnc_helper = _FakeHelper(error=RuntimeError("browser crash"))  # pylint: disable=protected-access
    monkeypatch.setattr(browser_module, "vnc_bridge", lambda: bridge)

    with pytest.raises(NetdiskError) as error:
        adapter.start(NetdiskPlatform.QUARK, "https://pan.quark.cn/")

    assert error.value.code is NetdiskErrorCode.UNKNOWN
    assert bridge.released


def test_poll_reports_missing_and_expired_session(monkeypatch) -> None:
    """
    会话不存在或已过期时返回 expired。
    """
    adapter = browser_module.BrowserQrLoginAdapter()

    assert adapter.poll("unknown").status == "expired"

    adapter._sessions["session-1"] = _session(expires_at=0.0)  # pylint: disable=protected-access
    cancelled: list[str] = []
    monkeypatch.setattr(adapter, "cancel", lambda session_id: cancelled.append(session_id))

    result = adapter.poll("session-1")

    assert result.status == "expired"
    assert cancelled == ["session-1"]


def test_poll_maps_browser_errors_to_pending() -> None:
    """
    读取凭据异常时保持 pending。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    adapter._sessions["session-1"] = _session(helper=_FakeHelper(error=RuntimeError("gone")))  # pylint: disable=protected-access

    assert adapter.poll("session-1").status == "pending"


def test_poll_returns_pending_without_credential() -> None:
    """
    尚未登录时返回 pending。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    adapter._sessions["session-1"] = _session(  # pylint: disable=protected-access
        helper=_FakeHelper(result={}),
    )

    assert adapter.poll("session-1").status == "pending"


def test_poll_returns_success_with_credential() -> None:
    """
    读到凭据时返回 success。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    adapter._sessions["session-1"] = _session(  # pylint: disable=protected-access
        helper=_FakeHelper(result={"cookie": "a=b"}),
    )

    result = adapter.poll("session-1")

    assert result == LoginResult(status="success", credential={"cookie": "a=b"}, message="登录成功")


def test_read_credential_variants() -> None:
    """
    迅雷/光鸭需要 localStorage 字段，其他平台只取 Cookie。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    state = _FakeState()
    monkeypatch_platforms = NetdiskPlatform

    adapter._xunlei_fields = lambda state: {"access_token": "t"}  # type: ignore[method-assign]
    adapter._guangya_fields = lambda state: {"token": "g"}  # type: ignore[method-assign]

    assert adapter._read_credential(state, monkeypatch_platforms.XUNLEI) == {"access_token": "t"}  # pylint: disable=protected-access
    assert adapter._read_credential(state, monkeypatch_platforms.GUANGYA) == {"token": "g"}  # pylint: disable=protected-access
    assert adapter._read_credential(state, monkeypatch_platforms.QUARK) == {}  # pylint: disable=protected-access

    adapter._xunlei_fields = lambda state: {}  # type: ignore[method-assign]
    adapter._guangya_fields = lambda state: {}  # type: ignore[method-assign]
    assert adapter._read_credential(state, monkeypatch_platforms.XUNLEI) == {}  # pylint: disable=protected-access
    assert adapter._read_credential(state, monkeypatch_platforms.GUANGYA) == {}  # pylint: disable=protected-access

    adapter._read_cookie = lambda state, platform, session=None: "cookie=1"  # type: ignore[method-assign]
    adapter._xunlei_fields = lambda state: {"access_token": "t"}  # type: ignore[method-assign]
    adapter._guangya_fields = lambda state: {"token": "g"}  # type: ignore[method-assign]
    assert adapter._read_credential(state, monkeypatch_platforms.XUNLEI) == {  # pylint: disable=protected-access
        "cookie": "cookie=1",
        "access_token": "t",
    }
    assert adapter._read_credential(state, monkeypatch_platforms.GUANGYA) == {  # pylint: disable=protected-access
        "cookie": "cookie=1",
        "token": "g",
    }
    assert adapter._read_credential(state, monkeypatch_platforms.UC) == {"cookie": "cookie=1"}  # pylint: disable=protected-access


def test_page_for_host_skips_unreadable_pages() -> None:
    """
    上下文不可读时返回 None，页面地址不可读时跳过。
    """
    adapter = browser_module.BrowserQrLoginAdapter()

    assert adapter._page_for_host(_FakeState(pages_error=True), "xunlei.com") is None  # pylint: disable=protected-access

    broken = _FakePage(url_error=True)
    match = _FakePage(url="https://pan.xunlei.com/login")
    state = _FakeState([broken, match])

    assert adapter._page_for_host(state, "xunlei.com") is match  # pylint: disable=protected-access
    assert adapter._page_for_host(state, "missing.com") is None  # pylint: disable=protected-access


def test_xunlei_fields_reads_local_storage() -> None:
    """
    迅雷凭据包含 access_token/deviceid 及可选字段。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    payload = {
        "access_token": "at",
        "deviceid": "dev",
        "refresh_token": "rt",
        "expires_in": "3600",
        "captcha": "cap",
        "captcha_expires_at": "later",
        "nickname": "迅雷用户",
    }

    assert adapter._xunlei_fields(_FakeState([_FakePage(url="https://pan.xunlei.com/")])) == {}  # pylint: disable=protected-access

    page = _FakePage(url="https://pan.xunlei.com/", evaluate_result=json.dumps(payload))
    assert adapter._xunlei_fields(_FakeState([page])) == {  # pylint: disable=protected-access
        "access_token": "at",
        "deviceid": "dev",
        "refresh_token": "rt",
        "expires_in": "3600",
        "captcha": "cap",
        "captcha_expires_at": "later",
        "nickname": "迅雷用户",
    }


@pytest.mark.parametrize(
    "raw",
    [None, "", "{not-json", json.dumps([1, 2]), json.dumps({"access_token": "at"})],
)
def test_xunlei_fields_rejects_unusable_payloads(raw: Any) -> None:
    """
    空值、非 JSON、非对象与缺少 deviceid 时返回空字典。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage(url="https://pan.xunlei.com/", evaluate_result=raw)

    assert adapter._xunlei_fields(_FakeState([page])) == {}  # pylint: disable=protected-access


def test_xunlei_fields_swallows_evaluate_error() -> None:
    """
    localStorage 读取失败时返回空字典。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage(url="https://pan.xunlei.com/", evaluate_error=True)

    assert adapter._xunlei_fields(_FakeState([page])) == {}  # pylint: disable=protected-access


def test_guangya_fields_reads_local_storage() -> None:
    """
    光鸭凭据读取 token、refresh_token 与 expires_at。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage(
        url="https://www.guangyapan.com/",
        evaluate_result=json.dumps({"token": "t", "refresh_token": "rt", "expires_at": "later"}),
    )

    assert adapter._guangya_fields(_FakeState([page])) == {  # pylint: disable=protected-access
        "token": "t",
        "refresh_token": "rt",
        "expires_at": "later",
    }
    assert adapter._guangya_fields(_FakeState([_FakePage(url="https://www.guangyapan.com/")])) == {}  # pylint: disable=protected-access


@pytest.mark.parametrize(
    "raw",
    [None, "", "{not-json", json.dumps([1]), json.dumps({"token": ""})],
)
def test_guangya_fields_rejects_unusable_payloads(raw: Any) -> None:
    """
    空值、非 JSON、非对象与缺少 token 时返回空字典。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage(url="https://www.guangyapan.com/", evaluate_result=raw)

    assert adapter._guangya_fields(_FakeState([page])) == {}  # pylint: disable=protected-access


def test_guangya_fields_swallows_evaluate_error() -> None:
    """
    读取失败时返回空字典。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage(url="https://www.guangyapan.com/", evaluate_error=True)

    assert adapter._guangya_fields(_FakeState([page])) == {}  # pylint: disable=protected-access


def test_current_page_prefers_last_page_and_falls_back() -> None:
    """
    优先返回最后一个页面，上下文不可读时退回活动页。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    first = _FakePage(url="https://a.com")
    last = _FakePage(url="https://b.com")
    active = _FakePage(url="https://c.com")

    assert adapter._current_page(_FakeState([first, last], active_page=active)) is last  # pylint: disable=protected-access
    assert adapter._current_page(_FakeState(pages_error=True, active_page=active)) is active  # pylint: disable=protected-access
    assert adapter._current_page(_FakeState([], active_page=active)) is active  # pylint: disable=protected-access


def test_display_page_prefers_live_stream_page() -> None:
    """
    实时画面绑定的页面仍可用时直接返回；失效时退回当前页面。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    live = _FakePage(url="https://live.com")
    adapter._stream_pages["session-1"] = live  # pylint: disable=protected-access

    assert adapter._display_page("session-1", _FakeState()) is live  # pylint: disable=protected-access

    adapter._stream_pages["session-1"] = _FakePage(closed_error=True)  # pylint: disable=protected-access
    fallback = _FakePage(url="https://fallback.com")

    assert adapter._display_page("session-1", _FakeState([fallback])) is fallback  # pylint: disable=protected-access

    adapter._stream_pages["session-1"] = _FakePage(closed=True)  # pylint: disable=protected-access
    assert adapter._display_page("session-1", _FakeState([fallback])) is fallback  # pylint: disable=protected-access


def test_start_stream_forwards_frames_and_switches_pages() -> None:
    """
    启动 screencast 后转发帧，并在新页面出现时切换画面。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage()
    context = _FakeContext([page])
    state = SimpleNamespace(context=context, active_page=page)
    session = _session(helper=_FakeHelper())
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    adapter._sessions["session-1"] = session  # pylint: disable=protected-access
    frames: list[tuple[bytes, dict[str, Any]]] = []

    adapter.start_stream("session-1", lambda data, meta: frames.append((data, meta)))

    callback = page.screencast.callback
    callback({"data": base64.b64encode(b"jpg").decode(), "viewportWidth": 800, "viewportHeight": 600})
    callback({"data": b"raw"})

    assert frames[0] == (b"jpg", {"deviceWidth": 800, "deviceHeight": 600})
    assert frames[1] == (b"raw", {"deviceWidth": 1280, "deviceHeight": 720})

    new_page = _FakePage(url="https://new.example.com")
    context.listeners["page"](new_page)

    assert adapter._stream_pages["session-1"] is new_page  # pylint: disable=protected-access
    assert new_page.screencast.started == 1


def test_start_stream_keeps_old_stream_when_new_page_fails() -> None:
    """
    新页面启动失败时保留旧画面，且旧页面停止失败不影响切换。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage()
    page.screencast = _FakeScreencast(stop_error=True)
    context = _FakeContext([page])
    state = SimpleNamespace(context=context, active_page=page)
    session = _session(helper=_FakeHelper())
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    adapter._sessions["session-1"] = session  # pylint: disable=protected-access

    adapter.start_stream("session-1", lambda data, meta: None)

    broken = _FakePage(url="https://broken.example.com")
    broken.screencast = _FakeScreencast(start_error=True)
    context.listeners["page"](broken)
    context.listeners["page"](broken)

    assert adapter._stream_pages["session-1"] is broken  # pylint: disable=protected-access


def test_stop_stream_removes_listener_and_ignores_failures() -> None:
    """
    停止画面推送时移除监听器；页面停止失败只记录。
    """
    adapter = browser_module.BrowserQrLoginAdapter()

    adapter.stop_stream("missing")

    page = _FakePage()
    page.screencast = _FakeScreencast(stop_error=True)
    context = _FakeContext([page])
    context.remove_error = True
    adapter._sessions["session-1"] = _session()  # pylint: disable=protected-access
    adapter._stream_pages["session-1"] = page  # pylint: disable=protected-access
    adapter._stream_callbacks["session-1"] = lambda data, meta: None  # pylint: disable=protected-access
    adapter._page_listeners["session-1"] = (context, lambda page: None)  # pylint: disable=protected-access

    adapter.stop_stream("session-1")

    assert "session-1" not in adapter._stream_pages  # pylint: disable=protected-access


def test_focus_main_page_only_for_baidu_sessions(monkeypatch) -> None:
    """
    只有百度会话会切回主页面。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    called: list[Any] = []
    monkeypatch.setattr(adapter, "_focus_main_page", lambda session, close_popups=False: called.append(session))

    adapter.focus_main_page("missing")
    adapter._sessions["session-1"] = _session(NetdiskPlatform.QUARK)  # pylint: disable=protected-access
    adapter.focus_main_page("session-1")

    assert called == []

    adapter._sessions["session-1"] = _session(NetdiskPlatform.BAIDU)  # pylint: disable=protected-access
    adapter.focus_main_page("session-1")

    assert len(called) == 1


def test_focus_main_page_closes_auth_popups_and_switches_stream() -> None:
    """
    切回主页面时关闭明确的登录弹窗、置前并切换画面。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    main = _FakePage(url="https://pan.baidu.com/")
    popup = _FakePage(url="https://open.weixin.qq.com/connect")
    session = _session(NetdiskPlatform.BAIDU)
    session.main_page = main
    context = _FakeContext([main, popup])
    state = SimpleNamespace(context=context, active_page=main)
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    old = _FakePage()
    adapter._stream_pages["session-1"] = old  # pylint: disable=protected-access
    adapter._stream_callbacks["session-1"] = lambda data, meta: None  # pylint: disable=protected-access

    adapter._focus_main_page(session, close_popups=True)  # pylint: disable=protected-access

    assert popup.closed_calls == 1
    assert main.brought_to_front == 1
    assert adapter._stream_pages["session-1"] is main  # pylint: disable=protected-access


def test_focus_main_page_survives_browser_errors() -> None:
    """
    页面列表不可读或浏览器会话异常时保持现状。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    session = _session(NetdiskPlatform.BAIDU)
    session.helper.with_session = lambda key, action, timeout: action(SimpleNamespace(context=_FakeContext([], pages_error=True)))  # type: ignore[method-assign]

    adapter._focus_main_page(session)  # pylint: disable=protected-access

    session.helper.with_session = _FakeHelper(error=RuntimeError("closed")).with_session  # type: ignore[method-assign]
    adapter._focus_main_page(session)  # pylint: disable=protected-access


def test_focus_main_page_returns_when_no_page_matches() -> None:
    """
    页面列表为空或无法定位主页面时不做任何切换。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    session = _session(NetdiskPlatform.BAIDU)
    state = SimpleNamespace(context=_FakeContext([]), active_page=None)
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]

    adapter._focus_main_page(session)  # pylint: disable=protected-access

    main = _FakePage(url="https://pan.baidu.com/")
    state = SimpleNamespace(context=_FakeContext([main]), active_page=main)
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    adapter._main_page = lambda session, pages: None  # type: ignore[method-assign]

    adapter._focus_main_page(session)  # pylint: disable=protected-access


def test_main_page_falls_back_through_candidates() -> None:
    """
    主页面失效时按主域名、非弹窗页面、首页面依次回退。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    session = _session(NetdiskPlatform.BAIDU)
    closed_main = _FakePage(url="https://example.org/old", closed=True)
    session.main_page = closed_main
    primary = _FakePage(url="https://pan.baidu.com/list")
    other = _FakePage(url="https://example.com/")

    assert adapter._main_page(session, [closed_main, other, primary]) is primary  # pylint: disable=protected-access

    session.main_page = _FakePage(url_error=True)
    assert adapter._main_page(session, [other]) is other  # pylint: disable=protected-access

    popup = _FakePage(url="https://open.weixin.qq.com/x")
    assert adapter._main_page(session, [popup]) is popup  # pylint: disable=protected-access


def test_is_auth_popup_recognises_login_hosts() -> None:
    """
    只把微信/百度登录域名视为弹窗，百度域名需带 opener。
    """
    assert browser_module.BrowserQrLoginAdapter._is_auth_popup(_FakePage(url="")) is False  # pylint: disable=protected-access
    assert browser_module.BrowserQrLoginAdapter._is_auth_popup(_FakePage(url="https://login.weixin.qq.com/x")) is True  # pylint: disable=protected-access

    class _OpenerErrorPage(_FakePage):
        """
        opener 不可读的百度页面。
        """

        def opener(self) -> Any:
            """
            模拟 opener 读取失败。

            :return: 不会返回
            """
            raise RuntimeError("opener unavailable")

    assert browser_module.BrowserQrLoginAdapter._is_auth_popup(_OpenerErrorPage(url="https://passport.baidu.com/x")) is False  # pylint: disable=protected-access

    opener_page = _FakePage(url="https://passport.baidu.com/x")
    opener_page.opener = lambda: object()  # type: ignore[method-assign]

    assert browser_module.BrowserQrLoginAdapter._is_auth_popup(opener_page) is True  # pylint: disable=protected-access


def test_switch_stream_guards_and_swallows_failures() -> None:
    """
    无回调或页面未变化时不切换；启动失败只记录。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage()

    adapter._switch_stream("session-1", page)  # pylint: disable=protected-access

    adapter._stream_callbacks["session-1"] = lambda data, meta: None  # pylint: disable=protected-access
    adapter._stream_pages["session-1"] = page  # pylint: disable=protected-access
    adapter._switch_stream("session-1", page)  # pylint: disable=protected-access
    assert page.screencast.started == 0

    old = _FakePage()
    old.screencast = _FakeScreencast(stop_error=True)
    adapter._stream_pages["session-1"] = old  # pylint: disable=protected-access
    broken = _FakePage()
    broken.screencast = _FakeScreencast(start_error=True)

    adapter._switch_stream("session-1", broken)  # pylint: disable=protected-access

    assert adapter._stream_pages["session-1"] is broken  # pylint: disable=protected-access


def test_send_event_dispatches_all_kinds() -> None:
    """
    鼠标、拖拽、输入、按键与滚动事件都转发到登录页。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage()
    state = SimpleNamespace(context=_FakeContext([page]), active_page=page)
    session = _session()
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    adapter._sessions["session-1"] = session  # pylint: disable=protected-access
    dragged: list[tuple[Any, ...]] = []
    adapter_module_drag = browser_module._drag

    try:
        browser_module._drag = lambda *args: dragged.append(args)  # type: ignore[assignment]
        adapter.send_event("session-1", {"kind": "mouse", "x": 1, "y": 2})
        adapter.send_event("session-1", {"kind": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4})
        adapter.send_event("session-1", {"kind": "text", "text": "hello"})
        adapter.send_event("session-1", {"kind": "key", "key": "Tab"})
        adapter.send_event("session-1", {"kind": "scroll", "delta_y": -100})
        with pytest.raises(NetdiskError):
            adapter.send_event("session-1", {"kind": "unknown"})
    finally:
        browser_module._drag = adapter_module_drag  # type: ignore[assignment]

    assert page.mouse.clicks == [(1.0, 2.0)]
    assert page.keyboard.typed == ["hello"]
    assert page.keyboard.pressed == ["Tab"]
    assert page.mouse.wheels == [(0, -100.0)]
    assert len(dragged) == 1


def test_frame_returns_screenshot_metadata() -> None:
    """
    截图接口返回图片、视口宽高与地址。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage(url="https://pan.quark.cn/login")
    state = SimpleNamespace(context=_FakeContext([page]), active_page=page)
    session = _session()
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    adapter._sessions["session-1"] = session  # pylint: disable=protected-access

    frame = adapter.frame("session-1")

    assert frame == {
        "image": b"page-jpeg",
        "width": 1280,
        "height": 720,
        "url": "https://pan.quark.cn/login",
    }

    page.viewport_size = None
    frame = adapter.frame("session-1")
    assert (frame["width"], frame["height"]) == (1440, 900)


def test_input_dispatches_all_kinds() -> None:
    """
    输入接口支持 click/drag/type/key/scroll，未知操作报错。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    page = _FakePage()
    state = SimpleNamespace(context=_FakeContext([page]), active_page=page)
    session = _session()
    session.helper.with_session = lambda key, action, timeout: action(state)  # type: ignore[method-assign]
    adapter._sessions["session-1"] = session  # pylint: disable=protected-access
    original = browser_module._drag

    try:
        browser_module._drag = lambda *args: None  # type: ignore[assignment]
        adapter.input("session-1", kind="click", x=5, y=6)
        adapter.input("session-1", kind="drag", x1=1, y1=2, x2=3, y2=4)
        adapter.input("session-1", kind="type", text="abc")
        adapter.input("session-1", kind="key", key="Enter")
        adapter.input("session-1", kind="scroll", delta_y=12)
        with pytest.raises(NetdiskError) as error:
            adapter.input("session-1", kind="tap")
    finally:
        browser_module._drag = original  # type: ignore[assignment]

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert page.mouse.clicks == [(5.0, 6.0)]
    assert page.keyboard.typed == ["abc"]
    assert page.keyboard.pressed == ["Enter"]
    assert page.mouse.wheels == [(0, 12.0)]


def test_require_session_rejects_missing_and_expired(monkeypatch) -> None:
    """
    会话不存在或过期时抛出链接过期错误。
    """
    adapter = browser_module.BrowserQrLoginAdapter()

    with pytest.raises(NetdiskError) as missing:
        adapter._require_session("missing")  # pylint: disable=protected-access

    assert missing.value.code is NetdiskErrorCode.LINK_EXPIRED

    adapter._sessions["session-1"] = _session(expires_at=0.0)  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "cancel", lambda session_id: None)

    with pytest.raises(NetdiskError) as expired:
        adapter._require_session("session-1")  # pylint: disable=protected-access

    assert expired.value.code is NetdiskErrorCode.LINK_EXPIRED


def test_cancel_ignores_unknown_session_and_close_errors(monkeypatch) -> None:
    """
    取消未知会话无副作用；关闭浏览器失败只记录。
    """
    adapter = browser_module.BrowserQrLoginAdapter()
    bridge = _FakeBridge()
    monkeypatch.setattr(browser_module, "vnc_bridge", lambda: bridge)

    adapter.cancel("missing")
    assert bridge.released == []

    adapter._sessions["session-1"] = _session()  # pylint: disable=protected-access

    def fail_close(session_key: str) -> None:
        """
        模拟关闭失败。

        :param session_key: 会话键
        """
        raise RuntimeError("close failed")

    monkeypatch.setattr(browser_module.BrowserSessionHelper, "close_session", staticmethod(fail_close))

    adapter.cancel("session-1")

    assert bridge.released == ["session-1"]
    assert "session-1" not in adapter._sessions  # pylint: disable=protected-access


def test_capture_falls_back_when_element_screenshot_fails() -> None:
    """
    元素定位/截图失败时回退整页截图。
    """
    adapter = browser_module.BrowserQrLoginAdapter()

    class _BadLocator:
        """
        定位器替身：count 抛异常或元素截图失败。
        """

        def __init__(self, mode: str) -> None:
            """
            保存失败模式。

            :param mode: `count` 或 `shot`
            """
            self._mode = mode

        def count(self) -> int:
            """
            返回匹配数量。

            :return: 1
            """
            if self._mode == "count":
                raise RuntimeError("bad selector")
            return 1

        def nth(self, index: int) -> "_BadLocator":
            """
            返回自身。

            :param index: 下标
            :return: 自身
            """
            del index
            return self

        def is_visible(self) -> bool:
            """
            元素不可见。

            :return: False
            """
            return False

        def bounding_box(self) -> dict[str, float]:
            """
            返回元素尺寸。

            :return: 尺寸
            """
            return {"width": 300.0, "height": 300.0}

        def screenshot(self) -> bytes:
            """
            模拟元素截图失败。

            :return: 不会返回
            """
            raise RuntimeError("screenshot failed")

    page = _FakePage()
    page.locator = lambda selector: _BadLocator("count") if selector == browser_module._QR_SELECTORS[0] else _BadLocator("shot")  # type: ignore[method-assign]
    page.wait_for_timeout = lambda timeout: None  # type: ignore[method-assign]
    page.screenshot = lambda **kwargs: b"full"  # type: ignore[method-assign]

    image, page_image = adapter._capture(SimpleNamespace(active_page=page), "https://x")  # pylint: disable=protected-access

    assert (image, page_image) == (b"full", b"full")
