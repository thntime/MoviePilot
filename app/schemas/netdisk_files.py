"""
个人网盘浏览、分享与文件操作的请求模型。
"""

from typing import Optional

from pydantic import BaseModel, Field


class NetdiskDriveFileModel(BaseModel):
    """
    个人盘文件或目录条目。
    """

    file_id: str = Field(..., description="平台文件标识")
    name: str = Field(..., description="文件名")
    size: int = Field(0, description="字节数；目录为 0")
    is_dir: bool = Field(False, description="是否目录")
    updated_at: Optional[str] = Field(default=None, description="修改时间")
    parent_id: str = Field("", description="父目录标识")
    path: str = Field("", description="分享内相对路径；个人盘条目为空")


class NetdiskDriveQuotaModel(BaseModel):
    """
    个人盘空间配额。
    """

    used: int = Field(0, description="已用字节")
    total: int = Field(0, description="总字节")


class NetdiskDriveShareModel(BaseModel):
    """
    分享结果。
    """

    url: str = Field(..., description="分享链接")
    password: str = Field("", description="提取码")
    expires_at: Optional[str] = Field(default=None, description="过期时间")


class NetdiskDriveCapabilitiesModel(BaseModel):
    """
    平台个人盘能力声明。
    """

    quota: bool = False
    browse: bool = False
    download: bool = False
    share: bool = False
    move: bool = False
    rename: bool = False
    delete: bool = False
    recycle_bin: bool = False


class NetdiskShareRequest(BaseModel):
    """
    创建分享的请求体。
    """

    files: list[NetdiskDriveFileModel] = Field(
        ..., min_length=1, description="要分享的个人盘文件列表；条目来自 netdisk.files.list 的浏览结果。"
    )
    password: str = Field("", description="提取码，留空由平台决定")
    expires_in: int = Field(0, description="有效期秒数，0 为平台默认")


class NetdiskMoveRequest(BaseModel):
    """
    移动文件的请求体。
    """

    files: list[NetdiskDriveFileModel] = Field(
        ..., min_length=1, description="要移动的个人盘文件列表；条目来自 netdisk.files.list 的浏览结果。"
    )
    target_id: str = Field(..., description="目标目录标识")


class NetdiskRenameRequest(BaseModel):
    """
    重命名请求体。
    """

    file: NetdiskDriveFileModel
    name: str = Field(..., min_length=1, description="新的文件名，不包含路径。")


class NetdiskDeleteRequest(BaseModel):
    """
    删除请求体。
    """

    files: list[NetdiskDriveFileModel] = Field(
        ..., min_length=1, description="要删除的个人盘文件列表；条目来自 netdisk.files.list 的浏览结果。"
    )
