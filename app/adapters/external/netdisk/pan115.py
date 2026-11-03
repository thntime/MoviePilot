"""
115 网盘协议适配器。

只负责「分享 → 直链」这一段：分享目录、网页端免转存取链、大文件转存兜底、
临时目录清理与账号探测。网页端对大文件拒发直链（errno 50029）时，转存到唯一
临时目录后再走电脑端加密通道取链，下载完成后由用例层调用 `cleanup` 删除目录。

与其它平台不同的两点：

- 分享直链必须带**分享者** `user_id`，它只出现在 `share/snap` 的 `userinfo` 里，
  因此分享会话令牌按 `<提取码>|<分享者 UID>` 编码后放进 `ShareRef.share_token`；
- 直链 CDN 只认「登录 Cookie + 取链响应头下发的 900 秒 CDN Cookie」双拼，
  下载请求头由适配器统一装配，调用方原样交给下载器即可。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.adapters.external.netdisk.base import (
    BaseNetdiskAdapter,
    NetdiskHttp,
    as_int,
    as_text,
    cookie_header,
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
    TransferHandle,
)

_API_BASE = "https://webapi.115.com"
_USER_INFO_URL = f"{_API_BASE}/user/info?ct=json"
_SHARE_SNAP_URL = f"{_API_BASE}/share/snap"
_SHARE_DOWNLOAD_URL = f"{_API_BASE}/share/downurl"
_SHARE_RECEIVE_URL = f"{_API_BASE}/share/receive"
_FILES_URL = f"{_API_BASE}/files"
_FILE_ADD_URL = f"{_API_BASE}/files/add"
_FILE_DELETE_URL = f"{_API_BASE}/rb/delete"
_FILE_DOWNLOAD_URL = f"{_API_BASE}/files/download"
_APP_DOWNLOAD_URL = "https://proapi.115.com/app/chrome/downurl"
_INDEX_INFO_URL = f"{_API_BASE}/files/index_info?count_space_nums=1"
_FILE_MOVE_URL = f"{_API_BASE}/files/move"
_MOVE_PROGRESS_URL = f"{_API_BASE}/files/move_progress"
_FILE_RENAME_URL = f"{_API_BASE}/files/batch_rename"
_SHARE_SEND_URL = f"{_API_BASE}/share/send"
_SHARE_UPDATE_URL = f"{_API_BASE}/share/updateshare"
_ALLOW_PROTOCOL_URL = f"{_API_BASE}/user/allow_protocol"

_SHARE_HOST = "https://115cdn.com"
_REFERER = "https://115.com/"

# 取链与下载必须使用客户端 UA：浏览器 UA 会被判「版本过低」直接拒发直链（errno 50029）。
CLIENT_USER_AGENT = "Mozilla/5.0 115Browser/36.0.1"

# 登录 Cookie 中需要保留的会话字段；其余（WAF、统计等）字段与平台会话无关。
_LOGIN_COOKIE_KEYS = ("UID", "CID", "SEID", "KID", "PHPSESSID", "GST", "USERSESSIONID")

_ROOT_CID = "0"
_AID_PERSONAL = "1"
_SHARE_PAGE_LIMIT = 100
_FILE_PAGE_LIMIT = 100
_SHARE_MAX_PAGES = 50

# 转存临时目录前缀；名称带毫秒时间戳与随机串，保证每次转存都是唯一目标目录。
_TEMP_DIR_PREFIX = "mp_tr_"
_TRANSFER_POLL_TIMES = 6
_TRANSFER_POLL_INTERVAL = 0.5

# 服务端错误码。
_ERRNO_LOGIN_TIMEOUT = 990001
_ERRNO_NEED_CODE = 4100012
_ERRNO_WRONG_CODE = 4100008
_ERRNO_SHARE_GONE = 4100026
_ERRNO_SHARE_CANCELLED = 4100010
_ERRNO_SHARE_EXPIRED = 4100018
_ERRNO_EMPTY_FOLDER = 4100016
_ERRNO_BUSY = 990009
_ERRNO_WEB_SIZE_LIMIT = 50028
_ERRNO_CLIENT_TOO_OLD = 50029
_ERRNO_NEED_CONFIRM = 800007

# 移动是异步任务：响应含 move_proid 时按固定间隔轮询进度。
_MOVE_POLL_MAX = 20
_MOVE_POLL_INTERVAL = 0.4

# 电脑端取链接口的固定协议参数（RSA + XOR，见 `_encode_payload` / `_decode_payload`）。
_RSA_BLOCK = 128
_RSA_ENCODE_BLOCK = 117
_RSA_EXPONENT = 0x10001
_RSA_MODULUS = int(
    "8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021"
    "36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad633"
    "8c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f1"
    "0a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683",
    16,
)
_KEY_TABLE = bytes(
    (
        0xF0, 0xE5, 0x69, 0xAE, 0xBF, 0xDC, 0xBF, 0x8A,
        0x1A, 0x45, 0xE8, 0xBE, 0x7D, 0xA6, 0x73, 0xB8,
        0xDE, 0x8F, 0xE7, 0xC4, 0x45, 0xDA, 0x86, 0xC4,
        0x9B, 0x64, 0x8B, 0x14, 0x6A, 0xB4, 0xF1, 0xAA,
        0x38, 0x01, 0x35, 0x9E, 0x26, 0x69, 0x2C, 0x86,
        0x00, 0x6B, 0x4F, 0xA5, 0x36, 0x34, 0x62, 0xA6,
        0x2A, 0x96, 0x68, 0x18, 0xF2, 0x4A, 0xFD, 0xBD,
        0x6B, 0x97, 0x8F, 0x4D, 0x8F, 0x89, 0x13, 0xB7,
        0x6C, 0x8E, 0x93, 0xED, 0x0E, 0x0D, 0x48, 0x3E,
        0xD7, 0x2F, 0x88, 0xD8, 0xFE, 0xFE, 0x7E, 0x86,
        0x50, 0x95, 0x4F, 0xD1, 0xEB, 0x83, 0x26, 0x34,
        0xDB, 0x66, 0x7B, 0x9C, 0x7E, 0x9D, 0x7A, 0x81,
        0x32, 0xEA, 0xB6, 0x33, 0xDE, 0x3A, 0xA9, 0x59,
        0x34, 0x66, 0x3B, 0xAA, 0xBA, 0x81, 0x60, 0x48,
        0xB9, 0xD5, 0x81, 0x9C, 0xF8, 0x6C, 0x84, 0x77,
        0xFF, 0x54, 0x78, 0x26, 0x5F, 0xBE, 0xE8, 0x1E,
        0x36, 0x9F, 0x34, 0x80, 0x5C, 0x45, 0x2C, 0x9B,
        0x76, 0xD5, 0x1B, 0x8F, 0xCC, 0xC3, 0xB8, 0xF5,
    )
)
_LONG_KEY = bytes((0x78, 0x06, 0xAD, 0x4C, 0x33, 0x86, 0x5D, 0x18, 0x4C, 0x01, 0x3F, 0x46))


class _WebLinkDenied(NetdiskError):
    """
    网页端拒绝下发直链（文件过大），调用方可回退到转存通道。

    :param errno: 115 服务端错误码
    :param message: 可直接展示的原因
    """

    def __init__(self, errno: int, message: str) -> None:
        """
        保存错误码并固定为可回退的未支持分类。
        """
        super().__init__(NetdiskErrorCode.UNSUPPORTED, message, platform=NetdiskPlatform.PAN115)
        self.errno = errno


def _filter_login_cookies(raw: str) -> str:
    """
    只保留 115 会话字段，拼成请求头可直接使用的 Cookie 串。

    :param raw: 原始 Cookie 头
    :return: 过滤后的 Cookie；没有会话字段时返回空串
    """
    kept: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name in _LOGIN_COOKIE_KEYS and name not in kept:
            kept[name] = value.strip()
    return "; ".join(f"{name}={value}" for name, value in kept.items())


def _merge_cookies(*parts: str) -> str:
    """
    合并登录 Cookie 与直链响应下发的 CDN Cookie。

    :param parts: 多段 Cookie
    :return: 以分号拼接的 Cookie 头
    """
    return "; ".join(part.strip() for part in parts if str(part or "").strip())


def _credential_cookie(credential: Mapping[str, str]) -> str:
    """
    从明文凭据中取出 Cookie 原文。

    :param credential: 凭据字段
    :return: Cookie 原文
    """
    for key in ("cookie", "cookie_header", "cookies"):
        value = credential.get(key)
        if value:
            return str(value)
    return "; ".join(
        f"{key}={value}" for key, value in credential.items() if str(value or "").strip()
    )


def _download_cookie(response: Any) -> str:
    """
    读取直链接口响应头下发的 CDN Cookie。

    优先取 path 绑定到对象路径的那条（CDN 只认它），否则退回第一条可用 Cookie。

    :param response: HTTP 响应
    :return: `name=value` 形式的 Cookie；没有时返回空串
    """
    jar = getattr(response, "cookies", None)
    fallback = ""
    try:
        items = list(jar) if jar is not None else []
    except TypeError:
        items = []
    for item in items:
        name = as_text(getattr(item, "name", ""))
        value = getattr(item, "value", "")
        if not name or value is None:
            continue
        pair = f"{name}={value}"
        if not fallback:
            fallback = pair
        path = as_text(getattr(item, "path", ""))
        if path and path != "/":
            return pair
    if fallback:
        return fallback
    return _first_set_cookie(response)


def _first_set_cookie(response: Any) -> str:
    """
    从 Set-Cookie 头中取第一条 `name=value`。

    :param response: HTTP 响应
    :return: `name=value`；响应缺少该头时返回空串
    """
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    except Exception:  # noqa: BLE001 - 响应替身可能没有 headers 映射
        return ""
    for part in str(raw).split(","):
        pair = part.split(";")[0].strip()
        name = pair.split("=", 1)[0].strip().lower()
        if "=" in pair and name not in {"expires", "path", "domain", "max-age", "samesite"}:
            return pair
    return ""


def _encode_share_token(receive_code: str, user_id: str) -> str:
    """
    编码分享会话令牌：`<提取码>|<分享者 UID>`。

    :param receive_code: 提取码
    :param user_id: 分享者 UID
    :return: 会话令牌
    """
    return f"{receive_code}|{user_id}"


def _decode_share_token(token: str) -> tuple[str, str]:
    """
    反解分享会话令牌。

    :param token: 会话令牌
    :return: 提取码与分享者 UID
    """
    index = str(token or "").find("|")
    if index < 0:
        return str(token or ""), ""
    return token[:index], token[index + 1 :]


def _normalize_error(error: NetdiskError) -> NetdiskError:
    """
    补齐基类未覆盖的错误分类，尤其是 412 防盗链拦截。

    :param error: 基类抛出的异常
    :return: 分类明确的异常
    """
    if error.code is not NetdiskErrorCode.UNKNOWN:
        return error
    message = str(error.message or "")
    if "412" in message or "防盗链" in message:
        return NetdiskError(
            NetdiskErrorCode.LINK_EXPIRED,
            "115 直链已失效或被防盗链拦截",
            platform=NetdiskPlatform.PAN115,
        )
    return error


def _error_for_errno(errno: int, message: str, action: str) -> NetdiskError:
    """
    把 115 服务端错误码收敛为统一错误分类。

    :param errno: 服务端错误码
    :param message: 服务端提示
    :param action: 用于错误提示的动作名
    :return: 分类异常
    """
    text = message or "115 接口返回失败"
    if errno in (_ERRNO_WEB_SIZE_LIMIT, _ERRNO_CLIENT_TOO_OLD):
        return _WebLinkDenied(
            errno,
            "115 拒绝向网页端下发该文件直链（文件过大），需要转存后使用客户端通道下载",
        )
    if errno == _ERRNO_LOGIN_TIMEOUT:
        return NetdiskError(
            NetdiskErrorCode.AUTH_EXPIRED, "115 登录态已失效", platform=NetdiskPlatform.PAN115
        )
    if errno in (_ERRNO_NEED_CODE, _ERRNO_WRONG_CODE):
        return NetdiskError(
            NetdiskErrorCode.SHARE_INVALID,
            "115 分享需要提取码或提取码错误",
            platform=NetdiskPlatform.PAN115,
        )
    if errno in (_ERRNO_SHARE_GONE, _ERRNO_SHARE_CANCELLED):
        return NetdiskError(
            NetdiskErrorCode.SHARE_INVALID,
            "115 分享不存在、已取消或已被删除",
            platform=NetdiskPlatform.PAN115,
        )
    if errno == _ERRNO_SHARE_EXPIRED:
        return NetdiskError(
            NetdiskErrorCode.LINK_EXPIRED,
            "115 分享链接已过期",
            platform=NetdiskPlatform.PAN115,
        )
    if errno == _ERRNO_EMPTY_FOLDER:
        return NetdiskError(
            NetdiskErrorCode.SHARE_INVALID,
            "115 不允许分享空文件夹",
            platform=NetdiskPlatform.PAN115,
        )
    if errno == _ERRNO_BUSY:
        return NetdiskError(
            NetdiskErrorCode.RATE_LIMITED,
            "115 上一次操作尚未完成，请稍后重试",
            platform=NetdiskPlatform.PAN115,
        )
    suffix = f"（errno={errno}）" if errno and str(errno) not in text else ""
    return NetdiskError(
        NetdiskErrorCode.UNKNOWN,
        f"{action}失败：{text}{suffix}",
        platform=NetdiskPlatform.PAN115,
    )


def _check_state(payload: Mapping[str, Any], action: str) -> dict[str, Any]:
    """
    校验 115 统一响应的 `state` 字段。

    :param payload: 响应 JSON
    :param action: 用于错误提示的动作名
    :return: 原响应 JSON
    :raises NetdiskError: 服务端返回失败状态时抛出
    """
    if payload.get("state") is True:
        return dict(payload)
    errno = as_int(payload.get("errno") or payload.get("errNo"))
    message = as_text(payload.get("error") or payload.get("message") or payload.get("msg"))
    raise _error_for_errno(errno, message, action)


def _parse_share_files(data: Mapping[str, Any], parent_id: str) -> list[ShareFile]:
    """
    解析分享目录条目：`fc == 0` 为目录（ID 取 `cid`），其余为文件（ID 取 `fid`）。

    :param data: `share/snap` 的 data 节点
    :param parent_id: 当前目录标识
    :return: 目录条目
    """
    result: list[ShareFile] = []
    for item in data.get("list") or []:
        if not isinstance(item, dict):
            continue
        name = as_text(item.get("n") or item.get("file_name"))
        if not name:
            continue
        is_dir = as_int(item.get("fc")) == 0
        file_id = as_text(item.get("cid")) if is_dir else as_text(item.get("fid"))
        if not file_id:
            continue
        parent = as_text(item.get("pid")) if is_dir else as_text(item.get("cid"))
        result.append(
            ShareFile(
                file_id=file_id,
                name=name,
                size=0 if is_dir else as_int(item.get("s")),
                is_dir=is_dir,
                parent_id=parent or parent_id,
            )
        )
    return result


def _parse_personal_files(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """
    解析个人盘目录条目，保留 pickcode 等取链所需字段。

    :param payload: `files` 响应 JSON
    :return: 原始条目字典列表
    """
    result: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        if not as_text(item.get("n")):
            continue
        if not as_text(item.get("fid")) and not as_text(item.get("cid")):
            continue
        result.append(dict(item))
    return result


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


def _same_name(candidate: str, name: str) -> bool:
    """
    判断目录条目是否为「原名」或重名后的「原名(序号).后缀」。

    :param candidate: 目录条目名称
    :param name: 目标文件名
    :return: 是否匹配
    """
    if candidate == name:
        return True
    base, _, ext = name.rpartition(".")
    if not base:
        base, ext = name, ""
    if not candidate.startswith(f"{base}("):
        return False
    return candidate.endswith(")") if not ext else candidate.endswith(f".{ext}")


def _first_download_entry(decoded: Mapping[str, Any]) -> dict[str, Any]:
    """
    从电脑端取链响应中取出第一条带下载地址的条目。

    :param decoded: 解密后的响应 JSON
    :return: 条目字典；没有时返回空字典
    """
    for value in decoded.values():
        if isinstance(value, dict) and isinstance(value.get("url"), dict):
            return value
    return {}


def _derive_key(seed: bytes, size: int) -> bytes:
    """
    按 115 协议从种子派生 XOR 密钥。

    :param seed: 种子字节
    :param size: 派生长度
    :return: 派生密钥
    """
    return bytes(
        (
            ((seed[index % len(seed)] + _KEY_TABLE[size * index]) & 0xFF)
            ^ _KEY_TABLE[size * (size - 1 - index)]
        )
        for index in range(size)
    )


def _xor_transform(data: bytearray, key: bytes) -> None:
    """
    就地把数据与密钥做 XOR，前 `len % 4` 字节单独处理。

    :param data: 待变换数据
    :param key: XOR 密钥
    """
    offset = len(data) % 4
    for index in range(offset):
        data[index] ^= key[index % len(key)]
    for index in range(offset, len(data)):
        data[index] ^= key[(index - offset) % len(key)]


def _symmetric_encode(data: bytearray, key: bytes) -> None:
    """
    请求体的对称编码：XOR → 反转 → 长密钥 XOR。

    :param data: 待编码数据
    :param key: 16 字节会话密钥
    """
    _xor_transform(data, _derive_key(key, 4))
    data.reverse()
    _xor_transform(data, _LONG_KEY)


def _symmetric_decode(data: bytearray, key: bytes, salt: bytes) -> None:
    """
    响应体的对称解码：服务端 salt XOR → 反转 → 会话密钥 XOR。

    :param data: 待解码数据
    :param key: 16 字节会话密钥
    :param salt: 服务端下发的 16 字节 salt
    """
    _xor_transform(data, _derive_key(salt, 12))
    data.reverse()
    _xor_transform(data, _derive_key(key, 4))


def _rsa_encrypt(data: bytes) -> bytes:
    """
    按 PKCS#1 v1.5 填充后分块做公钥指数运算。

    :param data: 明文
    :return: 定长分块密文
    """
    out = bytearray()
    for offset in range(0, len(data), _RSA_ENCODE_BLOCK):
        chunk = data[offset : offset + _RSA_ENCODE_BLOCK]
        block = bytearray(b"\xff" * _RSA_BLOCK)
        block[0] = 0
        block[1] = 2
        separator = _RSA_BLOCK - len(chunk) - 1
        block[separator] = 0
        block[separator + 1 :] = chunk
        value = pow(int.from_bytes(block, "big"), _RSA_EXPONENT, _RSA_MODULUS)
        out += value.to_bytes(_RSA_BLOCK, "big")
    return bytes(out)


def _rsa_decrypt(data: bytes) -> bytes:
    """
    用同一公钥指数还原服务端「私钥加密」的分块数据。

    :param data: 密文
    :return: 去掉 PKCS#1 填充后的明文
    :raises NetdiskError: 密文缺少分隔符时抛出
    """
    out = bytearray()
    for offset in range(0, len(data), _RSA_BLOCK):
        block = data[offset : offset + _RSA_BLOCK]
        plain = pow(int.from_bytes(block, "big"), _RSA_EXPONENT, _RSA_MODULUS).to_bytes(
            _RSA_BLOCK, "big"
        )
        start = plain.find(b"\x00", 2)
        if start < 0 or start + 1 > _RSA_BLOCK:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "115 下载响应缺少 RSA 分隔符",
                platform=NetdiskPlatform.PAN115,
            )
        out += plain[start + 1 :]
    return bytes(out)


def _encode_payload(payload: str, key: bytes) -> str:
    """
    把电脑端取链请求体编码为 `data` 参数。

    :param payload: 明文 JSON
    :param key: 16 字节会话密钥
    :return: base64 编码的请求体
    """
    body = bytearray(payload.encode("utf-8"))
    _symmetric_encode(body, key)
    return base64.b64encode(_rsa_encrypt(key + bytes(body))).decode("ascii")


def _decode_payload(data: str, key: bytes) -> dict[str, Any]:
    """
    解码电脑端取链响应。

    :param data: 响应中的 base64 密文
    :param key: 请求时使用的 16 字节会话密钥
    :return: 解密后的 JSON 对象
    :raises NetdiskError: 响应长度或 JSON 结构异常时抛出
    """
    decrypted = _rsa_decrypt(base64.b64decode(data))
    if len(decrypted) <= 16:
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "115 下载响应长度异常",
            platform=NetdiskPlatform.PAN115,
        )
    body = bytearray(decrypted[16:])
    _symmetric_decode(body, key, decrypted[:16])
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "115 下载响应解析失败",
            platform=NetdiskPlatform.PAN115,
        ) from error
    if not isinstance(payload, dict):
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "115 下载响应结构异常",
            platform=NetdiskPlatform.PAN115,
        )
    return payload


class _Pan115Http(NetdiskHttp):
    """
    记录直链接口响应头下发的 CDN Cookie。

    基类只返回 JSON 对象，而 CDN 直链需要响应头里的 900 秒 Cookie，
    因此在解析阶段留存原始响应。
    """

    def __init__(self) -> None:
        """
        初始化 115 固定请求头与 CDN Cookie 缓存。
        """
        super().__init__(
            platform=NetdiskPlatform.PAN115,
            user_agent=CLIENT_USER_AGENT,
            referer=_REFERER,
        )
        self.cdn_cookie = ""

    def headers(
        self,
        account: Optional[NetdiskAccountRecord] = None,
        *,
        extra: Optional[Mapping[str, str]] = None,
        include_cookie: bool = True,
    ) -> dict[str, str]:
        """
        在共享请求头基础上补充 115 要求的 XHR 标识。

        :param account: 账号记录
        :param extra: 额外请求头
        :param include_cookie: 是否带上账号 Cookie
        :return: 请求头
        """
        merged = {"X-Requested-With": "XMLHttpRequest"}
        merged.update({str(key): str(value) for key, value in (extra or {}).items()})
        return super().headers(account, extra=merged, include_cookie=include_cookie)

    def _json(self, response: Any) -> dict[str, Any]:
        """
        解析响应前先记录 Set-Cookie。

        :param response: HTTP 响应
        :return: 响应 JSON
        """
        self.cdn_cookie = _download_cookie(response)
        return super()._json(response)


class Pan115Adapter(BaseNetdiskAdapter):
    """
    115 网盘适配器：网页端免转存取链优先，大文件转存后走客户端通道。
    """

    http: _Pan115Http
    platform: NetdiskPlatform = NetdiskPlatform.PAN115
    link_policy: LinkPolicy = LinkPolicy.DIRECT_WITH_FALLBACK
    requires_account: bool = True
    guest_supported: bool = True
    drive_capabilities: DriveCapabilities = DriveCapabilities(
        quota=True,
        browse=True,
        download=True,
        share=True,
        move=True,
        rename=True,
        delete=True,
    )
    max_concurrency: int = 512
    cookie_required_keys: tuple[str, ...] = ("UID", "SEID")
    cookie_domains: tuple[str, ...] = (".115.com", ".115cdn.com")
    login_url: str = "https://115.com/"
    supports_password_login: bool = False
    user_agent: str = CLIENT_USER_AGENT
    referer: str = _REFERER

    def __init__(self) -> None:
        """
        使用带响应头缓存的 HTTP 客户端。
        """
        super().__init__()
        self.http = _Pan115Http()

    # ---- 请求封装 ----

    def _get_json(
        self,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord],
        params: Optional[Mapping[str, Any]] = None,
        action: str,
    ) -> dict[str, Any]:
        """
        发起 GET 请求并校验业务状态。

        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param action: 用于错误提示的动作名
        :return: 响应 JSON
        """
        try:
            payload = self.http.get_json(url, account=account, params=params)
        except NetdiskError as error:
            raise _normalize_error(error) from error
        return _check_state(payload, action)

    def _post_form(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        account: Optional[NetdiskAccountRecord],
        action: str,
    ) -> dict[str, Any]:
        """
        发起表单 POST 请求并校验业务状态。

        :param url: 请求地址
        :param body: 表单字段
        :param account: 账号记录
        :param action: 用于错误提示的动作名
        :return: 响应 JSON
        """
        try:
            payload = self.http.post_json(url, account=account, data=body)
        except NetdiskError as error:
            raise _normalize_error(error) from error
        return _check_state(payload, action)

    # ---- 分享与目录 ----

    def _snap_page(
        self,
        share_id: str,
        receive_code: str,
        cid: str,
        account: Optional[NetdiskAccountRecord],
        *,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        读取一页分享目录。

        :param share_id: 分享码
        :param receive_code: 提取码
        :param cid: 目录标识，根目录为 "0"
        :param account: 取链账号
        :param offset: 索引分页偏移
        :param limit: 单页条数，None 表示使用默认页大小
        :return: 响应 JSON
        """
        payload = self._get_json(
            _SHARE_SNAP_URL,
            account=account,
            params={
                "share_code": share_id,
                "receive_code": receive_code,
                "offset": str(offset),
                "limit": str(limit or _SHARE_PAGE_LIMIT),
                "asc": "0",
                "cid": cid or _ROOT_CID,
                "format": "json",
            },
            action="读取分享目录",
        )
        return dict(payload)

    def _share_root(
        self,
        ref: ShareRef,
        account: Optional[NetdiskAccountRecord],
    ) -> tuple[str, str, str, Optional[dict[str, Any]]]:
        """
        取得分享会话：提取码、分享者 UID、标题与根目录首页。

        调用方已透传会话令牌时不再重复请求；否则先探一次分享根目录，
        既校验提取码，也拿到取直链必需的分享者 UID。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 提取码、分享者 UID、标题与根目录首页
        """
        if ref.share_token:
            receive_code, user_id = _decode_share_token(ref.share_token)
            return receive_code, user_id, "", None
        receive_code = ref.password or ""
        payload = self._snap_page(ref.share_id, receive_code, _ROOT_CID, account)
        data = payload.get("data") or {}
        user_id = as_text((data.get("userinfo") or {}).get("user_id"))
        title = as_text((data.get("shareinfo") or {}).get("share_title"))
        if not user_id:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "获取分享信息失败：响应缺少分享者标识",
                platform=self.platform,
            )
        return receive_code, user_id, title, data

    def _list_share(
        self,
        share_id: str,
        receive_code: str,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        按索引分页列出分享目录，直到空页或取满总数。

        :param share_id: 分享码
        :param receive_code: 提取码
        :param parent_id: 父目录标识
        :param account: 取链账号
        :return: 目录条目
        """
        cid = parent_id or _ROOT_CID
        files: list[ShareFile] = []
        seen: set[str] = set()
        offset = 0
        for _ in range(_SHARE_MAX_PAGES):
            payload = self._snap_page(share_id, receive_code, cid, account, offset=offset)
            data = payload.get("data") or {}
            page = _parse_share_files(data, cid)
            added = 0
            for item in page:
                if item.file_id in seen:
                    continue
                seen.add(item.file_id)
                files.append(item)
                added += 1
            total = as_int(data.get("count"))
            if not page or len(page) < _SHARE_PAGE_LIMIT or (total and len(files) >= total):
                break
            if not added:
                break
            offset += len(page)
        return files

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        获取分享详情与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情
        """
        receive_code, user_id, title, root = self._share_root(ref, account)
        files: list[ShareFile] = []
        if root is not None:
            files = _parse_share_files(root, _ROOT_CID)
            if as_int(root.get("count")) > len(files):
                files = self._list_share(ref.share_id, receive_code, _ROOT_CID, account)
        else:
            files = self._list_share(ref.share_id, receive_code, _ROOT_CID, account)
        size = sum(item.size for item in files if not item.is_dir)
        return ShareDetail(
            title=title,
            size=size,
            files=tuple(files),
            stoken=_encode_share_token(receive_code, user_id),
        )

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
        :return: 目录条目
        """
        receive_code, _, _, _ = self._share_root(ref, account)
        return self._list_share(ref.share_id, receive_code, parent_id, account)

    # ---- 取链 ----

    def _share_link(
        self,
        ref: ShareRef,
        file: ShareFile,
        receive_code: str,
        user_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> dict[str, Any]:
        """
        网页端免转存取链：`share/downurl`。

        :param ref: 分享引用
        :param file: 目标文件
        :param receive_code: 提取码
        :param user_id: 分享者 UID
        :param account: 取链账号
        :return: 含 url/name/size/cdn_cookie 的字典
        """
        if not user_id:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "获取分享直链失败：缺少分享者标识，请重新解析分享链接",
                platform=self.platform,
            )
        params: dict[str, Any] = {
            "dl": "1",
            "user_id": user_id,
            "share_code": ref.share_id,
            "file_id": file.file_id,
        }
        if receive_code:
            params["receive_code"] = receive_code
        payload = self._get_json(
            _SHARE_DOWNLOAD_URL,
            account=account,
            params=params,
            action="获取分享直链",
        )
        data = payload.get("data") or {}
        url = (
            as_text(data.get("file_url_302"))
            or as_text(data.get("file_url"))
            or as_text((data.get("url") or {}).get("url"))
        )
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "获取分享直链失败：下载链接为空",
                platform=self.platform,
            )
        return {
            "url": url,
            "name": as_text(data.get("fn")) or file.name,
            "size": as_int(data.get("fs")) or file.size,
            "cdn_cookie": self.http.cdn_cookie,
        }

    def _download_headers(
        self,
        account: Optional[NetdiskAccountRecord],
        cdn_cookie: str,
    ) -> dict[str, str]:
        """
        组装下载请求头：登录 Cookie + CDN Cookie 双拼，并固定客户端 UA。

        :param account: 取链账号
        :param cdn_cookie: 取链响应头下发的 CDN Cookie
        :return: 请求头
        """
        headers = self.http.headers(account, include_cookie=False)
        cookie = _merge_cookies(self._login_cookie(account), cdn_cookie)
        if cookie:
            headers["Cookie"] = cookie
        headers["User-Agent"] = CLIENT_USER_AGENT
        headers["Referer"] = _REFERER
        return headers

    @staticmethod
    def _login_cookie(account: Optional[NetdiskAccountRecord]) -> str:
        """
        取出只含会话字段的登录 Cookie。

        :param account: 取链账号
        :return: Cookie 头
        """
        return _filter_login_cookies(cookie_header(account))

    def _build_link(
        self,
        link: Mapping[str, Any],
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        handle: Optional[TransferHandle],
    ) -> DirectLink:
        """
        组装直链及其下载请求头。

        :param link: 取链接口返回的直链信息
        :param file: 目标文件
        :param account: 取链账号
        :param handle: 转存清理句柄，免转存时为 None
        :return: 直链
        """
        return DirectLink(
            url=as_text(link.get("url")),
            headers=self._download_headers(account, as_text(link.get("cdn_cookie"))),
            size=as_int(link.get("size")) or file.size,
            expires_at=None,
            cleanup_handle=handle,
            account_id=account.id if account else 0,
            is_hls=False,
        )

    # ---- 转存与清理 ----

    def _ensure_temp_dir(self, account: NetdiskAccountRecord) -> str:
        """
        在个人网盘根目录创建唯一临时目录。

        :param account: 取链账号
        :return: 新目录 cid
        """
        name = f"{_TEMP_DIR_PREFIX}{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        payload = self._post_form(
            _FILE_ADD_URL,
            {"pid": _ROOT_CID, "cname": name},
            account=account,
            action="创建临时目录",
        )
        data = payload.get("data") or {}
        cid = as_text(data.get("cid")) or as_text(payload.get("cid"))
        if not cid:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建临时目录失败：响应缺少 cid",
                platform=self.platform,
            )
        return cid

    def _receive(
        self,
        ref: ShareRef,
        receive_code: str,
        file: ShareFile,
        to_cid: str,
        account: NetdiskAccountRecord,
    ) -> None:
        """
        把分享文件转存到个人网盘目录。

        :param ref: 分享引用
        :param receive_code: 提取码
        :param file: 目标文件
        :param to_cid: 目标目录 cid
        :param account: 取链账号
        """
        self._post_form(
            _SHARE_RECEIVE_URL,
            {
                "share_code": ref.share_id,
                "receive_code": receive_code,
                "file_id": file.file_id,
                "cid": to_cid,
            },
            account=account,
            action="转存分享文件",
        )

    def _wait_saved(
        self,
        cid: str,
        name: str,
        account: NetdiskAccountRecord,
    ) -> Optional[dict[str, Any]]:
        """
        轮询临时目录，按文件名认领转存后的文件。

        115 的转存接口不返回新文件 id，重名时服务端会命名为 `原名(1).后缀`。

        :param cid: 临时目录 cid
        :param name: 目标文件名
        :param account: 取链账号
        :return: 命中的目录条目；超时返回 None
        """
        params = {
            "aid": _AID_PERSONAL,
            "cid": cid,
            "o": "user_ptime",
            "asc": "0",
            "offset": "0",
            "show_dir": "1",
            "limit": str(_FILE_PAGE_LIMIT),
            "format": "json",
        }
        for attempt in range(_TRANSFER_POLL_TIMES):
            if attempt:
                self.sleep(_TRANSFER_POLL_INTERVAL)
            try:
                payload = self._get_json(
                    _FILES_URL,
                    account=account,
                    params=params,
                    action="读取转存目录",
                )
            except NetdiskError:
                continue
            for item in _parse_personal_files(payload):
                if _same_name(as_text(item.get("n")), name):
                    return item
        return None

    def _app_link(self, pickcode: str, account: NetdiskAccountRecord) -> dict[str, Any]:
        """
        电脑端加密通道取链：`proapi.115.com/app/chrome/downurl`。

        :param pickcode: 文件 pickcode
        :param account: 取链账号
        :return: 含 url/name/size/cdn_cookie 的字典
        """
        key = uuid.uuid4().bytes
        payload = json.dumps({"pickcode": pickcode}, separators=(",", ":"))
        response = self._post_form(
            f"{_APP_DOWNLOAD_URL}?t={int(time.time())}",
            {"data": _encode_payload(payload, key)},
            account=account,
            action="获取客户端直链",
        )
        encrypted = as_text(response.get("data"))
        if not encrypted:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "获取客户端直链失败：响应缺少密文",
                platform=self.platform,
            )
        info = _first_download_entry(_decode_payload(encrypted, key))
        url = as_text((info.get("url") or {}).get("url"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "获取客户端直链失败：下载链接为空",
                platform=self.platform,
            )
        return {
            "url": url,
            "name": as_text(info.get("file_name")),
            "size": as_int(info.get("file_size")),
            "cdn_cookie": self.http.cdn_cookie,
        }

    def _web_file_link(self, pickcode: str, account: NetdiskAccountRecord) -> dict[str, Any]:
        """
        网页端个人盘取链：`files/download?pickcode=`，仅小文件可用。

        :param pickcode: 文件 pickcode
        :param account: 取链账号
        :return: 含 url/name/size/cdn_cookie 的字典
        """
        payload = self._get_json(
            _FILE_DOWNLOAD_URL,
            account=account,
            params={"pickcode": pickcode},
            action="获取文件直链",
        )
        data = payload.get("data") or {}
        url = as_text(data.get("file_url"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "获取文件直链失败：下载链接为空",
                platform=self.platform,
            )
        return {
            "url": url,
            "name": as_text(data.get("file_name")),
            "size": as_int(data.get("file_size")),
            "cdn_cookie": self.http.cdn_cookie,
        }

    def _personal_link(self, pickcode: str, account: NetdiskAccountRecord) -> dict[str, Any]:
        """
        个人盘取链：客户端通道优先，失败后退回网页端接口。

        :param pickcode: 文件 pickcode
        :param account: 取链账号
        :return: 含 url/name/size/cdn_cookie 的字典
        """
        try:
            return self._app_link(pickcode, account)
        except NetdiskError as error:
            self.log_failure("客户端取链", error, account)
        return self._web_file_link(pickcode, account)

    def _transfer_link(
        self,
        ref: ShareRef,
        receive_code: str,
        file: ShareFile,
        account: NetdiskAccountRecord,
    ) -> tuple[TransferHandle, dict[str, Any]]:
        """
        转存到唯一临时目录后再取链，失败时清理刚建的目录。

        :param ref: 分享引用
        :param receive_code: 提取码
        :param file: 目标文件
        :param account: 取链账号
        :return: 清理句柄与直链信息
        """
        cid = self._ensure_temp_dir(account)
        try:
            self._receive(ref, receive_code, file, cid, account)
            saved = self._wait_saved(cid, file.name, account)
            if saved is None:
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "转存完成但未在临时目录找到文件，请稍后重试",
                    platform=self.platform,
                )
            fid = as_text(saved.get("fid"))
            pickcode = as_text(saved.get("pc"))
            if not pickcode:
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "转存文件缺少 pickcode，无法取链",
                    platform=self.platform,
                )
            link = self._personal_link(pickcode, account)
        except Exception:
            self._safe_delete([cid], account)
            raise
        link["name"] = file.name or link.get("name") or ""
        link["size"] = as_int(link.get("size")) or as_int(saved.get("s")) or file.size
        handle = TransferHandle(
            platform=self.platform,
            account_id=account.id,
            directory_id=cid,
            file_ids=(fid,),
        )
        return handle, link

    def resolve(
        self,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        allow_transfer: bool = True,
    ) -> DirectLink:
        """
        获取单个文件的下载直链。

        优先走网页端免转存；仅当网页端以「文件过大」拒绝下发直链且允许转存时，
        才转存到唯一临时目录并改走客户端通道。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底
        :return: 直链
        """
        receive_code, user_id, _, _ = self._share_root(ref, account)
        try:
            link = self._share_link(ref, file, receive_code, user_id, account)
        except _WebLinkDenied as error:
            if not allow_transfer or account is None:
                raise
            self.log_failure("免转存取链", error, account)
            handle, saved_link = self._transfer_link(ref, receive_code, file, account)
            return self._build_link(saved_link, file, account, handle)
        return self._build_link(link, file, account, None)

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除转存产生的临时目录；失败只记录，不抛出。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        if not handle.directory_id:
            return
        self._safe_delete([handle.directory_id], account)

    def _delete(self, fids: list[str], account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除个人网盘文件或目录。

        :param fids: 待删除的标识列表
        :param account: 取链账号
        """
        if not fids:
            return
        body: dict[str, Any] = {"pid": _ROOT_CID, "ignore_warn": "1"}
        for index, fid in enumerate(fids):
            body[f"fid[{index}]"] = fid
        self._post_form(_FILE_DELETE_URL, body, account=account, action="清理临时目录")

    def _safe_delete(self, fids: list[str], account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除失败只记录，不影响主流程。

        :param fids: 待删除的标识列表
        :param account: 取链账号
        """
        try:
            self._delete(fids, account)
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载
            self.log_failure("清理临时目录", error, account)

    # ---- 账号 ----

    def _probe(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        调用 `/user/info` 确认登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        try:
            payload = self._get_json(_USER_INFO_URL, account=account, action="校验115登录态")
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="115 登录态已失效")
            return AccountProbe(status="unknown", message="无法确认 115 登录态")
        nickname = as_text(payload.get("data"))
        message = f"115 登录态有效：{nickname}" if nickname else "115 登录态有效"
        return AccountProbe(status="active", message=message)

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验一份新凭据是否可用。

        保存前只保留 `UID/CID/SEID/KID` 等会话字段，再调用 `/user/info` 确认。

        :param credential: 明文凭据字段
        :return: 探测结果
        """
        cookie = _filter_login_cookies(_credential_cookie(credential))
        if not cookie:
            return AccountProbe(status="expired", message="凭据缺少必填字段：UID、SEID")
        probe = super().validate_credential({"cookie": cookie})
        if probe.status != "active":
            return probe
        record = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="",
            credential={"cookie": cookie},
        )
        return self._probe(record)

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从 115 账号信息接口读取昵称。

        :param credential: 浏览器抓到的凭据
        :return: 昵称；接口不可用时为空串
        """
        cookie = _filter_login_cookies(_credential_cookie(credential))
        if not cookie:
            return ""
        record = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="",
            credential={"cookie": cookie},
        )
        try:
            payload = self._get_json(_USER_INFO_URL, account=record, action="读取115账号信息")
        except NetdiskError:
            return ""
        return as_text(payload.get("data"))

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self._probe(account)

    # ---- 个人盘 ----

    @staticmethod
    def _user_id(account: NetdiskAccountRecord) -> str:
        """
        从登录 Cookie 的 UID 中取出纯数字用户标识。

        :param account: 账号
        :return: 用户标识；缺失时为空串
        """
        for part in cookie_header(account).split(";"):
            item = part.strip()
            if item.startswith("UID="):
                return item[4:].split("_", 1)[0]
        return ""

    def _personal_page(
        self,
        account: NetdiskAccountRecord,
        parent_id: str,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        读取一页个人盘目录原始条目。

        :param account: 账号
        :param parent_id: 父目录标识，空串为根
        :param offset: 索引分页偏移
        :return: 原始条目与总数
        """
        payload = self._get_json(
            _FILES_URL,
            account=account,
            params={
                "aid": _AID_PERSONAL,
                "cid": parent_id or _ROOT_CID,
                "o": "user_ptime",
                "asc": "0",
                "offset": str(offset),
                "show_dir": "1",
                "limit": str(_FILE_PAGE_LIMIT),
                "snap": "0",
                "natsort": "1",
                "record_open_time": "1",
                "count_folders": "1",
                "format": "json",
            },
            action="读取个人盘目录",
        )
        return _parse_personal_files(payload), as_int(payload.get("count"))

    def _find_item(self, account: NetdiskAccountRecord, file: DriveFile) -> dict[str, Any]:
        """
        在文件所在目录中定位原始条目，取链需要其中的 pickcode。

        :param account: 账号
        :param file: 文件条目
        :return: 原始条目
        :raises NetdiskError: 目录中找不到该文件时抛出
        """
        offset = 0
        for _ in range(_SHARE_MAX_PAGES):
            items, total = self._personal_page(account, file.parent_id, offset)
            for item in items:
                if (as_text(item.get("fid")) or as_text(item.get("cid"))) == file.file_id:
                    return item
            if not items or len(items) < _FILE_PAGE_LIMIT or (total and offset + len(items) >= total):
                break
            offset += len(items)
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "115 未找到目标文件",
            platform=self.platform,
        )

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询 115 个人盘空间。

        :param account: 账号
        :return: 已用/总空间
        """
        payload = self._get_json(_INDEX_INFO_URL, account=account, action="获取 115 空间信息失败")
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        space = data.get("space_info")
        if not isinstance(space, Mapping):
            return DriveQuota()
        total_raw = space.get("all_total")
        used_raw = space.get("all_use")
        return DriveQuota(
            used=as_int(used_raw.get("size")) if isinstance(used_raw, Mapping) else 0,
            total=as_int(total_raw.get("size")) if isinstance(total_raw, Mapping) else 0,
        )

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        列出 115 个人盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串为根
        :return: 目录条目
        """
        files: list[DriveFile] = []
        offset = 0
        for _ in range(_SHARE_MAX_PAGES):
            items, total = self._personal_page(account, parent_id, offset)
            for item in items:
                is_dir = not as_text(item.get("fid"))
                file_id = as_text(item.get("cid")) if is_dir else as_text(item.get("fid"))
                if not file_id:
                    continue
                parent = as_text(item.get("pid")) if is_dir else as_text(item.get("cid"))
                files.append(
                    DriveFile(
                        file_id=file_id,
                        name=as_text(item.get("n")),
                        size=0 if is_dir else as_int(item.get("s")),
                        is_dir=is_dir,
                        updated_at=_parse_time(item.get("t")),
                        parent_id=parent or parent_id,
                    )
                )
            if not items or len(items) < _FILE_PAGE_LIMIT or (total and offset + len(items) >= total):
                break
            offset += len(items)
        return files

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取 115 个人盘文件直链，客户端加密通道优先、网页端兜底。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        item = self._find_item(account, file)
        pickcode = as_text(item.get("pc"))
        if not pickcode:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "115 文件缺少 pickcode，无法取链",
                platform=self.platform,
            )
        link = self._personal_link(pickcode, account)
        share_file = ShareFile(
            file_id=file.file_id,
            name=file.name,
            size=file.size,
            is_dir=file.is_dir,
            parent_id=file.parent_id,
        )
        return self._build_link(link, share_file, account, None)

    @staticmethod
    def _share_duration(expires_in: int) -> str:
        """
        把有效期秒数换算为 115 的天数档位。

        :param expires_in: 有效期秒数，0 表示长期
        :return: `share_duration` 表单值
        """
        if expires_in <= 0:
            return "-1"
        return str(max(1, (expires_in + 86399) // 86400))

    def share_file(
        self,
        account: NetdiskAccountRecord,
        files: list[DriveFile],
        *,
        password: str = "",
        expires_in: int = 0,
    ) -> DriveShare:
        """
        创建 115 个人盘分享，并在创建后设置有效期。

        :param account: 账号
        :param files: 文件列表
        :param password: 期望提取码，平台自动生成时以平台为准
        :param expires_in: 有效期秒数，0 表示长期
        :return: 分享结果
        """
        if not files:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "请选择要分享的文件", platform=self.platform)
        user_id = self._user_id(account)
        if not user_id:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "115 登录态缺少用户标识，请重新登录",
                platform=self.platform,
            )
        file_ids = [item.file_id for item in files if item.file_id]
        if not file_ids:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "请选择要分享的文件", platform=self.platform)
        try:
            self._post_form(
                _ALLOW_PROTOCOL_URL,
                {"action": "pubshare"},
                account=account,
                action="确认 115 分享协议",
            )
        except NetdiskError as error:
            self.log_failure("确认分享协议", error, account)
        payload = self._post_form(
            _SHARE_SEND_URL,
            {
                "user_id": user_id,
                "file_ids": ",".join(file_ids),
                "ignore_warn": "1",
            },
            account=account,
            action="创建 115 分享失败",
        )
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

        def field(name: str) -> str:
            """优先读取 data 内层字段，兼容服务端放在顶层的旧格式。"""
            return as_text(data.get(name)) or as_text(payload.get(name))

        share_code = field("share_code")
        if not share_code:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建 115 分享失败：响应缺少分享编号",
                platform=self.platform,
            )
        try:
            self._post_form(
                _SHARE_UPDATE_URL,
                {
                    "share_code": share_code,
                    "share_duration": self._share_duration(expires_in),
                },
                account=account,
                action="设置 115 分享有效期",
            )
        except NetdiskError as error:
            self.log_failure("设置分享有效期", error, account)
        return DriveShare(
            url=field("share_url") or f"https://115cdn.com/s/{share_code}",
            password=field("receive_code") or field("sys_receive_code") or password,
        )

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动 115 个人盘文件，响应含 move_proid 时轮询异步进度。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录标识
        """
        file_ids = [item.file_id for item in files if item.file_id]
        if not file_ids:
            return
        body: dict[str, Any] = {"pid": target_id or _ROOT_CID}
        for index, file_id in enumerate(file_ids):
            body[f"fid[{index}]"] = file_id
        payload = self._post_form(_FILE_MOVE_URL, body, account=account, action="移动 115 文件失败")
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        move_id = as_text(payload.get("move_proid")) or as_text(data.get("move_proid"))
        if not move_id:
            return
        for _ in range(_MOVE_POLL_MAX):
            self.sleep(_MOVE_POLL_INTERVAL)
            try:
                progress = self._get_json(
                    _MOVE_PROGRESS_URL,
                    account=account,
                    params={"move_proid": move_id},
                    action="查询 115 移动进度",
                )
            except NetdiskError:
                return
            if not progress.get("state"):
                return
            progress_data = progress.get("data")
            if not isinstance(progress_data, Mapping):
                return
            percent = float(progress_data.get("percent") or 0)
            if percent >= 100 or as_int(progress_data.get("count")) <= 0:
                return

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名 115 个人盘文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        self._post_form(
            _FILE_RENAME_URL,
            {
                "fid": file.file_id,
                "file_name": name,
                f"files_new_name[{file.file_id}]": name,
            },
            account=account,
            action="重命名 115 文件失败",
        )

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除 115 个人盘文件（移入回收站），需要二次确认时重试一次。

        :param account: 账号
        :param files: 文件列表
        """
        file_ids = [item.file_id for item in files if item.file_id]
        if not file_ids:
            return
        body: dict[str, Any] = {"pid": _ROOT_CID, "ignore_warn": "1"}
        for index, file_id in enumerate(file_ids):
            body[f"fid[{index}]"] = file_id
        try:
            self._post_form(_FILE_DELETE_URL, body, account=account, action="删除 115 文件失败")
        except NetdiskError as error:
            if str(_ERRNO_NEED_CONFIRM) not in error.message:
                raise
            self._post_form(_FILE_DELETE_URL, body, account=account, action="删除 115 文件失败")
