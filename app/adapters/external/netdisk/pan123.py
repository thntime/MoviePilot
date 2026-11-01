"""
123 云盘的分享解析、目录读取与直链适配器。

协议依据 YunX `Pan123ResolveRepository` / `Pan123Api`：分享列表 `b/api/share/get`
可匿名读取，下载直链 `b/api/share/download/info` 需要登录 JWT 并携带
`auth-key`/`auth-value` 签名头。123 分享无需转存，取链策略固定为直链。
"""

from __future__ import annotations

import base64
import random
import time
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.adapters.external.netdisk.base import (
    BaseNetdiskAdapter,
    as_int,
    as_text,
)
from app.domain.netdisk import NetdiskAccountRecord
from app.domain.netdisk import (
    AccountProbe,
    DirectLink,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
)

# 账号密码登录接口，成功判定为 code == 200（与其他接口的 code == 0 不同）。
_LOGIN_URL = "https://user.123pan.cn/api/user/sign_in"
_LOGIN_PATH = "/api/user/sign_in"
# 分享文件列表：匿名端点，无需签名。
_SHARE_GET_URL = "https://yun.123pan.cn/b/api/share/get"
_SHARE_GET_PATH = "/b/api/share/get"
# 分享下载信息：需要登录 JWT 与签名头。
_SHARE_DOWNLOAD_URL = "https://www.123865.com/b/api/share/download/info"
_SHARE_DOWNLOAD_PATH = "/b/api/share/download/info"
# 用户信息：校验登录态并读取昵称。
_USER_INFO_URL = "https://yun.123pan.cn/b/api/user/info"
_USER_INFO_PATH = "/b/api/user/info"

# 网页端 UA，用于签名类接口与登录。
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
# Dart UA：匿名分享列表与直链下载时使用。
_DART_UA = "Dart/3.12 (dart:io)"
# 直链下载必须携带的 Referer，缺失会被 CDN 拒绝。
_DOWNLOAD_REFERER = "https://yun.123pan.cn/"
# 签名表：数字 0-9 到字母的替换表，索引即数字值。
_SIGN_TABLE = "adefghlmyijnopkqrstubcvwsz"
# 签名时间基准偏移：ts + 57600 秒后按 UTC 格式化。
_SIGN_OFFSET_SECONDS = 57600
# 分享列表单页条数与最大翻页次数。
_PAGE_LIMIT = 100
_MAX_PAGES = 50


def _crc32_hex(text: str) -> str:
    """
    计算标准 CRC-32 的十六进制文本。

    :param text: 参与计算的文本
    :return: 8 位以内的小写十六进制，前导零不补齐
    """
    return f"{zlib.crc32(text.encode('utf-8')) & 0xFFFFFFFF:x}"


def _make_sign(path: str, ts: Optional[int] = None) -> tuple[str, str]:
    """
    生成 123 云盘签名头。

    签名内部固定 OS=web、VER=3，与请求头 platform/app-version 无关。

    :param path: 参与签名的 URL 路径，含 `/b` 前缀且不含 host 与 query
    :param ts: 秒级时间戳，None 表示当前时间
    :return: `(auth-key, auth-value)`
    """
    stamp = int(ts if ts is not None else time.time())
    minute = datetime.fromtimestamp(stamp + _SIGN_OFFSET_SECONDS, tz=timezone.utc).strftime("%Y%m%d%H%M")
    substituted = "".join(_SIGN_TABLE[int(char)] for char in minute)
    auth_key = _crc32_hex(substituted)
    rand = random.randint(0, 9_999_999)
    data = f"{stamp}|{rand}|{path}|web|3|{auth_key}"
    return auth_key, f"{stamp}-{rand}-{_crc32_hex(data)}"


def _decode_token(token: str) -> tuple[str, str, str]:
    """
    解码列表项令牌。

    123 的取链参数编码在 `S3KeyFlag|Etag|StorageNode` 中，旧数据可能只有两段。

    :param token: 列表项令牌
    :return: `(S3KeyFlag, Etag, StorageNode)`
    """
    parts = str(token or "").split("|")
    return (
        parts[0] if len(parts) > 0 else "",
        parts[1] if len(parts) > 1 else "",
        parts[2] if len(parts) > 2 else "",
    )


def _decode_download_url(raw: str) -> Optional[str]:
    """
    解码 123 返回的下载地址。

    兼容两种形态：整段 Base64，以及 `download-v2?params=<base64>` 包装。

    :param raw: 接口返回的 DownloadURL
    :return: 真实 CDN 地址；无法解码时返回 None
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        try:
            decoded = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if decoded.lower().startswith("http") else None
    if "params=" not in text:
        return None
    params = text.split("params=", 1)[1].split("&", 1)[0]
    normalized = params.replace("-", "+").replace("_", "/")
    try:
        decoded = base64.b64decode(normalized + "=" * (-len(normalized) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded or None


def _cookie_field(raw: str, name: str) -> str:
    """
    从 Cookie 头文本中读取指定字段。

    :param raw: Cookie 头文本
    :param name: 字段名
    :return: 字段值；不存在时为空串
    """
    for part in str(raw or "").split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _credential_token(credential: Mapping[str, str]) -> str:
    """
    从凭据中提取 JWT。

    优先读取 `token`，其次兼容 `authorToken`/`access_token` 字段与 Cookie 头。

    :param credential: 明文凭据字段
    :return: JWT 文本；缺失时为空串
    """
    for key in ("token", "authorToken", "access_token"):
        value = as_text(credential.get(key))
        if value:
            return value.removeprefix("Bearer ").strip()
    for key in ("cookie", "cookie_header", "cookies"):
        raw = as_text(credential.get(key))
        if not raw:
            continue
        found = _cookie_field(raw, "authorToken")
        if found:
            return found
    return ""


def _parse_files(data: Mapping[str, Any]) -> list[ShareFile]:
    """
    把接口返回的 InfoList 映射为统一文件条目。

    :param data: 响应中的 data 对象
    :return: 文件与目录条目
    """
    files: list[ShareFile] = []
    for item in data.get("InfoList") or []:
        if not isinstance(item, Mapping):
            continue
        files.append(
            ShareFile(
                file_id=as_text(item.get("FileId")),
                name=as_text(item.get("FileName")),
                size=as_int(item.get("Size")),
                is_dir=as_int(item.get("Type")) == 1,
                parent_id=as_text(item.get("ParentFileId")),
                token="|".join(
                    (
                        as_text(item.get("S3KeyFlag")),
                        as_text(item.get("Etag")),
                        as_text(item.get("StorageNode")),
                    )
                ),
            )
        )
    return files


class Pan123Adapter(BaseNetdiskAdapter):
    """
    123 云盘协议适配器。

    分享列表匿名可读，取直链与账号信息需要 Bearer JWT；分享下载不需要转存。
    """

    platform = NetdiskPlatform.PAN123
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    guest_supported = False
    max_concurrency = 512
    cookie_required_keys = ("token",)
    cookie_domains = (".123pan.com", ".123952.com")
    login_url = "https://www.123pan.com/"
    supports_password_login = True
    user_agent = _WEB_UA
    referer = "https://www.123pan.com/"

    def __init__(self) -> None:
        """
        初始化共享 HTTP 客户端并生成稳定的设备标识。
        """
        super().__init__()
        self._login_uuid = uuid.uuid4().hex

    def _auth_headers(
        self,
        token: str,
        path: str,
        *,
        platform: str = "web",
        app_version: str = "3",
    ) -> dict[str, str]:
        """
        组装带签名的鉴权请求头。

        :param token: Bearer JWT
        :param path: 参与签名的路径
        :param platform: 请求头 platform 值
        :param app_version: 请求头 app-version 值
        :return: 请求头字典
        """
        auth_key, auth_value = _make_sign(path)
        return {
            "platform": platform,
            "app-version": app_version,
            "authorization": f"Bearer {token}",
            "loginuuid": self._login_uuid,
            "auth-key": auth_key,
            "auth-value": auth_value,
            "User-Agent": _WEB_UA,
        }

    def _token(self, account: Optional[NetdiskAccountRecord]) -> str:
        """
        读取取链账号的 JWT。

        :param account: 账号记录
        :return: JWT 文本
        :raises NetdiskError: 账号缺失或凭据不含 token 时抛出 `AUTH_EXPIRED`
        """
        if account is None:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "123 云盘取链需要登录账号",
                platform=self.platform,
            )
        token = _credential_token(account.credential or {})
        if not token:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "123 云盘登录态缺失，请重新登录",
                platform=self.platform,
                account_name=account.name,
            )
        return token

    def _check_code(self, payload: Mapping[str, Any], fallback: str) -> None:
        """
        校验业务响应码并映射为统一错误。

        :param payload: 响应 JSON
        :param fallback: 无服务端文案时的兜底说明
        :raises NetdiskError: 业务码非成功时抛出
        """
        code = payload.get("code", -1)
        try:
            numeric = int(code)
        except (TypeError, ValueError):
            numeric = -1
        if numeric in (0, 200):
            return
        message = as_text(payload.get("message")) or fallback
        if numeric in (401, 403):
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "123 云盘登录态已失效", platform=self.platform)
        if numeric == 429 or "频繁" in message or "限流" in message:
            raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "123 云盘接口请求过于频繁", platform=self.platform)
        if "分享" in message and any(word in message for word in ("失效", "不存在", "过期", "密码")):
            raise NetdiskError(NetdiskErrorCode.SHARE_INVALID, message, platform=self.platform)
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, message, platform=self.platform)

    def _fetch_page(
        self,
        ref: ShareRef,
        parent_id: str,
        page: int,
    ) -> tuple[list[ShareFile], Optional[str]]:
        """
        读取一页分享目录。

        :param ref: 分享引用，password 作为提取码
        :param parent_id: 父目录标识，根目录为 `0`
        :param page: 页码，从 1 开始
        :return: `(条目列表, 下一页游标)`；游标为 None 表示末页
        :raises NetdiskError: 接口失败或分享失效时抛出
        """
        params: dict[str, Any] = {
            "limit": _PAGE_LIMIT,
            "next": "0",
            "orderBy": "file_name",
            "orderDirection": "asc",
            "shareKey": ref.share_id,
            "ParentFileId": parent_id or "0",
            "Page": page,
        }
        # 无提取码时不能传 SharePwd，否则接口会报「请输入 Next」。
        if ref.password:
            params["SharePwd"] = ref.password
        payload = self.http.get_json(
            _SHARE_GET_URL,
            params=params,
            headers={"User-Agent": _DART_UA},
        )
        self._check_code(payload, "获取 123 分享文件列表失败")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return [], None
        if data.get("Expired") is True:
            raise NetdiskError(NetdiskErrorCode.SHARE_INVALID, "123 分享已失效", platform=self.platform)
        cursor = as_text(data.get("Next"))
        # `Next == "-1"` 为末页；空串表示仍有下一页，需要继续递增 Page。
        return _parse_files(data), None if cursor == "-1" else cursor

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        读取分享标题与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号，123 分享详情可匿名读取
        :return: 分享详情，标题回退为分享码
        """
        files, _cursor = self._fetch_page(ref, "0", 1)
        title = files[0].name if files else ref.share_id
        return ShareDetail(
            title=title,
            size=sum(item.size for item in files),
            files=tuple(files),
            stoken=ref.password or "",
        )

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        分页读取分享目录，直到游标为末页或达到翻页上限。

        :param ref: 分享引用
        :param parent_id: 父目录标识，根目录为空或 `0`
        :param account: 取链账号
        :return: 目录内全部条目
        """
        parent = parent_id or "0"
        collected: list[ShareFile] = []
        for page in range(1, _MAX_PAGES + 1):
            files, cursor = self._fetch_page(ref, parent, page)
            collected.extend(files)
            if not files or cursor is None:
                break
        return collected

    def resolve(
        self,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        allow_transfer: bool = True,
    ) -> DirectLink:
        """
        换取分享文件的直链。

        123 分享免转存，`allow_transfer` 仅用于对齐接口，不影响行为。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底，本平台忽略
        :return: 直链及其 Bearer、UA 与 Referer
        :raises NetdiskError: 登录态失效、分享失效或接口失败时抛出
        """
        del allow_transfer
        token = self._token(account)
        s3_key_flag, etag, _node = _decode_token(file.token)
        file_id: Any = int(file.file_id) if file.file_id.isdigit() else file.file_id
        payload = self.http.post_json(
            _SHARE_DOWNLOAD_URL,
            json={
                "ShareKey": ref.share_id,
                "FileID": file_id,
                "S3KeyFlag": s3_key_flag,
                "Size": file.size,
                "Etag": etag,
            },
            headers=self._auth_headers(token, _SHARE_DOWNLOAD_PATH, platform="android", app_version="39"),
        )
        self._check_code(payload, "获取 123 下载链接失败")
        data = payload.get("data")
        raw = as_text(data.get("DownloadURL")) if isinstance(data, Mapping) else ""
        url = _decode_download_url(raw) or raw
        if not url:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "123 云盘未返回下载地址", platform=self.platform)
        return DirectLink(
            url=url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": _DART_UA,
                "Referer": _DOWNLOAD_REFERER,
            },
            size=file.size,
            account_id=account.id if account else 0,
        )

    def password_login(self, username: str, password: str) -> Mapping[str, str]:
        """
        使用账号密码换取 JWT。

        密码按原值提交，只对账号去除首尾空白。

        :param username: 123 账号
        :param password: 登录密码
        :return: `{"token": <jwt>}`
        :raises NetdiskError: 账号或密码错误、风控或接口失败时抛出
        """
        passport = str(username or "").strip()
        if not passport or not password:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "请输入 123 云盘账号和密码", platform=self.platform)
        payload = self.http.post_json(
            _LOGIN_URL,
            json={"passport": passport, "password": password, "remember": False},
            headers={
                "platform": "web",
                "app-version": "132",
                "loginuuid": self._login_uuid,
                "Origin": "https://user.123pan.cn",
                "Referer": "https://user.123pan.cn/",
                "User-Agent": _WEB_UA,
                "Content-Type": "application/json;charset=UTF-8",
            },
        )
        data = payload.get("data")
        token = as_text(data.get("token")) if isinstance(data, Mapping) else ""
        if as_int(payload.get("code")) != 200 or not token:
            message = as_text(payload.get("message")) or "123 云盘账号或密码错误"
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, message, platform=self.platform)
        return {"token": token}

    def _user_info(self, token: str) -> dict[str, Any]:
        """
        读取账号信息，用于确认登录态。

        :param token: Bearer JWT
        :return: 响应中的 data 对象
        :raises NetdiskError: 登录态失效或接口失败时抛出
        """
        payload = self.http.get_json(
            _USER_INFO_URL,
            headers=self._auth_headers(token, _USER_INFO_PATH),
        )
        self._check_code(payload, "获取 123 账号信息失败")
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验凭据：先按字段判断，再调用账号信息接口确认。

        :param credential: 明文凭据字段
        :return: 探测结果；接口不可用时返回 `unknown`
        """
        token = _credential_token(credential)
        if not token:
            return AccountProbe(status="expired", message="凭据缺少必填字段：token")
        try:
            info = self._user_info(token)
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="123 云盘登录态已失效")
            return AccountProbe(status="unknown", message=error.message)
        nickname = as_text(info.get("Nickname"))
        message = f"123 云盘登录态有效：{nickname}" if nickname else "123 云盘登录态有效"
        return AccountProbe(status="active", message=message)

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {})
