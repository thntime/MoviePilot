"""
夸克网盘协议适配器。

只负责「分享 → 直链」这一段：分享令牌、目录详情、免转存取链、转存兜底与清理、
会话 Cookie 刷新。分片下载由下载引擎负责，接口字段与请求参数沿用
`app/modules/quark/quark.py` 的抓包实现，保证迁移前后行为一致。
"""

from __future__ import annotations

import time
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

_API_BASE = "https://drive-pc.quark.cn"
_DOWNLOAD_URL = f"{_API_BASE}/1/clouddrive/file/download?pr=ucpro&fr=pc&sys=win32&ve=3.23.2"
_SAVE_URL = f"{_API_BASE}/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc"
_TASK_URL = f"{_API_BASE}/1/clouddrive/task?pr=ucpro&fr=pc"
_TOKEN_URL = f"{_API_BASE}/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
_DETAIL_URL = f"{_API_BASE}/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc"
_FILE_URL = f"{_API_BASE}/1/clouddrive/file?pr=ucpro&fr=pc"
_DELETE_URL = f"{_API_BASE}/1/clouddrive/file/delete?pr=ucpro&fr=pc&uc_param_str="
_CONFIG_URL = f"{_API_BASE}/1/clouddrive/config?pr=ucpro&fr=pc"
_ACCOUNT_INFO_URL = "https://pan.quark.cn/account/info"

# 取链与下载必须使用客户端 UA 与固定 Referer，浏览器 UA 会被判为非客户端。
QUARK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.12-a038f7b798 "
    "Safari/537.36 Channel/pckk_other_ch"
)
QUARK_REFERER = "https://pan.quark.cn/"

# 转存临时目录前缀；目录名带毫秒时间戳，保证每次转存都是唯一目标目录。
_TEMP_DIR_PREFIX = "mp_tr_"
# 分享列表单页条数；服务端可能截断到 50，翻页以「整页无新条目」结束。
_SHARE_PAGE_SIZE = 100
_SHARE_MAX_PAGES = 20
# 转存任务轮询次数与间隔。
_TASK_POLL_TIMES = 10
_TASK_POLL_INTERVAL = 1.0
# 登录态失效与游客大小限制的服务端错误码。
_CODE_LOGIN_REQUIRED = 31001
_CODE_GUEST_LIMIT = 23018


def _strip_puus(cookie: str) -> str:
    """
    去掉 __puus，用于触发服务端重新下发新令牌。

    :param cookie: 原 Cookie
    :return: 不含 __puus 的 Cookie
    """
    return "; ".join(
        item.strip()
        for item in cookie.split(";")
        if item.strip() and not item.strip().startswith("__puus=")
    )


def _merge_cookie(cookie: str, raw: str) -> str:
    """
    把 Set-Cookie 中的 __puus/__pus 合并回 Cookie 串。

    :param cookie: 原 Cookie
    :param raw: 响应 Set-Cookie 原文
    :return: 合并后的 Cookie；没有新令牌时原样返回
    """
    if not raw:
        return cookie
    updates: dict[str, str] = {}
    for part in raw.replace("\n", ",").split(","):
        pair = part.strip().split(";")[0].strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if key in {"__puus", "__pus"} and value.strip():
            updates[key] = value.strip()
    if not updates:
        return cookie
    kept = [
        item.strip()
        for item in cookie.split(";")
        if item.strip() and item.strip().split("=", 1)[0].strip() not in updates
    ]
    kept.extend(f"{key}={value}" for key, value in updates.items())
    return "; ".join(kept)


def _set_cookie_header(response: Any) -> str:
    """
    读取响应的 Set-Cookie 头。

    :param response: HTTP 响应
    :return: Set-Cookie 原文；响应缺少该头时返回空串
    """
    headers = getattr(response, "headers", None)
    if not headers:
        return ""
    try:
        value = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
    except Exception:  # noqa: BLE001 - 响应替身可能没有 headers 映射
        return ""
    return str(value)


def _classify(code: int, status: int, message: str) -> NetdiskErrorCode:
    """
    把夸克服务端错误码收敛为统一错误分类。

    :param code: 服务端业务错误码
    :param status: 响应 status 字段
    :param message: 服务端提示
    :return: 统一错误分类
    """
    if code == _CODE_LOGIN_REQUIRED or status in (401, 403):
        return NetdiskErrorCode.AUTH_EXPIRED
    if code == _CODE_GUEST_LIMIT:
        return NetdiskErrorCode.UNSUPPORTED
    if status == 429:
        return NetdiskErrorCode.RATE_LIMITED
    if status == 412:
        return NetdiskErrorCode.LINK_EXPIRED
    if "提取码" in message or ("分享" in message and "失效" in message):
        return NetdiskErrorCode.SHARE_INVALID
    if "空间" in message:
        return NetdiskErrorCode.SPACE_INSUFFICIENT
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
    if "412" in message or "HTML" in message or "防盗链" in message:
        return NetdiskError(
            NetdiskErrorCode.LINK_EXPIRED,
            "夸克直链已失效或被防盗链拦截",
            platform=NetdiskPlatform.QUARK,
        )
    return error


def _check_payload(payload: Mapping[str, Any], action: str) -> dict[str, Any]:
    """
    校验夸克统一响应结构，失败时抛出分类异常。

    :param payload: 响应 JSON
    :param action: 用于错误提示的动作名
    :return: 原响应 JSON
    :raises NetdiskError: 服务端返回失败状态时抛出
    """
    status = as_int(payload.get("status"))
    code = as_int(payload.get("code"))
    if status == 200 or code == 0:
        return dict(payload)
    message = as_text(payload.get("message")) or f"HTTP {status}"
    raise NetdiskError(
        _classify(code, status, message),
        f"{action}失败：{message}",
        platform=NetdiskPlatform.QUARK,
    )


class _QuarkHttp(NetdiskHttp):
    """
    记录最近一次响应的 Set-Cookie，供会话刷新合并新令牌。

    基类只返回 JSON 对象，`refresh_account` 需要响应头里的 __puus/__pus，
    因此在这里拦截解析阶段并留存原始头。
    """

    def __init__(self) -> None:
        """
        初始化夸克固定请求头与响应头缓存。
        """
        super().__init__(
            platform=NetdiskPlatform.QUARK,
            user_agent=QUARK_USER_AGENT,
            referer=QUARK_REFERER,
        )
        self.last_set_cookie = ""

    def _json(self, response: Any) -> dict[str, Any]:
        """
        解析响应前先记录 Set-Cookie。

        :param response: HTTP 响应
        :return: 响应 JSON
        """
        self.last_set_cookie = _set_cookie_header(response)
        return super()._json(response)


class QuarkAdapter(BaseNetdiskAdapter):
    """
    夸克网盘适配器：免转存取链优先，失败后转存到唯一临时目录兜底。
    """

    platform: NetdiskPlatform = NetdiskPlatform.QUARK
    link_policy: LinkPolicy = LinkPolicy.DIRECT_WITH_FALLBACK
    requires_account: bool = True
    guest_supported: bool = True
    supports_refresh: bool = True
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
    cookie_required_keys: tuple[str, ...] = ("__pus", "__puus")
    cookie_domains: tuple[str, ...] = (".quark.cn",)
    login_url: str = "https://pan.quark.cn/"
    supports_password_login: bool = False
    user_agent: str = QUARK_USER_AGENT
    referer: str = QUARK_REFERER

    def __init__(self) -> None:
        """
        使用带响应头缓存的 HTTP 客户端。
        """
        super().__init__()
        self.http = _QuarkHttp()

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
        return _check_payload(payload, action)

    def _post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        account: Optional[NetdiskAccountRecord],
        action: str,
    ) -> dict[str, Any]:
        """
        发起 POST 请求并校验业务状态。

        :param url: 请求地址
        :param body: JSON 请求体
        :param account: 账号记录
        :param action: 用于错误提示的动作名
        :return: 响应 JSON
        """
        try:
            payload = self.http.post_json(url, account=account, json=body)
        except NetdiskError as error:
            raise _normalize_error(error) from error
        return _check_payload(payload, action)

    # ---- 分享与目录 ----

    def _stoken(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> tuple[str, str]:
        """
        取得分享访问令牌；调用方已透传时直接复用。

        :param ref: 分享引用
        :param account: 取链账号
        :return: stoken 与分享标题
        """
        if ref.share_token:
            return ref.share_token, ""
        payload = self._post_json(
            _TOKEN_URL,
            {
                "pwd_id": ref.share_id,
                "passcode": ref.password or "",
                "support_visit_limit_private_share": True,
            },
            account=account,
            action="获取分享令牌",
        )
        data = payload.get("data") or {}
        stoken = as_text(data.get("stoken"))
        if not stoken:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "获取分享令牌失败：响应缺少 stoken",
                platform=self.platform,
            )
        return stoken, as_text(data.get("title"))

    def _list_share(
        self,
        share_id: str,
        stoken: str,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        按 pdir_fid 翻页列出分享目录，按 fid 去重直到整页无新条目。

        :param share_id: 分享码
        :param stoken: 分享访问令牌
        :param parent_id: 父目录标识，根目录为 "0"
        :param account: 取链账号
        :return: 目录条目
        """
        pdir_fid = parent_id or "0"
        entries: dict[str, ShareFile] = {}
        for page in range(1, _SHARE_MAX_PAGES + 1):
            payload = self._get_json(
                _DETAIL_URL,
                account=account,
                params={
                    "pr": "ucpro",
                    "fr": "pc",
                    "pwd_id": share_id,
                    "stoken": stoken,
                    "pdir_fid": pdir_fid,
                    "force": "0",
                    "_page": str(page),
                    "_size": str(_SHARE_PAGE_SIZE),
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,updated_at:desc",
                },
                action="读取分享目录",
            )
            data = payload.get("data") or {}
            before = len(entries)
            for item in data.get("list") or []:
                if not isinstance(item, dict):
                    continue
                name = as_text(item.get("file_name") or item.get("fileName"))
                if not name:
                    continue
                file_id = as_text(item.get("fid"))
                entries.setdefault(
                    file_id or f"{pdir_fid}/{name}",
                    ShareFile(
                        file_id=file_id,
                        name=name,
                        size=as_int(item.get("size")),
                        is_dir=bool(item.get("dir")),
                        parent_id=as_text(item.get("pdir_fid")) or pdir_fid,
                        token=as_text(item.get("share_fid_token") or item.get("fid_token")),
                    ),
                )
            if not entries or len(entries) == before:
                break
        return list(entries.values())

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        换取 stoken 并返回分享详情与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情
        """
        stoken, title = self._stoken(ref, account)
        files = self._list_share(ref.share_id, stoken, "0", account)
        size = sum(item.size for item in files if not item.is_dir)
        return ShareDetail(title=title, size=size, files=tuple(files), stoken=stoken)

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
        stoken, _ = self._stoken(ref, account)
        return self._list_share(ref.share_id, stoken, parent_id, account)

    # ---- 取链 ----

    def _share_link(
        self,
        share_id: str,
        stoken: str,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
    ) -> dict[str, Any]:
        """
        免转存取链：把分享凭证直接交给下载接口换直链。

        :param share_id: 分享码
        :param stoken: 分享访问令牌
        :param file: 目标文件
        :param account: 取链账号
        :return: 含 url/name/size 的字典
        """
        payload = self._post_json(
            _DOWNLOAD_URL,
            {
                "fids": [file.file_id],
                "fids_token": [file.token],
                "pwd_id": share_id,
                "stoken": stoken,
                "speedup_session": "",
                "token": "",
            },
            account=account,
            action="获取分享直链",
        )
        return self._first_link(payload, "获取分享直链")

    def _file_link(self, fid: str, account: Optional[NetdiskAccountRecord]) -> dict[str, Any]:
        """
        个人网盘文件取链（转存后使用）。

        :param fid: 转存后的文件标识
        :param account: 取链账号
        :return: 含 url/name/size 的字典
        """
        payload = self._post_json(
            _DOWNLOAD_URL,
            {"fids": [fid]},
            account=account,
            action="获取文件直链",
        )
        return self._first_link(payload, "获取文件直链")

    def _first_link(self, payload: Mapping[str, Any], action: str) -> dict[str, Any]:
        """
        从下载接口响应中取出第一条直链。

        :param payload: 响应 JSON
        :param action: 用于错误提示的动作名
        :return: 含 url/name/size 的字典
        """
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                f"{action}失败：未返回下载链接",
                platform=self.platform,
            )
        item = data[0] if isinstance(data[0], dict) else {}
        url = as_text(item.get("download_url"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                f"{action}失败：下载链接为空",
                platform=self.platform,
            )
        return {
            "url": url,
            "name": as_text(item.get("file_name") or item.get("filename")),
            "size": as_int(item.get("size")),
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

        :param link: 下载接口返回的直链信息
        :param file: 目标文件
        :param account: 取链账号
        :param handle: 转存清理句柄，免转存时为 None
        :return: 直链
        """
        headers = self.download_headers(account)
        headers["User-Agent"] = self.user_agent
        headers["Referer"] = self.referer
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

    def _create_folder(self, name: str, account: NetdiskAccountRecord) -> str:
        """
        在个人网盘根目录创建临时目录。

        :param name: 目录名
        :param account: 取链账号
        :return: 新目录 fid
        """
        payload = self._post_json(
            _FILE_URL,
            {
                "pdir_fid": "0",
                "file_name": name,
                "dir_path": "",
                "dir_init_lock": False,
            },
            account=account,
            action="创建临时目录",
        )
        fid = as_text((payload.get("data") or {}).get("fid"))
        if not fid:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建临时目录失败：响应缺少 fid",
                platform=self.platform,
            )
        return fid

    def _save_share(
        self,
        share_id: str,
        stoken: str,
        file: ShareFile,
        to_pdir_fid: str,
        account: NetdiskAccountRecord,
    ) -> str:
        """
        把分享文件转存到个人网盘目录。

        :param share_id: 分享码
        :param stoken: 分享访问令牌
        :param file: 目标文件
        :param to_pdir_fid: 目标目录标识
        :param account: 取链账号
        :return: 转存任务标识
        """
        payload = self._post_json(
            _SAVE_URL,
            {
                "pwd_id": share_id,
                "stoken": stoken,
                "pdir_fid": file.parent_id or "0",
                "to_pdir_fid": to_pdir_fid,
                "fid_list": [file.file_id],
                "fid_token_list": [file.token],
                "scene": "link",
            },
            account=account,
            action="转存分享文件",
        )
        task_id = as_text((payload.get("data") or {}).get("task_id"))
        if not task_id:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "转存分享文件失败：响应缺少 task_id",
                platform=self.platform,
            )
        return task_id

    def _poll_task(self, task_id: str, account: NetdiskAccountRecord) -> str:
        """
        轮询转存任务，返回转存后的新文件标识。

        :param task_id: 转存任务标识
        :param account: 取链账号
        :return: 转存后的新 fid
        """
        params = {"pr": "ucpro", "fr": "pc", "task_id": task_id, "retry_index": 0}
        for _ in range(_TASK_POLL_TIMES):
            try:
                payload = self._get_json(
                    _TASK_URL,
                    account=account,
                    params=params,
                    action="查询转存任务",
                )
            except NetdiskError:
                self.sleep(_TASK_POLL_INTERVAL)
                continue
            data = payload.get("data") or {}
            finished = (
                as_int(data.get("finished_at")) > 0
                or as_int(data.get("status")) == 2
                or as_int(data.get("task_status")) == 2
            )
            if finished:
                saved = (data.get("save_as") or {}).get("save_as_top_fids") or []
                fid = as_text(saved[0]) if saved else ""
                if fid:
                    return fid
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "转存完成但未返回新文件标识",
                    platform=self.platform,
                )
            self.sleep(_TASK_POLL_INTERVAL)
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "转存任务超时",
            platform=self.platform,
        )

    def _delete_files(self, fids: list[str], account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除个人网盘文件或目录。

        :param fids: 待删除的标识列表
        :param account: 取链账号
        """
        if not fids:
            return
        self._post_json(
            _DELETE_URL,
            {"action_type": 2, "filelist": list(fids), "exclude_fids": []},
            account=account,
            action="清理临时文件",
        )

    def _safe_delete(self, fids: list[str], account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除失败只记录，不影响主流程。

        :param fids: 待删除的标识列表
        :param account: 取链账号
        """
        try:
            self._delete_files(fids, account)
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载
            self.log_failure("清理转存目录", error, account)

    def _transfer_link(
        self,
        share_id: str,
        stoken: str,
        file: ShareFile,
        account: NetdiskAccountRecord,
    ) -> tuple[TransferHandle, dict[str, Any]]:
        """
        转存到唯一临时子目录后取链。

        :param share_id: 分享码
        :param stoken: 分享访问令牌
        :param file: 目标文件
        :param account: 取链账号
        :return: 清理句柄与直链信息
        """
        folder_name = f"{_TEMP_DIR_PREFIX}{int(time.time() * 1000)}"
        folder_fid = self._create_folder(folder_name, account)
        try:
            task_id = self._save_share(share_id, stoken, file, folder_fid, account)
            saved_fid = self._poll_task(task_id, account)
            link = self._file_link(saved_fid, account)
        except Exception:
            self._safe_delete([folder_fid], account)
            raise
        handle = TransferHandle(
            platform=self.platform,
            account_id=account.id,
            directory_id=folder_fid,
            file_ids=(saved_fid,),
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

        优先免转存取链；失败且允许转存时，转存到唯一临时子目录后再取链，
        并把临时目录标识放进清理句柄。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底
        :return: 直链
        """
        stoken, _ = self._stoken(ref, account)
        try:
            link = self._share_link(ref.share_id, stoken, file, account)
            return self._build_link(link, file, account, None)
        except NetdiskError as error:
            if not allow_transfer or account is None:
                raise
            self.log_failure("免转存取链", error, account)
        handle, link = self._transfer_link(ref.share_id, stoken, file, account)
        return self._build_link(link, file, account, handle)

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除转存产生的临时子目录；失败只记录，不抛出。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        if not handle.directory_id:
            return
        self._safe_delete([handle.directory_id], account)

    # ---- 账号 ----

    def _probe(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        调用夸克账号信息接口确认登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        try:
            payload = self._get_json(
                _ACCOUNT_INFO_URL,
                account=account,
                action="校验夸克登录态",
            )
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="夸克登录态已失效")
            return AccountProbe(status="unknown", message="无法确认夸克登录态")
        nickname = as_text((payload.get("data") or {}).get("nickname"))
        success = payload.get("success")
        if success is True:
            message = f"夸克登录态有效：{nickname}" if nickname else "夸克登录态有效"
            return AccountProbe(status="active", message=message)
        if success is False:
            return AccountProbe(status="expired", message="夸克登录态已失效")
        if as_text(payload.get("code")).upper() == "OK":
            message = f"夸克登录态有效：{nickname}" if nickname else "夸克登录态有效"
            return AccountProbe(status="active", message=message)
        return AccountProbe(status="expired", message="夸克登录态已失效")

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验一份新凭据是否可用。

        :param credential: 明文凭据字段
        :return: 探测结果
        """
        probe = super().validate_credential(credential)
        if probe.status != "active":
            return probe
        record = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="",
            credential=dict(credential),
        )
        return self._probe(record)

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从夸克账号信息接口读取昵称。

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
            payload = self._get_json(_ACCOUNT_INFO_URL, account=record, action="读取夸克账号信息")
        except NetdiskError:
            return ""
        data = payload.get("data")
        return as_text(data.get("nickname")) if isinstance(data, Mapping) else ""

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self._probe(account)

    # ---- 个人盘 ----

    _DRIVE_BASE = "https://drive-pc.quark.cn/1/clouddrive"

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询夸克空间。

        :param account: 账号
        :return: 已用/总空间
        """
        payload = self.http.get_json(
            f"{self._DRIVE_BASE}/member?pr=ucpro&fr=pc&fetch_subscribe=true&_ch=home",
            account=account,
        )
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        return DriveQuota(
            used=as_int(data.get("use_capacity")),
            total=as_int(data.get("total_capacity")),
        )

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        列出夸克个人盘目录。

        :param account: 账号
        :param parent_id: 父目录，空串为根
        :return: 目录条目
        """
        parent = parent_id or "0"
        payload = self.http.get_json(
            f"{self._DRIVE_BASE}/file/sort?pr=ucpro&fr=pc&uc_param_str=&pdir_fid={parent}"
            "&_page=1&_size=100&_fetch_total=1&_fetch_sub_dirs=0"
            "&_sort=file_type:asc,updated_at:desc&fetch_all_file=1&fetch_risk_file_name=1",
            account=account,
        )
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        raw_items = data.get("list")
        files: list[DriveFile] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, Mapping):
                continue
            files.append(
                DriveFile(
                    file_id=as_text(item.get("fid")),
                    name=as_text(item.get("file_name")) or as_text(item.get("fname")),
                    size=as_int(item.get("size")),
                    is_dir=bool(item.get("dir")),
                    parent_id=as_text(item.get("pdir_fid")),
                )
            )
        return files

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取夸克个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        payload = self.http.post_json(
            f"{self._DRIVE_BASE}/file/download?pr=ucpro&fr=pc&sys=win32&ve=3.23.2",
            account=account,
            json={"fids": [file.file_id]},
        )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        item = data[0] if data and isinstance(data[0], Mapping) else {}
        url = as_text(item.get("download_url"))
        if not url:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "夸克未返回下载直链", platform=self.platform)
        return DirectLink(
            url=url,
            headers=self.http.headers(account),
            size=as_int(item.get("size")) or file.size,
            account_id=account.id if account else 0,
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
        创建夸克分享并轮询异步任务。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码
        :param expires_in: 有效期秒数，0 为永久
        :return: 分享结果
        """
        expired_type = 1 if expires_in <= 0 else (2 if expires_in <= 86400 else 3 if expires_in <= 7 * 86400 else 4)
        payload = self.http.post_json(
            f"{self._DRIVE_BASE}/share?pr=ucpro&fr=pc&uc_param_str=",
            account=account,
            json={
                "fid_list": [item.file_id for item in files],
                "title": files[0].name if files else "分享文件",
                "url_type": 2 if password else 1,
                **({"passcode": password} if password else {}),
                "expired_type": expired_type,
                "support_error_code": ["41060"],
            },
        )
        raw = payload.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        task_id = as_text(data.get("task_id"))
        share_id = as_text(data.get("share_id"))
        for _ in range(15):
            if share_id:
                break
            if not task_id:
                break
            task = self.http.get_json(
                f"{self._DRIVE_BASE}/task?pr=ucpro&fr=pc&task_id={task_id}&retry_index=0",
                account=account,
            )
            raw_task = task.get("data")
            task_data: Mapping[str, Any] = raw_task if isinstance(raw_task, Mapping) else {}
            finished = as_int(task_data.get("finished_at")) > 0 or as_int(task_data.get("status")) == 2
            share_id = as_text(task_data.get("share_id")) if finished else ""
            if not share_id:
                time.sleep(1)
        if not share_id:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "夸克分享创建超时", platform=self.platform)
        return DriveShare(url=f"https://pan.quark.cn/s/{share_id}", password=password)

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动夸克文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录
        """
        self.http.post_json(
            f"{self._DRIVE_BASE}/file/move?pr=ucpro&fr=pc&uc_param_str=",
            account=account,
            json={
                "action_type": 1,
                "to_pdir_fid": target_id or "0",
                "filelist": [item.file_id for item in files],
                "exclude_fids": [],
            },
        )

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名夸克文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        self.http.post_json(
            f"{self._DRIVE_BASE}/file/rename?pr=ucpro&fr=pc&uc_param_str=",
            account=account,
            json={"fid": file.file_id, "file_name": name},
        )

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除夸克文件。

        :param account: 账号
        :param files: 文件列表
        """
        self.http.post_json(
            f"{self._DRIVE_BASE}/file/delete?pr=ucpro&fr=pc&uc_param_str=",
            account=account,
            json={
                "action_type": 2,
                "filelist": [item.file_id for item in files],
                "exclude_fids": [],
            },
        )

    def refresh_account(self, account: NetdiskAccountRecord) -> Optional[Mapping[str, str]]:
        """
        刷新会话令牌：剥离 __puus 后请求配置接口，合并响应下发的新令牌。

        :param account: 账号记录
        :return: 新凭据；没有新令牌时返回 None
        """
        cookie = cookie_header(account)
        if not cookie:
            return None
        stripped = _strip_puus(cookie)
        record = NetdiskAccountRecord(
            id=account.id,
            platform=account.platform,
            name=account.name,
            credential={"cookie": stripped},
            is_primary=account.is_primary,
            status=account.status,
            expires_at=account.expires_at,
        )
        self._get_json(_CONFIG_URL, account=record, action="刷新夸克会话")
        merged = _merge_cookie(cookie, getattr(self.http, "last_set_cookie", ""))
        if merged == cookie:
            return None
        return {"cookie": merged}
