"""
网盘账号与登录接口。

仅管理员可访问；凭据写入后不再回显，列表只返回脱敏信息。
"""

from typing import Any, Optional

from fastapi import Depends, Query
from app.runtime.execution import run_in_threadpool

from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import NetdiskAccountView
from app.application.netdisk.login import NetdiskLoginService
from app.domain.netdisk import NetdiskError
from app.schemas.netdisk import (
    NetdiskAccountView as NetdiskAccountPayload,
    NetdiskData,
    NetdiskPlatformInfo,
    NetdiskAccountUpdateRequest,
    NetdiskCookieCloudRequest,
    NetdiskCookieRequest,
    NetdiskPasswordRequest,
    NetdiskQrStartRequest,
)
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


def _account_view(view: NetdiskAccountView) -> NetdiskAccountPayload:
    """
    把账号视图转换为接口响应。

    :param view: 账号脱敏视图
    :return: 接口响应模型
    """
    return NetdiskAccountPayload(
        id=view.id,
        platform=view.platform,
        name=view.name,
        is_primary=view.is_primary,
        status=view.status,
        expires_at=view.expires_at.isoformat() if view.expires_at else None,
        last_checked_at=view.last_checked_at.isoformat() if view.last_checked_at else None,
        masked=dict(view.masked),
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
                login_url=adapter.login_url,
            )
        )
    return _SchemaResponse(success=True, data=NetdiskData(platforms=platforms))


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
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            session_id=session.session_id,
            platform=session.platform.value,
            status=session.status,
            image=image,
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
        success=result.status == "success",
        message=result.message or "",
        data=NetdiskData(status=result.status),
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


@router.put("/accounts/{account_id}", summary="更新网盘账号")
async def update_account(
    account_id: int,
    payload: NetdiskAccountUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    更新账号名称、主账号标记或凭据。
    """
    credential = {"cookie": payload.cookie} if payload.cookie else None
    try:
        view = await run_in_threadpool(
            _accounts().update,
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
