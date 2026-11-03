"""
迅雷云盘的分享解析、目录读取与转存取链适配器。

协议依据 YunX `XunleiResolveRepository` / `XunleiApi` / `XunleiWebCredential`：
分享列表 `GET /drive/v1/share` 与子目录 `GET /drive/v1/share/detail` 允许匿名读取，
转存 `POST /drive/v1/share/restore` 与文件详情取链必须使用 `access_token`。

迅雷凭据来自网页版 localStorage：`access_token`、`deviceid`、`captcha` 三者必须
属于同一个账号。分享必须转存后才能取链，下载完成后删除临时目录。
"""

from __future__ import annotations

import json
import time
import uuid
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

# 云盘接口主机。
_PAN_BASE = "https://api-pan.xunlei.com"
_FILES_URL = f"{_PAN_BASE}/drive/v1/files"
_BATCH_DELETE_URL = f"{_FILES_URL}:batchDelete"
_SHARE_URL = f"{_PAN_BASE}/drive/v1/share"
_SHARE_DETAIL_URL = f"{_PAN_BASE}/drive/v1/share/detail"
_RESTORE_URL = f"{_PAN_BASE}/drive/v1/share/restore"
_ABOUT_URL = f"{_PAN_BASE}/drive/v1/about"
_MOVE_URL = f"{_FILES_URL}:batchMove"
_TRASH_URL = f"{_FILES_URL}:batchTrash"

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

    @staticmethod
    def _identity(account: Optional[NetdiskAccountRecord]) -> tuple[str, str, str]:
        """
        读取账号的访问令牌、设备标识与验证码令牌。

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
        access, device, captcha = _identity_from(_fields(account.credential or {}))
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
        return access, device, captcha

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
        folder_id = as_text(self._data(payload).get("id"))
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
        new_id = as_text(mapping.get(file.file_id)) or as_text(data.get("file_id"))
        if not new_id:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "迅雷云盘转存完成但未返回新文件标识",
                platform=self.platform,
            )
        return new_id

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
        :raises NetdiskError: 接口失败或未返回直链时抛出
        """
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
        links = data.get("links")
        stream = links.get("application/octet-stream") if isinstance(links, Mapping) else None
        url = as_text(stream.get("url")) if isinstance(stream, Mapping) else ""
        if not url:
            url = as_text(data.get("web_content_link"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "迅雷云盘未返回可用下载链接",
                platform=self.platform,
            )
        return url, as_int(data.get("size"))

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

    def resolve(
        self,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        allow_transfer: bool = True,
    ) -> DirectLink:
        """
        转存分享文件并换取下载直链。

        迅雷分享无法免转存取链，`allow_transfer` 为假时直接返回 `UNSUPPORTED`。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存
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
        try:
            new_id = self._restore(ref, file, directory, account)
            url, size = self._file_link(new_id, account)
        except Exception:
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
        access, device, captcha = _identity_from(fields)
        headers = self._about_headers(access, device, captcha)
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
        缺失时再调用 `/drive/v1/about` 尝试读取。

        :param credential: 浏览器抓到的凭据
        :return: 昵称；无法获取时为空串
        """
        fields = _fields(credential)
        nickname = _pick(fields, "nickname", "nick_name", "nickName", "user_name", "userName", "name")
        if nickname:
            return nickname
        access, device, captcha = _identity_from(fields)
        if not access or not device:
            return ""
        try:
            payload = self.http.get_json(_ABOUT_URL, headers=self._about_headers(access, device, captcha))
        except NetdiskError:
            return ""
        data = payload.get("data")
        if not isinstance(data, Mapping):
            data = payload
        return _pick(
            {str(key): as_text(value) for key, value in data.items()},
            "nickname",
            "nick_name",
            "nickName",
            "user_name",
            "userName",
            "name",
        )

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {})
