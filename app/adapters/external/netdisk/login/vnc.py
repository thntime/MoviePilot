"""
139 网页登录的 noVNC 桥接。

YunX-Desktop 在桌面端嵌入真实 Chromium（JCEF），用户在真实浏览器上拖拽滑块。
服务端无法把 JCEF 嵌进 Web 前端，这里用等价方案：Xvfb + 有头 Chromium +
x11vnc + websockify，前端通过 noVNC canvas 接收真实鼠标事件。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

from app.domain.netdisk import NetdiskError, NetdiskErrorCode
from app.runtime.log import logger
from app.runtime.resources import acquire_managed_resource

_DISPLAY_WIDTH = 1440
_DISPLAY_HEIGHT = 900
_RFB_PORT = 5900
_WS_PORT = 6080
_NOVNC_WEB = "/usr/share/novnc"


class VncBridge:
    """
    管理网页登录使用的虚拟显示、x11vnc 与 websockify 进程。

    同一时间只允许一个网页登录会话，避免多个有头浏览器窗口重叠。
    """

    _instance: Optional["VncBridge"] = None
    _instance_lock = threading.RLock()

    def __init__(self) -> None:
        """
        初始化进程句柄与活动会话。
        """
        self._lock = threading.RLock()
        self._x11vnc: Optional[subprocess.Popen[bytes]] = None
        self._websockify: Optional[subprocess.Popen[bytes]] = None
        self._openbox: Optional[subprocess.Popen[bytes]] = None
        self._active_session: Optional[str] = None

    @classmethod
    def instance(cls) -> "VncBridge":
        """
        返回进程级单例。

        :return: VNC 桥接实例
        """
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def acquire(self, session_id: str) -> None:
        """
        占用唯一的网页登录通道并确保 VNC 服务已启动。

        :param session_id: 登录会话标识
        :raises NetdiskError: 已有其他会话或 VNC 组件缺失时抛出
        """
        with self._lock:
            if self._active_session and self._active_session != session_id:
                raise NetdiskError(
                    NetdiskErrorCode.UNSUPPORTED,
                    "已有网页登录会话，请先取消后再试",
                )
            self._active_session = session_id
            try:
                self._ensure_started()
            except Exception:
                self._active_session = None
                raise

    def release(self, session_id: str) -> None:
        """
        释放网页登录通道；VNC 进程保留给下一次会话复用。

        :param session_id: 登录会话标识
        """
        with self._lock:
            if self._active_session == session_id:
                self._active_session = None

    def _ensure_started(self) -> None:
        """
        启动虚拟显示、x11vnc、窗口管理器与 websockify。

        :raises NetdiskError: 组件缺失或端口启动超时时抛出
        """
        for binary in ("Xvfb", "x11vnc", "websockify"):
            if not shutil.which(binary):
                raise NetdiskError(
                    NetdiskErrorCode.UNSUPPORTED,
                    f"服务器缺少 noVNC 组件：{binary}",
                )
        resource = acquire_managed_resource(
            "host.display",
            reason="netdisk_vnc_login",
            retry=True,
        )
        display = self._display_value(resource)
        if not display:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "无法启动网页登录所需的虚拟显示",
            )
        os.environ["DISPLAY"] = display
        env = os.environ.copy()
        if self._x11vnc is None or self._x11vnc.poll() is not None:
            self._x11vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    display,
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-rfbport",
                    str(_RFB_PORT),
                    "-localhost",
                    "-noxdamage",
                    "-quiet",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        if shutil.which("openbox") and (self._openbox is None or self._openbox.poll() is not None):
            self._openbox = subprocess.Popen(
                ["openbox", "--display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        if self._websockify is None or self._websockify.poll() is not None:
            self._websockify = subprocess.Popen(
                [
                    "websockify",
                    f"--web={_NOVNC_WEB}",
                    f"127.0.0.1:{_WS_PORT}",
                    f"127.0.0.1:{_RFB_PORT}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        self._wait_port(_WS_PORT, timeout=12)
        logger.info(
            "网页登录 noVNC 已启动：display=%s rfb=127.0.0.1:%s ws=127.0.0.1:%s",
            display,
            _RFB_PORT,
            _WS_PORT,
        )

    @staticmethod
    def _display_value(resource: object) -> str:
        """
        读取 pyvirtualdisplay 分配的显示号。

        :param resource: 托管显示资源
        :return: 形如 `:99` 的显示号；无法读取时为空串
        """
        display = getattr(resource, "display", None)
        value = getattr(display, "new_display_var", "") if display is not None else ""
        return str(value or os.environ.get("DISPLAY", ""))

    @staticmethod
    def _wait_port(port: int, timeout: float) -> None:
        """
        等待本地端口可连接。

        :param port: 端口号
        :param timeout: 超时秒数
        :raises NetdiskError: 超时仍未监听时抛出
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.2)
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "网页登录 noVNC 服务启动超时",
        )

    def stop(self) -> None:
        """
        停止 VNC 相关进程；虚拟显示由托管资源统一回收。
        """
        with self._lock:
            for process in (self._websockify, self._x11vnc, self._openbox):
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                    except Exception:  # noqa: BLE001 - 停止失败只记录
                        continue
            self._websockify = None
            self._x11vnc = None
            self._openbox = None
            self._active_session = None


def vnc_bridge() -> VncBridge:
    """
    返回进程级 VNC 桥接实例。

    :return: VNC 桥接实例
    """
    return VncBridge.instance()
