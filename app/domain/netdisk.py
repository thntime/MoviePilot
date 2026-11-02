"""
网盘平台取链的纯业务模型与规则。

本模块只包含枚举、不可变 DTO、错误分类与分享链接解析，不访问网络、数据库或运行时配置。
平台协议细节由 `app/adapters/external/netdisk/` 承担，取链编排由
`app/application/netdisk/` 承担。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Optional


class NetdiskPlatform(StrEnum):
    """
    已支持取链的网盘平台。

    :cvar QUARK: 夸克网盘
    :cvar UC: UC 网盘
    :cvar PAN139: 中国移动云盘（139）
    :cvar PAN123: 123 云盘
    :cvar PAN115: 115 网盘
    :cvar GUANGYA: 光鸭云盘
    :cvar BAIDU: 百度网盘
    :cvar XUNLEI: 迅雷云盘
    """

    QUARK = "quark"
    UC = "uc"
    PAN139 = "pan139"
    PAN123 = "pan123"
    PAN115 = "pan115"
    GUANGYA = "guangya"
    BAIDU = "baidu"
    XUNLEI = "xunlei"


class LinkPolicy(StrEnum):
    """
    平台取链策略。

    :cvar DIRECT: 始终免转存直接取链
    :cvar DIRECT_WITH_FALLBACK: 优先免转存，失败时回退转存
    :cvar TRANSFER_REQUIRED: 必须转存后才能取链
    """

    DIRECT = "direct"
    DIRECT_WITH_FALLBACK = "direct_with_fallback"
    TRANSFER_REQUIRED = "transfer_required"


class NetdiskErrorCode(StrEnum):
    """
    网盘取链与登录的统一错误分类。

    :cvar AUTH_EXPIRED: 登录态缺失、过期或无效
    :cvar SHARE_INVALID: 分享不存在、已删除或提取码错误
    :cvar RATE_LIMITED: 平台限流或频控
    :cvar RISK_BLOCKED: 风控、验证码或设备异常
    :cvar LINK_EXPIRED: 直链过期或防盗链拦截
    :cvar UNSUPPORTED: 平台或分享类型未实现
    :cvar SPACE_INSUFFICIENT: 转存时网盘空间不足
    :cvar UNKNOWN: 无法归类的失败
    """

    AUTH_EXPIRED = "auth_expired"
    SHARE_INVALID = "share_invalid"
    RATE_LIMITED = "rate_limited"
    RISK_BLOCKED = "risk_blocked"
    LINK_EXPIRED = "link_expired"
    UNSUPPORTED = "unsupported"
    SPACE_INSUFFICIENT = "space_insufficient"
    UNKNOWN = "unknown"


class NetdiskError(Exception):
    """
    网盘操作的分类异常。

    异常消息只包含可直接展示的原因，不包含 Cookie、token 或完整响应体。

    :param code: 统一错误分类
    :param platform: 出错平台，未知时为空
    :param account_name: 出错账号名称，未使用账号时为空
    :param message: 可直接展示的中文原因
    """

    def __init__(
        self,
        code: NetdiskErrorCode,
        message: str,
        *,
        platform: Optional[NetdiskPlatform] = None,
        account_name: Optional[str] = None,
    ) -> None:
        """
        保存错误分类与脱敏后的展示信息。
        """
        super().__init__(message)
        self.code = code
        self.platform = platform
        self.account_name = account_name
        self.message = message


@dataclass(frozen=True, slots=True)
class ShareRef:
    """
    分享链接的解析结果。

    :param platform: 平台标识
    :param share_id: 平台分享码
    :param password: 提取码，未提供时为 None
    :param url: 规范化后的分享地址
    :param share_token: 分享令牌，候选透传时使用
    """

    platform: NetdiskPlatform
    share_id: str
    password: Optional[str] = None
    url: str = ""
    share_token: str = ""


@dataclass(frozen=True, slots=True)
class ShareFile:
    """
    分享内的文件或目录条目。

    :param file_id: 平台文件标识
    :param name: 文件或目录名称
    :param size: 文件大小，单位字节；目录为 0
    :param is_dir: 是否为目录
    :param parent_id: 父目录标识
    :param token: 平台返回的条目令牌，部分平台取链需要
    """

    file_id: str
    name: str
    size: int = 0
    is_dir: bool = False
    parent_id: str = ""
    token: str = ""


@dataclass(frozen=True, slots=True)
class ShareDetail:
    """
    分享详情。

    :param title: 分享标题
    :param size: 分享总大小，单位字节
    :param files: 根目录条目
    :param stoken: 分享令牌
    """

    title: str = ""
    size: int = 0
    files: tuple[ShareFile, ...] = field(default_factory=tuple)
    stoken: str = ""


@dataclass(frozen=True, slots=True)
class TransferHandle:
    """
    转存句柄，供下载完成后清理临时内容。

    :param platform: 平台标识
    :param account_id: 转存使用的账号主键
    :param directory_id: 临时目录标识
    :param file_ids: 转存后的文件标识
    """

    platform: NetdiskPlatform
    account_id: int
    directory_id: str = ""
    file_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DirectLink:
    """
    可直接下载的直链及其请求条件。

    :param url: 直链地址
    :param headers: 下载请求头，含 Cookie、UA、Referer
    :param size: 文件大小，单位字节
    :param expires_at: 直链过期时间，未知时为 None
    :param cleanup_handle: 下载完成后需要清理的转存句柄
    :param account_id: 取链使用的账号主键
    :param is_hls: 是否为 HLS（m3u8）分片流
    """

    url: str
    headers: Mapping[str, str]
    size: int = 0
    expires_at: Optional[datetime] = None
    cleanup_handle: Optional[TransferHandle] = None
    account_id: int = 0
    is_hls: bool = False


@dataclass(frozen=True, slots=True)
class NetdiskTuning:
    """
    下载与取链的可调参数快照。

    :param threads: 单任务默认并发连接数
    :param max_tasks: 单任务最大并发下载文件数
    :param rate_limit: 全局限速，单位字节/秒；0 表示不限
    :param retries: 分片重试次数
    :param buffer_size: 网络读缓冲大小，单位字节
    :param preempt_enabled: 是否启用慢连接抢占
    :param preempt_bps: 慢连接判定阈值，单位字节/秒
    :param preempt_age_seconds: 慢连接判定时长，单位秒
    :param multi_source: 是否启用多账号多源
    :param auto_transfer: 是否允许自动转存
    :param refresh_seconds: 直链刷新节流，单位秒
    :param probe_concurrency: 有效性检查并发数
    :param probe_budget_seconds: 有效性检查总预算，单位秒
    :param probe_limit: 单次搜索最多检查的候选数
    """

    threads: int = 8
    max_tasks: int = 3
    rate_limit: int = 0
    retries: int = 3
    buffer_size: int = 64 * 1024
    preempt_enabled: bool = True
    preempt_bps: int = 12 * 1024
    preempt_age_seconds: int = 15
    multi_source: bool = False
    auto_transfer: bool = True
    refresh_seconds: int = 30
    probe_concurrency: int = 16
    probe_budget_seconds: int = 30
    probe_limit: int = 200


@dataclass(frozen=True, slots=True)
class NetdiskAccountRecord:
    """
    账号的完整凭据记录，只在应用层与适配器之间流转。

    :param id: 账号主键
    :param platform: 平台标识
    :param name: 账号名称
    :param credential: 解密后的凭据字段
    :param is_primary: 是否为主账号
    :param status: `active`、`expired` 或 `disabled`
    :param expires_at: 已知过期时间
    """

    id: int
    platform: NetdiskPlatform
    name: str
    credential: Mapping[str, str] = field(default_factory=dict)
    is_primary: bool = False
    status: str = "active"
    expires_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class LoginSession:
    """
    一次扫码登录会话。

    :param session_id: 会话标识
    :param platform: 平台标识
    :param image: 二维码图片字节，非图片登录时为 None
    :param page_image: 完整登录页截图字节，便于用户查看页面其他入口
    :param status: `pending`、`success`、`failed` 或 `expired`
    :param message: 可直接展示的说明
    :param expires_at: 会话过期时间
    """

    session_id: str
    platform: NetdiskPlatform
    image: Optional[bytes] = None
    page_image: Optional[bytes] = None
    status: str = "pending"
    message: str = ""
    expires_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class LoginResult:
    """
    扫码登录轮询结果。

    :param status: `pending`、`success`、`failed` 或 `expired`
    :param credential: 登录成功后的凭据
    :param message: 可直接展示的说明
    """

    status: str
    credential: Optional[Mapping[str, str]] = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class AccountProbe:
    """
    账号探测结果。

    :param status: `active`、`expired` 或 `unknown`
    :param message: 可直接展示的说明
    :param expires_at: 平台给出的过期时间，未知时为 None
    """

    status: str
    message: str = ""
    expires_at: Optional[datetime] = None


_PLATFORM_ALIASES: dict[str, NetdiskPlatform] = {
    "quark": NetdiskPlatform.QUARK,
    "uc": NetdiskPlatform.UC,
    "ucdrive": NetdiskPlatform.UC,
    "pan139": NetdiskPlatform.PAN139,
    "c139": NetdiskPlatform.PAN139,
    "139": NetdiskPlatform.PAN139,
    "yidong": NetdiskPlatform.PAN139,
    "mobile": NetdiskPlatform.PAN139,
    "pan123": NetdiskPlatform.PAN123,
    "123": NetdiskPlatform.PAN123,
    "pan115": NetdiskPlatform.PAN115,
    "115": NetdiskPlatform.PAN115,
    "guangya": NetdiskPlatform.GUANGYA,
    "guangyapan": NetdiskPlatform.GUANGYA,
    "baidu": NetdiskPlatform.BAIDU,
    "baidupan": NetdiskPlatform.BAIDU,
    "xunlei": NetdiskPlatform.XUNLEI,
    "thunder": NetdiskPlatform.XUNLEI,
}


def mask_credential(credential: Mapping[str, str]) -> dict[str, str]:
    """
    生成只用于展示的脱敏凭据摘要。

    :param credential: 明文凭据字段
    :return: 字段名到固定掩码的映射
    """
    return {str(key): "***" for key in credential}


def normalize_platform(value: object) -> Optional[NetdiskPlatform]:
    """
    把 CloudSaver 或用户输入的平台标识收敛为统一枚举。

    :param value: 平台标识原文，允许大小写与下划线差异
    :return: 统一平台枚举；无法识别时返回 None
    """
    text = str(value or "").strip().lower().replace("-", "").replace("_", "")
    if not text:
        return None
    return _PLATFORM_ALIASES.get(text)


_URL_PATTERN = re.compile(r"https?://[^\s]+")
_QUARK_PATTERN = re.compile(r"pan\.quark\.cn/s/([A-Za-z0-9]+)", re.IGNORECASE)
_UC_PATTERN = re.compile(r"drive\.uc\.cn/s/([A-Za-z0-9]+)", re.IGNORECASE)
_XUNLEI_PATTERN = re.compile(r"pan\.xunlei\.com/s/([A-Za-z0-9_-]+)", re.IGNORECASE)
_BAIDU_PATTERN = re.compile(r"pan\.baidu\.com/s/(1[A-Za-z0-9_-]+)", re.IGNORECASE)
_C139_PATTERN = re.compile(r"yun\.139\.com/shareweb/.*?/w/i/([A-Za-z0-9_-]+)", re.IGNORECASE)
_PAN123_PATTERN = re.compile(r"123(?:865|pan)\.(?:com|cn)/s/([A-Za-z0-9]+-[A-Za-z0-9]+)", re.IGNORECASE)
_PAN123_SUB_PATTERN = re.compile(r"share\.123pan\.cn/123pan/([A-Za-z0-9-]+)", re.IGNORECASE)
_PAN123_SRR_PATTERN = re.compile(r"api/srr\?sk=([A-Za-z0-9-]+)", re.IGNORECASE)
_PAN115_PATTERN = re.compile(r"115(?:cdn|rc)?\.com/s/(sw[A-Za-z0-9]+)", re.IGNORECASE)
_PAN115_COMMAND_PATTERN = re.compile(r"115(?:cdn|rc)?\.com/(sw[A-Za-z0-9]{8,})-([A-Za-z0-9]{4,8})", re.IGNORECASE)
_GUANGYA_PATTERN = re.compile(r"guangyapan\.com/s/([A-Za-z0-9_-]+)", re.IGNORECASE)
_PWD_IN_URL_PATTERN = re.compile(r"[?&](?:pwd|password|passcode|pass)=([A-Za-z0-9]+)", re.IGNORECASE)
_PWD_IN_TEXT_PATTERN = re.compile(r"(?:提取码|访问码|密码)[：:]\s*([A-Za-z0-9]{2,16})")


def parse_share_link(text: str, password: Optional[str] = None) -> Optional[ShareRef]:
    """
    从分享链接或整段分享文案中解析平台、分享码与提取码。

    解析顺序与平台域名规则保持一致，115 口令形式优先于普通链接。

    :param text: 分享链接或包含链接的整段文案
    :param password: 调用方已知的提取码，优先于文案解析结果
    :return: 分享引用；无法识别时返回 None
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _URL_PATTERN.search(raw)
    url = match.group(0).rstrip("。，,；;)]}\"'") if match else raw
    pwd = password or _PWD_IN_URL_PATTERN.search(url)
    if not isinstance(pwd, str):
        pwd = pwd.group(1) if pwd else None
    if not pwd:
        text_match = _PWD_IN_TEXT_PATTERN.search(raw)
        pwd = text_match.group(1) if text_match else None

    def build(platform: NetdiskPlatform, share_id: str, code: Optional[str]) -> ShareRef:
        """
        构造统一的分享引用。
        """
        return ShareRef(platform=platform, share_id=share_id, password=code, url=url)

    command = _PAN115_COMMAND_PATTERN.search(url)
    if command:
        return build(NetdiskPlatform.PAN115, command.group(1), command.group(2))
    for pattern, platform in (
        (_QUARK_PATTERN, NetdiskPlatform.QUARK),
        (_UC_PATTERN, NetdiskPlatform.UC),
        (_XUNLEI_PATTERN, NetdiskPlatform.XUNLEI),
        (_BAIDU_PATTERN, NetdiskPlatform.BAIDU),
        (_C139_PATTERN, NetdiskPlatform.PAN139),
        (_PAN123_PATTERN, NetdiskPlatform.PAN123),
        (_PAN123_SUB_PATTERN, NetdiskPlatform.PAN123),
        (_PAN123_SRR_PATTERN, NetdiskPlatform.PAN123),
        (_PAN115_PATTERN, NetdiskPlatform.PAN115),
        (_GUANGYA_PATTERN, NetdiskPlatform.GUANGYA),
    ):
        found = pattern.search(url)
        if not found:
            continue
        share_id = found.group(1)
        if platform is NetdiskPlatform.BAIDU:
            share_id = share_id.removeprefix("1")
        return build(platform, share_id, pwd)
    return None
