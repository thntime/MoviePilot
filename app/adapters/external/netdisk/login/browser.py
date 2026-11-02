"""
服务端无头浏览器扫码登录。

复用宿主已有的 `BrowserSessionHelper`：打开平台登录页、截图二维码、轮询 Cookie，
登录成功后把 Cookie 头交给用例层校验并落库。
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, cast

from app.adapters.network.browser import BrowserSessionHelper, _BrowserSessionState
from app.domain.netdisk import LoginResult, LoginSession
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform
from app.runtime.log import logger

# 平台 Cookie 域名后缀，用于从浏览器上下文筛出登录态。
_COOKIE_DOMAINS: dict[NetdiskPlatform, tuple[str, ...]] = {
    NetdiskPlatform.QUARK: (".quark.cn",),
    NetdiskPlatform.UC: (".uc.cn",),
    NetdiskPlatform.PAN115: (".115.com", ".115cdn.com"),
    NetdiskPlatform.BAIDU: (".baidu.com",),
    NetdiskPlatform.C139: (".139.com", ".10086.cn"),
    NetdiskPlatform.PAN123: (".123pan.com", ".123952.com"),
    NetdiskPlatform.GUANGYA: (".guangyapan.com",),
    NetdiskPlatform.XUNLEI: (".xunlei.com",),
}

# 扫码会话有效期，超时后关闭浏览器。
_SESSION_TTL_SECONDS = 300
# 页面加载等待上限。
_PAGE_TIMEOUT_SECONDS = 45
# 二维码元素选择器：优先截取元素本身，避免整页截图把二维码缩得过小。
_QR_SELECTORS = (
    "img[src*='qrcode']",
    "img[src*='qr']",
    "canvas",
    "[class*='qrcode']",
    "[class*='qr-code']",
    "[class*='qrCode']",
    "[id*='qrcode']",
    "[id*='qr-code']",
)
# 二维码元素的最小边长，过滤页面上的装饰性 canvas。
_QR_MIN_SIZE = 120


@dataclass(slots=True)
class _QrSession:
    """
    一个扫码登录会话。

    :param session_id: 会话标识
    :param session_key: 浏览器会话键
    :param platform: 平台标识
    :param created_at: 创建时间
    :param expires_at: 过期时间
    """

    session_id: str
    session_key: str
    platform: NetdiskPlatform
    created_at: float
    expires_at: float
    post_login_navigated: bool = False


class BrowserQrLoginAdapter:
    """
    使用无头浏览器完成扫码登录的端口实现。
    """

    def __init__(self) -> None:
        """
        初始化会话表与浏览器辅助类。
        """
        self._helper = BrowserSessionHelper(
            headless=True,
            viewport={"width": 1440, "height": 900},
        )
        self._sessions: dict[str, _QrSession] = {}
        self._lock = threading.RLock()

    def start(
        self,
        platform: NetdiskPlatform,
        login_url: str,
        account_id: Optional[int] = None,
    ) -> LoginSession:
        """
        打开登录页并返回二维码截图。

        :param platform: 平台标识
        :param login_url: 平台登录页地址
        :param account_id: 关联账号主键
        :return: 登录会话
        """
        session_id = uuid.uuid4().hex
        session_key = f"netdisk-qr-{session_id}"
        try:
            image = self._helper.with_session(
                session_key,
                lambda session: self._capture(session, login_url),
                timeout=_PAGE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 浏览器启动失败统一归类
            logger.warning(f"网盘扫码登录启动失败：platform={platform.value} error={type(error).__name__}")
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "无法启动登录浏览器，请改用粘贴 Cookie",
                platform=platform,
            ) from error
        now = time.monotonic()
        session = _QrSession(
            session_id=session_id,
            session_key=session_key,
            platform=platform,
            created_at=now,
            expires_at=now + _SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._sessions[session_id] = session
        return LoginSession(
            session_id=session_id,
            platform=platform,
            image=base64.b64encode(image),
            status="pending",
            message="请使用对应 App 扫码登录",
            expires_at=datetime.now() + timedelta(seconds=_SESSION_TTL_SECONDS),
        )

    def poll(self, session_id: str) -> LoginResult:
        """
        读取浏览器 Cookie 并判断登录是否完成。

        :param session_id: 会话标识
        :return: 轮询结果
        """
        session = self._sessions.get(session_id)
        if session is None:
            return LoginResult(status="expired", message="登录会话不存在或已过期")
        if time.monotonic() >= session.expires_at:
            self.cancel(session_id)
            return LoginResult(status="expired", message="二维码已过期，请重新获取")
        try:
            cookie = self._helper.with_session(
                session.session_key,
                lambda state: self._read_cookie(state, session.platform, session),
                timeout=_PAGE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 浏览器读取失败统一归类
            logger.warning(f"网盘扫码轮询失败：platform={session.platform.value} error={type(error).__name__}")
            return LoginResult(status="pending", message="正在等待登录")
        if not cookie:
            return LoginResult(status="pending", message="正在等待登录")
        return LoginResult(status="success", credential={"cookie": cookie}, message="登录成功")

    def cancel(self, session_id: str) -> None:
        """
        关闭浏览器会话。

        :param session_id: 会话标识
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            BrowserSessionHelper.close_session(session.session_key)
        except Exception as error:  # noqa: BLE001 - 关闭失败只记录
            logger.warning(f"关闭网盘登录浏览器失败：error={type(error).__name__}")

    def _capture(self, state: _BrowserSessionState, login_url: str) -> bytes:
        """
        在浏览器会话中打开登录页并截图。

        :param state: 浏览器会话状态
        :param login_url: 登录页地址
        :return: PNG 截图字节
        """
        page = state.active_page
        # BrowserPage 协议只声明常用方法，二维码元素定位依赖 Playwright 的扩展 API。
        raw = cast(Any, page)
        page.goto(login_url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT_SECONDS * 1000)
        try:
            # 部分登录页存在长连接，networkidle 可能永远不触发；等待失败也继续截图。
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - 页面已可用时继续
            pass
        # 二维码通常由前端脚本异步渲染，等待一小段时间再查找元素。
        raw.wait_for_timeout(2000)
        # UC/微信等登录二维码在 iframe 中，必须逐个 frame 查找。
        frames = list(getattr(raw, "frames", None) or [page])
        for frame in frames:
            for selector in _QR_SELECTORS:
                try:
                    locator = frame.locator(selector)
                    count = min(locator.count(), 3)
                except Exception:  # noqa: BLE001 - 选择器不适用时继续尝试下一个
                    continue
                for index in range(count):
                    try:
                        item = locator.nth(index)
                        if not item.is_visible():
                            continue
                        box = item.bounding_box()
                        if not box or box["width"] < _QR_MIN_SIZE or box["height"] < _QR_MIN_SIZE:
                            continue
                        return cast(bytes, item.screenshot())
                    except Exception:  # noqa: BLE001 - 元素截图失败时回退整页
                        continue
        return page.screenshot()

    def _read_cookie(
        self,
        state: _BrowserSessionState,
        platform: NetdiskPlatform,
        session: Optional[_QrSession] = None,
    ) -> str:
        """
        从浏览器上下文筛出平台 Cookie 并拼成请求头。

        :param state: 浏览器会话状态
        :param platform: 平台标识
        :param session: 扫码会话，用于记录登录后跳转
        :return: Cookie 头；未登录时为空串
        """
        cookie = self._cookie_header(state, platform)
        # 夸克扫码成功后需要进入网盘页才会下发 __puus，停留在登录页会缺字段。
        if (
            session is not None
            and not session.post_login_navigated
            and platform is NetdiskPlatform.QUARK
            and "b-user-id=" in cookie
            and "__puus=" not in cookie
        ):
            session.post_login_navigated = True
            try:
                page = cast(Any, state.active_page)
                page.goto("https://pan.quark.cn/list", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
            except Exception as error:  # noqa: BLE001 - 跳转失败时保留现有 Cookie
                logger.warning(f"夸克登录后跳转失败：error={type(error).__name__}")
            cookie = self._cookie_header(state, platform)
        return cookie

    @staticmethod
    def _cookie_header(state: _BrowserSessionState, platform: NetdiskPlatform) -> str:
        """
        按平台域名过滤并拼接 Cookie 头。

        :param state: 浏览器会话状态
        :param platform: 平台标识
        :return: Cookie 头
        """
        suffixes = _COOKIE_DOMAINS.get(platform, ())
        pairs: list[str] = []
        seen: set[str] = set()
        for cookie in state.context.cookies():
            domain = str(cookie.get("domain", "")).lower()
            name = str(cookie.get("name", "")).strip()
            if not name or name in seen:
                continue
            if suffixes and not any(domain.endswith(suffix) or domain == suffix.lstrip(".") for suffix in suffixes):
                continue
            seen.add(name)
            pairs.append(f"{name}={cookie.get('value', '')}")
        return "; ".join(pairs)
