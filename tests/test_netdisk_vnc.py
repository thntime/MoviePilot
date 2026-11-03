"""
139 noVNC 桥接的定向测试。

只验证票据、显示号读取与并发占用逻辑，不启动真实 VNC 进程。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.external.netdisk.login import vnc as vnc_module
from app.domain.netdisk import NetdiskError


def test_vnc_ticket_issue_and_validate() -> None:
    """
    票据只在签发会话内有效。
    """
    from app.api.endpoints import netdisk as netdisk_module

    ticket = netdisk_module._issue_vnc_ticket("session-1")  # pylint: disable=protected-access

    assert netdisk_module._valid_vnc_ticket("session-1", ticket) is True  # pylint: disable=protected-access
    assert netdisk_module._valid_vnc_ticket("session-2", ticket) is False  # pylint: disable=protected-access
    assert netdisk_module._valid_vnc_ticket("session-1", "unknown") is False  # pylint: disable=protected-access


def test_vnc_bridge_reads_display_var() -> None:
    """
    从 pyvirtualdisplay 资源读取显示号。
    """
    resource = SimpleNamespace(display=SimpleNamespace(new_display_var=":99"))

    assert vnc_module.VncBridge._display_value(resource) == ":99"  # pylint: disable=protected-access


def test_vnc_bridge_rejects_concurrent_sessions(monkeypatch) -> None:
    """
    同一时间只允许一个 139 登录通道。
    """
    bridge = vnc_module.VncBridge()
    monkeypatch.setattr(bridge, "_ensure_started", lambda: None)

    bridge.acquire("session-1")

    with pytest.raises(NetdiskError):
        bridge.acquire("session-2")


def test_vnc_bridge_release_allows_next_session(monkeypatch) -> None:
    """
    释放后允许新的 139 会话占用通道。
    """
    bridge = vnc_module.VncBridge()
    monkeypatch.setattr(bridge, "_ensure_started", lambda: None)

    bridge.acquire("session-1")
    bridge.release("session-1")
    bridge.acquire("session-2")

    assert bridge._active_session == "session-2"  # pylint: disable=protected-access


def test_start_qr_returns_vnc_path_for_139(monkeypatch) -> None:
    """
    139 启动扫码会话时返回 noVNC 路径。
    """
    import asyncio
    from app.api.endpoints import netdisk as netdisk_module
    from app.domain.netdisk import LoginSession, NetdiskPlatform
    from app.schemas.netdisk import NetdiskQrStartRequest

    class _FakeLogin:
        def start_qr(self, platform, **kwargs):
            del kwargs
            return LoginSession(
                session_id="session-139",
                platform=NetdiskPlatform(platform),
                image=None,
                page_image=None,
                status="pending",
                message="",
                expires_at=None,
            )

    monkeypatch.setattr(netdisk_module, "_login", lambda: _FakeLogin())

    response = asyncio.run(
        netdisk_module.start_qr_login(
            NetdiskQrStartRequest(platform="pan139"),
            None,  # type: ignore[arg-type]
        )
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.vnc_path is not None
    assert response.data.vnc_path.startswith("/api/v1/netdisk/accounts/qr/session-139/vnc/")


def test_start_qr_returns_vnc_path_for_other_platforms(monkeypatch) -> None:
    """
    所有网页登录平台都返回 noVNC 路径。
    """
    import asyncio
    from app.api.endpoints import netdisk as netdisk_module
    from app.domain.netdisk import LoginSession, NetdiskPlatform
    from app.schemas.netdisk import NetdiskQrStartRequest

    class _FakeLogin:
        def start_qr(self, platform, **kwargs):
            del kwargs
            return LoginSession(
                session_id="session-quark",
                platform=NetdiskPlatform(platform),
                image=None,
                page_image=None,
                status="pending",
                message="",
                expires_at=None,
            )

    monkeypatch.setattr(netdisk_module, "_login", lambda: _FakeLogin())

    response = asyncio.run(
        netdisk_module.start_qr_login(
            NetdiskQrStartRequest(platform="quark"),
            None,  # type: ignore[arg-type]
        )
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.vnc_path is not None
    assert response.data.vnc_path.startswith("/api/v1/netdisk/accounts/qr/session-quark/vnc/")


def test_vnc_proxy_relays_binary(monkeypatch) -> None:
    """
    FastAPI 的 noVNC 代理能把二进制帧转发到 websockify。
    """
    import threading

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from websockets.sync.server import serve

    from app.api.endpoints import netdisk as netdisk_module

    def echo(connection) -> None:
        """
        原样回显二进制帧。
        """
        for message in connection:
            connection.send(message)

    server = serve(echo, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_connect = netdisk_module.websockets.connect

    async def fake_connect(url, **kwargs):
        """
        把代理目标改到本地回显服务。
        """
        del url
        return await original_connect(
            f"ws://127.0.0.1:{port}/websockify",
            subprotocols=kwargs.get("subprotocols"),
            max_size=kwargs.get("max_size"),
            open_timeout=kwargs.get("open_timeout"),
            ping_interval=kwargs.get("ping_interval"),
        )

    monkeypatch.setattr(netdisk_module.websockets, "connect", fake_connect)
    app = FastAPI()
    app.include_router(netdisk_module.router)
    ticket = netdisk_module._issue_vnc_ticket("session-proxy")  # pylint: disable=protected-access
    try:
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/accounts/qr/session-proxy/vnc/{ticket}",
            ) as websocket:
                websocket.send_bytes(b"hello")
                assert websocket.receive_bytes() == b"hello"
    finally:
        server.shutdown()
