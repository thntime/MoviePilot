"""
网盘取链、账号与登录的应用层端口与数据传输对象。

本模块只声明接口与不可变 DTO，不导入任何具体 Adapter、数据库或运行时实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from app.domain.netdisk import (
    AccountProbe,
    DirectLink,
    DriveCapabilities,
    DriveFile,
    DriveQuota,
    DriveShare,
    LinkPolicy,
    LoginResult,
    LoginSession,
    NetdiskAccountRecord,
    NetdiskPlatform,
    NetdiskTuning,
    ShareDetail,
    ShareFile,
    ShareRef,
    TransferHandle,
)


@dataclass(frozen=True, slots=True)
class NetdiskAccountView:
    """
    账号的脱敏展示投影。

    :param id: 账号主键
    :param platform: 平台标识
    :param name: 账号名称
    :param is_primary: 是否为主账号
    :param status: 账号状态
    :param expires_at: 已知过期时间
    :param last_checked_at: 最近一次探测时间
    :param masked: 脱敏后的凭据字段摘要
    """

    id: int
    platform: str
    name: str
    is_primary: bool
    status: str
    expires_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    masked: Mapping[str, str] = field(default_factory=dict)
    nickname: str = ""
    quota_used: int = 0
    quota_total: int = 0


@dataclass(frozen=True, slots=True)
class NetdiskAccountDetail:
    """
    账号详情：脱敏投影与明文凭据。

    仅由超级管理员鉴权的详情接口消费，调用方不得写入日志或告警。

    :param account: 账号脱敏投影
    :param credential: 解密后的明文凭据字段
    """

    account: NetdiskAccountView
    credential: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NetdiskAccountWrite:
    """
    账号写入载荷。

    :param platform: 平台标识
    :param name: 账号名称
    :param credential: 明文凭据字段，由仓储负责加密
    :param is_primary: 是否设为主账号
    """

    platform: NetdiskPlatform
    name: str
    credential: Mapping[str, str]
    is_primary: bool = False
    nickname: str = ""
    quota_used: int = 0
    quota_total: int = 0


class CredentialCipher(Protocol):
    """
    网盘凭据的对称加解密端口。
    """

    def encrypt(self, plaintext: str) -> str:
        """
        加密凭据文本。

        :param plaintext: 明文
        :return: 密文字符串
        """
        ...

    def decrypt(self, token: str) -> Optional[str]:
        """
        解密凭据文本。

        :param token: 密文字符串
        :return: 明文；密钥不匹配或数据损坏时返回 None
        """
        ...


class NetdiskAccountStore(Protocol):
    """
    账号持久化端口。

    实现负责凭据加解密、唯一约束与主账号维护，调用方只处理明文凭据。
    """

    def list_accounts(self, platform: Optional[NetdiskPlatform] = None) -> list[NetdiskAccountView]:
        """
        列出账号脱敏投影。

        :param platform: 限定平台，None 表示全部
        :return: 按平台与创建顺序排列的账号列表
        """
        ...

    def get_record(self, account_id: int) -> Optional[NetdiskAccountRecord]:
        """
        按主键读取账号完整记录。

        :param account_id: 账号主键
        :return: 账号记录；不存在时返回 None
        """
        ...

    def create_account(self, payload: NetdiskAccountWrite) -> NetdiskAccountView:
        """
        创建账号并维护同平台主账号唯一性。

        :param payload: 账号写入载荷
        :return: 新账号的脱敏投影
        """
        ...

    def update_account(
        self,
        account_id: int,
        *,
        name: Optional[str] = None,
        credential: Optional[Mapping[str, str]] = None,
        is_primary: Optional[bool] = None,
        status: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        nickname: Optional[str] = None,
        quota_used: Optional[int] = None,
        quota_total: Optional[int] = None,
    ) -> Optional[NetdiskAccountView]:
        """
        更新账号字段。

        :param account_id: 账号主键
        :param name: 新名称
        :param credential: 新凭据，None 表示不改
        :param is_primary: 是否设为主账号
        :param status: 新状态
        :param expires_at: 新过期时间
        :param nickname: 账号昵称/手机号
        :param quota_used: 已用空间
        :param quota_total: 总空间
        :return: 更新后的脱敏投影；账号不存在时返回 None
        """
        ...

    def delete_account(self, account_id: int) -> bool:
        """
        删除账号。

        :param account_id: 账号主键
        :return: 删除成功返回 True
        """
        ...

    def touch_account(self, account_id: int, probe: AccountProbe) -> None:
        """
        记录一次账号探测结果。

        :param account_id: 账号主键
        :param probe: 探测结果
        """
        ...

    def update_quota(self, account_id: int, used: int, total: int) -> None:
        """
        只更新账号空间占用，不改变状态、检测时间与凭据。

        :param account_id: 账号主键
        :param used: 已用字节
        :param total: 总字节
        """
        ...


@runtime_checkable
class NetdiskSpeedStore(Protocol):
    """
    账号平均速度的持久化端口。
    """

    def load(self) -> Mapping[int, float]:
        """
        读取全部账号的平均速度。

        :return: 账号主键到字节每秒的映射；无数据时返回空映射
        """
        ...

    def save(self, speeds: Mapping[int, float]) -> None:
        """
        覆盖写入全部账号的平均速度。

        :param speeds: 账号主键到字节每秒的映射
        """
        ...


@runtime_checkable
class NetdiskAdapter(Protocol):
    """
    单个网盘的协议适配器。

    适配器只负责协议，不访问数据库、不决定调度、不选择账号。

    :cvar single_stream: 是否整文件单流下载，为真时每文件只用一条连接、不发 Range 分片请求
    """

    platform: NetdiskPlatform
    link_policy: LinkPolicy
    requires_account: bool
    guest_supported: bool
    max_concurrency: int
    single_stream: bool
    cookie_required_keys: tuple[str, ...]
    cookie_domains: tuple[str, ...]
    login_url: str
    supports_password_login: bool
    supports_refresh: bool
    drive_capabilities: DriveCapabilities

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从登录凭据中提取可展示的账号名。

        :param credential: 浏览器抓到的凭据
        :return: 账号名
        """
        ...

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询个人网盘空间。

        :param account: 账号
        :return: 空间配额
        """
        ...

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        列出个人网盘目录。

        :param account: 账号
        :param parent_id: 父目录
        :return: 目录条目
        """
        ...

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        ...

    def share_file(
        self,
        account: NetdiskAccountRecord,
        files: list[DriveFile],
        *,
        password: str = "",
        expires_in: int = 0,
    ) -> DriveShare:
        """
        创建个人盘分享。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码
        :param expires_in: 有效期秒数
        :return: 分享结果
        """
        ...

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动文件。
        """
        ...

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名文件。
        """
        ...

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除文件。
        """
        ...

    def list_recycle_bin(self, account: NetdiskAccountRecord) -> dict[str, Any]:
        """
        读取个人回收站摘要。

        :param account: 账号
        :return: 含 `count`、`rb_pass` 与 `items` 的摘要
        """
        ...

    def clear_recycle_bin(self, account: NetdiskAccountRecord) -> dict[str, Any]:
        """
        清空个人回收站。

        :param account: 账号
        :return: 含 `cleared` 与 `count` 的结果
        """
        ...

    def parse_share(self, url: str, password: Optional[str]) -> ShareRef:
        """
        解析分享链接。

        :param url: 分享链接或文案
        :param password: 已知提取码
        :return: 分享引用
        """
        ...

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        获取分享详情与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号，游客链路为 None
        :return: 分享详情
        """
        ...

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        列出分享目录内容。

        :param ref: 分享引用
        :param parent_id: 父目录标识
        :param account: 取链账号
        :return: 目录条目列表
        """
        ...

    def resolve(
        self,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        allow_transfer: bool = True,
        transfer_first: bool = False,
    ) -> DirectLink:
        """
        获取单个文件的下载直链。

        免转存不可用且 `allow_transfer` 为真时，适配器可以转存到唯一临时目录后取链，
        并在返回的直链上携带清理句柄。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底
        :param transfer_first: 是否优先转存；False 时保持免转存优先
        :return: 直链及其请求条件
        """
        ...

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        清理转存产生的临时内容。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        ...

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        ...

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验一份新凭据是否可用。

        :param credential: 明文凭据字段
        :return: 探测结果
        """
        ...

    def refresh_account(self, account: NetdiskAccountRecord) -> Optional[Mapping[str, str]]:
        """
        刷新账号令牌。

        :param account: 账号记录
        :return: 刷新后的凭据；平台不支持或无需刷新时返回 None
        """
        ...

    def password_login(self, username: str, password: str) -> Mapping[str, str]:
        """
        使用账号密码换取凭据。

        :param username: 账号
        :param password: 密码
        :return: 可保存的凭据字段
        """
        ...


class NetdiskLoginAdapter(Protocol):
    """
    服务端无头浏览器登录端口。
    """

    def start(
        self,
        platform: NetdiskPlatform,
        login_url: str,
        account_id: Optional[int] = None,
    ) -> LoginSession:
        """
        启动登录会话并返回二维码截图。

        :param platform: 平台标识
        :param login_url: 平台登录页地址
        :param account_id: 关联账号，新增账号时为 None
        :return: 登录会话
        """
        ...

    def poll(self, session_id: str) -> LoginResult:
        """
        轮询登录状态。

        :param session_id: 会话标识
        :return: 当前轮询结果
        """
        ...

    def cancel(self, session_id: str) -> None:
        """
        取消会话并释放浏览器。

        :param session_id: 会话标识
        """
        ...


class CookieImportPort(Protocol):
    """
    CookieCloud 等外部 Cookie 来源的导入端口。
    """

    def import_cookies(self, domains: Sequence[str]) -> Optional[Mapping[str, str]]:
        """
        按域名导入 Cookie 头。

        :param domains: 平台域名后缀列表
        :return: Cookie 名称到值的映射；未配置或无数据时返回 None
        """
        ...


class NetdiskSettingsPort(Protocol):
    """
    下载与取链参数的读取端口。
    """

    def load(self) -> NetdiskTuning:
        """
        读取当前参数快照。

        :return: 不可变参数快照
        """
        ...


__all__ = [
    "CookieImportPort",
    "CredentialCipher",
    "LoginResult",
    "LoginSession",
    "NetdiskAccountDetail",
    "NetdiskAccountRecord",
    "NetdiskAccountStore",
    "NetdiskAccountView",
    "NetdiskAccountWrite",
    "NetdiskAdapter",
    "NetdiskLoginAdapter",
    "NetdiskSettingsPort",
    "NetdiskSpeedStore",
    "NetdiskTuning",
]
