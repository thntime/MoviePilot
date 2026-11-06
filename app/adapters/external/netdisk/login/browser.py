"""
服务端无头浏览器扫码登录。

复用宿主已有的 `BrowserSessionHelper`：打开平台登录页、截图二维码、轮询 Cookie，
登录成功后把 Cookie 头交给用例层校验并落库。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional, cast
from urllib.parse import urlsplit

from app.adapters.external.netdisk.login.vnc import vnc_bridge
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
    NetdiskPlatform.PAN139: (".139.com", ".10086.cn"),
    NetdiskPlatform.PAN123: (".123pan.com", ".123952.com"),
    NetdiskPlatform.GUANGYA: (".guangyapan.com",),
    NetdiskPlatform.XUNLEI: (".xunlei.com",),
}

# 各平台网盘主页域名，用于登录后把画面切回主页面。
_PRIMARY_HOSTS: dict[NetdiskPlatform, str] = {
    NetdiskPlatform.BAIDU: "pan.baidu.com",
}

# 明确的登录弹窗域名：微信扫码与百度登录。
_AUTH_POPUP_HOSTS: tuple[str, ...] = ("open.weixin.qq.com", "login.weixin.qq.com")
_BAIDU_AUTH_HOSTS: tuple[str, ...] = ("passport.baidu.com", "wappass.baidu.com")

# 扫码会话有效期，超时后关闭浏览器。
_SESSION_TTL_SECONDS = 900
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

# 迅雷网页登录把凭据写在 localStorage，而不是 Cookie。
_XUNLEI_CLIENT_ID = "Xqp0kJBXWhwaTpB6"
_XUNLEI_STORAGE_KEY = f"credentials_{_XUNLEI_CLIENT_ID}"
_XUNLEI_CAPTCHA_KEY = f"captcha_{_XUNLEI_CLIENT_ID}"
_XUNLEI_READ_JS = f"""
(function(){{
  try {{
    var prefix = '{_XUNLEI_STORAGE_KEY}';
    var sub = localStorage.getItem('current_sub');
    var raw = sub ? localStorage.getItem(prefix + '@' + sub) : localStorage.getItem(prefix);
    if (!raw) return '';
    var data = JSON.parse(raw);
    if (!data || typeof data !== 'object' || Array.isArray(data)) return '';
    if (sub && data.sub && String(data.sub) !== sub) return '';
    var captcha = '';
    var captchaExpires = '';
    try {{
      var c = JSON.parse(localStorage.getItem('{_XUNLEI_CAPTCHA_KEY}') || '{{}}');
      if (c && c.token && Date.parse(c.expires_at) > Date.now()) {{
        captcha = c.token;
        captchaExpires = c.expires_at || '';
      }}
    }} catch (e) {{}}
    var device = '';
    try {{
      var all = document.cookie.split(';');
      for (var i = 0; i < all.length; i++) {{
        var s = all[i].trim();
        if (s.indexOf('deviceid=') !== 0) continue;
        var d = decodeURIComponent(s.slice(9));
        device = (d.indexOf('.') > -1 && d.length > 32) ? d.split('.')[1].slice(0, 32) : d;
        break;
      }}
    }} catch (e) {{}}
    var pageNick = '';
    try {{
      var store = window.$nuxt && window.$nuxt.$store && window.$nuxt.$store.state;
      var cur = store && store.users && store.users.curUser;
      pageNick = (cur && (cur.nick_name || cur.nickname)) || '';
    }} catch (e) {{}}
    return JSON.stringify({{
      access_token: data.access_token || data.accessToken || '',
      refresh_token: data.refresh_token || data.refreshToken || '',
      expires_in: data.expires_in || data.expiresIn || '',
      deviceid: data.device_id || data.deviceId || device,
      captcha: captcha || data.captcha_token || data.captchaToken || '',
      captcha_expires_at: captchaExpires,
      nickname: pageNick || data.nickname || data.nick_name || data.nickName || data.user_name || data.userName || data.name || ''
    }});
  }} catch (e) {{
    return '';
  }}
}})()
"""

# 光鸭网页登录使用与迅雷相同的 @xbase 凭据存储：localStorage 的
# `credentials_<client_id>` 里保存 access_token/refresh_token，而不是 Cookie。
_GUANGYA_CLIENT_ID = "aMe-8VSlkrbQXpUR"
_GUANGYA_STORAGE_KEY = f"credentials_{_GUANGYA_CLIENT_ID}"
_GUANGYA_READ_JS = f"""
(function(){{
  try {{
    var prefix = '{_GUANGYA_STORAGE_KEY}';
    var sub = localStorage.getItem('current_sub');
    var raw = sub ? (localStorage.getItem(prefix + '@' + sub) || localStorage.getItem(prefix)) : localStorage.getItem(prefix);
    if (!raw) {{
      // client_id 变更时兜底扫描：同源只会有光鸭自己的凭据。
      for (var i = 0; i < localStorage.length; i++) {{
        var key = localStorage.key(i) || '';
        if (key.indexOf('credentials_') !== 0) continue;
        var candidate = localStorage.getItem(key);
        if (candidate && candidate.indexOf('access_token') > -1) {{ raw = candidate; break; }}
      }}
    }}
    if (!raw) return '';
    var data = JSON.parse(raw);
    if (!data || typeof data !== 'object' || Array.isArray(data)) return '';
    var token = data.access_token || data.accessToken || '';
    if (!token) return '';
    return JSON.stringify({{
      token: token,
      refresh_token: data.refresh_token || data.refreshToken || '',
      expires_at: data.expires_at || '',
      sub: data.sub || ''
    }});
  }} catch (e) {{
    return '';
  }}
}})()
"""

# 139 滑块验证码的拖动句柄；前端坐标经过截图缩放，命中点可能落在句柄边缘，
# 拖拽前优先用真实元素位置校正起点。
_SLIDER_HANDLE_SELECTORS = (
    ".slide-block",
    ".slide-block-wrapper",
    "[class*='slide-block']",
    "[class*='slider-handle']",
    "[class*='drag-handle']",
)


def _safe_url(url: Any) -> str:
    """
    日志用的脱敏地址：只保留 scheme、host 与 path。

    :param url: 原始地址
    :return: 脱敏地址
    """
    text = str(url or "")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.split("?")[0].split("#")[0][:160]
    if not parsed.scheme or not parsed.netloc:
        return text.split("?")[0].split("#")[0][:160]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:160]


def _candidate_score(candidate: Mapping[str, Any], x: float, y: float) -> tuple[float, ...]:
    """
    给滑块候选排序：优先包含用户原始点、可命中、非宽轨道，再取面积更小、距离更近的。

    :param candidate: 候选描述
    :param x: 用户拖拽起点横坐标
    :param y: 用户拖拽起点纵坐标
    :return: 排序键
    """
    box = candidate["box"]
    return (
        0 if candidate["top_ok"] else 1,
        0 if candidate["contains"] else 1,
        1 if candidate["wide"] else 0,
        float(box["width"]) * float(box["height"]),
        abs(float(box["x"]) - x) + abs(float(box["y"]) - y),
    )


def _slider_candidate(page: Any, x: float, y: float) -> Optional[dict[str, Any]]:
    """
    在所有 frame 中查找最可能接收拖拽的滑块句柄。

    139 登录页可能同时存在多个验证码容器，或把验证码放在 iframe 内；
    这里遍历全部 frame，并优先选择包含用户原始点、未被遮挡的窄句柄。

    :param page: Playwright 页面
    :param x: 用户拖拽起点横坐标
    :param y: 用户拖拽起点纵坐标
    :return: 候选描述；找不到时返回 None
    """
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001 - 页面列表不可读时退回主页面
        frames = []
    if not frames:
        frames = [page]
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for frame in frames:
        for selector in _SLIDER_HANDLE_SELECTORS:
            try:
                locator = frame.locator(selector)
                count = min(locator.count(), 5)
            except Exception:  # noqa: BLE001 - 选择器不适用时继续
                continue
            for index in range(count):
                try:
                    item = locator.nth(index)
                    if not item.is_visible():
                        continue
                    box = item.bounding_box()
                except Exception:  # noqa: BLE001 - 元素可能已卸载
                    continue
                if not box:
                    continue
                width = float(box.get("width") or 0)
                height = float(box.get("height") or 0)
                if width <= 0 or height <= 0:
                    continue
                key = (round(float(box["x"])), round(float(box["y"])), round(width), round(height))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    top_ok = bool(
                        item.evaluate(
                            """el => {
                              const r = el.getBoundingClientRect();
                              const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                              return !!hit && (hit === el || el.contains(hit));
                            }"""
                        )
                    )
                except Exception:  # noqa: BLE001 - 遮挡检查失败时按不可命中处理
                    top_ok = False
                contains = (
                    float(box["x"]) <= x <= float(box["x"]) + width
                    and float(box["y"]) <= y <= float(box["y"]) + height
                )
                candidates.append(
                    {
                        "frame": frame,
                        "locator": item,
                        "selector": selector,
                        "index": index,
                        "box": {key: float(value) for key, value in box.items()},
                        "contains": contains,
                        "top_ok": top_ok,
                        "wide": width > 120,
                        "area": width * height,
                    }
                )
    if not candidates:
        return None
    return min(candidates, key=lambda item: _candidate_score(item, x, y))


def _slider_state(page: Any, frame: Any = None) -> dict[str, Optional[float]]:
    """
    读取拼图块与滑块句柄的横向位置，用于判断拖拽是否真的生效。

    :param page: Playwright 页面
    :param frame: 优先读取的 frame；为空时按页面顺序查找
    :return: `puzzle` 与 `handle` 的横向位置；缺失时为 None
    """
    state: dict[str, Optional[float]] = {"puzzle": None, "handle": None}
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001 - 页面列表不可读时退回主页面
        frames = []
    if frame is not None:
        frames = [frame] + [item for item in frames if item is not frame]
    if not frames:
        frames = [page]
    for frame in frames:
        try:
            values = frame.evaluate(
                """() => {
                  const puzzle = document.querySelector('.verify-puzzle');
                  const handle = document.querySelector('.slide-block-wrapper')
                    || document.querySelector('.slide-block');
                  const read = (el) => {
                    if (!el) return null;
                    const style = getComputedStyle(el);
                    if (style.transform && style.transform !== 'none') {
                      try { return new DOMMatrixReadOnly(style.transform).m41; } catch (e) {}
                    }
                    return el.getBoundingClientRect().left;
                  };
                  return {puzzle: read(puzzle), handle: read(handle)};
                }"""
            )
        except Exception:  # noqa: BLE001 - 页面跳转中读取失败时继续
            continue
        if not isinstance(values, Mapping):
            continue
        for key in ("puzzle", "handle"):
            value = values.get(key)
            if value is not None:
                state[key] = float(value)
        if state["puzzle"] is not None or state["handle"] is not None:
            return state
    return state


def _state_moved(before: Mapping[str, Optional[float]], after: Mapping[str, Optional[float]]) -> bool:
    """
    判断两次采样之间拼图块或句柄是否发生了水平位移。

    :param before: 拖拽前状态
    :param after: 拖拽后状态
    :return: 是否移动
    """
    for key in ("puzzle", "handle"):
        first = before.get(key)
        second = after.get(key)
        if first is not None and second is not None and abs(second - first) >= 2:
            return True
    return False


_PROBE_INSTALL_JS = """
(() => {
  const w = window;
  w.__mpDragProbe = {events: {}, targets: []};
  if (w.__mpDragProbeInstalled) return true;
  w.__mpDragProbeInstalled = true;
  const record = (type) => (event) => {
    const probe = w.__mpDragProbe;
    if (!probe) return;
    probe.events[type] = (probe.events[type] || 0) + 1;
    if (probe.targets.length >= 8) return;
    const target = event.target;
    probe.targets.push({
      type,
      tag: target && target.tagName ? target.tagName : '',
      cls: target && target.className ? String(target.className).slice(0, 80) : '',
      trusted: !!event.isTrusted,
      x: Math.round(event.clientX || 0),
      y: Math.round(event.clientY || 0),
    });
  };
  ['mousedown', 'mousemove', 'mouseup', 'touchstart', 'touchmove', 'touchend',
   'pointerdown', 'pointermove', 'pointerup'].forEach((type) => {
    document.addEventListener(type, record(type), true);
  });
  return true;
})()
"""


def _install_probe(frame: Any) -> None:
    """
    在目标 frame 上安装拖拽事件探针。

    :param frame: Playwright 页面或 frame
    """
    try:
        frame.evaluate(_PROBE_INSTALL_JS)
    except Exception:  # noqa: BLE001 - 探针失败不影响拖拽
        return


def _read_probe(frame: Any) -> Any:
    """
    读取拖拽事件探针结果。

    :param frame: Playwright 页面或 frame
    :return: 探针结果；读取失败时返回 None
    """
    try:
        return frame.evaluate("() => window.__mpDragProbe || null")
    except Exception:  # noqa: BLE001 - 探针读取失败不影响拖拽
        return None


def _mouse_drag(page: Any, x1: float, y1: float, x2: float, y2: float) -> None:
    """
    用 Playwright 鼠标事件执行一次带缓动的拖拽。

    :param page: Playwright 页面
    :param x1: 起点横坐标
    :param y1: 起点纵坐标
    :param x2: 终点横坐标
    :param y2: 终点纵坐标
    """
    page.mouse.move(x1, y1)
    page.wait_for_timeout(120)
    page.mouse.down()
    page.wait_for_timeout(90)
    steps = 40
    for step in range(1, steps + 1):
        ratio = step / steps
        # ease-out：先快后慢，更接近真人拖动
        eased = 1 - (1 - ratio) ** 2
        jitter = 1.2 if step % 7 == 0 else 0
        page.mouse.move(
            x1 + (x2 - x1) * eased + jitter,
            y1 + (y2 - y1) * eased,
        )
        page.wait_for_timeout(10 if ratio < 0.8 else 18)
    page.mouse.move(x2, y2)
    page.wait_for_timeout(180)
    page.mouse.up()


_SYNTHETIC_DRAG_JS = """
async ({selector, index, dx, dy}) => {
  const items = document.querySelectorAll(selector);
  const handle = items[index] || items[0];
  if (!handle) return {ok: false, reason: 'no-handle'};
  const rect = handle.getBoundingClientRect();
  const startX = rect.left + rect.width / 2;
  const startY = rect.top + rect.height / 2;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const emit = (target, type, x, y, buttons) => {
    target.dispatchEvent(new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: x,
      clientY: y,
      button: 0,
      buttons,
      detail: type === 'mousemove' ? 0 : 1,
    }));
  };
  emit(handle, 'mousedown', startX, startY, 1);
  await sleep(80 + Math.random() * 70);
  const steps = 28;
  for (let step = 1; step <= steps; step += 1) {
    const ratio = step / steps;
    const eased = 1 - Math.pow(1 - ratio, 3);
    const jitter = (Math.random() - 0.5) * 1.5;
    emit(document, 'mousemove', startX + dx * eased, startY + dy * eased + jitter, 1);
    await sleep(12 + Math.random() * 10);
  }
  await sleep(80 + Math.random() * 70);
  emit(document, 'mouseup', startX + dx, startY + dy, 0);
  return {ok: true, startX, startY};
}
"""


def _synthetic_drag(candidate: Mapping[str, Any], delta_x: float, delta_y: float) -> Any:
    """
    在句柄元素上派发合成鼠标事件，绕过 hit-testing 与遮挡。

    :param candidate: 滑块候选
    :param delta_x: 水平位移
    :param delta_y: 垂直位移
    :return: 脚本结果；失败时返回 None
    """
    try:
        return candidate["frame"].evaluate(
            _SYNTHETIC_DRAG_JS,
            {
                "selector": candidate["selector"],
                "index": candidate["index"],
                "dx": delta_x,
                "dy": delta_y,
            },
        )
    except Exception as error:  # noqa: BLE001 - 合成失败时保留原状
        logger.warning(f"139滑块合成拖拽失败：error={type(error).__name__}")
        return None


def _drag(
    page: Any,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    platform: Optional[NetdiskPlatform] = None,
    session_id: str = "",
) -> None:
    """
    在页面上模拟一次拖拽，供滑块验证码使用。

    139 的滑块句柄很窄，前端坐标经过截图缩放后容易落在句柄边缘；这里
    先在所有 frame 中定位真实句柄并校正起点，拖拽后读取拼图块位移做闭环
    校验。真实鼠标未生效时依次尝试原始坐标重试和合成 DOM 事件。

    :param page: Playwright 页面
    :param x1: 起点横坐标
    :param y1: 起点纵坐标
    :param x2: 终点横坐标
    :param y2: 终点纵坐标
    :param platform: 平台标识，用于启用平台专属校正
    :param session_id: 登录会话标识，仅用于日志
    """
    if platform is not NetdiskPlatform.PAN139:
        _mouse_drag(page, x1, y1, x2, y2)
        return
    delta_x = x2 - x1
    # 139 滑块只认水平位移，纵向保持在校正后的句柄中心，避免拖出句柄。
    delta_y = 0.0
    candidate = _slider_candidate(page, x1, y1)
    start_x, start_y = x1, y1
    if candidate is not None:
        box = candidate["box"]
        if candidate["wide"]:
            start_x = min(max(x1, box["x"] + 8), box["x"] + box["width"] - 8)
            start_y = min(max(y1, box["y"] + 8), box["y"] + box["height"] - 8)
        else:
            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
    before = _slider_state(page, candidate["frame"] if candidate is not None else None)
    if before["handle"] is None and candidate is not None:
        before["handle"] = candidate["box"]["x"]
    probe_frame = candidate["frame"] if candidate is not None else page
    _install_probe(probe_frame)
    if candidate is not None:
        try:
            candidate["locator"].hover(timeout=1000)
        except Exception as error:  # noqa: BLE001 - 遮挡时继续尝试真实拖拽
            logger.warning(f"139滑块 hover 失败：session={session_id[:8]} error={type(error).__name__}")
    _mouse_drag(page, start_x, start_y, start_x + delta_x, start_y + delta_y)
    page.wait_for_timeout(300)
    after = _slider_state(page, candidate["frame"] if candidate is not None else None)
    closed = after["puzzle"] is None and after["handle"] is None
    moved = closed or _state_moved(before, after)
    if not moved:
        # 用前端原始坐标重试一次，排除句柄校正本身带来的偏差。
        _mouse_drag(page, x1, y1, x2, y2)
        page.wait_for_timeout(300)
        after = _slider_state(page, candidate["frame"] if candidate is not None else None)
        moved = _state_moved(before, after)
    if not moved and candidate is not None:
        _synthetic_drag(candidate, delta_x, delta_y)
        page.wait_for_timeout(300)
        after = _slider_state(page, candidate["frame"] if candidate is not None else None)
        moved = _state_moved(before, after)
    pick_log = None
    if candidate is not None:
        pick_log = {
            "selector": candidate["selector"],
            "index": candidate["index"],
            "box": {key: round(value, 1) for key, value in candidate["box"].items()},
            "contains": candidate["contains"],
            "top_ok": candidate["top_ok"],
            "wide": candidate["wide"],
            "frame": _safe_url(getattr(candidate["frame"], "url", "")),
        }
    logger.info(
        "139滑块诊断：session=%s page=%s frames=%s pick=%s orig=(%.1f,%.1f) start=(%.1f,%.1f) "
        "before=%s after=%s moved=%s closed=%s probe=%s",
        session_id[:8],
        _safe_url(getattr(page, "url", "")),
        len(list(getattr(page, "frames", []) or [])),
        json.dumps(pick_log, ensure_ascii=False, default=str),
        x1,
        y1,
        start_x,
        start_y,
        json.dumps(before, ensure_ascii=False, default=str),
        json.dumps(after, ensure_ascii=False, default=str),
        moved,
        closed,
        json.dumps(_read_probe(probe_frame), ensure_ascii=False, default=str),
    )


def _host_of(page: Any) -> str:
    """
    读取页面 URL 的主机名。

    :param page: 页面对象
    :return: 小写主机名；不可读时返回空串
    """
    try:
        return (urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001 - URL 不可读时按未知主机处理
        return ""


@dataclass(slots=True)
class _QrSession:
    """
    一个扫码登录会话。

    :param session_id: 会话标识
    :param session_key: 浏览器会话键
    :param platform: 平台标识
    :param created_at: 创建时间
    :param expires_at: 过期时间
    :param helper: 该会话使用的浏览器辅助类
    """

    session_id: str
    session_key: str
    platform: NetdiskPlatform
    created_at: float
    expires_at: float
    helper: BrowserSessionHelper
    post_login_navigated: bool = False
    # 会话启动时记录的主页面引用，避免用 pages[0] 盲猜。
    main_page: Any = None


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
            args=["--no-proxy-server"],
        )
        # 139 使用有头浏览器 + noVNC，让用户用真实鼠标操作验证码。
        self._vnc_helper = BrowserSessionHelper(
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=[
                "--window-size=1440,900",
                "--window-position=0,0",
                "--start-maximized",
                "--no-proxy-server",
            ],
        )
        self._sessions: dict[str, _QrSession] = {}
        self._stream_pages: dict[str, Any] = {}
        self._stream_callbacks: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        self._page_listeners: dict[str, tuple[Any, Any]] = {}
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
        # 所有网页登录都使用真实有头浏览器 + noVNC，让用户用真实鼠标操作验证码。
        use_vnc = True
        helper = self._vnc_helper
        vnc_bridge().acquire(session_id)
        try:
            image, page_image = helper.with_session(
                session_key,
                lambda session: self._capture(session, login_url),
                timeout=_PAGE_TIMEOUT_SECONDS,
            )
            main_page = helper.with_session(
                session_key,
                lambda session: session.active_page,
                timeout=_PAGE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 浏览器启动失败统一归类
            vnc_bridge().release(session_id)
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
            helper=helper,
            main_page=main_page,
        )
        with self._lock:
            self._sessions[session_id] = session
        return LoginSession(
            session_id=session_id,
            platform=platform,
            image=base64.b64encode(image),
            page_image=base64.b64encode(page_image),
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
            credential = session.helper.with_session(
                session.session_key,
                lambda state: self._read_credential(state, session.platform, session),
                timeout=_PAGE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 浏览器读取失败统一归类
            logger.warning(f"网盘扫码轮询失败：platform={session.platform.value} error={type(error).__name__}")
            return LoginResult(status="pending", message="正在等待登录")
        if not credential:
            return LoginResult(status="pending", message="正在等待登录")
        # 只返回凭据；页面切换/关闭由服务层在凭据校验通过后显式触发。
        return LoginResult(status="success", credential=credential, message="登录成功")

    def _read_credential(
        self,
        state: _BrowserSessionState,
        platform: NetdiskPlatform,
        session: Optional[_QrSession] = None,
    ) -> dict[str, str]:
        """
        读取平台登录凭据；迅雷额外从 localStorage 取 token。

        :param state: 浏览器会话状态
        :param platform: 平台标识
        :param session: 扫码会话
        :return: 凭据字段；未登录时为空字典
        """
        cookie = self._read_cookie(state, platform, session)
        if platform is NetdiskPlatform.XUNLEI:
            fields = self._xunlei_fields(state)
            if not fields:
                return {}
            if cookie:
                return {"cookie": cookie, **fields}
            return fields
        if platform is NetdiskPlatform.GUANGYA:
            fields = self._guangya_fields(state)
            if not fields:
                return {}
            if cookie:
                return {"cookie": cookie, **fields}
            return fields
        if not cookie:
            return {}
        return {"cookie": cookie}

    @staticmethod
    def _page_for_host(state: _BrowserSessionState, suffix: str) -> Optional[Any]:
        """
        按域名后缀查找页面，避免在微信弹窗上读错 localStorage。

        :param state: 浏览器会话状态
        :param suffix: 域名后缀
        :return: 匹配的页面；不存在时返回 None
        """
        try:
            pages = list(cast(Any, state.context).pages)
        except Exception:  # noqa: BLE001 - 页面列表不可读时放弃
            return None
        for page in reversed(pages):
            try:
                url = str(getattr(page, "url", "") or "")
            except Exception:  # noqa: BLE001 - 页面可能已关闭
                continue
            if suffix in url:
                return page
        return None

    def _xunlei_fields(self, state: _BrowserSessionState) -> dict[str, str]:
        """
        从 pan.xunlei.com 的 localStorage 读取网页版 token。

        :param state: 浏览器会话状态
        :return: 含 access_token/deviceid/captcha/captcha_expires_at/refresh_token/expires_in 的字段；未登录时为空字典
        """
        page = self._page_for_host(state, "xunlei.com")
        if page is None:
            return {}
        try:
            raw = page.evaluate(_XUNLEI_READ_JS)
        except Exception as error:  # noqa: BLE001 - 页面跳转中读取失败时继续轮询
            logger.warning(f"读取迅雷网页凭据失败：error={type(error).__name__}")
            return {}
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, Mapping):
            return {}
        access = str(data.get("access_token") or "").strip()
        device = str(data.get("deviceid") or "").strip()
        if not access or not device:
            return {}
        fields = {"access_token": access, "deviceid": device}
        refresh = str(data.get("refresh_token") or data.get("refreshToken") or "").strip()
        if refresh:
            fields["refresh_token"] = refresh
        expires_in = str(data.get("expires_in") or data.get("expiresIn") or "").strip()
        if expires_in:
            fields["expires_in"] = expires_in
        captcha = str(data.get("captcha") or "").strip()
        if captcha:
            fields["captcha"] = captcha
            captcha_expires_at = str(data.get("captcha_expires_at") or "").strip()
            if captcha_expires_at:
                fields["captcha_expires_at"] = captcha_expires_at
        nickname = str(data.get("nickname") or "").strip()
        if nickname:
            fields["nickname"] = nickname
        return fields

    def _guangya_fields(self, state: _BrowserSessionState) -> dict[str, str]:
        """
        从 guangyapan.com 的 localStorage 读取网页版访问令牌。

        :param state: 浏览器会话状态
        :return: 含 token/refresh_token 的字段；未登录时为空字典
        """
        page = self._page_for_host(state, "guangyapan.com")
        if page is None:
            return {}
        try:
            raw = page.evaluate(_GUANGYA_READ_JS)
        except Exception as error:  # noqa: BLE001 - 页面跳转中读取失败时继续轮询
            logger.warning(f"读取光鸭网页凭据失败：error={type(error).__name__}")
            return {}
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, Mapping):
            return {}
        token = str(data.get("token") or "").strip()
        if not token:
            return {}
        fields = {"token": token}
        refresh = str(data.get("refresh_token") or "").strip()
        if refresh:
            fields["refresh_token"] = refresh
        expires_at = str(data.get("expires_at") or "").strip()
        if expires_at:
            fields["expires_at"] = expires_at
        return fields

    def _current_page(self, state: _BrowserSessionState) -> Any:
        """
        返回当前应展示的页面；弹窗/新标签页优先。

        :param state: 浏览器会话状态
        :return: Playwright 页面对象
        """
        try:
            pages = list(cast(Any, state.context).pages)
            if pages:
                return pages[-1]
        except Exception:  # noqa: BLE001 - 读取失败时回退活动页
            pass
        return state.active_page

    def _display_page(self, session_id: str, state: _BrowserSessionState) -> Any:
        """
        返回用户正在看的页面：优先使用实时画面绑定的页面。

        :param session_id: 会话标识
        :param state: 浏览器会话状态
        :return: Playwright 页面对象
        """
        page = self._stream_pages.get(session_id)
        if page is not None:
            try:
                if not page.is_closed():
                    return page
            except Exception:  # noqa: BLE001 - 页面对象失效时回退
                pass
        page = self._current_page(state)
        logger.warning(
            "网页登录画面页缺失：session=%s fallback=%s",
            session_id[:8],
            _safe_url(getattr(page, "url", "")),
        )
        return page

    def start_stream(
        self,
        session_id: str,
        on_frame: Callable[[bytes, Mapping[str, Any]], None],
    ) -> None:
        """
        启动实时画面推送。

        使用 Playwright 原生 screencast：回调返回后由驱动自动 ack，
        避免手写 CDP 时在事件回调里重入同步 API。

        :param session_id: 会话标识
        :param on_frame: 收到帧时回调（JPEG 字节与元数据）
        """
        session = self._require_session(session_id)

        def start(state: _BrowserSessionState) -> None:
            page = self._current_page(state)
            current_size = {"width": 1440, "height": 900}

            def handle_frame(frame: Mapping[str, Any]) -> None:
                data = frame.get("data") or b""
                if isinstance(data, str):
                    data = base64.b64decode(data)
                on_frame(
                    bytes(data),
                    {
                        "deviceWidth": int(frame.get("viewportWidth") or current_size["width"]),
                        "deviceHeight": int(frame.get("viewportHeight") or current_size["height"]),
                    },
                )

            def start_page(target: Any) -> None:
                """
                在指定页面启动 screencast。
                """
                size = target.viewport_size or {"width": 1440, "height": 900}
                current_size["width"] = int(size.get("width") or 1440)
                current_size["height"] = int(size.get("height") or 900)
                target.screencast.start(
                    on_frame=handle_frame,
                    quality=55,
                    size=size,
                )

            start_page(page)
            self._stream_pages[session_id] = page
            self._stream_callbacks[session_id] = handle_frame
            context = cast(Any, state.context)

            def on_new_page(new_page: Any) -> None:
                """
                弹窗/新标签页出现时，把画面切换到新页面。
                """
                old = self._stream_pages.get(session_id)
                if old is new_page:
                    return
                try:
                    if old is not None:
                        old.screencast.stop()
                except Exception:  # noqa: BLE001 - 旧页面可能已关闭
                    pass
                self._stream_pages[session_id] = new_page
                logger.info("网页登录出现新页面：session=%s url=%s", session_id[:8], str(getattr(new_page, "url", ""))[:120])
                try:
                    start_page(new_page)
                except Exception as error:  # noqa: BLE001 - 新页面启动失败保留旧流
                    logger.warning(f"切换网页登录画面失败：error={type(error).__name__}")

            context.on("page", on_new_page)
            self._page_listeners[session_id] = (context, on_new_page)

        session.helper.with_session(session.session_key, start, timeout=_PAGE_TIMEOUT_SECONDS)

    def stop_stream(self, session_id: str) -> None:
        """
        停止实时画面推送。

        :param session_id: 会话标识
        """
        session = self._sessions.get(session_id)
        page = self._stream_pages.pop(session_id, None)
        self._stream_callbacks.pop(session_id, None)
        listener = self._page_listeners.pop(session_id, None)
        if listener is not None:
            try:
                listener[0].remove_listener("page", listener[1])
            except Exception:  # noqa: BLE001 - 监听器可能已失效
                pass
        if session is None or page is None:
            return
        try:
            page.screencast.stop()
        except Exception as error:  # noqa: BLE001 - 停止失败只记录
            logger.warning(f"停止网页登录画面推送失败：error={type(error).__name__}")

    def focus_main_page(self, session_id: str, *, close_popups: bool = False) -> None:
        """
        把网页登录画面切回主页面。

        服务层在凭据校验通过后调用。默认只把主页面置前并切换实时画面，
        不关闭任何页面；`close_popups` 为真时只关闭明确的微信/百度登录弹窗，
        且永远不会关闭主页面。

        :param session_id: 会话标识
        :param close_popups: 是否关闭明确的登录弹窗
        """
        session = self._sessions.get(session_id)
        if session is None or session.platform is not NetdiskPlatform.BAIDU:
            return
        self._focus_main_page(session, close_popups=close_popups)

    def _focus_main_page(self, session: _QrSession, *, close_popups: bool = False) -> None:
        """
        把实时画面切回主页面，可选关闭登录弹窗。

        百度微信扫码会在新标签页完成，登录态确认后弹窗常停留在「请稍后」页；
        回到主页面让用户看到已登录的网盘。关闭动作只针对明确的登录域名，
        避免误关主页面或用户自己打开的标签页。

        :param session: 扫码会话
        :param close_popups: 是否关闭明确的登录弹窗
        """

        def act(state: _BrowserSessionState) -> None:
            try:
                pages = list(cast(Any, state.context).pages)
            except Exception:  # noqa: BLE001 - 页面列表不可读时保持现状
                return
            if not pages:
                return
            main = self._main_page(session, pages)
            if main is None:
                return
            if close_popups:
                for page in pages:
                    if page is main or not self._is_auth_popup(page):
                        continue
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001 - 弹窗可能已自行关闭
                        continue
            try:
                main.bring_to_front()
            except Exception:  # noqa: BLE001 - 部分页面不支持置前
                pass
            self._switch_stream(session.session_id, main)

        try:
            session.helper.with_session(session.session_key, act, timeout=_PAGE_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001 - 切页失败不影响登录结果
            logger.warning(f"切换网页登录主页面失败：error={type(error).__name__}")

    def _main_page(self, session: _QrSession, pages: list[Any]) -> Optional[Any]:
        """
        定位会话的主页面。

        优先使用启动时记录的页面引用；失效时按平台主域名回退，
        再回退到第一个非登录弹窗页面，最后才使用 `pages[0]`。

        :param session: 扫码会话
        :param pages: 当前页面列表
        :return: 主页面；无法定位时返回 None
        """
        main = session.main_page
        if main is not None:
            try:
                if not main.is_closed() and any(main is page for page in pages):
                    return main
            except Exception:  # noqa: BLE001 - 页面对象可能已失效
                pass
        primary_host = _PRIMARY_HOSTS.get(session.platform, "")
        if primary_host:
            for page in pages:
                if _host_of(page) == primary_host:
                    return page
        for page in pages:
            if not self._is_auth_popup(page):
                return page
        return pages[0]

    @staticmethod
    def _is_auth_popup(page: Any) -> bool:
        """
        判断页面是否为明确的登录弹窗。

        只认微信扫码域名与百度登录域名的弹窗；其他页面一律视为非弹窗，
        避免误关用户自己的标签页。

        :param page: 页面对象
        :return: 是否登录弹窗
        """
        host = _host_of(page)
        if not host:
            return False
        if any(host == item or host.endswith("." + item) for item in _AUTH_POPUP_HOSTS):
            return True
        if any(host == item or host.endswith("." + item) for item in _BAIDU_AUTH_HOSTS):
            try:
                return page.opener() is not None
            except Exception:  # noqa: BLE001 - opener 不可读时保守处理
                return False
        return False

    def _switch_stream(self, session_id: str, page: Any) -> None:
        """
        把实时画面切换到指定页面。

        :param session_id: 会话标识
        :param page: 目标页面
        """
        old = self._stream_pages.get(session_id)
        callback = self._stream_callbacks.get(session_id)
        if callback is None or old is page:
            return
        try:
            if old is not None:
                old.screencast.stop()
        except Exception:  # noqa: BLE001 - 旧页面可能已关闭
            pass
        self._stream_pages[session_id] = page
        try:
            size = page.viewport_size or {"width": 1440, "height": 900}
            page.screencast.start(
                on_frame=callback,
                quality=55,
                size=size,
            )
        except Exception as error:  # noqa: BLE001 - 切换失败保留旧流
            logger.warning(f"切换网页登录画面失败：error={type(error).__name__}")

    def send_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        """
        把前端事件转发到登录页。

        :param session_id: 会话标识
        :param event: 事件描述
        """
        session = self._require_session(session_id)

        def act(state: _BrowserSessionState) -> None:
            page = cast(Any, self._display_page(session_id, state))
            kind = str(event.get("kind") or "")
            if kind == "mouse":
                page.mouse.click(float(event.get("x") or 0), float(event.get("y") or 0))
            elif kind == "drag":
                _drag(
                    page,
                    float(event.get("x1") or 0),
                    float(event.get("y1") or 0),
                    float(event.get("x2") or 0),
                    float(event.get("y2") or 0),
                    session.platform,
                    session_id,
                )
            elif kind == "text":
                page.keyboard.type(str(event.get("text") or ""))
            elif kind == "key":
                page.keyboard.press(str(event.get("key") or "Enter"))
            elif kind == "scroll":
                page.mouse.wheel(0, float(event.get("delta_y") or 0))
            else:
                raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, f"不支持的页面事件：{kind}")

        logger.info(
            "网页登录事件：session=%s kind=%s x=%s y=%s x1=%s y1=%s x2=%s y2=%s",
            session_id[:8],
            event.get("kind"),
            event.get("x"),
            event.get("y"),
            event.get("x1"),
            event.get("y1"),
            event.get("x2"),
            event.get("y2"),
        )
        session.helper.with_session(session.session_key, act, timeout=_PAGE_TIMEOUT_SECONDS)

    def frame(self, session_id: str) -> dict[str, Any]:
        """
        截取当前登录页画面，供前端持续刷新。

        :param session_id: 会话标识
        :return: 图片、视口宽高与当前地址
        """
        session = self._require_session(session_id)

        def capture(state: _BrowserSessionState) -> dict[str, Any]:
            page = cast(Any, self._display_page(session_id, state))
            image = page.screenshot(type="jpeg", quality=70)
            size = page.viewport_size or {"width": 1440, "height": 900}
            return {
                "image": image,
                "width": int(size.get("width") or 1440),
                "height": int(size.get("height") or 900),
                "url": str(getattr(page, "url", "") or ""),
            }

        return cast(
            dict[str, Any],
            session.helper.with_session(session.session_key, capture, timeout=_PAGE_TIMEOUT_SECONDS),
        )

    def input(
        self,
        session_id: str,
        *,
        kind: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        x1: Optional[float] = None,
        y1: Optional[float] = None,
        x2: Optional[float] = None,
        y2: Optional[float] = None,
        text: Optional[str] = None,
        key: Optional[str] = None,
        delta_y: Optional[float] = None,
    ) -> None:
        """
        把前端点击、拖拽、输入、按键与滚动转发到登录页。

        :param session_id: 会话标识
        :param kind: 操作类型：click/drag/type/key/scroll
        :param x: 点击横坐标（页面像素）
        :param y: 点击纵坐标（页面像素）
        :param x1: 拖拽起点横坐标
        :param y1: 拖拽起点纵坐标
        :param x2: 拖拽终点横坐标
        :param y2: 拖拽终点纵坐标
        :param text: 输入文本
        :param key: 按键名
        :param delta_y: 滚动距离
        """
        session = self._require_session(session_id)

        def act(state: _BrowserSessionState) -> None:
            page = cast(Any, self._display_page(session_id, state))
            if kind == "click":
                page.mouse.click(float(x or 0), float(y or 0))
            elif kind == "drag":
                _drag(
                    page,
                    float(x1 or 0),
                    float(y1 or 0),
                    float(x2 or 0),
                    float(y2 or 0),
                    session.platform,
                    session_id,
                )
            elif kind == "type":
                page.keyboard.type(str(text or ""))
            elif kind == "key":
                page.keyboard.press(str(key or "Enter"))
            elif kind == "scroll":
                page.mouse.wheel(0, float(delta_y or 0))
            else:
                raise NetdiskError(
                    NetdiskErrorCode.UNSUPPORTED,
                    f"不支持的网页登录操作：{kind}",
                    platform=session.platform,
                )

        session.helper.with_session(session.session_key, act, timeout=_PAGE_TIMEOUT_SECONDS)

    def _require_session(self, session_id: str) -> _QrSession:
        """
        读取未过期的扫码会话。

        :param session_id: 会话标识
        :return: 会话
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise NetdiskError(NetdiskErrorCode.LINK_EXPIRED, "登录会话不存在或已过期")
        if time.monotonic() >= session.expires_at:
            self.cancel(session_id)
            raise NetdiskError(NetdiskErrorCode.LINK_EXPIRED, "登录会话已过期，请重新获取")
        return session

    def cancel(self, session_id: str) -> None:
        """
        关闭浏览器会话。

        :param session_id: 会话标识
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        vnc_bridge().release(session_id)
        try:
            BrowserSessionHelper.close_session(session.session_key)
        except Exception as error:  # noqa: BLE001 - 关闭失败只记录
            logger.warning(f"关闭网盘登录浏览器失败：error={type(error).__name__}")

    def _capture(self, state: _BrowserSessionState, login_url: str) -> tuple[bytes, bytes]:
        """
        在浏览器会话中打开登录页并截图。

        :param state: 浏览器会话状态
        :param login_url: 登录页地址
        :return: 二维码截图与完整页面截图
        """
        page = state.active_page
        # BrowserPage 协议只声明常用方法，二维码元素定位依赖 Playwright 的扩展 API。
        raw = cast(Any, page)
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        # 不再等待 networkidle（登录页常驻长连接，容易白等 15 秒）；
        # 给前端脚本一个很短的渲染窗口，后续画面由 WebSocket 持续刷新。
        raw.wait_for_timeout(1200)
        page_png = page.screenshot(type="jpeg", quality=70)
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
                        return cast(bytes, item.screenshot()), page_png
                    except Exception:  # noqa: BLE001 - 元素截图失败时回退整页
                        continue
        return page_png, page_png

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
        if platform is NetdiskPlatform.BAIDU:
            # 与 yunx 保持一致：百度原样拼接全部 .baidu.com Cookie（含同名），不做去重。
            pairs: list[str] = []
            for cookie in state.context.cookies():
                domain = str(cookie.get("domain", "")).lower()
                name = str(cookie.get("name", "")).strip()
                if not name:
                    continue
                if suffixes and not any(domain.endswith(suffix) or domain == suffix.lstrip(".") for suffix in suffixes):
                    continue
                pairs.append(f"{name}={cookie.get('value', '')}")
            return "; ".join(pairs)
        primary = _PRIMARY_HOSTS.get(platform, "")
        best: dict[str, tuple[int, str]] = {}
        order: list[str] = []
        for cookie in state.context.cookies():
            domain = str(cookie.get("domain", "")).lower()
            name = str(cookie.get("name", "")).strip()
            if not name:
                continue
            if suffixes and not any(domain.endswith(suffix) or domain == suffix.lstrip(".") for suffix in suffixes):
                continue
            score = 0
            if primary:
                if domain == primary:
                    score = 2
                elif primary.endswith(f".{domain.lstrip('.')}"):
                    score = 1
            current = best.get(name)
            if current is None:
                order.append(name)
                best[name] = (score, str(cookie.get("value", "")))
            elif score > current[0]:
                best[name] = (score, str(cookie.get("value", "")))
        return "; ".join(f"{name}={best[name][1]}" for name in order)
