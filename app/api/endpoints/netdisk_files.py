"""
个人网盘浏览、下载、分享与文件操作接口。

下载采用服务端代理流式转发：直链和 Cookie 不返回前端，浏览器只访问本接口。
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import quote

from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.netdisk.files import NetdiskFilesService
from app.domain.netdisk import DirectLink, DriveFile, NetdiskError, NetdiskErrorCode
from app.adapters.network.http import RequestUtils
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.netdisk import NetdiskData
from app.schemas.netdisk_files import (
    NetdiskDeleteRequest,
    NetdiskDriveCapabilitiesModel,
    NetdiskDriveFileModel,
    NetdiskDriveQuotaModel,
    NetdiskDriveShareModel,
    NetdiskMoveRequest,
    NetdiskRenameRequest,
    NetdiskShareRequest,
)
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


def _service() -> NetdiskFilesService:
    """
    构造个人盘用例。
    """
    return NetdiskFilesService()


def _error(error: NetdiskError) -> _SchemaResponse[NetdiskData]:
    """
    把分类异常转换为统一失败响应。
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


def _failure(error: Exception) -> _SchemaResponse[NetdiskData]:
    """
    把任意异常转换为统一失败响应。

    平台适配器偶尔会抛出未分类异常（例如接口结构变化）；直接冒泡会被
    FastAPI 变成 500 且前端只能看到插画，这里统一收敛为业务错误。

    :param error: 捕获到的异常
    :return: 失败响应
    """
    if isinstance(error, NetdiskError):
        return _error(error)
    logger.exception("网盘文件接口异常")
    return _SchemaResponse(
        success=False,
        message="网盘接口异常，请稍后重试",
        data=NetdiskData(code=NetdiskErrorCode.UNKNOWN.value),
    )


def _file(model: NetdiskDriveFileModel) -> DriveFile:
    """
    把请求模型转换为领域条目。
    """
    return DriveFile(
        file_id=model.file_id,
        name=model.name,
        size=model.size,
        is_dir=model.is_dir,
        parent_id=model.parent_id,
    )


def _file_model(file: DriveFile) -> NetdiskDriveFileModel:
    """
    把领域条目转换为响应模型。
    """
    return NetdiskDriveFileModel(
        file_id=file.file_id,
        name=file.name,
        size=file.size,
        is_dir=file.is_dir,
        updated_at=file.updated_at.isoformat() if file.updated_at else None,
        parent_id=file.parent_id,
    )


@router.get("/{account_id}/capabilities", summary="查询个人盘能力")
async def drive_capabilities(
    account_id: int,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    返回账号对应平台的个人盘能力。
    """
    try:
        caps = await run_in_threadpool(_service().capabilities, account_id)
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            capabilities=NetdiskDriveCapabilitiesModel(
                quota=caps.quota,
                browse=caps.browse,
                download=caps.download,
                share=caps.share,
                move=caps.move,
                rename=caps.rename,
                delete=caps.delete,
                recycle_bin=caps.recycle_bin,
            )
        ),
    )


@router.get("/{account_id}/quota", summary="查询个人盘空间")
async def drive_quota(
    account_id: int,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    查询账号空间配额。
    """
    try:
        quota = await run_in_threadpool(_service().quota, account_id)
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(quota=NetdiskDriveQuotaModel(used=quota.used, total=quota.total)),
    )


@router.get("/{account_id}/list", summary="浏览个人盘目录")
async def drive_list(
    account_id: int,
    parent_id: str = Query("", description="父目录标识，空串为根目录"),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    列出目录条目。
    """
    try:
        files = await run_in_threadpool(_service().list_dir, account_id, parent_id)
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(files=[_file_model(item) for item in files]),
    )


@router.post("/{account_id}/share", summary="创建个人盘分享")
async def drive_share(
    account_id: int,
    payload: NetdiskShareRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    为文件或文件夹创建分享。
    """
    try:
        share = await run_in_threadpool(
            _service().share,
            account_id,
            [_file(item) for item in payload.files],
            password=payload.password,
            expires_in=payload.expires_in,
        )
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(
        success=True,
        data=NetdiskData(
            share=NetdiskDriveShareModel(
                url=share.url,
                password=share.password,
                expires_at=share.expires_at.isoformat() if share.expires_at else None,
            )
        ),
    )


@router.post("/{account_id}/move", summary="移动个人盘文件")
async def drive_move(
    account_id: int,
    payload: NetdiskMoveRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    移动文件到目标目录。
    """
    try:
        await run_in_threadpool(
            _service().move,
            account_id,
            [_file(item) for item in payload.files],
            payload.target_id,
        )
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(success=True, data=NetdiskData())


@router.post("/{account_id}/rename", summary="重命名个人盘文件")
async def drive_rename(
    account_id: int,
    payload: NetdiskRenameRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    重命名文件。
    """
    try:
        await run_in_threadpool(_service().rename, account_id, _file(payload.file), payload.name)
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(success=True, data=NetdiskData())


@router.post("/{account_id}/delete", summary="删除个人盘文件")
async def drive_delete(
    account_id: int,
    payload: NetdiskDeleteRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[NetdiskData]:
    """
    删除文件。
    """
    try:
        await run_in_threadpool(_service().delete, account_id, [_file(item) for item in payload.files])
    except Exception as error:
        return _failure(error)
    return _SchemaResponse(success=True, data=NetdiskData())


@router.get("/{account_id}/stream/{file_id}", summary="代理下载个人盘文件")
async def drive_stream(
    account_id: int,
    file_id: str,
    request: Request,
    name: str = Query("", description="文件名"),
    size: int = Query(0, description="文件大小"),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> StreamingResponse:
    """
    服务端取直链并流式转发，Range 请求原样透传。
    """
    target = DriveFile(file_id=file_id, name=name or file_id, size=size)
    link: DirectLink = await run_in_threadpool(_service().download, account_id, target)
    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    http = RequestUtils(headers=dict(link.headers), timeout=120)
    response = await run_in_threadpool(
        lambda: http.get_res(link.url, headers=headers, allow_redirects=True, stream=True)
    )
    if response is None:
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "获取下载流失败")

    def iterate() -> Iterator[bytes]:
        """
        转发响应体并在结束时释放连接。
        """
        try:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            response.close()

    out_headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(target.name)}",
        "Accept-Ranges": "bytes",
    }
    for key in ("Content-Length", "Content-Range", "Content-Type"):
        value = response.headers.get(key)
        if value:
            out_headers[key] = value
    return StreamingResponse(
        iterate(),
        status_code=response.status_code,
        headers=out_headers,
        media_type=response.headers.get("Content-Type") or "application/octet-stream",
    )
