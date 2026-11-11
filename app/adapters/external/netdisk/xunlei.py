"""
迅雷云盘的分享解析、目录读取与转存取链适配器。

协议依据 YunX `XunleiResolveRepository` / `XunleiApi` / `XunleiWebCredential`：
分享列表 `GET /drive/v1/share` 与子目录 `GET /drive/v1/share/detail` 允许匿名读取，
转存 `POST /drive/v1/share/restore` 与文件详情取链必须使用 `access_token`。

迅雷凭据来自网页版 localStorage：`access_token`、`deviceid`、`captcha` 三者必须
属于同一个账号。分享必须转存后才能取链，下载完成后删除临时目录。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.adapters.external.netdisk.base import (
    BaseNetdiskAdapter,
    as_int,
    as_text,
)
from app.adapters.network.http import RequestUtils
from app.domain.netdisk import NetdiskAccountRecord
from app.domain.netdisk import (
    AccountProbe,
    DirectLink,
    DriveCapabilities,
    DriveFile,
    DriveQuota,
    DriveShare,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
    TransferHandle,
)
from app.runtime.log import logger

# 云盘接口主机。
_PAN_BASE = "https://api-pan.xunlei.com"
_FILES_URL = f"{_PAN_BASE}/drive/v1/files"
_BATCH_DELETE_URL = f"{_FILES_URL}:batchDelete"
_SHARE_URL = f"{_PAN_BASE}/drive/v1/share"
_SHARE_DETAIL_URL = f"{_PAN_BASE}/drive/v1/share/detail"
_RESTORE_URL = f"{_PAN_BASE}/drive/v1/share/restore"
_ABOUT_URL = f"{_PAN_BASE}/drive/v1/about"
_AUTH_BASE = "https://xluser-ssl.xunlei.com"
_AUTH_TOKEN_URL = f"{_AUTH_BASE}/v1/auth/token"
_MOVE_URL = f"{_FILES_URL}:batchMove"
_TRASH_URL = f"{_FILES_URL}:batchTrash"

# 网页登录使用的 client_id；与 `login/browser.py` 的 `_XUNLEI_CLIENT_ID` 保持一致。
_AUTH_CLIENT_ID = "Xqp0kJBXWhwaTpB6"
# 网页版 captcha 现签：client_version 与 9 条盐随网页 bundle 轮换，需整组更新。
_CAPTCHA_INIT_URL = f"{_AUTH_BASE}/v1/shield/captcha/init"
_CAPTCHA_CLIENT_VERSION = "1.93.10"
_CAPTCHA_PACKAGE_NAME = "pan.xunlei.com"
_CAPTCHA_SIGN_HOST = "pan.xunlei.com"
_CAPTCHA_ALG_VERSION = "1"
_CAPTCHA_ACTION = "GET:/drive/v1/about"
# 服务端 captcha_token 有效期 300s，保守提前 30s 视为过期。
_CAPTCHA_TTL_SECONDS = 270
_CAPTCHA_SALTS = (
    "DVM8BRQfJWDsIE0JbH5GCUM0z0jU1yGymBTj4Eyloazv1F+w0G7ig",
    "T",
    "a1fUxfjOO1/dW++DpGupRgw9P93L",
    "aktKbp+/lQc3MvlMHIupt65LjAgJOL6ttMFvTKK+7+WKubSf9rT",
    "SB/ne9FQUnQmWz4Wuv9KqKfGOBOCUubpHpECxOHfD4hNoMclYA2Yq",
    "sOZFBQ8IMFy3OFjAIf/eA65iqFvS9TIKzLyrhiT6",
    "ZhJdLgL7MGh2GanboB0yk",
    "Osf",
    "v",
)
# JWT 距过期不足该秒数时视为临期，需要先续期。
_JWT_LEEWAY_SECONDS = 60.0
# 与桌面 Chrome UA 匹配的客户端提示头。
_SEC_CH_UA = '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
_SEC_CH_UA_MOBILE = "?0"
_SEC_CH_UA_PLATFORM = '"Windows"'

# 网页客户端 UA 与请求头常量，与迅雷网页版抓包一致。
_CLIENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CLIENT_VERSION = "8.31.0.9726"
_ORIGIN = "https://pan.xunlei.com"
_REFERER = "https://pan.xunlei.com/"
# 临时转存目录名前缀，后面拼接时间戳与随机串，避免平台按目录去重。
_TEMP_DIR_PREFIX = "MoviePilot临时转存"

# 分享列表单页条数与最大翻页次数。
_PAGE_LIMIT = 100
_MAX_PAGES = 100
# 个人盘文件列表单页条数（对齐 YunX 抓包）。
_DRIVE_PAGE_LIMIT = 50

# 取链只请求 usage=PLAY（DOWNLOAD 已被接口废弃并返回 400），轮间按指数退避等待转码就绪。
# 元组是「轮间等待」序列，因此总请求次数比它多一次（1+2+4+8+16≈31s，共 6 次请求）。
_LINK_RETRY_DELAYS = (1, 2, 4, 8, 16)
# 文件审核状态为正常时，取链失败才按「未就绪」处理。
_LINK_AUDIT_OK = "STATUS_OK"

# 云盘业务接口均为国内直连域名，显式关闭宿主代理。
_DIRECT_PROXIES = {"http": "", "https": ""}

# 凭据中可能出现的 Cookie 容器字段。
_COOKIE_KEYS = frozenset({"cookie", "cookie_header", "cookies"})


def _fields(credential: Mapping[str, str]) -> dict[str, str]:
    """
    把凭据展开为字段字典。

    `{"cookie": "access_token=...; deviceid=..."}` 会拆成独立字段，其余键原样保留。

    :param credential: 明文凭据字段
    :return: 展开后的字段字典
    """
    fields: dict[str, str] = {}
    for key, value in credential.items():
        text = as_text(value)
        if key in _COOKIE_KEYS:
            for part in text.split(";"):
                item = part.strip()
                if "=" not in item:
                    continue
                name, field_value = item.split("=", 1)
                fields[name.strip()] = field_value.strip()
            continue
        fields[as_text(key)] = text
    return fields


def _pick(fields: Mapping[str, str], *names: str) -> str:
    """
    读取第一个非空字段。

    :param fields: 字段字典
    :param names: 候选字段名
    :return: 字段值；全部缺失时为空串
    """
    for name in names:
        value = as_text(fields.get(name))
        if value:
            return value
    return ""


def _strip_bearer(value: str) -> str:
    """
    去掉 token 的 `Bearer ` 前缀与首尾空白。

    :param value: 原始 token
    :return: 纯 token
    """
    text = as_text(value)
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text


def _identity_from(fields: Mapping[str, str]) -> tuple[str, str, str]:
    """
    从字段中读取 `(access_token, deviceid, captcha)`。

    :param fields: 展开后的字段字典
    :return: 三元组，缺失字段为空串
    """
    access = _strip_bearer(_pick(fields, "access_token", "accessToken", "token"))
    device = _pick(fields, "deviceid", "device_id", "deviceId")
    captcha = _pick(fields, "captcha", "captcha_token", "captchaToken")
    return access, device, captcha


def jwt_expires_at(token: str) -> Optional[float]:
    """
    从 JWT 载荷中读取过期时间戳，不校验签名、不发起网络请求。

    :param token: JWT 文本
    :return: `exp` 对应的 Unix 时间戳；不是 JWT 或缺少 `exp` 时返回 None
    """
    segments = str(token or "").split(".")
    if len(segments) < 2:
        return None
    payload = segments[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}")
        data = json.loads(decoded)
        return float(data.get("exp"))
    except (AttributeError, TypeError, ValueError):
        return None


def jwt_expiring(token: str, *, now: Optional[float] = None, leeway: float = _JWT_LEEWAY_SECONDS) -> bool:
    """
    判断 JWT 是否已进入临期窗口。

    :param token: JWT 文本
    :param now: 当前 Unix 时间戳；省略时取 `time.time()`
    :param leeway: 提前视为临期的秒数
    :return: 剩余时间不足 `leeway` 时返回 True；无法解析时返回 False
    """
    expires_at = jwt_expires_at(token)
    if expires_at is None:
        return False
    current = time.time() if now is None else now
    return expires_at - current <= leeway


def _jwt_claim(token: str, name: str) -> str:
    """
    读取 JWT 载荷中的字符串声明，不校验签名。

    :param token: JWT 文本
    :param name: 声明名
    :return: 声明值；缺失或无法解析时为空串
    """
    segments = str(token or "").split(".")
    if len(segments) < 2:
        return ""
    payload = segments[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(f"{payload}{padding}"))
    except (AttributeError, TypeError, ValueError):
        return ""
    value = data.get(name) if isinstance(data, Mapping) else None
    return as_text(value)


def _expiry_epoch(value: Any) -> float:
    """
    把秒/毫秒时间戳或 ISO 时间文本换算为 Unix 时间戳。

    :param value: 时间戳或 ISO 文本
    :return: Unix 时间戳；无法解析时返回 0
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        number = float(text)
    except ValueError:
        number = 0.0
    if number:
        return number / 1000 if number > 1e12 else number
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _captcha_sign(device: str, timestamp_ms: int) -> str:
    """
    按网页版算法现签 captcha：client_id + version + host + device + 时间戳，再依次套 9 条盐做 MD5。

    :param device: 设备标识
    :param timestamp_ms: 毫秒时间戳
    :return: 带算法版本前缀的签名
    """
    digest = f"{_AUTH_CLIENT_ID}{_CAPTCHA_CLIENT_VERSION}{_CAPTCHA_SIGN_HOST}{device}{timestamp_ms}"
    for salt in _CAPTCHA_SALTS:
        digest = hashlib.md5(f"{digest}{salt}".encode()).hexdigest()
    return f"{_CAPTCHA_ALG_VERSION}.{digest}"


def _stored_captcha(fields: Mapping[str, str]) -> str:
    """
    读取仍在有效期内的已保存 captcha。

    :param fields: 展开后的凭据字段
    :return: 有效 captcha；缺失或已过期时为空串
    """
    captcha = _pick(fields, "captcha", "captcha_token", "captchaToken")
    if not captcha:
        return ""
    expires_at = _pick(fields, "captcha_expires_at", "captchaExpiresAt")
    if not expires_at:
        return ""
    return captcha if _expiry_epoch(expires_at) > time.time() else ""


def _classify(error: str, message: str) -> NetdiskErrorCode:
    """
    把迅雷业务错误收敛为统一分类。

    :param error: 响应 `error` 字段
    :param message: 服务端提示
    :return: 统一错误分类
    """
    lower = f"{error} {message}".lower()
    if error == "unauthenticated" or ("token" in lower and ("expired" in lower or "invalid" in lower)):
        return NetdiskErrorCode.AUTH_EXPIRED
    if any(word in lower for word in ("captcha", "risk", "verify", "forbidden")):
        return NetdiskErrorCode.RISK_BLOCKED
    if any(word in lower for word in ("space", "quota", "insufficient")):
        return NetdiskErrorCode.SPACE_INSUFFICIENT
    if any(word in lower for word in ("rate", "too many", "too_many", "frequent")):
        return NetdiskErrorCode.RATE_LIMITED
    if "pass_code" in lower or "passcode" in lower:
        return NetdiskErrorCode.SHARE_INVALID
    if "share" in lower and any(word in lower for word in ("not", "invalid", "expire", "delete")):
        return NetdiskErrorCode.SHARE_INVALID
    if "空间" in message or "验证码" in message or "风控" in message:
        if "空间" in message:
            return NetdiskErrorCode.SPACE_INSUFFICIENT
        return NetdiskErrorCode.RISK_BLOCKED
    if "分享" in message and any(word in message for word in ("失效", "不存在", "过期", "提取码")):
        return NetdiskErrorCode.SHARE_INVALID
    return NetdiskErrorCode.UNKNOWN


def _parse_files(data: Mapping[str, Any]) -> list[ShareFile]:
    """
    把迅雷文件数组映射为统一文件条目。

    :param data: 响应中的 data 对象
    :return: 文件与目录条目
    """
    files: list[ShareFile] = []
    for item in data.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        files.append(
            ShareFile(
                file_id=as_text(item.get("id")),
                name=as_text(item.get("name")),
                size=as_int(item.get("size")),
                is_dir=as_text(item.get("kind")) == "drive#folder",
                parent_id=as_text(item.get("parent_id")),
            )
        )
    return files


def _link_url(value: Any) -> str:
    """
    从单个 links 值中取出 url；兼容 mapping 嵌套与 list。

    :param value: links 字段下的单个值
    :return: 直链；没有时为空串
    """
    if isinstance(value, str):
        return as_text(value)
    if isinstance(value, Mapping):
        url = as_text(value.get("url"))
        if url:
            return url
        link = value.get("link")
        if link is not None and link is not value:
            url = _link_url(link)
            if url:
                return url
        return as_text(value.get("download_url"))
    if isinstance(value, list):
        for item in value:
            url = _link_url(item)
            if url:
                return url
    return ""


def _first_link_url(links: Any) -> str:
    """
    从迅雷文件详情的 links 中取第一个可用直链。

    线上 links 既可能是 mapping（按 MIME 分键），也可能是 list；
    优先取原始文件类型，取不到再遍历全部值，并兼容 `medias` 列表结构。

    :param links: 文件详情中的 links 字段
    :return: 直链；没有时为空串
    """
    if isinstance(links, Mapping):
        for key in (
            "application/octet-stream",
            "application/octet-stream; charset=utf-8",
            "video/mp4",
            "video/x-matroska",
        ):
            url = _link_url(links.get(key))
            if url:
                return url
        medias = links.get("medias")
        if isinstance(medias, list):
            for media in medias:
                url = _link_url(media)
                if url:
                    return url
        for value in links.values():
            url = _link_url(value)
            if url:
                return url
    elif isinstance(links, list):
        for value in links:
            url = _link_url(value)
            if url:
                return url
    logger.debug(
        f"迅雷 links 未解析出直链：type={type(links).__name__} "
        f"keys={list(links.keys()) if isinstance(links, Mapping) else '-'}"
    )
    return ""


class XunleiAdapter(BaseNetdiskAdapter):
    """
    迅雷云盘协议适配器。

    分享必须转存后取链：先做空间预算校验，再转存到唯一临时目录，
    最后读取文件详情中的 `application/octet-stream` 直链并返回清理句柄。
    """

    platform = NetdiskPlatform.XUNLEI
    link_policy = LinkPolicy.TRANSFER_REQUIRED
    requires_account = True
    guest_supported = False
    supports_refresh = True
    # 设备标识 -> (captcha_token, 过期时间戳)；captcha 只有 300s，进程内缓存即可。
    _captcha_cache: dict[str, tuple[str, float]] = {}
    _captcha_lock = threading.Lock()
    drive_capabilities = DriveCapabilities(
        quota=True,
        browse=True,
        download=True,
        share=True,
        move=True,
        rename=True,
        delete=True,
    )
    max_concurrency = 8
    cookie_required_keys = ("access_token", "deviceid")
    cookie_domains = (".xunlei.com",)
    login_url = "https://pan.xunlei.com/"
    supports_password_login = False
    user_agent = _CLIENT_UA
    referer = _REFERER

    # ---- 凭据与请求头 ----

    def _identity(self, account: Optional[NetdiskAccountRecord]) -> tuple[str, str, str]:
        """
        读取账号的访问令牌、设备标识与验证码令牌。

        迅雷网页版 captcha 只有 300s 有效期，已保存的 token 过期后继续使用会被风控
        判为 `captcha_required`；这里优先使用未过期的已保存 token，否则本地现签一个。

        :param account: 账号记录
        :return: `(access_token, deviceid, captcha)`
        :raises NetdiskError: 账号缺失或必填字段缺失时抛出 `AUTH_EXPIRED`
        """
        if account is None:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "迅雷云盘取链需要登录账号",
                platform=NetdiskPlatform.XUNLEI,
            )
        fields = _fields(account.credential or {})
        access, device, _captcha = _identity_from(fields)
        if not access:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "迅雷云盘登录态缺失，请重新登录",
                platform=NetdiskPlatform.XUNLEI,
                account_name=account.name,
            )
        if not device:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "迅雷云盘凭据缺少设备标识",
                platform=NetdiskPlatform.XUNLEI,
                account_name=account.name,
            )
        return access, device, self._fresh_captcha(device, access, _stored_captcha(fields))

    def _init_captcha(self, device: str, access: str) -> tuple[str, float]:
        """
        本地现签 captcha 并换取新 token。

        :param device: 设备标识
        :param access: 访问令牌，用于读取 `user_id`
        :return: `(captcha_token, 过期时间戳)`；失败时返回空串与 0
        """
        if not device:
            return "", 0.0
        timestamp_ms = int(time.time() * 1000)
        body = {
            "client_id": _AUTH_CLIENT_ID,
            "action": _CAPTCHA_ACTION,
            "device_id": device,
            "meta": {
                "username": "",
                "phone_number": "",
                "email": "",
                "package_name": _CAPTCHA_PACKAGE_NAME,
                "client_version": _CAPTCHA_CLIENT_VERSION,
                "captcha_sign": _captcha_sign(device, timestamp_ms),
                "timestamp": str(timestamp_ms),
                "user_id": _jwt_claim(access, "sub"),
            },
        }
        headers = {
            "Accept": "*/*",
            "Origin": _ORIGIN,
            "Referer": _REFERER,
            "User-Agent": _CLIENT_UA,
        }
        try:
            payload = self.http.post_json(_CAPTCHA_INIT_URL, json=body, headers=headers)
        except NetdiskError:
            return "", 0.0
        token = as_text(payload.get("captcha_token"))
        if not token:
            return "", 0.0
        expires_in = as_int(payload.get("expires_in")) or _CAPTCHA_TTL_SECONDS
        return token, time.time() + min(expires_in, _CAPTCHA_TTL_SECONDS)

    def _fresh_captcha(self, device: str, access: str, stored: str = "") -> str:
        """
        取得当前可用的 captcha：内存缓存 → 未过期的已保存 token → 本地现签。

        :param device: 设备标识
        :param access: 访问令牌
        :param stored: 凭据里仍在有效期内的 captcha
        :return: captcha token；现签失败时退回已保存值
        """
        now = time.time()
        with self._captcha_lock:
            cached = self._captcha_cache.get(device)
            if cached and cached[1] > now:
                return cached[0]
        if stored:
            with self._captcha_lock:
                self._captcha_cache[device] = (stored, now + _CAPTCHA_TTL_SECONDS)
            return stored
        token, expires_at = self._init_captcha(device, access)
        if token:
            with self._captcha_lock:
                self._captcha_cache[device] = (token, expires_at)
            return token
        return stored

    def _api_headers(
        self,
        account: Optional[NetdiskAccountRecord],
        *,
        extra: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """
        组装云盘接口请求头。

        未登录时只带设备与客户端标识，不写 `Authorization`：迅雷分享接口允许匿名，
        但携带无效 Bearer 会被判定为未认证。

        :param account: 账号记录
        :param extra: 额外请求头
        :return: 请求头
        """
        headers = self.http.headers(None, include_cookie=False)
        headers.update(
            {
                "User-Agent": _CLIENT_UA,
                "X-Client-Version": _CLIENT_VERSION,
                "Origin": _ORIGIN,
                "Referer": _REFERER,
            }
        )
        if account is not None:
            access, device, captcha = self._identity(account)
            headers["Authorization"] = f"Bearer {access}"
            headers["X-Device-Id"] = device
            if captcha:
                headers["X-Captcha-Token"] = captcha
        if extra:
            headers.update({str(key): str(value) for key, value in extra.items()})
        return headers

    def _data(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """
        取出响应中的业务数据。

        部分接口把结果包在 `data` 里，网页通道则直接放在顶层。

        :param payload: 响应 JSON
        :return: 业务数据对象
        """
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else dict(payload)

    def _check_payload(self, payload: Mapping[str, Any], action: str) -> None:
        """
        校验迅雷业务响应中的 `error` 字段。

        :param payload: 响应 JSON
        :param action: 用于错误提示的动作名
        :raises NetdiskError: 响应包含错误时抛出
        """
        error = as_text(payload.get("error"))
        if not error:
            return
        message = (
            as_text(payload.get("error_description"))
            or as_text(payload.get("message"))
            or error
        )
        raise NetdiskError(
            _classify(error, message),
            f"{action}失败：{message}",
            platform=self.platform,
        )

    # ---- 分享解析 ----

    def _share(
        self,
        ref: ShareRef,
        account: Optional[NetdiskAccountRecord],
        *,
        page_token: str = "",
    ) -> dict[str, Any]:
        """
        读取分享首页信息，校验提取码状态。

        :param ref: 分享引用
        :param account: 取链账号
        :param page_token: 翻页游标
        :return: 分享数据对象
        :raises NetdiskError: 提取码错误、分享失效或接口失败时抛出
        """
        payload = self.http.get_json(
            _SHARE_URL,
            params={
                "share_id": ref.share_id,
                "pass_code": ref.password or "",
                "limit": _PAGE_LIMIT,
                "page_token": page_token,
                "thumbnail_size": "SIZE_SMALL",
            },
            headers=self._api_headers(account),
        )
        self._check_payload(payload, "获取迅雷云盘分享信息")
        data = self._data(payload)
        status = as_text(data.get("share_status"))
        if status == "PASS_CODE_EMPTY":
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "请输入迅雷云盘提取码",
                platform=self.platform,
            )
        if status == "PASS_CODE_ERROR":
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "迅雷云盘提取码错误",
                platform=self.platform,
            )
        if status == "PASS_CODE_NEED":
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "该迅雷云盘分享需要提取码",
                platform=self.platform,
            )
        return data

    def _pass_code_token(
        self,
        ref: ShareRef,
        account: Optional[NetdiskAccountRecord],
    ) -> str:
        """
        获取分享的 `pass_code_token`，优先使用调用方透传的分享令牌。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享令牌
        """
        if ref.share_token:
            return ref.share_token
        return as_text(self._share(ref, account).get("pass_code_token"))

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        读取分享标题与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情，标题回退为分享码
        """
        data = self._share(ref, account)
        files = _parse_files(data)
        return ShareDetail(
            title=as_text(data.get("title")) or ref.share_id,
            size=sum(item.size for item in files),
            files=tuple(files),
            stoken=as_text(data.get("pass_code_token")),
        )

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        分页读取分享目录。

        顶层使用 `share`（携带提取码），子目录使用 `share/detail`（携带父目录与分享令牌）。

        :param ref: 分享引用
        :param parent_id: 父目录标识，根目录为空或 `0`
        :param account: 取链账号
        :return: 目录内全部条目
        """
        is_root = parent_id in ("", "0")
        # 根目录的 share 接口本身会返回分享令牌，无需额外再取一次。
        token = "" if is_root else self._pass_code_token(ref, account)
        collected: list[ShareFile] = []
        page_token = ""
        for _ in range(_MAX_PAGES):
            if is_root:
                url = _SHARE_URL
                params: dict[str, Any] = {
                    "share_id": ref.share_id,
                    "pass_code": ref.password or "",
                    "limit": _PAGE_LIMIT,
                    "page_token": page_token,
                    "thumbnail_size": "SIZE_SMALL",
                }
            else:
                url = _SHARE_DETAIL_URL
                params = {
                    "share_id": ref.share_id,
                    "parent_id": parent_id,
                    "pass_code_token": token,
                    "limit": _PAGE_LIMIT,
                    "page_token": page_token,
                    "thumbnail_size": "SIZE_SMALL",
                }
            payload = self.http.get_json(url, params=params, headers=self._api_headers(account))
            self._check_payload(payload, "获取迅雷云盘分享文件列表")
            data = self._data(payload)
            collected.extend(_parse_files(data))
            page_token = as_text(data.get("next_page_token"))
            if not page_token:
                break
        return collected

    # ---- 转存与取链 ----

    def _ensure_space(self, file: ShareFile, account: NetdiskAccountRecord) -> None:
        """
        转存前做空间预算校验。

        配额接口不可用时不阻断取链；只有明确查到剩余空间不足才抛出。

        :param file: 目标文件
        :param account: 取链账号
        :raises NetdiskError: 剩余空间不足时抛出 `SPACE_INSUFFICIENT`
        """
        if file.size <= 0:
            return
        try:
            payload = self.http.get_json(_ABOUT_URL, headers=self._api_headers(account))
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                raise
            self.log_failure("查询迅雷云盘空间", error, account)
            return
        self._check_payload(payload, "查询迅雷云盘空间")
        data = self._data(payload)
        quota = data.get("quota")
        if not isinstance(quota, Mapping):
            quota = payload.get("quota")
        if not isinstance(quota, Mapping):
            return
        limit = as_int(quota.get("limit"))
        usage = as_int(quota.get("usage"))
        free = max(limit - usage, 0)
        if limit > 0 and free < file.size:
            raise NetdiskError(
                NetdiskErrorCode.SPACE_INSUFFICIENT,
                f"迅雷云盘剩余空间不足：需要 {file.size} 字节，当前可用 {free} 字节",
                platform=self.platform,
                account_name=account.name,
            )

    def _create_temp_dir(self, account: NetdiskAccountRecord) -> str:
        """
        在个人网盘根目录创建唯一临时目录。

        :param account: 取链账号
        :return: 新目录标识
        :raises NetdiskError: 创建失败时抛出
        """
        name = f"{_TEMP_DIR_PREFIX}{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        payload = self.http.post_json(
            _FILES_URL,
            json={"kind": "drive#folder", "name": name, "parent_id": "", "space": ""},
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
        )
        self._check_payload(payload, "创建迅雷云盘临时目录")
        data = self._data(payload)
        # 当前迅雷接口把新建目录放在 `file` 对象里；旧版接口直接返回顶层 `id`。
        folder = data.get("file")
        folder_id = as_text(data.get("id"))
        if not folder_id and isinstance(folder, Mapping):
            folder_id = as_text(folder.get("id"))
        if not folder_id:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建迅雷云盘临时目录失败：响应缺少目录标识",
                platform=self.platform,
            )
        return folder_id

    def _restore(
        self,
        ref: ShareRef,
        file: ShareFile,
        directory: str,
        account: NetdiskAccountRecord,
    ) -> str:
        """
        把分享文件转存到临时目录，返回转存后的文件标识。

        :param ref: 分享引用
        :param file: 目标文件
        :param directory: 临时目录标识
        :param account: 取链账号
        :return: 转存后的文件标识
        :raises NetdiskError: 转存失败或未返回新标识时抛出
        """
        token = self._pass_code_token(ref, account)
        payload = self.http.post_json(
            _RESTORE_URL,
            json={
                "share_id": ref.share_id,
                "pass_code_token": token,
                "parent_id": directory,
                "ancestor_ids": [],
                "file_ids": [file.file_id],
                "specify_parent_id": True,
            },
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
        )
        self._check_payload(payload, "转存迅雷云盘分享文件")
        data = self._data(payload)

        def first_id(value: Any) -> str:
            """
            从字符串、列表或映射值中取第一个非空文件标识。
            """
            if isinstance(value, (list, tuple)):
                for item in value:
                    text = as_text(item)
                    if text:
                        return text
                return ""
            return as_text(value)

        trace = (data.get("params") or {}).get("trace_file_ids")
        mapping: dict[str, Any] = {}
        if isinstance(trace, str) and trace:
            try:
                parsed = json.loads(trace)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping):
                mapping = dict(parsed)
        elif isinstance(trace, Mapping):
            mapping = dict(trace)
        new_id = (
            as_text(data.get("file_id"))
            or first_id(data.get("file_ids"))
            or first_id(data.get("ids"))
            or first_id(mapping.get(file.file_id))
        )
        if not new_id:
            logger.warning(
                f"迅雷云盘转存未返回新文件标识：file_id={file.file_id} keys={sorted(data.keys())}"
            )
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "迅雷云盘转存完成但未返回新文件标识",
                platform=self.platform,
            )
        return new_id

    @staticmethod
    def _extract_link(data: Mapping[str, Any]) -> Optional[tuple[str, int]]:
        """
        从文件详情响应中解析直链与文件大小。

        :param data: 文件详情业务数据
        :return: `(直链, 文件大小)`；响应中没有直链时返回 None
        """
        url = _first_link_url(data.get("links"))
        if not url:
            url = (
                as_text(data.get("web_content_link"))
                or as_text(data.get("original_url"))
                or as_text(data.get("url"))
            )
        if not url:
            medias = data.get("medias")
            if isinstance(medias, list):
                for media in medias:
                    url = _link_url(media)
                    if url:
                        break
        if not url:
            return None
        return url, as_int(data.get("size"))

    def _file_link(
        self,
        file_id: str,
        account: NetdiskAccountRecord,
    ) -> tuple[str, int]:
        """
        读取个人网盘文件详情中的下载直链。

        :param file_id: 文件标识
        :param account: 取链账号
        :return: `(直链, 文件大小)`
        :raises NetdiskError: 请求失败时抛出原错误；有成功响应但始终无直链时抛 LINK_NOT_READY
        """
        data: dict[str, Any] = {}
        outcomes: list[str] = []
        # 文件详情接口已不接受 usage=DOWNLOAD（稳定返回 400），只按 PLAY 取链；
        # 转存后迅雷需要时间生成直链，对「HTTP 200 但无直链」按指数退避重试，
        # 请求本身报错时立即上抛，避免把限流/风控掩盖成「未就绪」。
        attempts = len(_LINK_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            payload = self.http.get_json(
                f"{_FILES_URL}/{file_id}",
                params={
                    "_magic": "2021",
                    "usage": "PLAY",
                    "thumbnail_size": "SIZE_LARGE",
                    "with": "hdr10",
                },
                headers=self._api_headers(account),
            )
            self._check_payload(payload, "获取迅雷云盘下载链接")
            data = self._data(payload)
            link = self._extract_link(data)
            if link is not None:
                return link
            outcomes.append(f"r{attempt}:{self._detail_diagnosis(data)}")
            if attempt < attempts:
                time.sleep(_LINK_RETRY_DELAYS[attempt - 1])
        logger.warning(f"迅雷文件详情未返回直链；尝试明细={outcomes}")
        raise NetdiskError(
            NetdiskErrorCode.LINK_NOT_READY,
            self._link_not_ready_message(data),
            platform=self.platform,
        )

    @staticmethod
    def _link_not_ready_message(data: Mapping[str, Any]) -> str:
        """
        生成取链失败提示；审核状态异常时带出具体状态，便于区分未就绪与受限。

        :param data: 文件详情业务数据
        :return: 错误提示
        """
        audit = data.get("audit")
        if isinstance(audit, Mapping):
            status = as_text(audit.get("status"))
            if status and status != _LINK_AUDIT_OK:
                detail = " ".join(part for part in (status, as_text(audit.get("message"))) if part)
                return f"迅雷直链尚未就绪（审核状态：{detail}）"
        return "迅雷直链尚未就绪"

    @staticmethod
    def _detail_diagnosis(data: Mapping[str, Any]) -> str:
        """
        汇总文件详情响应中与取链相关的状态，便于定位空直链。

        只输出状态、类型与数量，不输出直链或令牌。

        :param data: 文件详情业务数据
        :return: 诊断摘要
        """
        audit = data.get("audit")
        if isinstance(audit, Mapping):
            audit_state = f"{audit.get('status')}/{audit.get('audit_status')}"
        else:
            audit_state = type(audit).__name__ if audit is not None else "-"
        links = data.get("links")
        medias = data.get("medias")
        link_count = len(links) if isinstance(links, Mapping) else type(links).__name__
        media_count = len(medias) if isinstance(medias, list) else type(medias).__name__
        return (
            f"phase={data.get('phase')} space={data.get('space')!r} kind={data.get('kind')} "
            f"category={data.get('file_category')} audit={audit_state} links={link_count} "
            f"medias={media_count} web_content_link={bool(data.get('web_content_link'))} "
            f"original_url={bool(data.get('original_url'))}"
        )

    def _batch_delete(
        self,
        ids: list[str],
        account: NetdiskAccountRecord,
    ) -> None:
        """
        批量删除个人网盘文件或目录。

        :param ids: 待删除标识
        :param account: 取链账号
        :raises NetdiskError: 删除失败时抛出
        """
        if not ids:
            return
        payload = self.http.post_json(
            _BATCH_DELETE_URL,
            json={"ids": list(ids), "space": ""},
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
        )
        self._check_payload(payload, "删除迅雷云盘转存文件")

    def _safe_delete(
        self,
        ids: list[str],
        account: Optional[NetdiskAccountRecord],
    ) -> None:
        """
        清理失败只记录，不影响主流程。

        :param ids: 待删除标识
        :param account: 取链账号
        """
        if account is None or not ids:
            return
        try:
            self._batch_delete(ids, account)
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载
            self.log_failure("清理迅雷云盘转存内容", error, account)

    def _log_directory_state(
        self,
        directory: str,
        new_id: str,
        account: NetdiskAccountRecord,
    ) -> None:
        """
        取链失败后记录临时目录的实际内容，用于判断转存是否已落盘。

        :param directory: 临时目录标识
        :param new_id: 转存响应给出的文件标识
        :param account: 取链账号
        """
        try:
            entries = self.list_dir(account, directory)
        except Exception as error:  # noqa: BLE001 - 诊断失败不阻断清理
            logger.warning(f"迅雷取链失败后无法读取临时目录：{error}")
            return
        present = any(item.file_id == new_id for item in entries)
        names = [f"{item.name}({item.file_id[:10]})" for item in entries[:5]]
        logger.warning(
            f"迅雷取链失败时的临时目录状态：目录={directory[:10]} 条目数={len(entries)} "
            f"转存标识已落盘={present} 条目={names}"
        )

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
        转存分享文件并换取下载直链。

        迅雷分享无法免转存取链，`allow_transfer` 为假时直接返回 `UNSUPPORTED`。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存
        :param transfer_first: 是否优先转存；本平台始终转存，参数仅对齐接口
        :return: 直链及其访问令牌、设备标识、验证码与清理句柄
        :raises NetdiskError: 空间不足、登录态失效或接口失败时抛出
        """
        if file.is_dir:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "迅雷云盘不支持直接下载目录",
                platform=self.platform,
            )
        if not allow_transfer:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "迅雷云盘必须转存后才能取链",
                platform=self.platform,
            )
        self._identity(account)
        assert account is not None
        self._ensure_space(file, account)
        directory = self._create_temp_dir(account)
        new_id = ""
        try:
            new_id = self._restore(ref, file, directory, account)
            url, size = self._file_link(new_id, account)
        except Exception:
            if new_id:
                self._log_directory_state(directory, new_id, account)
            self._safe_delete([directory], account)
            raise
        handle = TransferHandle(
            platform=self.platform,
            account_id=account.id,
            directory_id=directory,
            file_ids=(new_id,),
        )
        return DirectLink(
            url=url,
            headers=self.download_headers(account),
            size=size or file.size,
            expires_at=None,
            cleanup_handle=handle,
            account_id=account.id,
            is_hls=False,
        )

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除转存文件与临时目录；失败只记录，不抛出。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        ids = list(handle.file_ids)
        if handle.directory_id:
            ids.append(handle.directory_id)
        self._safe_delete(ids, account)

    def download_headers(
        self,
        account: Optional[NetdiskAccountRecord],
        *,
        extra: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """
        组装直链下载请求头。

        下载请求必须携带取链账号的 access_token、deviceid 与 captcha，三者不能跨账号混用。

        :param account: 取链账号
        :param extra: 额外请求头
        :return: 请求头
        :raises NetdiskError: 账号缺失或必填字段缺失时抛出 `AUTH_EXPIRED`
        """
        access, device, captcha = self._identity(account)
        headers = self.http.headers(None, include_cookie=False)
        headers.update(
            {
                "User-Agent": _CLIENT_UA,
                "Authorization": f"Bearer {access}",
                "X-Device-Id": device,
                "X-Client-Version": _CLIENT_VERSION,
                "Origin": _ORIGIN,
                "Referer": _REFERER,
            }
        )
        if captcha:
            headers["X-Captcha-Token"] = captcha
        if extra:
            headers.update({str(key): str(value) for key, value in extra.items()})
        return headers

    # ---- 个人盘 ----

    def _patch_json(
        self,
        url: str,
        *,
        account: NetdiskAccountRecord,
        json: Mapping[str, Any],
    ) -> dict[str, Any]:
        """
        发起 PATCH 请求并解析 JSON。

        共享 HTTP 封装只提供 GET/POST，重命名需要 PATCH，这里按同一套请求头与直连策略补一个入口。

        :param url: 请求地址
        :param account: 账号
        :param json: JSON 请求体
        :return: 响应 JSON
        """
        request = RequestUtils(
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
            timeout=self.http.timeout,
            proxies=_DIRECT_PROXIES,
        )
        response = request.request("patch", url, json=dict(json))
        if response is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口无响应", platform=self.platform)
        if response.status_code >= 400:
            raise self.http.error_for_status(response.status_code)
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - 非 JSON 响应统一归类
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "网盘接口返回了非 JSON 响应",
                platform=self.platform,
            ) from error
        if not isinstance(payload, dict):
            return {"data": payload}
        return {str(key): value for key, value in payload.items()}

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询迅雷云盘空间。

        :param account: 账号
        :return: 已用/总空间
        """
        self._identity(account)
        payload = self.http.get_json(_ABOUT_URL, headers=self._api_headers(account))
        self._check_payload(payload, "查询迅雷云盘空间")
        data = self._data(payload)
        quota = data.get("quota")
        if not isinstance(quota, Mapping):
            quota = payload.get("quota")
        if not isinstance(quota, Mapping):
            return DriveQuota()
        return DriveQuota(used=as_int(quota.get("usage")), total=as_int(quota.get("limit")))

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        分页列出迅雷个人盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串为根目录
        :return: 目录条目
        """
        self._identity(account)
        collected: list[DriveFile] = []
        page_token = ""
        filters = json.dumps({"trashed": {"eq": False}})
        for _ in range(_MAX_PAGES):
            payload = self.http.get_json(
                _FILES_URL,
                params={
                    "parent_id": parent_id,
                    "page_token": page_token,
                    "limit": _DRIVE_PAGE_LIMIT,
                    "with_audit": "true",
                    "filters": filters,
                },
                headers=self._api_headers(account),
            )
            self._check_payload(payload, "获取迅雷云盘文件列表")
            data = self._data(payload)
            for item in data.get("files") or []:
                if not isinstance(item, Mapping):
                    continue
                collected.append(
                    DriveFile(
                        file_id=as_text(item.get("id")),
                        name=as_text(item.get("name")),
                        size=as_int(item.get("size")),
                        is_dir=as_text(item.get("kind")) == "drive#folder",
                        parent_id=as_text(item.get("parent_id")),
                    )
                )
            page_token = as_text(data.get("next_page_token"))
            if not page_token:
                break
        return collected

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取迅雷个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        if file.is_dir:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "迅雷云盘不支持直接下载目录",
                platform=self.platform,
            )
        self._identity(account)
        url, size = self._file_link(file.file_id, account)
        return DirectLink(
            url=url,
            headers=self.download_headers(account),
            size=size or file.size,
            account_id=account.id,
        )

    def share_file(
        self,
        account: NetdiskAccountRecord,
        files: list[DriveFile],
        *,
        password: str = "",
        expires_in: int = 0,
    ) -> DriveShare:
        """
        创建迅雷云盘分享。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码，留空由服务端生成
        :param expires_in: 有效期秒数，0 为永久
        :return: 分享结果
        """
        if not files:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "迅雷云盘分享至少需要一个文件",
                platform=self.platform,
            )
        self._identity(account)
        expiration = "-1"
        if expires_in > 0:
            expiration = "1" if expires_in <= 86400 else "7" if expires_in <= 7 * 86400 else "30"
        params: dict[str, Any] = {
            "subscribe_push": "false",
            "WithPassCodeInLink": "true",
            "with_pass_code_in_link": "true",
        }
        if password:
            params["pass_code"] = password
        payload = self.http.post_json(
            _SHARE_URL,
            json={
                "file_ids": [item.file_id for item in files],
                "share_to": "copy",
                "params": params,
                "title": files[0].name or "分享文件",
                "restore_limit": "-1",
                "expiration_days": expiration,
            },
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
        )
        self._check_payload(payload, "创建迅雷云盘分享")
        data = self._data(payload)
        url = as_text(data.get("share_url"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建迅雷云盘分享失败：响应缺少分享链接",
                platform=self.platform,
            )
        return DriveShare(url=url, password=as_text(data.get("pass_code")) or password)

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动迅雷个人盘文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录标识
        """
        if not files:
            return
        self._identity(account)
        payload = self.http.post_json(
            _MOVE_URL,
            json={
                "ids": [item.file_id for item in files],
                "to": {"parent_id": target_id, "space": ""},
                "space": "",
            },
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
        )
        self._check_payload(payload, "移动迅雷云盘文件")

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名迅雷个人盘文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        self._identity(account)
        payload = self._patch_json(
            f"{_FILES_URL}/{file.file_id}",
            account=account,
            json={"name": name},
        )
        self._check_payload(payload, "重命名迅雷云盘文件")

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除迅雷个人盘文件（移入回收站）。

        :param account: 账号
        :param files: 文件列表
        """
        if not files:
            return
        self._identity(account)
        payload = self.http.post_json(
            _TRASH_URL,
            json={"ids": [item.file_id for item in files], "space": ""},
            headers=self._api_headers(account, extra={"Content-Type": "application/json"}),
        )
        self._check_payload(payload, "删除迅雷云盘文件")

    # ---- 账号 ----

    @staticmethod
    def _about_headers(access: str, device: str, captcha: str) -> dict[str, str]:
        """
        组装 `/drive/v1/about` 请求头。

        :param access: 访问令牌
        :param device: 设备标识
        :param captcha: 验证码令牌
        :return: 请求头
        """
        headers = {
            "User-Agent": _CLIENT_UA,
            "X-Client-Version": _CLIENT_VERSION,
            "Origin": _ORIGIN,
            "Referer": _REFERER,
            "Authorization": f"Bearer {access}",
            "X-Device-Id": device,
        }
        if captcha:
            headers["X-Captcha-Token"] = captcha
        return headers

    def _probe(self, fields: Mapping[str, str]) -> AccountProbe:
        """
        调用云盘 about 接口确认凭据是否可用。

        :param fields: 展开后的凭据字段
        :return: 探测结果
        """
        access, device, _captcha = _identity_from(fields)
        headers = self._about_headers(
            access,
            device,
            self._fresh_captcha(device, access, _stored_captcha(fields)),
        )
        try:
            payload = self.http.get_json(_ABOUT_URL, headers=headers)
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="迅雷云盘登录态已失效")
            return AccountProbe(status="unknown", message=error.message)
        if as_text(payload.get("error")):
            code = _classify(
                as_text(payload.get("error")),
                as_text(payload.get("error_description")) or as_text(payload.get("message")),
            )
            if code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="迅雷云盘登录态已失效")
            return AccountProbe(status="unknown", message="无法确认迅雷云盘登录态")
        return AccountProbe(status="active", message="迅雷云盘登录态有效")

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验凭据：支持 `{"cookie": ...}` 与字段字典两种形态。

        :param credential: 明文凭据字段
        :return: 探测结果；接口不可用时返回 `unknown`
        """
        fields = _fields(credential)
        access, device, _captcha = _identity_from(fields)
        missing = []
        if not access:
            missing.append("access_token")
        if not device:
            missing.append("deviceid")
        if missing:
            return AccountProbe(status="expired", message="凭据缺少必填字段：" + "、".join(missing))
        return self._probe(fields)

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        读取迅雷网页账号昵称。

        网页 localStorage 通常不写昵称，优先使用浏览器凭据里的字段，
        缺失时再调用 `/drive/v1/about`，依次在 `data`、`data.user` 与响应顶层查找。

        :param credential: 浏览器抓到的凭据
        :return: 昵称；无法获取时为空串
        """
        fields = _fields(credential)
        nickname = _pick(fields, "nickname", "nick_name", "nickName", "user_name", "userName", "name")
        if nickname:
            return nickname
        access, device, _captcha = _identity_from(fields)
        if not access or not device:
            return ""
        captcha = self._fresh_captcha(device, access, _stored_captcha(fields))
        try:
            payload = self.http.get_json(_ABOUT_URL, headers=self._about_headers(access, device, captcha))
        except NetdiskError:
            return ""
        data = payload.get("data")
        candidates: list[Mapping[Any, Any]] = []
        if isinstance(data, Mapping):
            candidates.append(data)
            user = data.get("user")
            if isinstance(user, Mapping):
                candidates.append(user)
        candidates.append(payload)
        for candidate in candidates:
            nickname = _pick(
                {str(key): as_text(value) for key, value in candidate.items()},
                "nickname",
                "nick_name",
                "nickName",
                "user_name",
                "userName",
                "name",
            )
            if nickname:
                return nickname
        return ""

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {})

    def needs_refresh(self, account: NetdiskAccountRecord) -> bool:
        """
        判断访问令牌是否已临期或无法解析。

        没有 `refresh_token` 时无法续期，直接返回 False。

        :param account: 账号记录
        :return: 需要在下次使用前续期时返回 True
        """
        fields = _fields(account.credential or {})
        refresh_token = _pick(fields, "refresh_token", "refreshToken")
        if not refresh_token:
            return False
        access = _strip_bearer(_pick(fields, "access_token", "accessToken", "token"))
        expires_at = jwt_expires_at(access)
        if expires_at is None:
            return True
        return expires_at - time.time() <= _JWT_LEEWAY_SECONDS

    def refresh_account(self, account: NetdiskAccountRecord) -> Optional[Mapping[str, str]]:
        """
        使用 `refresh_token` 换取新的访问令牌。

        新令牌、轮换后的 `refresh_token` 与 `expires_in` 写回完整凭据；
        刷新失败返回 None，保留旧凭据。

        :param account: 账号记录
        :return: 刷新后的完整凭据；缺少 refresh_token 或刷新失败时返回 None
        """
        fields = _fields(account.credential or {})
        refresh_token = _pick(fields, "refresh_token", "refreshToken")
        if not refresh_token:
            return None
        headers = {
            "User-Agent": _CLIENT_UA,
            "X-Client-Version": _CLIENT_VERSION,
            "X-Client-Id": _AUTH_CLIENT_ID,
            "Origin": _ORIGIN,
            "Referer": _REFERER,
            "Content-Type": "application/json",
            "Sec-Ch-Ua": _SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": _SEC_CH_UA_MOBILE,
            "Sec-Ch-Ua-Platform": _SEC_CH_UA_PLATFORM,
        }
        device = _pick(fields, "deviceid", "device_id", "deviceId")
        if device:
            headers["X-Device-Id"] = device
        try:
            payload = self.http.post_json(
                _AUTH_TOKEN_URL,
                json={
                    "client_id": _AUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers=headers,
            )
        except NetdiskError as error:
            self.log_failure("刷新登录态", error, account)
            return None
        if as_text(payload.get("error")):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        response_fields = {str(key): as_text(value) for key, value in data.items()}
        access = _pick(response_fields, "access_token", "accessToken")
        if not access:
            return None
        refreshed = dict(account.credential or {})
        refreshed["access_token"] = access
        new_refresh = _pick(response_fields, "refresh_token", "refreshToken")
        if new_refresh:
            refreshed["refresh_token"] = new_refresh
        expires_in = as_int(response_fields.get("expires_in")) or as_int(response_fields.get("expiresIn"))
        if expires_in:
            refreshed["expires_in"] = str(expires_in)
        return refreshed
