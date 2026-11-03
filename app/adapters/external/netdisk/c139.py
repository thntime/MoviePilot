"""
139（中国移动云盘）分享解析、目录读取与直链适配器。

协议依据 YunX `C139ResolveRepository` / `C139Api` 与公开的明文 V6 协议：
`getOutLinkInfoV6` 匿名列目录、`dlFromOutLinkV3` 登录后取直链，分享下载不需要转存。
登录态有两种等价形式：Cookie 中的 `Os_Sso_Sid` + `RMKEY`，或 `authorization`。

分享接口当前使用明文 V6 协议（不发送 `hcy-cool-flag`、不做请求体 AES 加密），
这是官方旧版客户端仍在使用的兼容通道；管理接口按 YunX 实现携带 `mcloud-sign`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from urllib.parse import quote

from app.adapters.external.netdisk.base import (
    BaseNetdiskAdapter,
    as_int,
    as_text,
    cookie_header,
    credential_value,
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
from app.runtime.log import logger

# 分享专用 host：列目录、取直链与分享信息都在该域。
_SHARE_BASE = "https://share-kd-njs.yun.139.com"
_SHARE_LIST_URL = f"{_SHARE_BASE}/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6"
_SHARE_LINK_URL = f"{_SHARE_BASE}/yun-share/richlifeApp/devapp/IOutLink/dlFromOutLinkV3"
_SHARE_GENERAL_URL = f"{_SHARE_BASE}/yun-share/richlifeApp/devapp/IOutLink/getOutLinkGeneral"
# 个人网盘管理接口，用于确认 authorization 是否仍可用。
_CLOUD_LIST_URL = "https://personal-kd-njs.yun.139.com/hcy/file/list"
_CLOUD_UPDATE_URL = "https://personal-kd-njs.yun.139.com/hcy/file/update"
_CLOUD_MOVE_URL = "https://personal-kd-njs.yun.139.com/hcy/file/batchMove"
_CLOUD_TRASH_URL = "https://personal-kd-njs.yun.139.com/hcy/recyclebin/batchTrash"
_CLOUD_DOWNLOAD_URL = "https://personal-kd-njs.yun.139.com/hcy/file/getDownloadUrl"
_CLOUD_QUOTA_URL = "https://user-njs.yun.139.com/user/disk/quota/detail"
_OUTLINK_CREATE_URL = (
    "https://yun.139.com/orchestration/personalCloud-rebuild/outlink/v1.0/getOutLink"
)
# 个人盘根目录在列目录、移动与创建目录接口中统一使用 `/`。
_DRIVE_ROOT_ID = "/"
_DRIVE_PAGE_SIZE = 100

# 分享接口必须使用浏览器/WebView UA，okhttp 等客户端 UA 会被设备校验拒绝。
_SHARE_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
)
# 分享接口设备与渠道头，缺任一会被网关判定设备异常。
_SHARE_X_DEVICEINFO = "||3|12.27.0|||||chrome 150.0.0.0|360X444|zh-cn|||"
_SHARE_X_HUAWEI_CHANNELSRC = "10245500"
_SHARE_X_MM_SOURCE = "0002"
# 管理接口 PC UA 与渠道头，缺失会返回 04000005 认证失败。
_CLOUD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_CHANNEL_SOURCE = "10000034"
_MCLOUD_VERSION = "7.17.9"
_MCLOUD_CLIENT = "10701"
_MCLOUD_CHANNEL = "1000101"
_YUN_MODULE_TYPE = "100"
_M4C_SRC = "10002"
_M4C_CALLER = "PC"
_X_DEVICEINFO = "||9|7.17.9|chrome|116.0.0.0|2cdaf7ada9e353c70eba99092e177991||windows 10||zh-CN|||"
_X_CLIENT_INFO = "||9|7.17.9|chrome|116.0.0.0|2cdaf7ada9e353c70eba99092e177991||windows 10||zh-CN|||dW5kZWZpbmVk||"

# 分享列表单页条数与最大翻页次数。
_PAGE_SIZE = 200
_MAX_PAGES = 100


def _credential_fields(credential: Mapping[str, str]) -> dict[str, str]:
    """
    展开凭据字段。

    `cookie`/`cookie_header`/`cookies` 中的键值会被拆分，显式字段优先于 Cookie 内的同名字段。

    :param credential: 明文凭据字段
    :return: 展开后的字段表
    """
    fields: dict[str, str] = {}
    for key in ("cookie", "cookie_header", "cookies"):
        raw = as_text(credential.get(key))
        if not raw:
            continue
        for part in raw.split(";"):
            item = part.strip()
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            fields[name.strip()] = value.strip()
    for key, value in credential.items():
        if key in {"cookie", "cookie_header", "cookies"}:
            continue
        text = as_text(value)
        if text:
            fields[str(key)] = text
    return fields


def _decode_base64_text(value: str) -> str:
    """
    解码 Base64 文本，失败时返回空串。

    :param value: Base64 文本
    :return: 解码结果
    """
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _authorization(fields: Mapping[str, str]) -> str:
    """
    读取登录凭据中的 authorization。

    :param fields: 展开后的凭据字段
    :return: authorization 文本；缺失时为空串
    """
    return as_text(fields.get("authorization"))


def _account_number(fields: Mapping[str, str]) -> str:
    """
    提取完整账号（手机号）。

    依次尝试 `ORCHES-I-ACCOUNT-ENCRYPT`、authorization 中的账号段与 `Login_UserNumber`。

    :param fields: 展开后的凭据字段
    :return: 账号文本；无法解析时为空串
    """
    encrypted = as_text(fields.get("ORCHES-I-ACCOUNT-ENCRYPT"))
    if encrypted:
        decoded = _decode_base64_text(encrypted)
        if decoded:
            return decoded
    authorization = _authorization(fields)
    if authorization:
        payload = _decode_base64_text(authorization.removeprefix("Basic").strip())
        parts = payload.split(":")
        if len(parts) > 1 and parts[1].strip():
            return parts[1].strip()
    return as_text(fields.get("Login_UserNumber"))


def _has_login(fields: Mapping[str, str]) -> bool:
    """
    判断凭据是否满足登录态要求。

    `authorization` 单独成立；否则需要 `Os_Sso_Sid` 与 `RMKEY` 同时存在。

    :param fields: 展开后的凭据字段
    :return: 是否具备登录态
    """
    if _authorization(fields):
        return True
    if fields.get("Os_Sso_Sid") and fields.get("RMKEY"):
        return True
    # YunX 网页登录导出的 Cookie 也可能只带 a_k/skey（分享接口使用）。
    return bool(fields.get("a_k")) and bool(fields.get("skey"))


def _md5(text: str) -> str:
    """
    计算小写 MD5。

    :param text: 待计算文本
    :return: 32 位小写十六进制
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _encode_uri_component(text: str) -> str:
    """
    按 `encodeURIComponent` 语义编码。

    保留 `! ' ( ) *` 与 Java `URLEncoder` 的不编码字符，与 YunX 的签名实现一致。

    :param text: 待编码文本
    :return: 编码结果
    """
    return quote(text, safe="!*'()-._")


def _sign_header(body: str) -> str:
    """
    生成管理接口的 `mcloud-sign` 头值。

    签名基于实际发送的明文 JSON：字符升序 → Base64 → 两次 MD5 拼接后再 MD5 并转大写。

    :param body: 实际发送的明文 JSON
    :return: `<ts>,<rand>,<sign>`
    """
    encoded = _encode_uri_component(body)
    sorted_text = "".join(sorted(encoded))
    digest = _md5(base64.b64encode(sorted_text.encode("utf-8")).decode("utf-8"))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    signature = _md5(digest + _md5(f"{stamp}:{rand}")).upper()
    return f"{stamp},{rand},{signature}"


def _parse_time(value: Any) -> Optional[datetime]:
    """
    解析个人盘条目返回的修改时间。

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


def _parse_share_files(data: Mapping[str, Any], parent_id: str) -> list[ShareFile]:
    """
    合并 `caLst`（子目录）与 `coLst`（文件）为统一条目。

    :param data: 响应中的 data 对象
    :param parent_id: 当前目录标识
    :return: 目录条目，子目录在前
    """
    files: list[ShareFile] = []
    for item in data.get("caLst") or []:
        if not isinstance(item, Mapping):
            continue
        files.append(
            ShareFile(
                file_id=as_text(item.get("caID")),
                name=as_text(item.get("caName")),
                size=0,
                is_dir=True,
                parent_id=parent_id,
            )
        )
    for item in data.get("coLst") or []:
        if not isinstance(item, Mapping):
            continue
        files.append(
            ShareFile(
                file_id=as_text(item.get("coID")),
                name=as_text(item.get("coName")),
                size=as_int(item.get("coSize")),
                is_dir=item.get("isdir") is True or as_int(item.get("coType")) == 2,
                parent_id=parent_id,
            )
        )
    return files


class C139Adapter(BaseNetdiskAdapter):
    """
    139（中国移动云盘）协议适配器。

    列目录为匿名端点，取直链需要登录账号；分享下载不需要转存。
    """

    platform = NetdiskPlatform.PAN139
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    guest_supported = False
    drive_capabilities: DriveCapabilities = DriveCapabilities(
        quota=True,
        browse=True,
        download=True,
        share=True,
        move=True,
        rename=True,
        delete=True,
    )
    max_concurrency = 512
    # 139 支持 authorization、Os_Sso_Sid+RMKEY、a_k+skey 三种登录态，
    # 由 validate_credential 统一判断，避免预检查只认移动端字段。
    cookie_required_keys: tuple[str, ...] = ()
    cookie_domains = (".139.com", ".10086.cn")
    login_url = "https://yun.139.com/"
    supports_password_login = False
    user_agent = _SHARE_MOBILE_UA
    referer = "https://yun.139.com/"

    def _share_headers(self) -> dict[str, str]:
        """
        组装分享接口请求头。

        :return: 请求头字典
        """
        return {
            "x-deviceinfo": _SHARE_X_DEVICEINFO,
            "x-huawei-channelsrc": _SHARE_X_HUAWEI_CHANNELSRC,
            "x-mm-source": _SHARE_X_MM_SOURCE,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
            "User-Agent": _SHARE_MOBILE_UA,
            "Content-Type": "application/json;charset=UTF-8",
        }

    def _check_result(self, payload: Mapping[str, Any], fallback: str) -> None:
        """
        校验分享接口的业务响应码。

        :param payload: 响应 JSON
        :param fallback: 无服务端文案时的兜底说明
        :raises NetdiskError: 业务码非成功时抛出
        """
        code = as_text(payload.get("resultCode")) or as_text(payload.get("code"))
        success = payload.get("success", True)
        message = as_text(payload.get("desc")) or as_text(payload.get("message")) or fallback
        if not code or code == "0":
            if success is False:
                raise NetdiskError(NetdiskErrorCode.SHARE_INVALID, message, platform=self.platform)
            return
        if code in {"401", "403", "9187", "04000005"}:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "139 云盘登录态已失效", platform=self.platform)
        if code == "429":
            raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "139 云盘接口请求过于频繁", platform=self.platform)
        if code in {"9188", "9189", "9531"}:
            raise NetdiskError(NetdiskErrorCode.SHARE_INVALID, message, platform=self.platform)
        if code == "9530":
            raise NetdiskError(NetdiskErrorCode.RISK_BLOCKED, message, platform=self.platform)
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, message, platform=self.platform)

    def _share_post(
        self,
        url: str,
        payload: Mapping[str, Any],
        fallback: str,
        *,
        authorization: str = "",
    ) -> dict[str, Any]:
        """
        调用分享接口并校验业务码。

        :param url: 接口地址
        :param payload: 明文 JSON 请求体
        :param fallback: 失败兜底说明
        :param authorization: 可选登录凭据
        :return: 响应 JSON
        """
        headers = self._share_headers()
        if authorization:
            headers["Authorization"] = authorization
        response = self.http.post_json(url, json=payload, headers=headers)
        self._check_result(response, fallback)
        return response

    def _general(self, link_id: str) -> tuple[str, str]:
        """
        读取分享标题与明文提取码。

        该端点失败不影响列目录，异常时返回空值由调用方回退。

        :param link_id: 分享标识
        :return: `(标题, 提取码)`
        """
        try:
            response = self._share_post(
                _SHARE_GENERAL_URL,
                {
                    "getOutLinkGeneralReq": {
                        "linkID": link_id,
                        "isPasswd": 1,
                        "account": "",
                    }
                },
                "获取 139 分享信息失败",
            )
        except NetdiskError as error:
            logger.warning(f"139 分享信息读取失败：link={link_id} error={error.code.value}")
            return "", ""
        data = response.get("data")
        if not isinstance(data, Mapping):
            return "", ""
        general = data.get("getOutLinkGeneralResp")
        items = general.get("outLinkGeneral") if isinstance(general, Mapping) else None
        if not items:
            return "", ""
        first = items[0] if isinstance(items[0], Mapping) else {}
        return as_text(first.get("lkName")), as_text(first.get("passwd"))

    def _fetch_page(
        self,
        link_id: str,
        parent_id: str,
        password: str,
        begin: int,
        end: int,
    ) -> list[ShareFile]:
        """
        读取一页分享目录。

        :param link_id: 分享标识
        :param parent_id: 父目录标识，根目录为 `root`
        :param password: 提取码，无则空串
        :param begin: 分页起始序号
        :param end: 分页结束序号
        :return: 目录条目
        :raises NetdiskError: 提取码错误、分享失效或接口失败时抛出
        """
        response = self._share_post(
            _SHARE_LIST_URL,
            {
                "getOutLinkInfoReq": {
                    "account": "",
                    "linkID": link_id,
                    "passwd": password,
                    "caSrt": 1,
                    "coSrt": 1,
                    "srtDr": 0,
                    "bNum": begin,
                    "pCaID": parent_id,
                    "eNum": end,
                }
            },
            "获取 139 分享文件列表失败",
        )
        data = response.get("data")
        if not isinstance(data, Mapping):
            return []
        return _parse_share_files(data, parent_id)

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        读取分享标题与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号，列目录允许匿名
        :return: 分享详情
        """
        title, leaked_password = self._general(ref.share_id)
        password = ref.password or leaked_password
        files = self._fetch_page(ref.share_id, "root", password, 1, _PAGE_SIZE)
        return ShareDetail(
            title=title or ref.share_id,
            size=sum(item.size for item in files),
            files=tuple(files),
            stoken=password,
        )

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        分页读取分享目录。

        根目录必须传 `root`，子目录传父级 `caID` 或 `coType == 2` 的 `coID`。

        :param ref: 分享引用
        :param parent_id: 父目录标识
        :param account: 取链账号
        :return: 目录内全部条目
        """
        pca_id = "root" if parent_id in ("", "0") else parent_id
        password = ref.password or ""
        collected: list[ShareFile] = []
        begin = 1
        for _ in range(_MAX_PAGES):
            batch = self._fetch_page(ref.share_id, pca_id, password, begin, begin + _PAGE_SIZE - 1)
            collected.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            begin += _PAGE_SIZE
        return collected

    def _download_headers(self, account: Optional[NetdiskAccountRecord]) -> dict[str, str]:
        """
        组装下载请求头。

        必须携带账号 Cookie（含 authorization）与客户端 UA，直链与账号绑定。

        :param account: 取链账号
        :return: 请求头字典
        """
        headers = self.http.headers(
            account,
            extra={"User-Agent": _SHARE_MOBILE_UA, "Referer": "https://yun.139.com/"},
        )
        authorization = credential_value(account, "authorization")
        if authorization and "authorization=" not in headers.get("Cookie", ""):
            cookie = headers.get("Cookie", "")
            headers["Cookie"] = f"{cookie}; authorization={authorization}" if cookie else f"authorization={authorization}"
        return headers

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

        139 分享免转存，`allow_transfer` 仅用于对齐接口，不影响行为。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底，本平台忽略
        :return: 直链及其账号 Cookie 与 UA
        :raises NetdiskError: 登录态缺失、分享失效或接口失败时抛出
        """
        del allow_transfer
        if account is None:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "139 云盘取链需要登录账号", platform=self.platform)
        fields = _credential_fields(account.credential or {})
        account_number = _account_number(fields)
        if not account_number:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "139 云盘登录态缺少账号信息，请重新登录",
                platform=self.platform,
                account_name=account.name,
            )
        response = self._share_post(
            _SHARE_LINK_URL,
            {
                "dlFromOutLinkReqV3": {
                    "account": account_number,
                    "linkID": ref.share_id,
                    "coIDLst": {"item": [file.file_id]},
                    "commonAccountInfo": {"account": account_number, "accountType": 1},
                }
            },
            "获取 139 下载链接失败",
            authorization=_authorization(fields),
        )
        data = response.get("data")
        url = as_text(data.get("redrUrl")) if isinstance(data, Mapping) else ""
        if not url:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "139 云盘未返回下载地址", platform=self.platform)
        return DirectLink(
            url=url,
            headers=self._download_headers(account),
            size=file.size,
            account_id=account.id,
        )

    def _cloud_headers(self, authorization: str, body: Mapping[str, Any]) -> dict[str, str]:
        """
        组装管理接口请求头。

        `mcloud-sign` 基于实际发送的明文 JSON 计算，因此与 `json.dumps` 默认分隔符保持一致。

        :param authorization: 登录凭据
        :param body: 明文 JSON 请求体
        :return: 请求头字典
        """
        plain = json.dumps(body, ensure_ascii=False, allow_nan=False)
        return {
            "Authorization": authorization,
            "mcloud-sign": _sign_header(plain),
            "x-yun-channel-source": _CHANNEL_SOURCE,
            "x-yun-app-channel": _CHANNEL_SOURCE,
            "x-huawei-channelSrc": _CHANNEL_SOURCE,
            "mcloud-version": _MCLOUD_VERSION,
            "mcloud-client": _MCLOUD_CLIENT,
            "mcloud-channel": _MCLOUD_CHANNEL,
            "mcloud-route": "001",
            "x-yun-module-type": _YUN_MODULE_TYPE,
            "x-yun-api-version": "v1",
            "x-yun-svc-type": "1",
            "x-SvcType": "1",
            "caller": "web",
            "x-inner-ntwk": "2",
            "CMS-DEVICE": "default",
            "x-m4c-src": _M4C_SRC,
            "x-m4c-caller": _M4C_CALLER,
            "X-Deviceinfo": _X_DEVICEINFO,
            "x-yun-client-info": _X_CLIENT_INFO,
            "INNER-HCY-ROUTER-HTTPS": "1",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": _CLOUD_UA,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
        }

    def _check_cloud(self, payload: Mapping[str, Any], fallback: str) -> None:
        """
        校验管理接口响应。

        :param payload: 响应 JSON
        :param fallback: 失败兜底说明
        :raises NetdiskError: 认证失败或接口失败时抛出
        """
        code = as_text(payload.get("code"))
        success = payload.get("success", True)
        if success is not False and code in ("", "0", "0000"):
            return
        message = as_text(payload.get("message")) or fallback
        if code in {"401", "403", "04000005"}:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "139 云盘登录态已失效", platform=self.platform)
        if code == "429":
            raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "139 云盘接口请求过于频繁", platform=self.platform)
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, message, platform=self.platform)

    def _account_info(self, authorization: str) -> dict[str, Any]:
        """
        调用个人网盘列表接口确认登录态。

        :param authorization: 登录凭据
        :return: 响应中的 data 对象
        :raises NetdiskError: 登录态失效或接口失败时抛出
        """
        body = {
            "pageInfo": {"pageSize": 1, "pageCursor": None},
            "orderBy": "updated_at",
            "orderDirection": "DESC",
            "parentFileId": "root",
            "imageThumbnailStyleList": ["Small", "Large"],
        }
        payload = self.http.post_json(_CLOUD_LIST_URL, json=body, headers=self._cloud_headers(authorization, body))
        self._check_cloud(payload, "获取 139 账号信息失败")
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def login_ready(self, credential: Mapping[str, str]) -> bool:
        """
        网页登录必须拿到 authorization 或移动端会话字段才可确认。

        `a_k`/`skey` 在未完成登录时也可能存在，不能作为登录完成依据。

        :param credential: 浏览器抓到的凭据
        :return: 是否已登录
        """
        fields = _credential_fields(credential)
        if _authorization(fields):
            return True
        return bool(fields.get("Os_Sso_Sid")) and bool(fields.get("RMKEY"))

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从 139 凭据中提取手机号。

        :param credential: 浏览器抓到的凭据
        :return: 账号名；无法提取时为空
        """
        try:
            return _account_number(_credential_fields(credential))
        except Exception:  # noqa: BLE001 - 账号提取失败不影响登录
            return ""

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验凭据：先按字段判断，有 authorization 时再调用管理接口确认。

        :param credential: 明文凭据字段
        :return: 探测结果；接口不可用时返回 `unknown`
        """
        fields = _credential_fields(credential)
        if not _has_login(fields):
            return AccountProbe(
                status="expired",
                message="凭据缺少必填字段：authorization、Os_Sso_Sid+RMKEY 或 a_k+skey",
            )
        authorization = _authorization(fields)
        if not authorization:
            # 没有 authorization 时无法调用个人盘接口，按字段完整性接受，后续取链再校验。
            return AccountProbe(status="active", message="凭据字段完整（未校验个人盘接口）")
        try:
            info = self._account_info(authorization)
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="139 云盘登录态已失效")
            return AccountProbe(status="unknown", message=error.message)
        nickname = as_text(info.get("nickName")) or as_text(info.get("nickname"))
        message = f"139 云盘登录态有效：{nickname}" if nickname else "139 云盘登录态有效"
        return AccountProbe(status="active", message=message)

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {})

    # ---- 个人盘 ----

    def _cloud_post(
        self,
        url: str,
        body: Mapping[str, Any],
        account: NetdiskAccountRecord,
        fallback: str,
        *,
        with_cookie: bool = False,
    ) -> dict[str, Any]:
        """
        调用个人盘管理接口并校验业务响应码。

        :param url: 接口地址
        :param body: 明文 JSON 请求体
        :param account: 账号
        :param fallback: 失败兜底说明
        :param with_cookie: 是否附带 Cookie 与 `mcloud-skey`（创建分享与空间查询需要）
        :return: 响应 JSON
        :raises NetdiskError: 登录态缺失或接口失败时抛出
        """
        fields = _credential_fields(account.credential or {})
        authorization = _authorization(fields)
        if not authorization:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "139 云盘个人盘操作需要 authorization",
                platform=self.platform,
                account_name=account.name,
            )
        headers = self._cloud_headers(authorization, body)
        if with_cookie:
            cookie = cookie_header(account)
            if cookie:
                headers["Cookie"] = cookie
            skey = as_text(fields.get("skey"))
            if skey:
                headers["mcloud-skey"] = skey
        payload = self.http.post_json(url, json=body, headers=headers)
        self._check_cloud(payload, fallback)
        return payload

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询 139 个人盘空间。

        接口返回 MB，统一换算为字节；已用空间优先取个人云配额，缺失时用总量减剩余。

        :param account: 账号
        :return: 已用/总空间
        """
        fields = _credential_fields(account.credential or {})
        number = _account_number(fields)
        if not number:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "139 云盘登录态缺少账号信息，请重新登录",
                platform=self.platform,
                account_name=account.name,
            )
        payload = self._cloud_post(
            _CLOUD_QUOTA_URL,
            {
                "userDomainId": "",
                "commonAccountInfo": {"account": number, "accountType": 1},
            },
            account,
            "获取 139 空间信息失败",
            with_cookie=True,
        )
        data = payload.get("data")
        info: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        total = as_int(info.get("diskSize")) * 1024 * 1024
        quota_list = info.get("quotaList")
        used = 0
        if isinstance(quota_list, list) and quota_list and isinstance(quota_list[0], Mapping):
            used = as_int(quota_list[0].get("usedSize")) * 1024 * 1024
        if not used:
            free = as_int(info.get("freeDiskSize")) * 1024 * 1024
            used = max(total - free, 0)
        return DriveQuota(used=used, total=total)

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        按游标分页列出 139 个人盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串表示根目录
        :return: 目录条目
        """
        parent = parent_id or _DRIVE_ROOT_ID
        files: list[DriveFile] = []
        cursor: Optional[str] = None
        for _ in range(_MAX_PAGES):
            payload = self._cloud_post(
                _CLOUD_LIST_URL,
                {
                    "pageInfo": {"pageSize": _DRIVE_PAGE_SIZE, "pageCursor": cursor},
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "parentFileId": parent,
                    "imageThumbnailStyleList": ["Small", "Large"],
                },
                account,
                "获取 139 个人盘文件列表失败",
            )
            data = payload.get("data")
            info: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
            items = info.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, Mapping):
                    files.append(self._to_drive_file(item, parent))
            next_cursor = as_text(info.get("nextPageCursor"))
            if not next_cursor:
                break
            cursor = next_cursor
        return files

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取 139 个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        payload = self._cloud_post(
            _CLOUD_DOWNLOAD_URL,
            {"fileId": file.file_id},
            account,
            "获取 139 下载链接失败",
        )
        data = payload.get("data")
        info: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        url = as_text(info.get("url"))
        if not url:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "139 云盘未返回下载地址", platform=self.platform)
        return DirectLink(
            url=url,
            headers={"User-Agent": _CLOUD_UA},
            size=as_int(info.get("size")) or file.size,
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
        创建 139 个人盘分享。

        139 由服务端生成提取码，`password` 仅作为响应缺失时的兜底；有效期只支持
        永久、1 天、7 天与 30 天，超出范围按 30 天处理。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码兜底值
        :param expires_in: 有效期秒数，0 表示永久
        :return: 分享结果
        """
        fields = _credential_fields(account.credential or {})
        number = _account_number(fields)
        if not number:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "139 云盘登录态缺少账号信息，请重新登录",
                platform=self.platform,
                account_name=account.name,
            )
        if expires_in <= 0:
            period: Optional[int] = None
        elif expires_in <= 86400:
            period = 1
        elif expires_in <= 7 * 86400:
            period = 7
        else:
            period = 30
        title = files[0].name if files else "分享文件"
        body = {
            "getOutLinkReq": {
                "subLinkType": 0,
                "encrypt": 1,
                "coIDLst": [item.file_id for item in files if not item.is_dir],
                "caIDLst": [item.file_id for item in files if item.is_dir],
                "pubType": 1,
                "dedicatedName": title,
                "periodUnit": 1,
                **({"period": period} if period is not None else {}),
                "viewerLst": [],
                "extInfo": {"isWatermark": 0, "shareChannel": "3001"},
                "commonAccountInfo": {"account": number, "accountType": 1},
            }
        }
        payload = self._cloud_post(
            _OUTLINK_CREATE_URL,
            body,
            account,
            "创建 139 分享失败",
            with_cookie=True,
        )
        data = payload.get("data")
        info: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        result = info.get("getOutLinkRes")
        result_info: Mapping[str, Any] = result if isinstance(result, Mapping) else {}
        items = result_info.get("getOutLinkResSet")
        first = items[0] if isinstance(items, list) and items and isinstance(items[0], Mapping) else {}
        url = as_text(first.get("linkUrl"))
        if not url:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "139 云盘未返回分享链接", platform=self.platform)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in > 0 else None
        )
        return DriveShare(
            url=url,
            password=as_text(first.get("passwd")) or password,
            expires_at=expires_at,
        )

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动 139 个人盘文件。

        接口异步执行，响应中的 taskId 由平台排队处理。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录，空串表示根目录
        """
        self._cloud_post(
            _CLOUD_MOVE_URL,
            {
                "fileIds": [item.file_id for item in files],
                "toParentFileId": target_id or _DRIVE_ROOT_ID,
            },
            account,
            "移动 139 文件失败",
        )

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名 139 个人盘文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        self._cloud_post(
            _CLOUD_UPDATE_URL,
            {"fileId": file.file_id, "name": name, "description": ""},
            account,
            "重命名 139 文件失败",
        )

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除 139 个人盘文件（移入回收站）。

        接口异步执行，响应中的 taskId 由平台排队处理。

        :param account: 账号
        :param files: 文件列表
        """
        self._cloud_post(
            _CLOUD_TRASH_URL,
            {"fileIds": [item.file_id for item in files]},
            account,
            "删除 139 文件失败",
        )

    @staticmethod
    def _to_drive_file(item: Mapping[str, Any], parent_id: str) -> DriveFile:
        """
        把个人盘条目转换为领域文件对象。

        :param item: 接口条目
        :param parent_id: 请求的父目录标识
        :return: 领域文件对象
        """
        return DriveFile(
            file_id=as_text(item.get("fileId")),
            name=as_text(item.get("name")),
            size=as_int(item.get("size")),
            is_dir=as_text(item.get("type")) == "folder",
            updated_at=_parse_time(item.get("updatedAt")),
            parent_id=parent_id,
        )
