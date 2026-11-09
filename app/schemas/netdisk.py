"""
网盘账号与登录接口的请求模型。
"""

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from app.schemas.netdisk_files import (
    NetdiskDriveCapabilitiesModel,
    NetdiskDriveFileModel,
    NetdiskDriveQuotaModel,
    NetdiskDriveShareModel,
)


class NetdiskCookieRequest(BaseModel):
    """
    粘贴 Cookie 或令牌的请求体。
    """

    platform: Optional[str] = Field(default=None, description="网盘平台标识；传入 account_id 时可省略")
    cookie: str = Field(..., description="浏览器复制的 Cookie 头或平台令牌")
    name: Optional[str] = Field(default=None, description="账号名称，缺省时使用平台默认名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")
    account_id: Optional[int] = Field(default=None, description="已有账号主键，传入时更新凭据而不新建")


class NetdiskCookieCloudRequest(BaseModel):
    """
    从 CookieCloud 导入 Cookie 的请求体。
    """

    platform: Optional[str] = Field(default=None, description="网盘平台标识；传入 account_id 时可省略")
    name: Optional[str] = Field(default=None, description="账号名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")
    account_id: Optional[int] = Field(default=None, description="已有账号主键，传入时更新凭据而不新建")


class NetdiskPasswordRequest(BaseModel):
    """
    账号密码登录的请求体。
    """

    platform: Optional[str] = Field(default=None, description="网盘平台标识，仅 123 与光鸭支持；传入 account_id 时可省略")
    username: str = Field(..., description="登录账号")
    password: str = Field(..., description="登录密码，仅用于换取令牌")
    name: Optional[str] = Field(default=None, description="账号名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")
    account_id: Optional[int] = Field(default=None, description="已有账号主键，传入时更新凭据而不新建")


class NetdiskQrInputRequest(BaseModel):
    """
    网页登录页面操作请求。

    kind 为 click 时使用 x/y，drag 时使用 x1/y1/x2/y2，type 时使用 text，
    key 时使用 key，scroll 时使用 delta_y。
    """

    kind: Literal["click", "type", "key", "scroll", "drag"] = Field(..., description="操作类型")
    x: Optional[float] = Field(default=None, description="点击横坐标（页面像素）")
    y: Optional[float] = Field(default=None, description="点击纵坐标（页面像素）")
    x1: Optional[float] = Field(default=None, description="拖拽起点横坐标（页面像素）")
    y1: Optional[float] = Field(default=None, description="拖拽起点纵坐标（页面像素）")
    x2: Optional[float] = Field(default=None, description="拖拽终点横坐标（页面像素）")
    y2: Optional[float] = Field(default=None, description="拖拽终点纵坐标（页面像素）")
    text: Optional[str] = Field(default=None, description="输入文本")
    key: Optional[str] = Field(default=None, description="按键名，如 Enter")
    delta_y: Optional[float] = Field(default=None, description="滚动距离")


class NetdiskSmsSendRequest(BaseModel):
    """
    发送短信验证码的请求体。
    """

    platform: Optional[str] = Field(default=None, description="网盘平台标识，仅光鸭支持；传入 account_id 时可省略")
    phone: str = Field(..., description="手机号")
    account_id: Optional[int] = Field(default=None, description="已有账号主键，用于重新授权")


class NetdiskSmsLoginRequest(BaseModel):
    """
    短信验证码登录的请求体。
    """

    platform: Optional[str] = Field(default=None, description="网盘平台标识，仅光鸭支持；传入 account_id 时可省略")
    phone: str = Field(..., description="手机号")
    code: str = Field(..., description="短信验证码")
    verification_id: str = Field(..., description="发送验证码接口返回的验证会话")
    name: Optional[str] = Field(default=None, description="账号名称")
    is_primary: bool = Field(False, description="是否设为该平台主账号")
    account_id: Optional[int] = Field(default=None, description="已有账号主键，传入时更新凭据而不新建")


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


class NetdiskSharePreviewRequest(BaseModel):
    """
    分享文件 AI 预选预览的请求体。

    只传分享链接与提取码，不接收或回显账号凭据。
    """

    url: str = Field(..., description="分享链接")
    password: str = Field("", description="分享提取码，留空表示无提取码")
    platform: Optional[str] = Field(default=None, description="网盘平台标识；缺省时按链接识别")
    account_id: Optional[int] = Field(default=None, description="指定取链账号，缺省使用平台主账号")
    season: Optional[Union[int, str, list[int]]] = Field(
        default=None, description="目标季号，支持整数、S01 文本或季号列表"
    )
    episodes: Optional[Union[int, str, list[int]]] = Field(
        default=None, description="目标集数，支持整数、E01-E03 文本或集数列表"
    )
    user_intent: str = Field("", description="用户自然语言意图，作为预选补充")


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
    supports_refresh: bool = Field(False, description="是否支持刷新令牌")
    recycle_bin: bool = Field(False, description="是否支持读取与清空回收站")
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
    nickname: str = Field("", description="账号昵称/手机号")
    quota_used: int = Field(0, description="已用空间")
    quota_total: int = Field(0, description="总空间")
    speed: float = Field(0.0, description="账号平均下载速度，单位字节每秒")


class NetdiskAccountDetailView(NetdiskAccountView):
    """
    网盘账号详情，附带明文凭据。

    仅由超级管理员鉴权的单账号详情接口返回；列表接口继续使用脱敏视图。
    """

    credential: dict[str, str] = Field(default_factory=dict, description="明文凭据字段，仅详情接口返回")


class NetdiskShareBlacklistEntry(BaseModel):
    """
    分享黑名单条目。
    """

    platform: str = Field(..., description="网盘平台标识")
    share_code: str = Field(..., description="分享码")
    reason: str = Field(..., description="加入黑名单的原因")
    created_at: str = Field(..., description="写入时间")
    expires_at: str = Field(..., description="过期时间")


class NetdiskData(BaseModel):
    """
    网盘接口的统一数据体，按操作填充对应字段。
    """

    platforms: list[NetdiskPlatformInfo] = Field(default_factory=list, description="平台列表")
    accounts: list[NetdiskAccountView] = Field(default_factory=list, description="账号列表")
    share_blacklist: list[NetdiskShareBlacklistEntry] = Field(
        default_factory=list, description="当前有效的分享黑名单"
    )
    account: Optional[NetdiskAccountDetailView] = Field(default=None, description="单个账号；详情接口附带明文凭据")
    checked: int = Field(0, description="本次检测的账号总数")
    active: int = Field(0, description="检测为有效的账号数")
    expired: int = Field(0, description="登录态失效的账号数")
    failed: int = Field(0, description="检测失败或状态未知的账号数")
    session_id: Optional[str] = Field(default=None, description="扫码会话标识")
    ticket: Optional[str] = Field(default=None, description="WebSocket 一次性票据")
    platform: Optional[str] = Field(default=None, description="相关平台")
    status: Optional[str] = Field(default=None, description="状态")
    image: Optional[str] = Field(default=None, description="二维码图片（base64）")
    page_image: Optional[str] = Field(default=None, description="完整登录页截图（base64）")
    page_width: Optional[int] = Field(default=None, description="登录页视口宽度")
    page_height: Optional[int] = Field(default=None, description="登录页视口高度")
    vnc_path: Optional[str] = Field(default=None, description="noVNC WebSocket 路径")
    expires_at: Optional[str] = Field(default=None, description="会话过期时间")
    deleted: Optional[bool] = Field(default=None, description="是否已删除")
    cleared: Optional[bool] = Field(default=None, description="回收站是否已清空")
    cleared_at: Optional[str] = Field(default=None, description="回收站清理执行时间（ISO 8601）")
    refreshed: Optional[bool] = Field(default=None, description="是否已刷新")
    sms_verification_id: Optional[str] = Field(default=None, description="短信验证会话")
    sms_is_user: Optional[bool] = Field(default=None, description="手机号是否已注册")
    sms_expires_in: Optional[int] = Field(default=None, description="验证码有效期（秒）")
    code: Optional[str] = Field(default=None, description="错误分类")
    account_name: Optional[str] = Field(default=None, description="相关账号名称")
    files: list[NetdiskDriveFileModel] = Field(default_factory=list, description="目录条目")
    selected_file_ids: list[str] = Field(
        default_factory=list, description="AI 预选中的文件标识，保持模型返回顺序"
    )
    suggested_episode_selection: str = Field(
        "", description="由 AI 预选文件压缩出的建议集数，可直接填入集数输入框"
    )
    quota: Optional[NetdiskDriveQuotaModel] = Field(default=None, description="空间配额")
    share: Optional[NetdiskDriveShareModel] = Field(default=None, description="分享结果")
    capabilities: Optional[NetdiskDriveCapabilitiesModel] = Field(default=None, description="个人盘能力")
