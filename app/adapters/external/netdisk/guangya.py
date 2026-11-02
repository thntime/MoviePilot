"""
光鸭云盘协议适配器。

只负责「分享 → 直链」这一段：分享访问令牌、分享目录、免登录取链、受限分享转存兜底、
临时目录清理、账号探测与账号密码登录。光鸭分两套域名：业务接口在 `api.guangyapan.com`，
账号接口在 `account.guangyapan.com`，两者的公共请求头不同，分别由 `_business_headers`
与 `_account_headers` 装配。

分享者未开启「免登录下载」时业务码为 207，此时登录态下转存到唯一临时目录，
再从个人盘取链并返回清理句柄；未登录只能给出可展示的失败原因。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Mapping, Optional
from urllib.parse import quote

from app.adapters.external.netdisk.base import (
    BaseNetdiskAdapter,
    as_int,
    as_text,
    credential_value,
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
    TransferHandle,
)

_API_BASE = "https://api.guangyapan.com"
_ACCOUNT_BASE = "https://account.guangyapan.com"
_WEB_SITE = "https://www.guangyapan.com"

_SIGNIN_URL = f"{_ACCOUNT_BASE}/v1/auth/signin"
_USER_ME_URL = f"{_ACCOUNT_BASE}/v1/user/me"
_CAPTCHA_INIT_URL = f"{_ACCOUNT_BASE}/v1/shield/captcha/init"
_VERIFICATION_URL = f"{_ACCOUNT_BASE}/v1/auth/verification"
_VERIFICATION_VERIFY_URL = f"{_ACCOUNT_BASE}/v1/auth/verification/verify"
_SHARE_SUMMARY_URL = f"{_API_BASE}/userres/v1/get_share_summary"
_SHARE_TOKEN_URL = f"{_API_BASE}/userres/v1/get_share_access_token"
_SHARE_FILES_URL = f"{_API_BASE}/userres/v1/get_share_page_files_list"
_SHARE_DOWNLOAD_URL = f"{_API_BASE}/userres/v1/get_share_download_url"
_RESTORE_SHARE_URL = f"{_API_BASE}/userres/v1/restore_share"
_FILE_LIST_URL = f"{_API_BASE}/userres/v1/file/get_file_list"
_CREATE_DIR_URL = f"{_API_BASE}/userres/v1/file/create_dir"
_DELETE_FILE_URL = f"{_API_BASE}/userres/v1/file/delete_file"
_RES_DOWNLOAD_URL = f"{_API_BASE}/userres/v1/get_res_download_url"
_TASK_STATUS_URL = f"{_API_BASE}/userres/v1/get_task_status"

_CLIENT_ID = "aMe-8VSlkrbQXpUR"
_CLIENT_VERSION = "0.0.1"
_SDK_VERSION = "9.1.3"
_PROTOCOL_VERSION = "301"
_DEVICE_MODEL = "chrome%2F131.0.0.0"
_DEVICE_NAME = "PC-Chrome"
_OS_VERSION = "Win32"
_PLATFORM_VERSION = "1"

GUANGYA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_REFERER = f"{_WEB_SITE}/"

_ROOT_ID = ""
_PAGE_SIZE = 100
_MAX_PAGES = 50
_TASK_POLL_TIMES = 20
_TASK_POLL_INTERVAL = 0.75

# 转存临时目录前缀；名称带毫秒时间戳与随机串，避免平台按目标目录去重。
_TEMP_DIR_PREFIX = "mp_tr_"
_TRANSFER_POLL_TIMES = 20
_TRANSFER_POLL_INTERVAL = 0.75

# 分享者未开启免登录下载时的业务码。
_CODE_SHARE_RESTRICTED = "207"


class _ShareRestrictedError(NetdiskError):
    """
    分享者未开启免登录下载，登录态下可转存后取链。
    """

    def __init__(self) -> None:
        """
        固定为可回退的未支持分类。
        """
        super().__init__(
            NetdiskErrorCode.UNSUPPORTED,
            "该光鸭分享需登录并转存后下载",
            platform=NetdiskPlatform.GUANGYA,
        )


def _first_non_blank(*values: Any) -> str:
    """
    返回第一个非空文本。

    :param values: 候选字段
    :return: 文本；全部为空时返回空串
    """
    for value in values:
        text = as_text(value)
        if text:
            return text
    return ""


def _normalize_account(raw: str) -> str:
    """
    把账号规范化为接口要求的形态：11 位手机号补 `+86 `，其余原样。

    :param raw: 用户输入
    :return: 规范化账号
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(char for char in text if char.isdigit())
    if text.startswith("+86"):
        return f"+86 {digits.removeprefix('86')}"
    if len(text) == 11 and text.isdigit():
        return f"+86 {text}"
    return text


def _is_success(payload: Mapping[str, Any]) -> bool:
    """
    判断业务响应是否成功。

    :param payload: 响应 JSON
    :return: 是否成功
    """
    if payload.get("success") is False:
        return False
    if as_text(payload.get("error")):
        return False
    code = as_text(payload.get("code"))
    return code in ("", "0")


def _classify(code: str, message: str) -> NetdiskErrorCode:
    """
    把光鸭业务码与提示收敛为统一错误分类。

    :param code: 业务码或错误标识
    :param message: 服务端提示
    :return: 统一错误分类
    """
    lowered = code.lower()
    if code == "209" or "提取码" in message:
        return NetdiskErrorCode.SHARE_INVALID
    if code in {"200", "201", "202"} or "过期" in message or "不存在" in message:
        return NetdiskErrorCode.SHARE_INVALID
    if code in {"205", "206", "504"}:
        return NetdiskErrorCode.UNSUPPORTED
    if code == "157" or "空间不足" in message:
        return NetdiskErrorCode.SPACE_INSUFFICIENT
    if code in {"401", "unauthenticated", "invalid_token"}:
        return NetdiskErrorCode.AUTH_EXPIRED
    if code in {"429", "too_many_requests"} or "频繁" in message:
        return NetdiskErrorCode.RATE_LIMITED
    if "captcha" in lowered or "verification" in lowered or "验证码" in message or "风控" in message:
        return NetdiskErrorCode.RISK_BLOCKED
    return NetdiskErrorCode.UNKNOWN


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
            "光鸭直链已失效或被防盗链拦截",
            platform=NetdiskPlatform.GUANGYA,
        )
    return error


def _list_array(data: Mapping[str, Any]) -> list[Any]:
    """
    取出列表字段：光鸭不同接口用 list/items/files/records/content 等命名。

    :param data: 响应 data 节点
    :return: 列表字段；没有时返回空列表
    """
    for key in ("list", "items", "files", "records", "content"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_url(data: Mapping[str, Any]) -> str:
    """
    从下载地址响应中取出直链。

    :param data: 响应 data 节点
    :return: 直链；没有时返回空串
    """
    return _first_non_blank(
        data.get("signedURL"),
        data.get("signedUrl"),
        data.get("downloadUrl"),
        data.get("download_url"),
        data.get("url"),
        data.get("cdnUrl"),
    )


def _parse_file(item: Any) -> Optional[ShareFile]:
    """
    解析光鸭目录条目。

    :param item: 原始条目
    :return: 目录条目；字段不足时返回 None
    """
    if not isinstance(item, dict):
        return None
    file_id = _first_non_blank(
        item.get("fileId"),
        item.get("file_id"),
        item.get("id"),
        item.get("resId"),
        item.get("resourceId"),
    )
    name = _first_non_blank(
        item.get("fileName"),
        item.get("name"),
        item.get("filename"),
        item.get("dirName"),
        item.get("title"),
    )
    if not file_id or not name:
        return None
    res_type = as_text(item.get("resType"))
    is_dir = res_type == "2" or (
        res_type != "1"
        and any(
            item.get(flag) is True
            for flag in ("isDir", "isdir", "dir", "isFolder", "folder")
        )
    )
    parent_id = _first_non_blank(
        item.get("parentId"),
        item.get("parentFileId"),
        item.get("parent_id"),
    )
    return ShareFile(
        file_id=file_id,
        name=name,
        size=0 if is_dir else as_int(item.get("fileSize") or item.get("size")),
        is_dir=is_dir,
        parent_id=parent_id,
    )


def _traceparent() -> str:
    """
    生成一次请求用的 W3C traceparent。

    :return: traceparent 头
    """
    return f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


class GuangYaAdapter(BaseNetdiskAdapter):
    """
    光鸭云盘适配器：免登录取链优先，受限分享在登录态下转存后取链。
    """

    platform: NetdiskPlatform = NetdiskPlatform.GUANGYA
    link_policy: LinkPolicy = LinkPolicy.DIRECT_WITH_FALLBACK
    requires_account: bool = True
    guest_supported: bool = True
    max_concurrency: int = 512
    cookie_required_keys: tuple[str, ...] = ("token",)
    cookie_domains: tuple[str, ...] = (".guangyapan.com",)
    login_url: str = "https://www.guangyapan.com/"
    supports_password_login: bool = True
    user_agent: str = GUANGYA_USER_AGENT
    referer: str = _REFERER

    def __init__(self) -> None:
        """
        初始化共享 HTTP 客户端与稳定的匿名设备标识。
        """
        super().__init__()
        self._device_id = uuid.uuid4().hex
        self._device_sign = f"wdi10.{self._device_id}{uuid.uuid4().hex}"

    # ---- 请求封装 ----

    def _business_headers(self) -> dict[str, str]:
        """
        业务接口公共请求头。

        :return: 请求头
        """
        return {
            "Origin": _WEB_SITE,
            "Referer": _REFERER,
            "dt": "4",
            "did": self._device_id,
            "traceparent": _traceparent(),
        }

    def _account_headers(self) -> dict[str, str]:
        """
        账号接口公共请求头。

        :return: 请求头
        """
        return {
            "X-Client-Id": _CLIENT_ID,
            "X-Client-Version": _CLIENT_VERSION,
            "X-Device-Id": quote(self._device_id, safe=""),
            "X-Device-Model": _DEVICE_MODEL,
            "X-Device-Name": _DEVICE_NAME,
            "X-Device-Sign": quote(self._device_sign, safe=""),
            "X-Os-Version": _OS_VERSION,
            "X-Platform-Version": _PLATFORM_VERSION,
            "X-Protocol-Version": _PROTOCOL_VERSION,
            "X-Sdk-Version": _SDK_VERSION,
        }

    def _post_business(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        token: str = "",
        action: str,
    ) -> dict[str, Any]:
        """
        发起业务 POST 请求并校验业务状态。

        :param url: 请求地址
        :param body: JSON 请求体
        :param token: 账号访问令牌
        :param action: 用于错误提示的动作名
        :return: 响应 JSON
        """
        headers = self._business_headers()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            payload = self.http.post_json(url, json=body, headers=headers)
        except NetdiskError as error:
            raise _normalize_error(error) from error
        return _check_business(payload, action)

    # ---- 分享与目录 ----

    def _share_token(self, ref: ShareRef) -> str:
        """
        取得分享访问令牌；调用方已透传时直接复用。

        :param ref: 分享引用
        :return: 分享访问令牌
        """
        if ref.share_token:
            return ref.share_token
        payload = self._post_business(
            _SHARE_TOKEN_URL,
            {"shareId": ref.share_id, "code": ref.password or ""},
            action="获取分享访问令牌",
        )
        data = payload.get("data") or {}
        token = _first_non_blank(data.get("accessToken"), data.get("access_token"))
        if not token:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "获取分享访问令牌失败：响应缺少 accessToken",
                platform=self.platform,
            )
        return token

    def _share_title(self, share_id: str) -> str:
        """
        读取分享标题；失败时返回空串，不影响取链。

        :param share_id: 分享码
        :return: 分享标题
        """
        try:
            payload = self._post_business(
                _SHARE_SUMMARY_URL,
                {"shareId": share_id},
                action="获取分享信息",
            )
        except NetdiskError:
            return ""
        data = payload.get("data") or {}
        return _first_non_blank(data.get("title"), data.get("shareName"))

    def _list_share(self, token: str, parent_id: str) -> list[ShareFile]:
        """
        按游标翻页列出分享目录。

        :param token: 分享访问令牌
        :param parent_id: 父目录标识
        :return: 目录条目
        """
        files: list[ShareFile] = []
        seen: set[str] = set()
        cursor = ""
        for _ in range(_MAX_PAGES):
            body: dict[str, Any] = {
                "parentId": parent_id or _ROOT_ID,
                "pageSize": _PAGE_SIZE,
                "orderBy": 0,
                "sortType": 0,
                "accessToken": token,
            }
            if cursor:
                body["cursor"] = cursor
            payload = self._post_business(_SHARE_FILES_URL, body, action="获取分享文件列表")
            data = payload.get("data") or {}
            for item in _list_array(data):
                parsed = _parse_file(item)
                if parsed is None or parsed.file_id in seen:
                    continue
                seen.add(parsed.file_id)
                files.append(parsed)
            if not data.get("hasMore"):
                break
            cursor = as_text(data.get("cursor"))
            if not cursor:
                break
        return files

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        获取分享详情与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情
        """
        token = self._share_token(ref)
        title = self._share_title(ref.share_id)
        files = self._list_share(token, _ROOT_ID)
        size = sum(item.size for item in files if not item.is_dir)
        return ShareDetail(
            title=title or "光鸭分享",
            size=size,
            files=tuple(files),
            stoken=token,
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
        return self._list_share(self._share_token(ref), parent_id)

    # ---- 取链 ----

    def _share_link(self, file: ShareFile, token: str) -> dict[str, Any]:
        """
        免登录取链：`get_share_download_url`。

        :param file: 目标文件
        :param token: 分享访问令牌
        :return: 含 url/name/size 的字典
        """
        payload = self._post_business(
            _SHARE_DOWNLOAD_URL,
            {"fileId": file.file_id, "accessToken": token},
            action="获取下载地址",
        )
        data = payload.get("data") or {}
        url = _first_url(data)
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "获取下载地址失败：下载链接为空",
                platform=self.platform,
            )
        return {
            "url": url,
            "name": _first_non_blank(data.get("fileName"), data.get("name")) or file.name,
            "size": as_int(data.get("size")) or file.size,
        }

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
        headers = self.http.headers(None, extra=self._business_headers())
        if account is not None:
            headers["Authorization"] = f"Bearer {self._account_token(account)}"
        headers["User-Agent"] = GUANGYA_USER_AGENT
        headers["Referer"] = _REFERER
        return DirectLink(
            url=as_text(link.get("url")),
            headers=headers,
            size=as_int(link.get("size")) or file.size,
            expires_at=None,
            cleanup_handle=handle,
            account_id=account.id if account else 0,
            is_hls=False,
        )

    # ---- 转存与清理 ----

    @staticmethod
    def _account_token(account: Optional[NetdiskAccountRecord]) -> str:
        """
        读取账号访问令牌。

        :param account: 账号记录
        :return: 访问令牌
        :raises NetdiskError: 账号缺失或没有令牌时抛出
        """
        token = credential_value(account, "token", "access_token", "accessToken")
        if not token:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "光鸭账号缺少 token，请重新登录",
                platform=NetdiskPlatform.GUANGYA,
                account_name=account.name if account else None,
            )
        return token

    def _create_dir(self, token: str) -> str:
        """
        在个人网盘根目录创建唯一临时目录。

        :param token: 账号访问令牌
        :return: 新目录标识
        """
        name = f"{_TEMP_DIR_PREFIX}{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        payload = self._post_business(
            _CREATE_DIR_URL,
            {"parentId": _ROOT_ID, "dirName": name, "failIfNameExist": True},
            token=token,
            action="创建临时目录",
        )
        data = payload.get("data") or {}
        dir_id = _first_non_blank(data.get("fileId"), data.get("id"))
        if not dir_id or as_text(data.get("resType")) == "1":
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建临时目录失败：响应缺少目录标识",
                platform=self.platform,
            )
        return dir_id

    def _restore(self, token: str, share_token: str, file: ShareFile, dir_id: str) -> None:
        """
        把分享文件转存到临时目录，并按需等待异步任务完成。

        :param token: 账号访问令牌
        :param share_token: 分享访问令牌
        :param file: 目标文件
        :param dir_id: 临时目录标识
        """
        payload = self._post_business(
            _RESTORE_SHARE_URL,
            {"accessToken": share_token, "fileIds": [file.file_id], "parentId": dir_id},
            token=token,
            action="转存分享文件",
        )
        task_id = as_text((payload.get("data") or {}).get("taskId"))
        if task_id:
            self._wait_task(token, task_id)

    def _wait_task(self, token: str, task_id: str) -> None:
        """
        轮询转存任务，失败或超时抛出分类异常。

        :param token: 账号访问令牌
        :param task_id: 任务标识
        """
        for _ in range(_TASK_POLL_TIMES):
            self.sleep(_TASK_POLL_INTERVAL)
            payload = self._post_business(
                _TASK_STATUS_URL,
                {"taskId": task_id},
                token=token,
                action="查询转存任务",
            )
            data = payload.get("data") or {}
            status = as_int(data.get("status"))
            detail = (data.get("detail") if isinstance(data.get("detail"), dict) else {}) or {}
            detail_code = as_int(detail.get("code"))
            if status == 2 and detail_code == 0:
                return
            if status == 3 or (status == 2 and detail_code != 0):
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    as_text(detail.get("msg")) or "光鸭转存失败",
                    platform=self.platform,
                )
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "光鸭转存仍在处理中，请稍后重试",
            platform=self.platform,
        )

    def _list_cloud(self, token: str, parent_id: str) -> list[ShareFile]:
        """
        列出个人盘目录，按页码翻页。

        :param token: 账号访问令牌
        :param parent_id: 父目录标识
        :return: 目录条目
        """
        files: list[ShareFile] = []
        seen: set[str] = set()
        for page in range(_MAX_PAGES):
            body = {
                "parentId": parent_id,
                "pageSize": _PAGE_SIZE,
                "orderBy": 0,
                "sortType": 0,
                "page": page,
            }
            payload = self._post_business(_FILE_LIST_URL, body, token=token, action="获取文件列表")
            data = payload.get("data")
            if not isinstance(data, dict):
                break
            items = _list_array(data)
            added = 0
            for item in items:
                parsed = _parse_file(item)
                if parsed is None or parsed.file_id in seen:
                    continue
                seen.add(parsed.file_id)
                files.append(parsed)
                added += 1
            total = as_int(data.get("total"))
            if not items or not added or (total and len(files) >= total):
                break
            if not total and len(items) < _PAGE_SIZE:
                break
        return files

    def _wait_saved(
        self,
        token: str,
        dir_id: str,
        file: ShareFile,
    ) -> Optional[ShareFile]:
        """
        轮询临时目录直到转存完成（同名文件出现）。

        :param token: 账号访问令牌
        :param dir_id: 临时目录标识
        :param file: 源文件
        :return: 转存后的文件；超时返回 None
        """
        for attempt in range(_TRANSFER_POLL_TIMES):
            if attempt:
                self.sleep(_TRANSFER_POLL_INTERVAL)
            try:
                files = self._list_cloud(token, dir_id)
            except NetdiskError:
                continue
            matches = [item for item in files if not item.is_dir and item.name == file.name]
            if len(matches) > 1:
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "临时目录出现同名文件，无法确定下载目标",
                    platform=self.platform,
                )
            if matches:
                target = matches[0]
                if file.size and target.size and file.size != target.size:
                    raise NetdiskError(
                        NetdiskErrorCode.UNKNOWN,
                        "转存文件大小不一致",
                        platform=self.platform,
                    )
                return target
        return None

    def _file_link(self, token: str, file: ShareFile) -> dict[str, Any]:
        """
        从个人盘取下载地址。

        :param token: 账号访问令牌
        :param file: 目标文件
        :return: 含 url/name/size 的字典
        """
        payload = self._post_business(
            _RES_DOWNLOAD_URL,
            {"fileId": file.file_id},
            token=token,
            action="获取文件下载地址",
        )
        data = payload.get("data") or {}
        url = _first_url(data)
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "获取文件下载地址失败：下载链接为空",
                platform=self.platform,
            )
        return {
            "url": url,
            "name": file.name,
            "size": as_int(data.get("size")) or file.size,
        }

    def _transfer_link(
        self,
        file: ShareFile,
        share_token: str,
        account: NetdiskAccountRecord,
    ) -> tuple[TransferHandle, dict[str, Any]]:
        """
        转存到唯一临时目录后取链，失败时清理刚建的目录。

        :param file: 目标文件
        :param share_token: 分享访问令牌
        :param account: 取链账号
        :return: 清理句柄与直链信息
        """
        token = self._account_token(account)
        dir_id = self._create_dir(token)
        try:
            self._restore(token, share_token, file, dir_id)
            target = self._wait_saved(token, dir_id, file)
            if target is None:
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "转存已提交但目标文件暂不可见，请稍后重试",
                    platform=self.platform,
                )
            link = self._file_link(token, target)
        except Exception:
            self._safe_delete(token, [dir_id])
            raise
        link["name"] = file.name or link.get("name") or ""
        link["size"] = as_int(link.get("size")) or target.size or file.size
        handle = TransferHandle(
            platform=self.platform,
            account_id=account.id,
            directory_id=dir_id,
            file_ids=(target.file_id,),
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

        优先免登录取链；分享者未开启免登录下载时，若允许转存则转存到唯一临时目录
        再从个人盘取链，并返回清理句柄。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底
        :return: 直链
        """
        share_token = self._share_token(ref)
        try:
            link = self._share_link(file, share_token)
        except _ShareRestrictedError as error:
            if not allow_transfer or account is None:
                raise NetdiskError(
                    NetdiskErrorCode.UNSUPPORTED,
                    "该光鸭分享需登录并转存后才能下载",
                    platform=self.platform,
                ) from error
            self.log_failure("免登录取链", error, account)
            handle, saved_link = self._transfer_link(file, share_token, account)
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
        try:
            token = self._account_token(account)
        except NetdiskError as error:
            self.log_failure("清理临时目录", error, account)
            return
        self._safe_delete(token, [handle.directory_id])

    def _delete(self, token: str, file_ids: list[str]) -> None:
        """
        删除个人盘文件或目录。

        :param token: 账号访问令牌
        :param file_ids: 待删除的标识列表
        """
        if not file_ids:
            return
        self._post_business(
            _DELETE_FILE_URL,
            {"fileIds": list(file_ids)},
            token=token,
            action="清理临时目录",
        )

    def _safe_delete(self, token: str, file_ids: list[str]) -> None:
        """
        删除失败只记录，不影响主流程。

        :param token: 账号访问令牌
        :param file_ids: 待删除的标识列表
        """
        try:
            self._delete(token, file_ids)
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载
            self.log_failure("清理临时目录", error, None)

    # ---- 账号 ----

    def _probe_token(self, token: str) -> AccountProbe:
        """
        调用 `/v1/user/me` 确认令牌可用。

        :param token: 账号访问令牌
        :return: 探测结果
        """
        headers = self._account_headers()
        headers["Authorization"] = f"Bearer {token}"
        try:
            payload = self.http.get_json(_USER_ME_URL, headers=headers)
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="光鸭登录态已失效")
            return AccountProbe(status="unknown", message="无法确认光鸭登录态")
        if not _is_success(payload):
            return AccountProbe(status="expired", message="光鸭登录态已失效")
        data = (payload.get("data") if isinstance(payload.get("data"), dict) else payload) or {}
        nickname = _first_non_blank(data.get("nickname"), data.get("nick_name"), data.get("name"))
        message = f"光鸭登录态有效：{nickname}" if nickname else "光鸭登录态有效"
        return AccountProbe(status="active", message=message)

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验一份新凭据是否可用。

        :param credential: 明文凭据字段
        :return: 探测结果
        """
        probe = super().validate_credential(credential)
        if probe.status != "active":
            return probe
        token = _first_non_blank(credential.get("token"), credential.get("access_token"))
        if not token:
            return AccountProbe(status="expired", message="凭据缺少必填字段：token")
        return self._probe_token(token)

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        try:
            token = self._account_token(account)
        except NetdiskError as error:
            return AccountProbe(status="expired", message=error.message)
        return self._probe_token(token)

    def password_login(self, username: str, password: str) -> Mapping[str, str]:
        """
        使用账号密码换取访问令牌。

        需要人机验证或短信验证码时抛出 `RISK_BLOCKED`，提示改用网页扫码登录。

        :param username: 账号（手机号或邮箱）
        :param password: 密码
        :return: 可保存的凭据字段
        """
        account_name = _normalize_account(username)
        if not account_name or not password:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "请填写光鸭账号与密码",
                platform=self.platform,
            )
        captcha_token = self._captcha_init("POST:/v1/auth/signin", {"username": account_name})
        headers = self._account_headers()
        if captcha_token:
            headers["X-Captcha-Token"] = captcha_token
        try:
            payload = self.http.post_json(
                _SIGNIN_URL,
                json={
                    "client_id": _CLIENT_ID,
                    "username": account_name,
                    "password": password,
                },
                headers=headers,
            )
        except NetdiskError as error:
            raise _normalize_error(error) from error
        code = as_text(payload.get("code")) or as_text(payload.get("error"))
        if code in {"captcha_required", "captcha_invalid"} or "captcha" in code.lower():
            raise NetdiskError(
                NetdiskErrorCode.RISK_BLOCKED,
                "光鸭登录需要人机验证，请改用网页扫码登录",
                platform=self.platform,
            )
        if code in {"verification_required", "verification_code_required"}:
            raise NetdiskError(
                NetdiskErrorCode.RISK_BLOCKED,
                "光鸭要求短信验证码验证，请改用网页扫码登录或稍后重试",
                platform=self.platform,
            )
        if not _is_success(payload):
            message = _first_non_blank(
                payload.get("msg"),
                payload.get("message"),
                payload.get("error_description"),
            )
            raise NetdiskError(
                _signin_error_code(code, message),
                _signin_error_message(code, message),
                platform=self.platform,
                account_name=account_name,
            )
        data = (payload.get("data") if isinstance(payload.get("data"), dict) else payload) or {}
        token = _first_non_blank(data.get("access_token"), data.get("accessToken"))
        if not token:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "光鸭登录失败：响应缺少 access_token",
                platform=self.platform,
                account_name=account_name,
            )
        return {"token": token}

    def _captcha_init(self, action: str, meta: Mapping[str, Any]) -> Optional[str]:
        """
        初始化光鸭人机验证。

        :param action: 触发动作，如 `POST:/v1/auth/signin`
        :param meta: 动作相关参数
        :return: 人机验证令牌；服务端未要求时为空
        :raises NetdiskError: 需要网页人机验证时抛出 RISK_BLOCKED
        """
        payload = self.http.post_json(
            _CAPTCHA_INIT_URL,
            json={
                "client_id": _CLIENT_ID,
                "action": action,
                "device_id": self._device_id,
                "captcha_token": "",
                "meta": dict(meta),
            },
            headers=self._account_headers(),
        )
        url = as_text(payload.get("url"))
        if url:
            raise NetdiskError(
                NetdiskErrorCode.RISK_BLOCKED,
                f"光鸭需要网页人机验证，请先在浏览器打开 {url} 完成验证",
                platform=self.platform,
            )
        return as_text(payload.get("captcha_token")) or None

    def send_sms_code(self, phone: str) -> dict[str, Any]:
        """
        发送光鸭短信验证码。

        :param phone: 手机号
        :return: 验证会话信息
        """
        phone_number = _normalize_account(phone)
        if not phone_number:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "请填写光鸭手机号",
                platform=self.platform,
            )
        meta = {"phone_number": phone_number}
        for _attempt in range(3):
            token = self._captcha_init("POST:/v1/auth/verification", meta)
            headers = self._account_headers()
            headers["X-Captcha-Token"] = token or ""
            payload = self.http.post_json(
                _VERIFICATION_URL,
                json={"client_id": _CLIENT_ID, "phone_number": phone_number, "target": "ANY"},
                headers=headers,
            )
            code = as_text(payload.get("code")) or as_text(payload.get("error"))
            if code in {"captcha_required", "captcha_invalid"}:
                continue
            if not _is_success(payload):
                message = _first_non_blank(
                    payload.get("msg"),
                    payload.get("message"),
                    payload.get("error_description"),
                )
                raise NetdiskError(
                    _signin_error_code(code, message),
                    _signin_error_message(code, message),
                    platform=self.platform,
                    account_name=phone_number,
                )
            verification_id = as_text(payload.get("verification_id"))
            if not verification_id:
                raise NetdiskError(
                    NetdiskErrorCode.RATE_LIMITED,
                    "光鸭验证码请求过于频繁，请稍后重试",
                    platform=self.platform,
                )
            return {
                "verification_id": verification_id,
                "is_user": bool(payload.get("is_user")),
                "expires_in": as_int(payload.get("expires_in")) or 600,
            }
        raise NetdiskError(
            NetdiskErrorCode.RISK_BLOCKED,
            "光鸭安全验证未通过，请稍后重试或改用账号密码登录",
            platform=self.platform,
        )

    def sms_login(self, phone: str, code: str, verification_id: str) -> Mapping[str, str]:
        """
        使用短信验证码登录光鸭。

        :param phone: 手机号
        :param code: 短信验证码
        :param verification_id: 发码接口返回的验证会话
        :return: 可保存的凭据字段
        """
        phone_number = _normalize_account(phone)
        verification_code = str(code or "").strip()
        if not phone_number or not verification_code or not verification_id:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "请填写手机号、验证码并先获取验证码",
                platform=self.platform,
            )
        verify = self.http.post_json(
            _VERIFICATION_VERIFY_URL,
            json={
                "client_id": _CLIENT_ID,
                "verification_id": verification_id,
                "verification_code": verification_code,
            },
            headers=self._account_headers(),
        )
        if not _is_success(verify):
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "光鸭短信验证码错误或已过期，请重新获取",
                platform=self.platform,
            )
        verification_token = as_text(verify.get("verification_token"))
        if not verification_token:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "光鸭短信验证码错误或已过期，请重新获取",
                platform=self.platform,
            )
        payload = self.http.post_json(
            _SIGNIN_URL,
            json={
                "client_id": _CLIENT_ID,
                "username": phone_number,
                "verification_code": verification_code,
                "verification_token": verification_token,
            },
            headers=self._account_headers(),
        )
        if not _is_success(payload):
            message = _first_non_blank(
                payload.get("msg"),
                payload.get("message"),
                payload.get("error_description"),
            )
            raise NetdiskError(
                _signin_error_code(as_text(payload.get("code")), message),
                _signin_error_message(as_text(payload.get("code")), message),
                platform=self.platform,
                account_name=phone_number,
            )
        data = (payload.get("data") if isinstance(payload.get("data"), dict) else payload) or {}
        token = _first_non_blank(data.get("access_token"), data.get("accessToken"))
        if not token:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "光鸭登录失败：响应缺少 access_token",
                platform=self.platform,
                account_name=phone_number,
            )
        return {"token": token}


def _signin_error_code(code: str, message: str) -> NetdiskErrorCode:
    """
    把登录失败标识收敛为统一错误分类。

    :param code: 业务码或错误标识
    :param message: 服务端提示
    :return: 统一错误分类
    """
    if code in {
        "invalid_password",
        "invalid_account_or_password",
        "invalid_credentials",
        "unauthenticated",
        "user_not_found",
    }:
        return NetdiskErrorCode.AUTH_EXPIRED
    classified = _classify(code, message)
    return NetdiskErrorCode.AUTH_EXPIRED if classified is NetdiskErrorCode.UNKNOWN else classified


def _signin_error_message(code: str, message: str) -> str:
    """
    给出可直接展示的登录失败原因。

    :param code: 业务码或错误标识
    :param message: 服务端提示
    :return: 展示文案
    """
    if code in {
        "invalid_password",
        "invalid_account_or_password",
        "invalid_credentials",
        "unauthenticated",
        "user_not_found",
    }:
        return "光鸭账号或密码不正确，请检查后重试"
    if code in {"password_required", "password_not_set"}:
        return "该光鸭账号尚未设置密码，请改用网页扫码登录"
    if code == "too_many_requests":
        return "光鸭登录尝试过于频繁，请稍后重试"
    return message or "光鸭登录失败，请检查账号密码或改用网页扫码登录"


def _check_business(payload: Mapping[str, Any], action: str) -> dict[str, Any]:
    """
    校验光鸭业务响应，失败时抛出分类异常。

    :param payload: 响应 JSON
    :param action: 用于错误提示的动作名
    :return: 原响应 JSON
    :raises NetdiskError: 服务端返回失败状态时抛出
    """
    if _is_success(payload):
        return dict(payload)
    code = as_text(payload.get("code")) or as_text(payload.get("error"))
    if code == _CODE_SHARE_RESTRICTED:
        raise _ShareRestrictedError()
    message = _first_non_blank(
        payload.get("msg"),
        payload.get("message"),
        payload.get("error_description"),
    )
    raise NetdiskError(
        _classify(code, message),
        f"{action}失败：{message or f'code={code}'}",
        platform=NetdiskPlatform.GUANGYA,
    )
