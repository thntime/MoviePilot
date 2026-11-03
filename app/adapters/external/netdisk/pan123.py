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
from datetime import datetime, timedelta, timezone
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
# 个人盘文件列表与取链（签名 path 必须与请求路径逐字一致）。
_FILE_LIST_URL = "https://yun.123pan.cn/b/api/file/list/new"
_FILE_LIST_PATH = "/b/api/file/list/new"
_FILE_DOWNLOAD_INFO_URL = "https://yun.123pan.cn/api/file/download_info"
_FILE_DOWNLOAD_INFO_PATH = "/api/file/download_info"
_FILE_TRASH_URL = "https://yun.123pan.cn/b/api/file/trash"
_FILE_TRASH_PATH = "/b/api/file/trash"
_FILE_RENAME_URL = "https://yun.123pan.cn/b/api/file/rename"
_FILE_RENAME_PATH = "/b/api/file/rename"
_FILE_MOD_PID_URL = "https://yun.123pan.cn/b/api/file/mod_pid"
_FILE_MOD_PID_PATH = "/b/api/file/mod_pid"
_SHARE_CREATE_URL = "https://yun.123pan.cn/b/api/share/create"
_SHARE_CREATE_PATH = "/b/api/share/create"
# 永久分享的过期时间；其余有效期按创建时刻加秒数生成绝对时间。
_EXPIRATION_FOREVER = "2099-12-12T08:00:00+08:00"
_CN_TZ = timezone(timedelta(hours=8))

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


def _parse_time(value: Any) -> Optional[datetime]:
    """
    解析接口返回的修改时间。

    :param value: ISO 时间文本或秒级时间戳
    :return: 带时区的时间；无法解析时返回 None
    """
    text = as_text(value)
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _drive_file(item: Mapping[str, Any]) -> DriveFile:
    """
    把个人盘 InfoList 条目映射为统一文件条目。

    :param item: 接口返回的单个条目
    :return: 文件或目录条目
    """
    return DriveFile(
        file_id=as_text(item.get("FileId")),
        name=as_text(item.get("FileName")),
        size=as_int(item.get("Size")),
        is_dir=as_int(item.get("Type")) == 1,
        updated_at=_parse_time(item.get("UpdateAt")),
        parent_id=as_text(item.get("ParentFileId")),
    )


def _numeric_id(value: str) -> int:
    """
    把文件标识转换为 123 接口要求的整数。

    :param value: 文件标识
    :return: 整数标识；非数字时返回 0
    """
    return int(value) if str(value).isdigit() else 0


class Pan123Adapter(BaseNetdiskAdapter):
    """
    123 云盘协议适配器。

    分享列表匿名可读，取直链与账号信息需要 Bearer JWT；分享下载不需要转存。
    """

    platform = NetdiskPlatform.PAN123
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    guest_supported = False
    drive_capabilities = DriveCapabilities(
        quota=True,
        browse=True,
        download=True,
        share=True,
        move=True,
        rename=True,
        delete=True,
    )
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

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从 123 云盘账号信息接口读取昵称。

        :param credential: 浏览器抓到的凭据
        :return: 昵称；接口不可用时为空串
        """
        token = _credential_token(credential)
        if not token:
            return ""
        try:
            info = self._user_info(token)
        except NetdiskError:
            return ""
        return as_text(info.get("Nickname"))

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {})

    # ---- 个人盘 ----

    def _auth_get(
        self,
        url: str,
        path: str,
        account: NetdiskAccountRecord,
        *,
        params: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        发起带签名的个人盘 GET 请求。

        :param url: 请求地址
        :param path: 参与签名的路径
        :param account: 账号
        :param params: 查询参数
        :return: 响应 JSON
        """
        token = self._token(account)
        payload = self.http.get_json(
            url,
            account=account,
            params=params,
            headers=self._auth_headers(token, path),
        )
        self._check_code(payload, "获取 123 个人盘数据失败")
        return payload

    def _auth_post(
        self,
        url: str,
        path: str,
        account: NetdiskAccountRecord,
        body: Mapping[str, Any],
        action: str,
    ) -> dict[str, Any]:
        """
        发起带签名的个人盘 POST 请求。

        :param url: 请求地址
        :param path: 参与签名的路径
        :param account: 账号
        :param body: JSON 请求体
        :param action: 用于错误提示的动作名
        :return: 响应 JSON
        """
        token = self._token(account)
        payload = self.http.post_json(
            url,
            account=account,
            json=body,
            headers=self._auth_headers(token, path),
        )
        self._check_code(payload, action)
        return payload

    def _personal_items(
        self,
        account: NetdiskAccountRecord,
        parent_id: str,
    ) -> list[Mapping[str, Any]]:
        """
        分页读取个人盘目录原始条目。

        :param account: 账号
        :param parent_id: 父目录标识，空串为根
        :return: 原始条目列表
        """
        items: list[Mapping[str, Any]] = []
        cursor = "0"
        for page in range(1, _MAX_PAGES + 1):
            payload = self._auth_get(
                _FILE_LIST_URL,
                _FILE_LIST_PATH,
                account,
                params={
                    "driveId": "0",
                    "limit": str(_PAGE_LIMIT),
                    "next": cursor,
                    "orderBy": "update_time",
                    "orderDirection": "desc",
                    "parentFileId": parent_id or "0",
                    "trashed": "false",
                    "SearchData": "",
                    "Page": str(page),
                    "OnlyLookAbnormalFile": "0",
                    "event": "homeListFile",
                    "operateType": "1",
                    "inDirectSpace": "false",
                },
            )
            data = payload.get("data")
            if not isinstance(data, Mapping):
                break
            page_items = [item for item in data.get("InfoList") or [] if isinstance(item, Mapping)]
            items.extend(page_items)
            cursor = as_text(data.get("Next"))
            if not page_items or cursor == "-1":
                break
        return items

    def _find_item(self, account: NetdiskAccountRecord, file: DriveFile) -> Mapping[str, Any]:
        """
        在文件所在目录中定位原始条目，取链与删除需要 S3KeyFlag/Etag。

        :param account: 账号
        :param file: 文件条目
        :return: 原始条目
        :raises NetdiskError: 目录中找不到该文件时抛出
        """
        for item in self._personal_items(account, file.parent_id):
            if as_text(item.get("FileId")) == file.file_id:
                return item
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "123 云盘未找到目标文件",
            platform=self.platform,
        )

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询 123 个人盘空间。

        :param account: 账号
        :return: 已用/总空间
        """
        payload = self._auth_get(_USER_INFO_URL, _USER_INFO_PATH, account)
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        return DriveQuota(
            used=as_int(data.get("SpaceUsed")),
            total=as_int(data.get("SpacePermanent")) + as_int(data.get("SpaceTemp")),
        )

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        列出 123 个人盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串为根
        :return: 目录条目
        """
        return [
            _drive_file(item)
            for item in self._personal_items(account, parent_id)
            if as_text(item.get("FileId"))
        ]

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取 123 个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        item = self._find_item(account, file)
        body = {
            "driveId": 0,
            "etag": as_text(item.get("Etag")),
            "fileId": _numeric_id(file.file_id),
            "s3keyFlag": as_text(item.get("S3KeyFlag")),
            "type": 0,
            "fileName": file.name or as_text(item.get("FileName")),
            "size": file.size or as_int(item.get("Size")),
        }
        payload = self._auth_post(
            _FILE_DOWNLOAD_INFO_URL,
            _FILE_DOWNLOAD_INFO_PATH,
            account,
            body,
            "获取 123 下载链接失败",
        )
        data = payload.get("data")
        raw_url = as_text(data.get("DownloadUrl")) if isinstance(data, Mapping) else ""
        url = _decode_download_url(raw_url) or raw_url
        if not url:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "123 云盘未返回下载地址", platform=self.platform)
        token = self._token(account)
        return DirectLink(
            url=url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": _DART_UA,
                "Referer": _DOWNLOAD_REFERER,
            },
            size=file.size or as_int(item.get("Size")),
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
        创建 123 个人盘分享。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码
        :param expires_in: 有效期秒数，0 表示永久
        :return: 分享结果
        """
        if not files:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "请选择要分享的文件", platform=self.platform)
        expires_at = (
            datetime.now(tz=_CN_TZ) + timedelta(seconds=expires_in) if expires_in > 0 else None
        )
        expiration = expires_at.isoformat() if expires_at is not None else _EXPIRATION_FOREVER
        file_ids = [_numeric_id(item.file_id) for item in files]
        file_id_list: Any = file_ids[0] if len(file_ids) == 1 else file_ids
        body: dict[str, Any] = {
            "driveId": 0,
            "expiration": expiration,
            "fileIdList": file_id_list,
            "shareName": files[0].name or "分享文件",
            "event": "shareCreate",
            "fileNum": len(files),
            "shareModality": 4,
            "trafficLimitSwitch": 1,
            "trafficLimit": 0,
            "trafficSwitch": 1,
            "fillPwdSwitch": 0,
        }
        if password:
            body["sharePwd"] = password
        payload = self._auth_post(
            _SHARE_CREATE_URL,
            _SHARE_CREATE_PATH,
            account,
            body,
            "创建 123 分享失败",
        )
        data = payload.get("data")
        share_key = as_text(data.get("ShareKey")) if isinstance(data, Mapping) else ""
        if not share_key:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建 123 分享失败：响应缺少 ShareKey",
                platform=self.platform,
            )
        url = ""
        link_list = data.get("shareLinkList") if isinstance(data, Mapping) else None
        if isinstance(link_list, Mapping):
            links = link_list.get("list")
            if isinstance(links, list) and links:
                url = as_text(links[0])
            if not url:
                url = as_text(link_list.get("standBy"))
        return DriveShare(
            url=url or f"https://www.123pan.com/s/{share_key}",
            password=password,
            expires_at=expires_at,
        )

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动 123 个人盘文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录标识
        """
        if not files:
            return
        self._auth_post(
            _FILE_MOD_PID_URL,
            _FILE_MOD_PID_PATH,
            account,
            {
                "fileIdList": [{"FileId": _numeric_id(item.file_id)} for item in files],
                "parentFileId": _numeric_id(target_id),
                "event": "fileMove",
                "operatePlace": 1,
                "RequestSource": None,
            },
            "移动 123 文件失败",
        )

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名 123 个人盘文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        self._auth_post(
            _FILE_RENAME_URL,
            _FILE_RENAME_PATH,
            account,
            {
                "driveId": 0,
                "fileId": _numeric_id(file.file_id),
                "fileName": name,
                "duplicate": 1,
                "event": "fileRename",
                "operatePlace": "right",
                "RequestSource": None,
            },
            "重命名 123 文件失败",
        )

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除 123 个人盘文件（移入回收站）。

        :param account: 账号
        :param files: 文件列表
        """
        if not files:
            return
        trash: list[dict[str, Any]] = []
        for file in files:
            item = self._find_item(account, file)
            trash.append(
                {
                    "FileId": _numeric_id(file.file_id),
                    "FileName": file.name or as_text(item.get("FileName")),
                    "Type": 1 if file.is_dir else 0,
                    "Size": file.size or as_int(item.get("Size")),
                    "S3KeyFlag": as_text(item.get("S3KeyFlag")),
                    "Etag": as_text(item.get("Etag")),
                }
            )
        self._auth_post(
            _FILE_TRASH_URL,
            _FILE_TRASH_PATH,
            account,
            {
                "driveId": 0,
                "fileTrashInfoList": trash,
                "operation": True,
                "event": "intoRecycle",
                "operatePlace": 1,
                "safeBox": False,
            },
            "删除 123 文件失败",
        )
