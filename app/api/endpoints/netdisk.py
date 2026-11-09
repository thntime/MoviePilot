"""
网盘账号与登录接口。

仅管理员可访问；凭据写入后不再回显，列表只返回脱敏信息。
"""

import asyncio
import base64
import queue
import secrets
import time
from typing import Any, Optional

import websockets
from fastapi import Depends, Query, WebSocket, WebSocketDisconnect
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger

from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.netdisk import registry, selection
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.ai_selection import select_share_files
from app.application.netdisk.contract import NetdiskAccountDetail, NetdiskAccountView
from app.application.netdisk.login import NetdiskLoginService
from app.application.netdisk.resolve import NetdiskResolveService
from app.application.netdisk.share_blacklist import get_share_blacklist
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, ShareFile
from app.schemas.netdisk import (
    NetdiskAccountDetailView,
    NetdiskData,
    NetdiskPlatformInfo,
    NetdiskShareBlacklistEntry,
    NetdiskAccountUpdateRequest,
    NetdiskCookieCloudRequest,
    NetdiskCookieRequest,
    NetdiskPasswordRequest,
    NetdiskQrInputRequest,
    NetdiskQrStartRequest,
    NetdiskSharePreviewRequest,
    NetdiskSmsLoginRequest,
    NetdiskSmsSendRequest,
)
from app.schemas.netdisk_files import NetdiskDriveFileModel
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


def _account_view(view: NetdiskAccountView) -> NetdiskAccountDetailView:
    """
    把账号视图转换为接口响应；非详情接口不填充明文凭据。

    :param view: 账号脱敏视图
    :return: 接口响应模型
    """
    return NetdiskAccountDetailView(
        id=view.id,
        platform=view.platform,
        name=view.name,
        is_primary=view.is_primary,
        status=view.status,
        expires_at=view.expires_at.isoformat() if view.expires_at else None,
        last_checked_at=view.last_checked_at.isoformat() if view.last_checked_at else None,
        masked=dict(view.masked),
        nickname=view.nickname,
        quota_used=view.quota_used,
        quota_total=view.quota_total,
        speed=selection.account_speed(view.id),
    )


def _account_detail_view(detail: NetdiskAccountDetail) -> NetdiskAccountDetailView:
    """
    把账号详情转换为接口响应，附带明文凭据。

    :param detail: 账号投影与明文凭据
    :return: 详情响应模型
    """
    payload = _account_view(detail.account)
    payload.credential = dict(detail.credential)
    return payload


def _share_file_model(item: ShareFile) -> NetdiskDriveFileModel:
    """
    把分享文件条目转换为接口响应；不输出平台条目令牌。

    :param item: 分享文件条目
    :return: 接口响应模型
    """
    return NetdiskDriveFileModel(
        file_id=item.file_id,
        name=item.name,
        size=item.size,
        is_dir=item.is_dir,
        parent_id=item.parent_id,
        path=item.path,
    )


def _error_response(error: NetdiskError) -> _SchemaResponse[NetdiskData]:
    """
    把分类异常转换为统一失败响应。

    :param error: 网盘异常
    :return: 失败响应
    """
    return _SchemaResponse(
        success=False,
        message=error.message,
        data=NetdiskData(
            code=error.code.value,
            platform=error.platform.value if error.platform else None,
            account_name=error.account_name,
        ),
    )


def _accounts() -> NetdiskAccountService:
    """
    构造账号用例。
    """
    return NetdiskAccountService()


def _login() -> NetdiskLoginService:
    """
    构造登录用例。
    """
    return NetdiskLoginService()


@router.get("/platforms", summary="查询已支持的网盘平台")
async def list_platforms(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    返回已注册平台及其登录能力，供前端渲染表单。
    """
    platforms = []
    for platform in registry.registered_platforms():
        adapter = registry.get_adapter(platform)
        platforms.append(
            NetdiskPlatformInfo(
                platform=platform.value,
                link_policy=adapter.link_policy.value,
                requires_account=adapter.requires_account,
                guest_supported=adapter.guest_supported,
                max_concurrency=adapter.max_concurrency,
                cookie_required_keys=list(adapter.cookie_required_keys),
                supports_password_login=adapter.supports_password_login,
                supports_refresh=adapter.supports_refresh,
                login_url=adapter.login_url,
            )
        )
    return _SchemaResponse(success=True, data=NetdiskData(platforms=platforms))


@router.get("/share-blacklist", summary="查询网盘分享黑名单")
async def list_share_blacklist(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    返回当前有效的分享黑名单，便于手动核对与清除误拉黑的分享。
    """
    entries = await run_in_threadpool(get_share_blacklist().list_entries)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            share_blacklist=[
                NetdiskShareBlacklistEntry(
                    platform=entry.platform,
                    share_code=entry.share_code,
                    reason=entry.reason,
                    created_at=entry.created_at,
                    expires_at=entry.expires_at,
                )
                for entry in entries
            ]
        ),
    )


@router.delete("/share-blacklist/{platform}/{share_code}", summary="清除网盘分享黑名单")
async def delete_share_blacklist(
    platform: str,
    share_code: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    按平台与分享码移除一条黑名单记录。
    """
    deleted = await run_in_threadpool(
        get_share_blacklist().remove, platform, share_code
    )
    return _SchemaResponse(
        success=deleted,
        message="" if deleted else "黑名单条目不存在",
        data=NetdiskData(deleted=deleted),
    )


@router.post("/share/preview", summary="预览分享文件并生成 AI 预选")
async def preview_share(
    payload: NetdiskSharePreviewRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    展开分享文件树并让 AI 预选目标季集，结果只作为建议，不发起下载。

    AI 不可用、超时或返回空结果时降级为空预选，仍返回文件列表供用户手动选择。
    """
    try:
        resolved = await run_in_threadpool(
            NetdiskResolveService().walk,
            payload.url,
            payload.password or None,
            payload.platform,
            payload.account_id,
            # 与下载链路 _MAX_WALK_DEPTH 保持一致，避免深层分享在预览阶段被截断。
            max_depth=12,
        )
    except NetdiskError as error:
        return _error_response(error)
    files = list(resolved.files)
    if not files:
        return _SchemaResponse(
            success=True,
            message="分享内没有可选择的文件",
            data=NetdiskData(),
        )
    result = await select_share_files(
        files,
        season=payload.season,
        episodes=payload.episodes,
        user_intent=payload.user_intent,
    )
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            files=[_share_file_model(item) for item in files],
            selected_file_ids=list(result.selected_file_ids),
            suggested_episode_selection=result.suggested_episode_selection,
        ),
    )


@router.get("/accounts", summary="查询网盘账号")
async def list_accounts(
    platform: Optional[str] = Query(None, description="限定平台"),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    列出账号脱敏信息。
    """
    try:
        views = await run_in_threadpool(_accounts().list, platform)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(accounts=[_account_view(view) for view in views]))


@router.post("/accounts/cookie", summary="粘贴 Cookie 新增账号")
async def create_cookie_account(
    payload: NetdiskCookieRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    校验并保存粘贴的 Cookie 或令牌。
    """
    try:
        view = await run_in_threadpool(
            _login().paste_cookie,
            payload.platform,
            payload.cookie,
            name=payload.name,
            is_primary=payload.is_primary,
            account_id=payload.account_id,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_view(view)))


@router.post("/accounts/cookiecloud", summary="从 CookieCloud 导入账号")
async def create_cookiecloud_account(
    payload: NetdiskCookieCloudRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    按平台域名从 CookieCloud 导入并保存 Cookie。
    """
    try:
        view = await run_in_threadpool(
            _login().import_cookiecloud,
            payload.platform,
            name=payload.name,
            is_primary=payload.is_primary,
            account_id=payload.account_id,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_view(view)))


@router.post("/accounts/password", summary="账号密码登录并保存")
async def create_password_account(
    payload: NetdiskPasswordRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    使用账号密码换取令牌并保存账号。
    """
    try:
        view = await run_in_threadpool(
            _login().password_login,
            payload.platform,
            payload.username,
            payload.password,
            name=payload.name,
            is_primary=payload.is_primary,
            account_id=payload.account_id,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_view(view)))


@router.post("/accounts/sms/send", summary="发送短信验证码")
async def send_sms_code(
    payload: NetdiskSmsSendRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    发送光鸭等平台的短信验证码。
    """
    try:
        result = await run_in_threadpool(
            _login().send_sms_code,
            payload.platform,
            payload.phone,
            account_id=payload.account_id,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            sms_verification_id=str(result.get("verification_id") or ""),
            sms_is_user=bool(result.get("is_user")),
            sms_expires_in=int(result.get("expires_in") or 0),
        ),
    )


@router.post("/accounts/sms/login", summary="短信验证码登录")
async def login_with_sms(
    payload: NetdiskSmsLoginRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    校验短信验证码并保存账号。
    """
    try:
        view = await run_in_threadpool(
            _login().sms_login,
            payload.platform,
            payload.phone,
            payload.code,
            payload.verification_id,
            name=payload.name,
            is_primary=payload.is_primary,
            account_id=payload.account_id,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_view(view)))


@router.post("/accounts/qr", summary="启动扫码登录")
async def start_qr_login(
    payload: NetdiskQrStartRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    启动扫码会话并返回二维码图片。
    """
    try:
        session = await run_in_threadpool(
            _login().start_qr,
            payload.platform,
            name=payload.name,
            is_primary=payload.is_primary,
            account_id=payload.account_id,
        )
    except NetdiskError as error:
        return _error_response(error)
    image = session.image.decode("ascii") if session.image else ""
    page_image = session.page_image.decode("ascii") if session.page_image else ""
    ticket = _issue_vnc_ticket(session.session_id)
    vnc_path = f"/api/v1/netdisk/accounts/qr/{session.session_id}/vnc/{ticket}"
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            session_id=session.session_id,
            platform=session.platform.value,
            status=session.status,
            image=image,
            page_image=page_image,
            vnc_path=vnc_path,
            expires_at=session.expires_at.isoformat() if session.expires_at else None,
        ),
    )


@router.get("/accounts/qr/{session_id}", summary="轮询扫码登录")
async def poll_qr_login(
    session_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    轮询扫码状态；成功后账号已落库。
    """
    try:
        result = await run_in_threadpool(_login().poll_qr, session_id)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(
        success=result.status in {"success", "detected"},
        message=result.message or "",
        data=NetdiskData(status=result.status, account_name=result.account_name),
    )


@router.post("/accounts/qr/{session_id}/confirm", summary="确认保存网页登录")
async def confirm_qr_login(
    session_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    用户确认后保存已检测到的登录态。
    """
    try:
        view = await run_in_threadpool(_login().confirm_qr, session_id)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_view(view)))


_QR_TICKETS: dict[str, tuple[str, float]] = {}
# noVNC 票据允许在同一会话内重复连接，过期时间与登录会话一致。
_VNC_TICKETS: dict[str, tuple[str, float]] = {}
_VNC_TICKET_TTL_SECONDS = 900


def _issue_vnc_ticket(session_id: str) -> str:
    """
    为网页登录 noVNC 连接签发可复用的票据。

    :param session_id: 登录会话标识
    :return: 票据
    """
    now = time.monotonic()
    for key, (_, expires) in list(_VNC_TICKETS.items()):
        if expires < now:
            _VNC_TICKETS.pop(key, None)
    ticket = secrets.token_urlsafe(24)
    _VNC_TICKETS[ticket] = (session_id, now + _VNC_TICKET_TTL_SECONDS)
    return ticket


def _valid_vnc_ticket(session_id: str, ticket: str) -> bool:
    """
    校验 noVNC 票据是否属于当前会话且未过期。

    :param session_id: 登录会话标识
    :param ticket: 票据
    :return: 是否有效
    """
    entry = _VNC_TICKETS.get(ticket)
    if not entry or entry[0] != session_id or entry[1] < time.monotonic():
        _VNC_TICKETS.pop(ticket, None)
        return False
    return True


@router.post("/accounts/qr/{session_id}/ticket", summary="申请网页登录实时画面票据")
async def qr_login_ticket(
    session_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    为 WebSocket 实时画面签发一次性票据。
    """
    ticket = secrets.token_urlsafe(24)
    _QR_TICKETS[ticket] = (session_id, time.monotonic() + 60)
    return _SchemaResponse(success=True, data=NetdiskData(session_id=session_id, ticket=ticket))


@router.websocket("/accounts/qr/{session_id}/vnc/{ticket}")
async def qr_login_vnc(websocket: WebSocket, session_id: str, ticket: str) -> None:
    """
    把 noVNC 的 WebSocket 连接代理到本机 websockify。

    前端 iframe 使用真实 Chromium 画面，鼠标事件由 VNC 直接送入浏览器，
    不再经过 Playwright 的合成鼠标事件。
    """
    if not _valid_vnc_ticket(session_id, ticket):
        logger.warning("noVNC 票据无效：session=%s", session_id[:8])
        await websocket.close(code=4401)
        return
    # noVNC 默认不请求 WebSocket 子协议，服务端不能强行选择 binary。
    await websocket.accept()
    logger.info("网页登录 noVNC 代理已连接：session=%s", session_id[:8])
    try:
        upstream = await websockets.connect(
            "ws://127.0.0.1:6080/websockify",
            max_size=None,
            open_timeout=10,
            ping_interval=None,
        )
    except Exception as error:  # noqa: BLE001 - VNC 服务未就绪时关闭连接
        logger.warning(f"noVNC 上游连接失败：error={type(error).__name__}")
        await websocket.close(code=1011)
        return

    async def client_to_upstream() -> None:
        """
        转发浏览器到 VNC 服务端的二进制帧。
        """
        try:
            while True:
                await upstream.send(await websocket.receive_bytes())
        except Exception:  # noqa: BLE001 - 任一端关闭即结束
            return

    async def upstream_to_client() -> None:
        """
        转发 VNC 服务端到浏览器的二进制帧。
        """
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except Exception:  # noqa: BLE001 - 任一端关闭即结束
            return

    tasks = {
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    }
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await upstream.close()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - 已断开时忽略
            pass


@router.websocket("/accounts/qr/{session_id}/ws")
async def qr_login_stream(websocket: WebSocket, session_id: str, ticket: str = Query(...)) -> None:
    """
    推送 CDP 实时画面并转发点击/键盘事件。
    """
    entry = _QR_TICKETS.pop(ticket, None)
    if not entry or entry[0] != session_id or entry[1] < time.monotonic():
        await websocket.close(code=4401)
        return
    await websocket.accept()
    service = _login()
    frames: queue.Queue[tuple[bytes, dict[str, Any]]] = queue.Queue(maxsize=3)

    def on_frame(data: bytes, meta: dict[str, Any]) -> None:
        """
        丢弃旧帧后放入最新画面，避免积压导致延迟。
        """
        try:
            frames.put_nowait((data, meta))
        except queue.Full:
            try:
                frames.get_nowait()
            except queue.Empty:
                pass
            frames.put_nowait((data, meta))

    await run_in_threadpool(service.qr_stream_start, session_id, on_frame)

    async def sender() -> None:
        """
        持续发送画面并每秒同步登录状态。
        """
        last_status = 0.0
        while True:
            try:
                data, meta = await asyncio.to_thread(frames.get, True, 0.2)
            except queue.Empty:
                data = b""
                meta = {}
            if data:
                await websocket.send_json(
                    {
                        "type": "frame",
                        "data": base64.b64encode(data).decode("ascii"),
                        "width": int(meta.get("deviceWidth") or 1280),
                        "height": int(meta.get("deviceHeight") or 800),
                    }
                )
            now = time.monotonic()
            if now - last_status >= 1.0:
                last_status = now
                result = await run_in_threadpool(service.poll_qr, session_id)
                await websocket.send_json(
                    {
                        "type": "status",
                        "status": result.status,
                        "message": result.message,
                        "account_name": result.account_name,
                    }
                )
                if result.status in {"success", "failed", "expired"}:
                    return

    async def receiver() -> None:
        """
        接收前端事件并转发到浏览器。
        """
        while True:
            event = await websocket.receive_json()
            await run_in_threadpool(service.qr_send_event, session_id, event)

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        await asyncio.wait({sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        receiver_task.cancel()
        await run_in_threadpool(service.qr_stream_stop, session_id)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - 已断开时忽略
            pass


@router.get("/accounts/qr/{session_id}/frame", summary="读取网页登录画面")
async def qr_login_frame(
    session_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    返回当前登录页截图与视口尺寸，供前端刷新与坐标换算。
    """
    try:
        frame = await run_in_threadpool(_login().qr_frame, session_id)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            image=base64.b64encode(frame.get("image") or b"").decode("ascii"),
            page_width=int(frame.get("width") or 0),
            page_height=int(frame.get("height") or 0),
        ),
    )


@router.post("/accounts/qr/{session_id}/input", summary="操作网页登录页面")
async def qr_login_input(
    session_id: str,
    payload: NetdiskQrInputRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    转发点击、输入、按键或滚动，并返回最新画面。
    """
    try:
        frame = await run_in_threadpool(
            _login().qr_input,
            session_id,
            kind=payload.kind,
            x=payload.x,
            y=payload.y,
            x1=payload.x1,
            y1=payload.y1,
            x2=payload.x2,
            y2=payload.y2,
            text=payload.text,
            key=payload.key,
            delta_y=payload.delta_y,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            image=base64.b64encode(frame.get("image") or b"").decode("ascii"),
            page_width=int(frame.get("width") or 0),
            page_height=int(frame.get("height") or 0),
        ),
    )


@router.delete("/accounts/qr/{session_id}", summary="取消扫码登录")
async def cancel_qr_login(
    session_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    取消扫码会话并释放浏览器。
    """
    await run_in_threadpool(_login().cancel_qr, session_id)
    return _SchemaResponse(success=True, data=NetdiskData(status="cancelled"))


@router.post("/accounts/check", summary="检查全部网盘账号")
async def check_all_accounts(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    逐个检测全部网盘账号的登录态，并刷新空间占用与昵称。

    单账号异常只计入失败数量，不中断整轮检测。
    """
    result = await run_in_threadpool(_accounts().check_all)
    views = await run_in_threadpool(_accounts().list)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            accounts=[_account_view(view) for view in views],
            checked=result.get("checked", 0),
            active=result.get("active", 0),
            expired=result.get("expired", 0),
            failed=result.get("failed", 0),
        ),
    )


@router.get("/accounts/{account_id}", summary="查询网盘账号详情")
async def get_account_detail(
    account_id: int,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    返回单个账号详情，附带明文凭据供编辑回填；列表接口保持脱敏。
    """
    try:
        detail = await run_in_threadpool(_accounts().detail, account_id)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_detail_view(detail)))


@router.put("/accounts/{account_id}", summary="更新网盘账号")
async def update_account(
    account_id: int,
    payload: NetdiskAccountUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    更新账号名称、主账号标记或凭据；凭据文本按平台解析。
    """
    try:
        service = _accounts()
        credential = None
        if payload.cookie:
            record = await run_in_threadpool(service.store.get_record, account_id)
            if record is None:
                raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号不存在")
            credential = await run_in_threadpool(
                _login().credential_from_text,
                record.platform,
                payload.cookie,
            )
        view = await run_in_threadpool(
            service.update,
            account_id,
            name=payload.name,
            credential=credential,
            is_primary=payload.is_primary,
        )
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=True, data=NetdiskData(account=_account_view(view)))


@router.delete("/accounts/{account_id}", summary="删除网盘账号")
async def delete_account(
    account_id: int,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    删除账号；主账号删除后自动提升同平台剩余账号。
    """
    deleted = await run_in_threadpool(_accounts().delete, account_id)
    return _SchemaResponse(success=deleted, message="" if deleted else "账号不存在", data=NetdiskData(deleted=deleted))


@router.post("/accounts/{account_id}/probe", summary="检测网盘账号")
async def probe_account(
    account_id: int,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    调用平台接口检测账号登录态。
    """
    try:
        probe = await run_in_threadpool(_accounts().probe, account_id)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(
        success=probe.status == "active",
        message=probe.message or "",
        data=NetdiskData(status=probe.status),
    )


@router.post("/accounts/{account_id}/refresh", summary="刷新网盘账号令牌")
async def refresh_account(
    account_id: int,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    刷新账号令牌；平台不支持时返回空结果。
    """
    try:
        record = await run_in_threadpool(_accounts().refresh, account_id)
    except NetdiskError as error:
        return _error_response(error)
    return _SchemaResponse(success=record is not None, data=NetdiskData(refreshed=record is not None))


__all__ = ["router"]
