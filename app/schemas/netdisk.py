"""
网盘账号与登录接口的请求模型。
"""

from typing import Optional

from pydantic import BaseModel, Field


class NetdiskCookieRequest(BaseModel):
    """
    粘贴 Cookie 或令牌的请求体。
    """

    platform: str = Field(..., description="网盘平台标识，如 quark、uc、pan123")
    cookie: str = Field(..., description="浏览器复制的 Cookie 头或平台令牌")
    name: Optional[str] = Field(default=None, description="账号名称，缺省时使用平台默认名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")


class NetdiskCookieCloudRequest(BaseModel):
    """
    从 CookieCloud 导入 Cookie 的请求体。
    """

    platform: str = Field(..., description="网盘平台标识")
    name: Optional[str] = Field(default=None, description="账号名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")


class NetdiskPasswordRequest(BaseModel):
    """
    账号密码登录的请求体。
    """

    platform: str = Field(..., description="网盘平台标识，仅 123 与光鸭支持")
    username: str = Field(..., description="登录账号")
    password: str = Field(..., description="登录密码，仅用于换取令牌")
    name: Optional[str] = Field(default=None, description="账号名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")


class NetdiskQrStartRequest(BaseModel):
    """
    启动扫码登录的请求体。
    """

    platform: str = Field(..., description="网盘平台标识")
    name: Optional[str] = Field(default=None, description="登录成功后写入的账号名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")
    account_id: Optional[int] = Field(default=None, description="已有账号主键，用于更新登录态")


class NetdiskAccountUpdateRequest(BaseModel):
    """
    更新账号的请求体。
    """

    name: Optional[str] = Field(default=None, description="新的账号名称")
    is_primary: Optional[bool] = Field(default=None, description="是否设为该平台主账号")
    cookie: Optional[str] = Field(default=None, description="新的 Cookie 或令牌，留空表示不修改")


class NetdiskPlatformInfo(BaseModel):
    """
    网盘平台能力与登录方式描述。
    """

    platform: str = Field(..., description="平台标识")
    link_policy: str = Field(..., description="取链策略")
    requires_account: bool = Field(..., description="取链是否必须登录")
    guest_supported: bool = Field(..., description="是否支持游客取链")
    max_concurrency: int = Field(..., description="平台并发上限")
    cookie_required_keys: list[str] = Field(default_factory=list, description="凭据必填字段")
    supports_password_login: bool = Field(..., description="是否支持账号密码登录")
    login_url: str = Field(..., description="平台登录页地址")


class NetdiskAccountView(BaseModel):
    """
    网盘账号的脱敏展示结构。
    """

    id: int = Field(..., description="账号主键")
    platform: str = Field(..., description="平台标识")
    name: str = Field(..., description="账号名称")
    is_primary: bool = Field(..., description="是否为主账号")
    status: str = Field(..., description="账号状态")
    expires_at: Optional[str] = Field(default=None, description="过期时间")
    last_checked_at: Optional[str] = Field(default=None, description="最近检测时间")
    masked: dict[str, str] = Field(default_factory=dict, description="脱敏后的凭据字段")


class NetdiskData(BaseModel):
    """
    网盘接口的统一数据体，按操作填充对应字段。
    """

    platforms: list[NetdiskPlatformInfo] = Field(default_factory=list, description="平台列表")
    accounts: list[NetdiskAccountView] = Field(default_factory=list, description="账号列表")
    account: Optional[NetdiskAccountView] = Field(default=None, description="单个账号")
    session_id: Optional[str] = Field(default=None, description="扫码会话标识")
    platform: Optional[str] = Field(default=None, description="相关平台")
    status: Optional[str] = Field(default=None, description="状态")
    image: Optional[str] = Field(default=None, description="二维码图片（base64）")
    expires_at: Optional[str] = Field(default=None, description="会话过期时间")
    deleted: Optional[bool] = Field(default=None, description="是否已删除")
    refreshed: Optional[bool] = Field(default=None, description="是否已刷新")
    code: Optional[str] = Field(default=None, description="错误分类")
    account_name: Optional[str] = Field(default=None, description="相关账号名称")
