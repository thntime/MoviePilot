"""
UC 网盘协议适配器。

UC 与夸克同源但域名、UA 与查询参数不同：分享取链全程免转存，
普通文件走 `file/download` 直链接口，视频优先走 `video_preview` 取原画播放流。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

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

UC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UC_WEB_ORIGIN = "https://drive.uc.cn"
UC_DOWNLOAD_REFERER = f"{UC_WEB_ORIGIN}/"
UC_ACCOUNT_INFO_URL = f"{UC_WEB_ORIGIN}/account/info"
UC_API_BASE = "https://pc-api.uc.cn"
UC_SHARE_TOKEN_URL = f"{UC_API_BASE}/1/clouddrive/share/sharepage/token?pr=UCBrowser&fr=pc"
UC_SHARE_DETAIL_URL = f"{UC_API_BASE}/1/clouddrive/transfer_share/detail?entry=ft&fr=pc&pr=UCBrowser"
UC_DOWNLOAD_URL = f"{UC_API_BASE}/1/clouddrive/file/download?entry=ft&fr=pc&pr=UCBrowser"
UC_VIDEO_PREVIEW_URL = f"{UC_API_BASE}/1/clouddrive/share/sharepage/video_preview"
UC_CONFIG_URL = f"{UC_API_BASE}/1/clouddrive/config?pr=UCBrowser&fr=pc"
UC_MEMBER_URL = f"{UC_API_BASE}/1/clouddrive/member?pr=UCBrowser&fr=pc&fetch_subscribe=true&_ch=home"
UC_FILE_SORT_URL = f"{UC_API_BASE}/1/clouddrive/file/sort?pr=UCBrowser&fr=pc"
UC_CLOUD_DOWNLOAD_URL = f"{UC_API_BASE}/1/clouddrive/file/download?pr=UCBrowser&fr=pc&sys=win32&ve=1.6.1"
UC_RENAME_URL = f"{UC_API_BASE}/1/clouddrive/file/rename?pr=UCBrowser&fr=pc"
UC_MOVE_URL = f"{UC_API_BASE}/1/clouddrive/file/move?pr=UCBrowser&fr=pc"
UC_DELETE_URL = f"{UC_API_BASE}/1/clouddrive/file/delete?pr=UCBrowser&fr=pc"
UC_SHARE_CREATE_URL = f"{UC_API_BASE}/1/clouddrive/share?pr=UCBrowser&fr=pc"
UC_TASK_URL = f"{UC_API_BASE}/1/clouddrive/task?pr=UCBrowser&fr=pc"
# 个人盘接口使用云盘客户端 UA，分享取链仍使用浏览器 UA。
UC_CLOUD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) uc-cloud-drive/1.6.1 Chrome/100.0.4896.160 "
    "Electron/18.3.5.16-b62cf9c50d Safari/537.36 Channel/ucpan_other_ch"
)
UC_ROOT_FID = "0"
UC_PAGE_SIZE = 50
UC_MAX_PAGES = 200
UC_TOKEN_CACHE_LIMIT = 64
UC_TRACKED_COOKIES = ("__puus", "__pus")
UC_VIDEO_SUFFIXES = frozenset(
    {"mp4", "mkv", "mov", "avi", "webm", "flv", "ts", "m3u8", "wmv", "rmvb", "m2ts"}
)

# 服务端错误码到统一错误分类的映射，消息固定为中文且不含 Cookie/token。
UC_ERROR_CODES: dict[int, tuple[NetdiskErrorCode, str]] = {
    401: (NetdiskErrorCode.AUTH_EXPIRED, "UC 网盘登录态已失效"),
    403: (NetdiskErrorCode.AUTH_EXPIRED, "UC 网盘登录态已失效"),
    429: (NetdiskErrorCode.RATE_LIMITED, "UC 网盘接口请求过于频繁"),
    31001: (NetdiskErrorCode.AUTH_EXPIRED, "该分享需要登录 UC 网盘后才能访问"),
    14001: (NetdiskErrorCode.SHARE_INVALID, "UC 分享已失效或提取码有误"),
    41020: (NetdiskErrorCode.LINK_EXPIRED, "UC 文件令牌已过期，请重新解析分享"),
    23018: (NetdiskErrorCode.UNSUPPORTED, "该文件超出当前取链方式的限制"),
}


def strip_puus(cookie: str) -> str:
    """
    去掉 Cookie 中的 `__puus` 字段，用于触发服务端重新下发。

    :param cookie: 原始 Cookie 头
    :return: 不含 `__puus` 的 Cookie 头
    """
    parts = [part.strip() for part in str(cookie or "").split(";")]
    return "; ".join(part for part in parts if part and not part.startswith("__puus="))


def merge_set_cookies(cookie: str, set_cookies: Sequence[str]) -> str:
    """
    把响应 `Set-Cookie` 中的 `__puus`/`__pus` 合并回原 Cookie。

    :param cookie: 原始 Cookie 头
    :param set_cookies: 响应下发的 `Set-Cookie` 行
    :return: 合并后的 Cookie 头
    """
    parts = [part.strip() for part in str(cookie or "").split(";") if part.strip()]
    for header in set_cookies:
        pair = str(header).split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if name not in UC_TRACKED_COOKIES:
            continue
        for index, part in enumerate(parts):
            if part.startswith(f"{name}="):
                parts[index] = f"{name}={value}"
                break
        else:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def is_video_name(name: str) -> bool:
    """
    按扩展名判断是否为视频文件。

    :param name: 文件名
    :return: 视频返回 True
    """
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in UC_VIDEO_SUFFIXES


def _set_cookie_headers(response: Any) -> list[str]:
    """
    读取单个响应的 `Set-Cookie` 行。

    :param response: HTTP 响应
    :return: `Set-Cookie` 行列表
    """
    raw = getattr(response, "raw", None)
    raw_headers = getattr(raw, "headers", None)
    if raw_headers is not None and hasattr(raw_headers, "getlist"):
        return [str(item) for item in (raw_headers.getlist("Set-Cookie") or [])]
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    value = headers.get("Set-Cookie")
    return [str(value)] if value else []


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


class UCHttp(NetdiskHttp):
    """
    记录响应 `Set-Cookie` 的 UC HTTP 客户端。

    会话刷新依赖服务端重新下发的 `__puus`/`__pus`，而基类只返回解析后的 JSON，
    因此在基类之上补充响应头捕获，其余请求行为保持不变。
    """

    def __init__(self, **kwargs: Any) -> None:
        """
        初始化客户端并准备 Cookie 捕获缓冲。

        :param kwargs: 透传给基类的平台、UA、Referer 与超时参数
        """
        super().__init__(**kwargs)
        self.last_set_cookies: list[str] = []

    def _json(self, response: Any) -> dict[str, Any]:
        """
        捕获响应 Cookie 后按基类规则解析 JSON。

        :param response: HTTP 响应
        :return: JSON 对象
        """
        self._capture(response)
        return super()._json(response)

    def drain_set_cookies(self) -> list[str]:
        """
        取出并清空最近一次响应捕获的 `Set-Cookie` 行。

        :return: `Set-Cookie` 行列表
        """
        cookies = list(self.last_set_cookies)
        self.last_set_cookies.clear()
        return cookies

    def _capture(self, response: Any) -> None:
        """
        汇总重定向链与最终响应的 `Set-Cookie`。

        :param response: HTTP 响应
        """
        collected: list[str] = []
        for item in (*getattr(response, "history", ()), response):
            collected.extend(_set_cookie_headers(item))
        if collected:
            self.last_set_cookies = collected


class UCAdapter(BaseNetdiskAdapter):
    """
    UC 网盘取链适配器。

    UC 从不转存，`allow_transfer` 一律忽略；游客可取链但能力受平台限制。
    """

    http: UCHttp
    platform = NetdiskPlatform.UC
    link_policy = LinkPolicy.DIRECT
    requires_account = True
    guest_supported = True
    supports_refresh = True
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
    cookie_required_keys = ("__pus", "__puus")
    cookie_domains = (".uc.cn",)
    login_url = "https://drive.uc.cn/"
    supports_password_login = False
    user_agent = UC_USER_AGENT
    referer = UC_DOWNLOAD_REFERER

    def __init__(self) -> None:
        """
        初始化适配器与分享令牌缓存。
        """
        super().__init__()
        self.http = UCHttp(
            platform=self.platform,
            user_agent=self.user_agent,
            referer=self.referer,
        )
        self._tokens: dict[str, str] = {}

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        用分享码与提取码换 token，并返回根目录条目。

        :param ref: 分享引用
        :param account: 取链账号，游客链路为 None
        :return: 分享详情
        """
        stoken, title = self._share_token(ref, account)
        files, total_size = self._fetch_files(ref, stoken, UC_ROOT_FID, account)
        return ShareDetail(
            title=title,
            size=total_size,
            files=tuple(files),
            stoken=stoken,
        )

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        按父目录翻页列出分享内容。

        :param ref: 分享引用
        :param parent_id: 父目录标识，空值表示根目录
        :param account: 取链账号
        :return: 目录条目
        """
        stoken = self._stoken_for(ref, account)
        files, _total_size = self._fetch_files(ref, stoken, parent_id, account)
        return files

    def resolve(
        self,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        allow_transfer: bool = True,
    ) -> DirectLink:
        """
        获取文件直链；视频走原画预览，UC 不实现转存。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底，UC 始终忽略
        :return: 直链及其请求头
        """
        del allow_transfer  # UC 全程免转存，从不写个人网盘
        if file.is_dir:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "UC 分享目录不能直接下载",
                platform=self.platform,
            )
        stoken = self._stoken_for(ref, account)
        headers = self.http.headers(account)
        account_id = account.id if account else 0
        if is_video_name(file.name):
            preview = self._video_preview(ref, file, stoken, account)
            if preview is not None:
                url, size = preview
                return DirectLink(
                    url=url,
                    headers=headers,
                    size=size or file.size,
                    account_id=account_id,
                    is_hls=False,
                )
        url, size = self._download_link(ref, file, stoken, account)
        return DirectLink(
            url=url,
            headers=headers,
            size=size or file.size,
            account_id=account_id,
            is_hls=False,
        )

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        UC 不产生转存内容，无需清理。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        del handle, account
        return None

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        调用账号信息接口确认登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        basic = super().validate_credential(account.credential)
        if basic.status != "active":
            return basic
        return self._probe_http(account)

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验新凭据：先查必填字段，再调用账号信息接口。

        :param credential: 明文凭据字段
        :return: 探测结果
        """
        basic = super().validate_credential(credential)
        if basic.status != "active":
            return basic
        record = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="待校验凭据",
            credential=dict(credential),
        )
        return self._probe_http(record)

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从 UC 账号信息接口读取昵称。

        :param credential: 浏览器抓到的凭据
        :return: 昵称；接口不可用时为空串
        """
        record = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="",
            credential=dict(credential),
        )
        try:
            payload = self.http.get_json(UC_ACCOUNT_INFO_URL, account=record)
        except NetdiskError:
            return ""
        if payload.get("success") is False:
            return ""
        return as_text(self._data(payload).get("nickname"))

    def refresh_account(self, account: NetdiskAccountRecord) -> Optional[Mapping[str, str]]:
        """
        剥离 `__puus` 请求配置接口，合并服务端新下发的 `__puus`/`__pus`。

        :param account: 账号记录
        :return: 刷新后的凭据；没有新 Cookie 时返回 None
        """
        cookie = cookie_header(account)
        if not cookie:
            return None
        probe = NetdiskAccountRecord(
            id=account.id,
            platform=self.platform,
            name=account.name,
            credential={"cookie": strip_puus(cookie)},
            is_primary=account.is_primary,
            status=account.status,
            expires_at=account.expires_at,
        )
        self.http.last_set_cookies.clear()
        try:
            self.http.get_json(UC_CONFIG_URL, account=probe)
        except NetdiskError as error:
            self.log_failure("刷新登录态", error, account)
            return None
        set_cookies = self.http.drain_set_cookies()
        if not set_cookies:
            return None
        merged = merge_set_cookies(cookie, set_cookies)
        if merged == cookie:
            return None
        refreshed = dict(account.credential or {})
        refreshed["cookie"] = merged
        return refreshed

    def _probe_http(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        请求账号信息接口判断登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        try:
            payload = self.http.get_json(UC_ACCOUNT_INFO_URL, account=account)
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="UC 网盘登录态已失效")
            return AccountProbe(status="unknown", message="UC 网盘登录态暂时无法确认")
        if payload.get("success") is False:
            return AccountProbe(status="expired", message="UC 网盘登录态已失效")
        nickname = as_text(self._data(payload).get("nickname"))
        if nickname:
            return AccountProbe(status="active", message=f"UC 网盘登录态有效：{nickname}")
        return AccountProbe(status="active", message="UC 网盘登录态有效")

    def _share_token(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> tuple[str, str]:
        """
        用分享码与提取码换取 `stoken`。

        :param ref: 分享引用
        :param account: 取链账号
        :return: `stoken` 与分享标题
        """
        payload = self.http.post_json(
            UC_SHARE_TOKEN_URL,
            account=account,
            json={
                "pwd_id": ref.share_id,
                "passcode": ref.password or "",
                "share_for_transfer": True,
            },
        )
        self._ensure_success(payload, "解析分享")
        data = self._data(payload)
        stoken = as_text(data.get("stoken"))
        if not stoken:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "UC 分享未返回有效令牌",
                platform=self.platform,
            )
        self._remember_token(ref.share_id, stoken)
        return stoken, as_text(data.get("title"))

    def _stoken_for(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> str:
        """
        取分享令牌，优先复用引用与进程内缓存。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享令牌
        """
        if ref.share_token:
            return ref.share_token
        cached = self._tokens.get(ref.share_id)
        if cached:
            return cached
        stoken, _title = self._share_token(ref, account)
        return stoken

    def _remember_token(self, share_id: str, stoken: str) -> None:
        """
        缓存分享令牌，超过上限时整体清空。

        :param share_id: 分享码
        :param stoken: 分享令牌
        """
        if len(self._tokens) >= UC_TOKEN_CACHE_LIMIT:
            self._tokens.clear()
        self._tokens[share_id] = stoken

    def _fetch_files(
        self,
        ref: ShareRef,
        stoken: str,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> tuple[list[ShareFile], int]:
        """
        按父目录翻页拉取条目，按文件标识去重。

        :param ref: 分享引用
        :param stoken: 分享令牌
        :param parent_id: 父目录标识
        :param account: 取链账号
        :return: 条目列表与总大小
        """
        parent = parent_id or UC_ROOT_FID
        merged: dict[str, ShareFile] = {}
        total_size = 0
        page = 1
        while page <= UC_MAX_PAGES:
            params = {
                "pwd_id": ref.share_id,
                "pdir_fid": parent,
                "fetch_file_list": 1,
                "passcode": "",
                "_page": page,
                "_size": UC_PAGE_SIZE,
                "_fetch_total": 1,
                "_fetch_task": 1,
                "_fetch_share": 1,
                "_sort": "",
                "stoken": stoken,
            }
            payload = self.http.get_json(UC_SHARE_DETAIL_URL, account=account, params=params)
            self._ensure_success(payload, "列出目录")
            data = self._data(payload)
            if page == 1:
                total_size = self._total_size(data)
            items = self._items(data)
            if not items:
                break
            added = 0
            for item in items:
                share_file = self._to_share_file(item, parent)
                key = share_file.file_id or f"{share_file.parent_id}/{share_file.name}"
                if key in merged:
                    continue
                merged[key] = share_file
                added += 1
            if added == 0:
                break
            page += 1
        files = list(merged.values())
        if total_size <= 0:
            total_size = sum(item.size for item in files if not item.is_dir)
        return files, total_size

    def _download_link(
        self,
        ref: ShareRef,
        file: ShareFile,
        stoken: str,
        account: Optional[NetdiskAccountRecord],
    ) -> tuple[str, int]:
        """
        调用下载接口获取普通文件直链。

        :param ref: 分享引用
        :param file: 目标文件
        :param stoken: 分享令牌
        :param account: 取链账号
        :return: 直链与文件大小
        """
        payload = self.http.post_json(
            UC_DOWNLOAD_URL,
            account=account,
            json={
                "fids": [file.file_id],
                "pwd_id": ref.share_id,
                "stoken": stoken,
                "fids_token": [file.token],
            },
        )
        self._ensure_success(payload, "获取下载直链")
        item = self._first_item(payload.get("data"))
        url = as_text(item.get("download_url"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "UC 网盘未返回下载直链",
                platform=self.platform,
            )
        return url, as_int(item.get("size"))

    def _video_preview(
        self,
        ref: ShareRef,
        file: ShareFile,
        stoken: str,
        account: Optional[NetdiskAccountRecord],
    ) -> Optional[tuple[str, int]]:
        """
        调用视频预览接口获取原画播放地址。

        :param ref: 分享引用
        :param file: 目标文件
        :param stoken: 分享令牌
        :param account: 取链账号
        :return: 播放地址与大小；不可用时返回 None
        """
        params = {
            "pr": "UCBrowser",
            "fr": "h5",
            "pwd_id": ref.share_id,
            "stoken": stoken,
            "fid": file.file_id,
            "fid_token": file.token,
        }
        try:
            payload = self.http.get_json(UC_VIDEO_PREVIEW_URL, account=account, params=params)
            self._ensure_success(payload, "获取视频原画")
        except NetdiskError as error:
            self.log_failure("获取视频原画", error, account)
            return None
        play_info = self._data(payload).get("play_info")
        if not isinstance(play_info, dict):
            return None
        url = as_text(play_info.get("url"))
        if not url:
            return None
        return url, as_int(play_info.get("size"))

    def _ensure_success(self, payload: Mapping[str, Any], action: str) -> None:
        """
        按 `status`/`code` 判断业务响应是否成功。

        :param payload: 响应 JSON
        :param action: 动作描述，用于兜底错误消息
        """
        status = payload.get("status")
        code = payload.get("code")
        if status is None and code is None:
            return
        if status in (200, "200") or code in (0, "0"):
            return
        raise self._error_for_code(code, action)

    def _error_for_code(self, code: Any, action: str) -> NetdiskError:
        """
        把服务端错误码映射为统一分类异常。

        :param code: 服务端错误码
        :param action: 动作描述
        :return: 分类异常
        """
        try:
            number = int(code)
        except (TypeError, ValueError):
            number = 0
        error_code, message = UC_ERROR_CODES.get(
            number,
            (NetdiskErrorCode.UNKNOWN, f"UC 网盘{action}失败"),
        )
        return NetdiskError(error_code, message, platform=self.platform)

    @staticmethod
    def _data(payload: Mapping[str, Any]) -> dict[str, Any]:
        """
        取出响应中的 `data` 对象。

        :param payload: 响应 JSON
        :return: `data` 对象，缺失时为空字典
        """
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _items(data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """
        兼容 `data.list` 与 `data.detail_info.list` 两种条目结构。

        :param data: 响应 `data` 对象
        :return: 条目字典列表
        """
        candidates: list[Any] = [data.get("list")]
        detail_info = data.get("detail_info")
        if isinstance(detail_info, dict):
            candidates.append(detail_info.get("list"))
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    @staticmethod
    def _first_item(data: Any) -> dict[str, Any]:
        """
        取出下载接口返回的首个条目，兼容数组与 `list` 字段。

        :param data: 响应 `data` 字段
        :return: 条目字典，缺失时为空字典
        """
        items = data.get("list") if isinstance(data, dict) else data
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return {}

    @staticmethod
    def _total_size(data: Mapping[str, Any]) -> int:
        """
        读取响应给出的总大小，缺失时返回 0。

        :param data: 响应 `data` 对象
        :return: 总大小，单位字节
        """
        for key in ("total_size", "size", "total"):
            size = as_int(data.get(key))
            if size:
                return size
        return 0

    @staticmethod
    def _to_share_file(item: Mapping[str, Any], parent_id: str) -> ShareFile:
        """
        把接口条目转换为领域文件对象。

        :param item: 接口条目
        :param parent_id: 请求的父目录标识
        :return: 领域文件对象
        """
        size = as_int(item.get("size"))
        if not size:
            size = as_int(item.get("fsize"))
        return ShareFile(
            file_id=as_text(item.get("fid")),
            name=as_text(item.get("file_name")) or as_text(item.get("fname")),
            size=size,
            is_dir=bool(item.get("dir")) or as_int(item.get("isdir")) == 1,
            parent_id=as_text(item.get("pdir_fid")) or parent_id,
            token=as_text(item.get("share_fid_token")) or as_text(item.get("fid_token")),
        )

    # ---- 个人盘 ----

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询 UC 个人盘空间。

        :param account: 账号
        :return: 已用/总空间
        """
        payload = self.http.get_json(
            UC_MEMBER_URL,
            account=account,
            headers={"User-Agent": UC_CLOUD_UA},
        )
        self._ensure_success(payload, "查询空间")
        data = self._data(payload)
        return DriveQuota(
            used=as_int(data.get("use_capacity")),
            total=as_int(data.get("total_capacity")),
        )

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        分页列出 UC 个人盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串表示根目录
        :return: 目录条目
        """
        parent = parent_id or UC_ROOT_FID
        merged: dict[str, DriveFile] = {}
        page = 1
        while page <= UC_MAX_PAGES:
            payload = self.http.get_json(
                UC_FILE_SORT_URL,
                account=account,
                params={
                    "pdir_fid": parent,
                    "_page": page,
                    "_size": UC_PAGE_SIZE,
                    "_fetch_total": 1,
                    "_fetch_sub_dirs": 0,
                    "_sort": "file_type:asc,updated_at:desc",
                },
                headers={"User-Agent": UC_CLOUD_UA},
            )
            self._ensure_success(payload, "列出目录")
            items = self._items(self._data(payload))
            if not items:
                break
            added = 0
            for item in items:
                file = self._to_drive_file(item, parent)
                key = file.file_id or f"{file.parent_id}/{file.name}"
                if key in merged:
                    continue
                merged[key] = file
                added += 1
            if added == 0 or len(items) < UC_PAGE_SIZE:
                break
            page += 1
        return list(merged.values())

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取 UC 个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        payload = self.http.post_json(
            UC_CLOUD_DOWNLOAD_URL,
            account=account,
            json={"fids": [file.file_id]},
            headers={"User-Agent": UC_CLOUD_UA},
        )
        self._ensure_success(payload, "获取下载直链")
        item = self._first_item(payload.get("data"))
        url = as_text(item.get("download_url"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "UC 网盘未返回下载直链",
                platform=self.platform,
            )
        return DirectLink(
            url=url,
            headers=self.http.headers(account),
            size=as_int(item.get("size")) or file.size,
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
        创建 UC 个人盘分享并轮询异步任务。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码，空串表示无提取码
        :param expires_in: 有效期秒数，0 表示永久
        :return: 分享结果
        """
        if expires_in <= 0:
            expired_type = 1
        elif expires_in <= 86400:
            expired_type = 2
        elif expires_in <= 7 * 86400:
            expired_type = 3
        else:
            expired_type = 4
        payload = self.http.post_json(
            UC_SHARE_CREATE_URL,
            account=account,
            json={
                "fid_list": [item.file_id for item in files],
                "title": files[0].name if files else "分享文件",
                "url_type": 2 if password else 1,
                "expired_type": expired_type,
                "public_search": 0 if password else 1,
                **({"passcode": password} if password else {}),
            },
        )
        self._ensure_success(payload, "创建分享")
        task_id = as_text(self._data(payload).get("task_id"))
        share_id = self._poll_share_task(account, task_id)
        if not share_id:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "UC 分享创建超时",
                platform=self.platform,
            )
        return DriveShare(url=f"https://drive.uc.cn/s/{share_id}", password=password)

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动 UC 个人盘文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录，空串表示根目录
        """
        payload = self.http.post_json(
            UC_MOVE_URL,
            account=account,
            json={
                "action_type": 1,
                "to_pdir_fid": target_id or UC_ROOT_FID,
                "filelist": [item.file_id for item in files],
                "exclude_fids": [],
            },
        )
        self._ensure_success(payload, "移动文件")

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名 UC 个人盘文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        payload = self.http.post_json(
            UC_RENAME_URL,
            account=account,
            json={"fid": file.file_id, "file_name": name},
        )
        self._ensure_success(payload, "重命名文件")

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除 UC 个人盘文件。

        :param account: 账号
        :param files: 文件列表
        """
        payload = self.http.post_json(
            UC_DELETE_URL,
            account=account,
            json={
                "action_type": 2,
                "filelist": [item.file_id for item in files],
                "exclude_fids": [],
            },
        )
        self._ensure_success(payload, "删除文件")

    def _poll_share_task(self, account: NetdiskAccountRecord, task_id: str) -> str:
        """
        轮询分享创建任务，直到完成或超时。

        :param account: 账号
        :param task_id: 创建分享返回的任务标识
        :return: 分享标识；超时返回空串
        """
        if not task_id:
            return ""
        for _ in range(15):
            payload = self.http.get_json(
                UC_TASK_URL,
                account=account,
                params={"task_id": task_id, "retry_index": 0},
            )
            self._ensure_success(payload, "查询分享任务")
            data = self._data(payload)
            finished = as_int(data.get("finished_at")) > 0 or as_int(data.get("status")) == 2
            share_id = as_text(data.get("share_id"))
            if finished and share_id:
                return share_id
            time.sleep(1)
        return ""

    @staticmethod
    def _to_drive_file(item: Mapping[str, Any], parent_id: str) -> DriveFile:
        """
        把个人盘条目转换为领域文件对象。

        :param item: 接口条目
        :param parent_id: 请求的父目录标识
        :return: 领域文件对象
        """
        size = as_int(item.get("size"))
        if not size:
            size = as_int(item.get("fsize"))
        return DriveFile(
            file_id=as_text(item.get("fid")),
            name=as_text(item.get("file_name")) or as_text(item.get("fname")),
            size=size,
            is_dir=bool(item.get("dir")) or as_int(item.get("isdir")) == 1,
            updated_at=_parse_time(item.get("updated_at")) or _parse_time(item.get("modify_time")),
            parent_id=as_text(item.get("pdir_fid")) or parent_id,
        )
