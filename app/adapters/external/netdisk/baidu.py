"""
百度网盘的分享解析、目录读取与转存取链适配器。

协议依据 YunX `BaiduResolveRepository` / `BaiduApi`：分享提取码经 `share/verify`
换取 `randsk`（作为 `sekey`），目录列表走 `xpan/share?method=list`；百度分享必须
转存后才能取链，转存到唯一临时目录后调用 `locatedownload` 拿 appall 明文直链。

appall 直链自带签名与过期时间，删除转存文件后仍可下载，因此取链成功后立即清理，
同时把清理句柄交给用例层，下载完成后再兜底清理一次。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from app.adapters.external.netdisk.base import (
    BaseNetdiskAdapter,
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

# 百度网盘统一 app_id。
_APP_ID = "250528"
# PC 网页 UA：分享校验、目录列表、账号信息接口使用。
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
# 手机端客户端 UA：locatedownload 与直链下载必须使用，否则拿不到高速链。
_CLIENT_UA = "netdisk;12.24.6;piano;android-android;16;JSbridge4.4.0;jointBridge;1.1.0"
# 临时转存目录名前缀，后面拼接时间戳与随机串，避免平台按目录去重。
_TEMP_DIR_PREFIX = "MoviePilot临时转存"

# 分享提取码校验。
_VERIFY_URL = "https://pan.baidu.com/share/verify"
# 分享目录列表。
_SHARE_LIST_URL = "https://pan.baidu.com/rest/2.0/xpan/share"
# bdstoken 模板变量。
_TEMPLATE_URL = "https://pan.baidu.com/api/gettemplatevariable"
# 账号信息（登录态与昵称）：xpan 接口只认 BDUSS，网页模板接口在部分会话下会返回 -6。
_UINFO_URL = "https://pan.baidu.com/rest/2.0/xpan/nas"
# 新建目录。
_CREATE_URL = "https://pan.baidu.com/api/create"
# 分享转存。
_TRANSFER_URL = "https://pan.baidu.com/share/transfer"
# 高速下载直链。
_LOCATE_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/file"
# 个人网盘文件删除。
_DELETE_URL = "https://pan.baidu.com/api/filemanager"
# 网盘空间配额。
_QUOTA_URL = "https://pan.baidu.com/api/quota"
# 个人网盘目录列表。
_LIST_URL = "https://pan.baidu.com/api/list"
# 创建分享。
_SHARE_SET_URL = "https://pan.baidu.com/share/set"

# locatedownload 抓包常量：psign 固定，rand/devuid/cuid/deviceid 在登录态下可复用。
_LOCATE_RAND = "5ed606e9da222cde0474cdf70eda884b"
_LOCATE_DEVUID = "0F1E9FC2E084472DA5A61C4CF4C759AF"
_LOCATE_DEVICEID = "348642637967375013"
_LOCATE_PSIGN = "860a071f77c860e8cea06e4e54c518f3"

# 分享列表单页条数与最大翻页次数。
_PAGE_LIMIT = 100
_MAX_PAGES = 100


def _errno(payload: Mapping[str, Any]) -> int:
    """
    读取百度响应中的 errno。

    :param payload: 响应 JSON
    :return: 业务错误码，缺失或非法时为 0
    """
    try:
        return int(payload.get("errno", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _classify(errno: int, message: str) -> NetdiskErrorCode:
    """
    把百度 errno 与提示收敛为统一错误分类。

    提示中的「空间」「验证码」等关键词优先于通用错误码，避免把配额不足误报成转存失败。

    :param errno: 百度业务错误码
    :param message: 服务端提示
    :return: 统一错误分类
    """
    lower = message.lower()
    if "空间" in message or "quota" in lower or "insufficient" in lower:
        return NetdiskErrorCode.SPACE_INSUFFICIENT
    if any(word in message for word in ("验证码", "风控", "安全验证", "设备异常")):
        return NetdiskErrorCode.RISK_BLOCKED
    if errno == -6:
        return NetdiskErrorCode.AUTH_EXPIRED
    if errno in (8888, 429):
        return NetdiskErrorCode.RATE_LIMITED
    if errno == -12:
        return NetdiskErrorCode.SHARE_INVALID
    if errno in (-20, 403, 31066):
        return NetdiskErrorCode.SHARE_INVALID
    return NetdiskErrorCode.UNKNOWN


def _credential_cookie(credential: Mapping[str, str]) -> str:
    """
    把凭据字段拼成 Cookie 头。

    兼容 `{"cookie": "BDUSS=..."}` 与 `{"BDUSS": "..."}` 两种录入形态。

    :param credential: 明文凭据字段
    :return: Cookie 头文本
    """
    for key in ("cookie", "cookie_header", "cookies"):
        value = as_text(credential.get(key))
        if value:
            return value
    return "; ".join(
        f"{as_text(key)}={as_text(value)}"
        for key, value in credential.items()
        if as_text(key) and as_text(value)
    )


def _parse_files(payload: Mapping[str, Any]) -> list[ShareFile]:
    """
    把分享列表响应映射为统一文件条目。

    目录以完整路径作为标识（百度子目录列表使用路径导航），文件以 `fs_id` 作为标识。

    :param payload: 分享列表响应
    :return: 文件与目录条目
    """
    files: list[ShareFile] = []
    for item in payload.get("list") or []:
        if not isinstance(item, Mapping):
            continue
        is_dir = as_text(item.get("isdir")) == "1"
        path = as_text(item.get("path"))
        parent = path.rsplit("/", 1)[0] or "/"
        files.append(
            ShareFile(
                file_id=path if is_dir else as_text(item.get("fs_id")),
                name=as_text(item.get("server_filename")),
                size=as_int(item.get("size")),
                is_dir=is_dir,
                parent_id=parent,
                token=path,
            )
        )
    return files


class BaiduAdapter(BaseNetdiskAdapter):
    """
    百度网盘协议适配器。

    分享必须转存后取链：先做空间预算校验，再转存到唯一临时目录，
    最后用 locatedownload 换取 appall 明文直链并返回清理句柄。
    """

    platform = NetdiskPlatform.BAIDU
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
    max_concurrency = 512
    cookie_required_keys = ("BDUSS",)
    cookie_domains = (".baidu.com",)
    login_url = "https://pan.baidu.com/"
    supports_password_login = False
    user_agent = _WEB_UA
    referer = "https://pan.baidu.com/"

    def __init__(self) -> None:
        """
        初始化共享 HTTP 客户端与分享级缓存。
        """
        super().__init__()
        # 分享码 -> 提取码校验得到的 sekey。
        self._secrets: dict[str, str] = {}
        # 分享码 -> (share_id, uk)，转存必需。
        self._share_infos: dict[str, tuple[str, str]] = {}
        # 账号主键 -> bdstoken。
        self._tokens: dict[int, str] = {}
        # (账号主键, 转存后 fs_id) -> 转存后完整路径，供清理使用。
        self._transfer_paths: dict[tuple[int, str], str] = {}

    # ---- 凭据 ----

    @staticmethod
    def _require_cookie(account: Optional[NetdiskAccountRecord]) -> str:
        """
        读取并校验账号 Cookie。

        :param account: 账号记录
        :return: 含 BDUSS 的 Cookie 头
        :raises NetdiskError: 账号缺失或 Cookie 不含 BDUSS 时抛出 `AUTH_EXPIRED`
        """
        if account is None:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "百度网盘取链需要登录账号",
                platform=NetdiskPlatform.BAIDU,
            )
        cookie = cookie_header(account)
        if "BDUSS=" not in cookie:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "百度网盘登录态缺失，请重新登录",
                platform=NetdiskPlatform.BAIDU,
                account_name=account.name,
            )
        return cookie

    def _check(self, payload: Mapping[str, Any], action: str) -> None:
        """
        校验百度业务响应码并映射为统一错误。

        :param payload: 响应 JSON
        :param action: 用于错误提示的动作名
        :raises NetdiskError: errno 非 0 时抛出
        """
        errno = _errno(payload)
        if errno == 0:
            return
        message = as_text(payload.get("err_msg")) or as_text(payload.get("show_msg")) or action
        raise NetdiskError(
            _classify(errno, message),
            f"{action}失败：{message}（errno={errno}）",
            platform=self.platform,
        )

    def _get_json(
        self,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord],
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """
        发起 GET 请求，补齐百度网页 UA。

        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param headers: 额外请求头
        :return: 响应 JSON
        """
        extra = {"User-Agent": _WEB_UA}
        if headers:
            extra.update({str(key): str(value) for key, value in headers.items()})
        return self.http.get_json(url, account=account, params=params, headers=extra)

    def _post_json(
        self,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord],
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """
        发起 POST 请求，补齐百度网页 UA。

        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param data: 表单请求体
        :param headers: 额外请求头
        :return: 响应 JSON
        """
        extra = {"User-Agent": _WEB_UA}
        if headers:
            extra.update({str(key): str(value) for key, value in headers.items()})
        return self.http.post_json(
            url,
            account=account,
            params=params,
            data=data,
            headers=extra,
        )

    # ---- 分享解析 ----

    def _secret(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> str:
        """
        获取分享的 sekey：优先使用调用方透传的分享令牌，其次校验提取码。

        :param ref: 分享引用
        :param account: 取链账号
        :return: sekey；公共分享无提取码时为空串
        :raises NetdiskError: 提取码错误或接口失败时抛出
        """
        if ref.share_token:
            return ref.share_token
        cached = self._secrets.get(ref.share_id)
        if cached is not None:
            return cached
        password = as_text(ref.password)
        if not password:
            self._secrets[ref.share_id] = ""
            return ""
        cookie = cookie_header(account)
        headers: dict[str, str] = {
            "Referer": f"https://pan.baidu.com/s/{ref.share_id}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if cookie:
            headers["Cookie"] = cookie
        payload = self._post_json(
            _VERIFY_URL,
            account=account,
            params={"surl": ref.share_id},
            data={"pwd": password, "vcode_str": "", "vcode": ""},
            headers=headers,
        )
        if _errno(payload) in (-12, 200025):
            # 带账号 Cookie 校验可能被风控拒绝（同一提取码公开校验可通过），
            # 去掉 Cookie 重试一次；分享密码本身正确时仍能拿到 randsk。
            retry_headers = {
                key: value for key, value in headers.items() if key.lower() != "cookie"
            }
            payload = self._post_json(
                _VERIFY_URL,
                account=None,
                params={"surl": ref.share_id},
                data={"pwd": password, "vcode_str": "", "vcode": ""},
                headers=retry_headers,
            )
        if _errno(payload) == -12:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "百度网盘提取码错误",
                platform=self.platform,
            )
        self._check(payload, "验证百度网盘提取码")
        sekey = as_text(payload.get("randsk"))
        if not sekey:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "百度网盘未返回分享密钥",
                platform=self.platform,
            )
        self._secrets[ref.share_id] = sekey
        return sekey

    def _list_page(
        self,
        share_id: str,
        sekey: str,
        directory: str,
        page: int,
        account: Optional[NetdiskAccountRecord],
    ) -> tuple[list[ShareFile], dict[str, Any]]:
        """
        读取一页分享目录。

        顶层使用 `root=1`，子目录使用 `root=0`；子目录与转存必须携带
        `BDCLND=<sekey>` Cookie，否则百度返回 errno=2。

        :param share_id: 分享码
        :param sekey: 分享密钥
        :param directory: 目录路径，根目录为 `/`
        :param page: 页码，从 1 开始
        :param account: 取链账号
        :return: `(条目列表, 原始响应)`
        :raises NetdiskError: 分享失效、缺少提取码或接口失败时抛出
        """
        is_root = directory in ("", "/")
        params: dict[str, Any] = {
            "method": "list",
            "shorturl": share_id,
            "page": page,
            "num": _PAGE_LIMIT,
            "root": "1" if is_root else "0",
            "dir": directory or "/",
        }
        if sekey:
            params["sekey"] = sekey
        cookie = cookie_header(account)
        if sekey and "BDCLND=" not in cookie:
            cookie = f"{cookie}; BDCLND={sekey}" if cookie else f"BDCLND={sekey}"
        headers: dict[str, str] = {
            "Referer": f"https://pan.baidu.com/s/{share_id}",
        }
        if cookie:
            headers["Cookie"] = cookie
        payload = self._get_json(_SHARE_LIST_URL, account=account, params=params, headers=headers)
        errno = _errno(payload)
        if errno != 0 and not sekey:
            if errno == -6:
                raise NetdiskError(
                    NetdiskErrorCode.AUTH_EXPIRED,
                    "百度网盘登录态已失效",
                    platform=self.platform,
                )
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                f"该百度分享需要提取码（errno={errno}）",
                platform=self.platform,
            )
        self._check(payload, "获取百度网盘分享文件列表")
        return _parse_files(payload), dict(payload)

    def _share_info(
        self,
        ref: ShareRef,
        sekey: str,
        account: Optional[NetdiskAccountRecord],
    ) -> tuple[str, str]:
        """
        读取转存所需的 `share_id` 与上传者 `uk`。

        :param ref: 分享引用
        :param sekey: 分享密钥
        :param account: 取链账号
        :return: `(share_id, uk)`
        :raises NetdiskError: 响应缺少 uk 时抛出
        """
        cached = self._share_infos.get(ref.share_id)
        if cached and cached[1]:
            return cached
        _files, payload = self._list_page(ref.share_id, sekey, "/", 1, account)
        share_id = as_text(payload.get("share_id")) or ref.share_id
        uk = as_text(payload.get("uk"))
        if not uk:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "百度网盘分享信息缺少 uk",
                platform=self.platform,
            )
        self._share_infos[ref.share_id] = (share_id, uk)
        return share_id, uk

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        读取分享标题与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情，标题回退为分享码
        """
        sekey = self._secret(ref, account)
        files, payload = self._list_page(ref.share_id, sekey, "/", 1, account)
        self._share_infos[ref.share_id] = (
            as_text(payload.get("share_id")) or ref.share_id,
            as_text(payload.get("uk")),
        )
        return ShareDetail(
            title=as_text(payload.get("title")) or ref.share_id,
            size=sum(item.size for item in files),
            files=tuple(files),
            stoken=sekey,
        )

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        分页读取分享目录，直到条目不足一页或达到翻页上限。

        :param ref: 分享引用
        :param parent_id: 父目录路径，根目录为空或 `/`
        :param account: 取链账号
        :return: 目录内全部条目
        """
        sekey = self._secret(ref, account)
        directory = parent_id or "/"
        collected: list[ShareFile] = []
        for page in range(1, _MAX_PAGES + 1):
            files, payload = self._list_page(ref.share_id, sekey, directory, page, account)
            self._share_infos[ref.share_id] = (
                as_text(payload.get("share_id")) or ref.share_id,
                as_text(payload.get("uk")),
            )
            collected.extend(files)
            if len(files) < _PAGE_LIMIT:
                break
        return collected

    # ---- 转存与取链 ----

    def _ensure_space(
        self,
        file: ShareFile,
        account: NetdiskAccountRecord,
    ) -> None:
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
            payload = self._get_json(
                _QUOTA_URL,
                account=account,
                params={"clienttype": 0, "app_id": _APP_ID, "web": 1, "channel": "chunlei"},
            )
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                raise
            self.log_failure("查询百度网盘空间", error, account)
            return
        if _errno(payload) != 0:
            return
        total = as_int(payload.get("total"))
        used = as_int(payload.get("used"))
        free = max(total - used, 0)
        if total > 0 and free < file.size:
            raise NetdiskError(
                NetdiskErrorCode.SPACE_INSUFFICIENT,
                f"百度网盘剩余空间不足：需要 {file.size} 字节，当前可用 {free} 字节",
                platform=self.platform,
                account_name=account.name,
            )

    def _bdstoken(self, account: NetdiskAccountRecord) -> str:
        """
        获取写操作所需的 bdstoken。

        :param account: 取链账号
        :return: bdstoken
        :raises NetdiskError: 未返回 bdstoken 时抛出 `AUTH_EXPIRED`
        """
        cached = self._tokens.get(account.id)
        if cached:
            return cached
        payload = self._get_json(
            _TEMPLATE_URL,
            account=account,
            params={
                "clienttype": 0,
                "app_id": _APP_ID,
                "web": 1,
                "fields": '["bdstoken"]',
            },
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://pan.baidu.com/disk/main"},
        )
        self._check(payload, "获取百度网盘 bdstoken")
        result = payload.get("result")
        token = as_text(result.get("bdstoken")) if isinstance(result, Mapping) else ""
        if not token:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "获取百度网盘 bdstoken 失败，请重新登录",
                platform=self.platform,
                account_name=account.name,
            )
        self._tokens[account.id] = token
        return token

    def _create_temp_dir(self, account: NetdiskAccountRecord) -> str:
        """
        在个人网盘根目录创建唯一临时目录。

        :param account: 取链账号
        :return: 临时目录完整路径
        :raises NetdiskError: 创建失败时抛出
        """
        bdstoken = self._bdstoken(account)
        name = f"{_TEMP_DIR_PREFIX}{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        path = f"/{name}"
        payload = self._post_json(
            _CREATE_URL,
            account=account,
            params={
                "a": "commit",
                "channel": "chunlei",
                "web": 1,
                "app_id": _APP_ID,
                "clienttype": 0,
                "bdstoken": bdstoken,
            },
            data={
                "path": path,
                "isdir": 1,
                "size": "",
                "block_list": "[]",
                "method": "post",
                "dataType": "json",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://pan.baidu.com/disk/main",
            },
        )
        errno = _errno(payload)
        if errno not in (0, -8):
            self._check(payload, "创建百度网盘临时目录")
        return path

    def _transfer(
        self,
        ref: ShareRef,
        file: ShareFile,
        to_dir: str,
        sekey: str,
        account: NetdiskAccountRecord,
    ) -> tuple[str, str]:
        """
        把分享文件转存到临时目录。

        :param ref: 分享引用
        :param file: 目标文件
        :param to_dir: 临时目录完整路径
        :param sekey: 分享密钥
        :param account: 取链账号
        :return: `(转存后 fs_id, 转存后完整路径)`
        :raises NetdiskError: 转存失败时抛出
        """
        bdstoken = self._bdstoken(account)
        share_id, uk = self._share_info(ref, sekey, account)
        cookie = cookie_header(account)
        if sekey and "BDCLND=" not in cookie:
            cookie = f"{cookie}; BDCLND={sekey}" if cookie else f"BDCLND={sekey}"
        payload = self._post_json(
            _TRANSFER_URL,
            account=account,
            params={
                "shareid": share_id,
                "from": uk,
                "channel": "chunlei",
                "sekey": sekey,
                "ondup": "newcopy",
                "web": 1,
                "app_id": _APP_ID,
                "bdstoken": bdstoken,
                "clienttype": 0,
            },
            data={
                "fsidlist": json.dumps([file.file_id]),
                "path": to_dir,
            },
            headers={
                "Cookie": cookie,
                "Origin": "https://pan.baidu.com",
                "Referer": "https://pan.baidu.com/s/",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        self._check(payload, "转存百度网盘分享文件")
        extra = payload.get("extra")
        entries = extra.get("list") if isinstance(extra, Mapping) else None
        first = entries[0] if isinstance(entries, list) and entries else None
        new_id = as_text(first.get("to_fs_id")) if isinstance(first, Mapping) else ""
        new_path = as_text(first.get("to")) if isinstance(first, Mapping) else ""
        if not new_id:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "百度网盘转存完成但未返回新文件标识",
                platform=self.platform,
            )
        return new_id, new_path or f"{to_dir}/{file.name}"

    def _locate_download(self, path: str, account: NetdiskAccountRecord) -> str:
        """
        调用 locatedownload 换取 appall 明文直链。

        :param path: 转存后文件完整路径
        :param account: 取链账号
        :return: 直链地址
        :raises NetdiskError: 接口失败或未返回可用直链时抛出
        """
        payload = self._post_json(
            _LOCATE_URL,
            account=account,
            params={
                "method": "locatedownload",
                "app_id": _APP_ID,
                "clienttype": 17,
                "ver": "4.0",
                "ant": 1,
                "check_blue": 1,
                "es": 1,
                "esl": 1,
                "apn_id": "1_-1",
                "freeisp": 0,
                "queryfree": 0,
                "use": 1,
                "dtype": 1,
                "eck": 1,
                "ehps": 1,
                "err_ver": "1.0",
                "network_type": "WIFI",
                "channel": 0,
                "path": path,
                "time": int(time.time()),
                "rand": _LOCATE_RAND,
                "devuid": _LOCATE_DEVUID,
                "cuid": _LOCATE_DEVUID,
                "deviceid": _LOCATE_DEVICEID,
                "psign": _LOCATE_PSIGN,
                "version": "2.2.111.34",
                "version_app": "12.24.6",
                "vip": 0,
            },
            data={},
            headers={
                "User-Agent": _CLIENT_UA,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self._check(payload, "获取百度网盘高速下载链接")
        candidates = [
            item
            for item in payload.get("urls") or []
            if isinstance(item, Mapping) and as_text(item.get("url"))
        ]
        plain = [item for item in candidates if as_int(item.get("encrypt")) == 0]
        ordered = sorted(
            plain or candidates,
            key=lambda item: (as_int(item.get("encrypt")) != 0, not as_text(item.get("url")).startswith("https")),
        )
        url = as_text(ordered[0].get("url")) if ordered else ""
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "百度网盘未返回可用下载链接",
                platform=self.platform,
            )
        return url

    def _delete_paths(
        self,
        paths: list[str],
        account: NetdiskAccountRecord,
    ) -> None:
        """
        按完整路径删除个人网盘文件或目录。

        :param paths: 待删除路径
        :param account: 取链账号
        :raises NetdiskError: 删除失败时抛出
        """
        if not paths:
            return
        bdstoken = self._bdstoken(account)
        payload = self._post_json(
            _DELETE_URL,
            account=account,
            params={
                "async": 2,
                "onnest": "fail",
                "opera": "delete",
                "bdstoken": bdstoken,
                "newVerify": 1,
                "clienttype": 0,
                "app_id": _APP_ID,
                "web": 1,
            },
            data={"filelist": json.dumps(paths, ensure_ascii=False)},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://pan.baidu.com/disk/main",
            },
        )
        self._check(payload, "删除百度网盘转存文件")

    def _safe_delete(
        self,
        paths: list[str],
        account: Optional[NetdiskAccountRecord],
    ) -> None:
        """
        清理失败只记录，不影响主流程。

        :param paths: 待删除路径
        :param account: 取链账号
        """
        if account is None or not paths:
            return
        try:
            self._delete_paths(paths, account)
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载
            self.log_failure("清理百度网盘转存内容", error, account)

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

        百度分享无法免转存取链，`allow_transfer` 为假时直接返回 `UNSUPPORTED`。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存
        :param transfer_first: 是否优先转存；本平台始终转存，参数仅对齐接口
        :return: 直链及其 BDUSS Cookie、手机端 UA 与清理句柄
        :raises NetdiskError: 空间不足、登录态失效或接口失败时抛出
        """
        if file.is_dir:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "百度网盘不支持直接下载目录",
                platform=self.platform,
            )
        if not allow_transfer:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "百度网盘必须转存后才能取链",
                platform=self.platform,
            )
        cookie = self._require_cookie(account)
        assert account is not None
        self._ensure_space(file, account)
        sekey = self._secret(ref, account)
        # 先确认分享信息可用，再创建临时目录，避免取不到 uk 时留下空目录。
        self._share_info(ref, sekey, account)
        directory = self._create_temp_dir(account)
        try:
            new_id, new_path = self._transfer(ref, file, directory, sekey, account)
            url = self._locate_download(new_path, account)
        except Exception:
            self._safe_delete([directory], account)
            raise
        self._transfer_paths[(account.id, new_id)] = new_path
        handle = TransferHandle(
            platform=self.platform,
            account_id=account.id,
            directory_id=directory,
            file_ids=(new_id,),
        )
        return DirectLink(
            url=url,
            headers=self.download_headers(account),
            size=file.size,
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
        paths: list[str] = []
        for file_id in handle.file_ids:
            path = self._transfer_paths.pop((handle.account_id, file_id), "")
            if path:
                paths.append(path)
        if handle.directory_id:
            paths.append(handle.directory_id)
        self._safe_delete(paths, account)

    def download_headers(
        self,
        account: Optional[NetdiskAccountRecord],
        *,
        extra: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """
        组装直链下载请求头。

        百度 appall 直链必须携带 BDUSS Cookie 与手机端 UA，否则会被防盗链拦截。

        :param account: 取链账号
        :param extra: 额外请求头
        :return: 请求头
        :raises NetdiskError: 账号 Cookie 缺少 BDUSS 时抛出 `AUTH_EXPIRED`
        """
        cookie = self._require_cookie(account)
        headers = self.http.headers(None, include_cookie=False)
        headers["Cookie"] = cookie
        headers["User-Agent"] = _CLIENT_UA
        headers["Referer"] = "https://pan.baidu.com/"
        if extra:
            headers.update({str(key): str(value) for key, value in extra.items()})
        return headers

    # ---- 个人盘 ----

    @staticmethod
    def _path(file: DriveFile) -> str:
        """
        还原个人盘文件的完整路径。

        百度目录以完整路径作为标识，文件以 `fs_id` 作为标识；写操作与 `locatedownload`
        都按路径定位，因此文件路径由父目录与文件名拼接。

        :param file: 文件条目
        :return: 完整路径
        """
        if file.is_dir and file.file_id.startswith("/"):
            return file.file_id
        if file.name:
            return f"{file.parent_id.rstrip('/')}/{file.name}"
        return file.file_id

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询百度网盘空间。

        :param account: 账号
        :return: 已用/总空间
        """
        self._require_cookie(account)
        payload = self._get_json(
            _QUOTA_URL,
            account=account,
            params={
                "clienttype": 0,
                "app_id": _APP_ID,
                "web": 1,
                "channel": "chunlei",
                "version": int(time.time()),
            },
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://pan.baidu.com/disk/main"},
        )
        self._check(payload, "查询百度网盘空间")
        return DriveQuota(used=as_int(payload.get("used")), total=as_int(payload.get("total")))

    def _list_cloud_page(
        self,
        directory: str,
        page: int,
        account: NetdiskAccountRecord,
    ) -> tuple[list[DriveFile], int]:
        """
        读取个人盘目录的一页。

        `bdstoken` 失效（errno=-6）时清缓存重取并重试一次。

        :param directory: 目录完整路径
        :param page: 页码，从 1 开始
        :param account: 账号
        :return: `(目录条目, 服务端返回的原始条数)`
        """
        params: dict[str, Any] = {
            "clienttype": 0,
            "app_id": _APP_ID,
            "web": 1,
            "channel": "chunlei",
            "showempty": 0,
            "order": "time",
            "desc": 1,
            "dir": directory,
            "num": _PAGE_LIMIT,
            "page": page,
            "bdstoken": self._bdstoken(account),
        }
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": "https://pan.baidu.com/disk/main"}
        payload = self._get_json(_LIST_URL, account=account, params=params, headers=headers)
        if _errno(payload) == -6:
            self._tokens.pop(account.id, None)
            params["bdstoken"] = self._bdstoken(account)
            payload = self._get_json(_LIST_URL, account=account, params=params, headers=headers)
        self._check(payload, "获取百度网盘文件列表")
        raw_items = payload.get("list")
        items = raw_items if isinstance(raw_items, list) else []
        files: list[DriveFile] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            is_dir = as_text(item.get("isdir")) == "1"
            path = as_text(item.get("path"))
            files.append(
                DriveFile(
                    file_id=path if is_dir else as_text(item.get("fs_id")),
                    name=as_text(item.get("server_filename")),
                    size=as_int(item.get("size")),
                    is_dir=is_dir,
                    parent_id=directory,
                )
            )
        return files, len(items)

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        分页列出百度个人盘目录。

        :param account: 账号
        :param parent_id: 父目录完整路径，空串为根目录
        :return: 目录条目
        """
        self._require_cookie(account)
        directory = parent_id or "/"
        collected: list[DriveFile] = []
        for page in range(1, _MAX_PAGES + 1):
            files, count = self._list_cloud_page(directory, page, account)
            collected.extend(files)
            if count < _PAGE_LIMIT:
                break
        return collected

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取百度个人盘文件直链。

        `locatedownload` 只接受完整路径，且必须携带手机端 UA 才能拿到 appall 明文直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        if file.is_dir:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "百度网盘不支持直接下载目录",
                platform=self.platform,
            )
        self._require_cookie(account)
        url = self._locate_download(self._path(file), account)
        return DirectLink(
            url=url,
            headers=self.download_headers(account),
            size=file.size,
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
        创建百度网盘分享。

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码
        :param expires_in: 有效期秒数，0 为永久
        :return: 分享结果
        """
        if not files:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "百度网盘分享至少需要一个文件",
                platform=self.platform,
            )
        self._require_cookie(account)
        bdstoken = self._bdstoken(account)
        period = 0
        if expires_in > 0:
            period = 1 if expires_in <= 86400 else 7 if expires_in <= 7 * 86400 else 30
        payload = self._post_json(
            _SHARE_SET_URL,
            account=account,
            params={
                "channel": "chunlei",
                "web": 1,
                "app_id": _APP_ID,
                "bdstoken": bdstoken,
                "clienttype": 0,
            },
            data={
                "fid_list": json.dumps([item.file_id for item in files]),
                "schannel": 4,
                "channel_list": "[]",
                "period": period,
                "pwd": password,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://pan.baidu.com/disk/main",
            },
        )
        self._check(payload, "创建百度网盘分享")
        link = as_text(payload.get("shorturl")) or as_text(payload.get("link"))
        if not link:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "创建百度网盘分享失败：响应缺少分享链接",
                platform=self.platform,
            )
        url = link if link.startswith("http") else f"https://pan.baidu.com/s/{link}"
        return DriveShare(url=url, password=password)

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动百度个人盘文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录完整路径
        """
        if not files:
            return
        self._require_cookie(account)
        items = [
            {"path": self._path(item), "dest": target_id or "/", "newname": item.name}
            for item in files
        ]
        self._filemanager("move", items, account, "移动百度网盘文件", async_op=2)

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名百度个人盘文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        self._require_cookie(account)
        self._filemanager(
            "rename",
            [{"path": self._path(file), "newname": name}],
            account,
            "重命名百度网盘文件",
            async_op=0,
        )

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除百度个人盘文件。

        :param account: 账号
        :param files: 文件列表
        """
        if not files:
            return
        self._require_cookie(account)
        bdstoken = self._bdstoken(account)
        payload = self._post_json(
            _DELETE_URL,
            account=account,
            params={
                "async": 2,
                "onnest": "fail",
                "opera": "delete",
                "bdstoken": bdstoken,
                "newVerify": 1,
                "clienttype": 0,
                "app_id": _APP_ID,
                "web": 1,
            },
            data={"filelist": json.dumps([self._path(item) for item in files], ensure_ascii=False)},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://pan.baidu.com/disk/main",
            },
        )
        self._check(payload, "删除百度网盘文件")

    def _filemanager(
        self,
        opera: str,
        items: Sequence[Mapping[str, Any]],
        account: NetdiskAccountRecord,
        action: str,
        *,
        async_op: int,
    ) -> None:
        """
        调用百度 `filemanager` 完成移动或重命名。

        :param opera: 操作名，`move` 或 `rename`
        :param items: `filelist` 条目
        :param account: 账号
        :param action: 用于错误提示的动作名
        :param async_op: `async` 参数，移动为 2，重命名为 0
        """
        bdstoken = self._bdstoken(account)
        payload = self._post_json(
            _DELETE_URL,
            account=account,
            params={
                "async": async_op,
                "onnest": "fail",
                "opera": opera,
                "bdstoken": bdstoken,
                "clienttype": 0,
                "app_id": _APP_ID,
                "web": 1,
            },
            data={"filelist": json.dumps(list(items), ensure_ascii=False)},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://pan.baidu.com/disk/main",
            },
        )
        self._check(payload, action)

    # ---- 账号 ----

    def _username(self, cookie: str, account: Optional[NetdiskAccountRecord] = None) -> str:
        """
        调用模板变量接口读取账号昵称，用于确认网页会话是否可用。

        传入账号时把响应 `Set-Cookie`（百度会轮换 `PANPSC`/`STOKEN`）回写，
        否则探测一次就会让已保存的会话过期。

        :param cookie: Cookie 头
        :param account: 账号记录；探测已保存账号时传入以启用 Cookie 回写
        :return: 昵称；接口未返回时为空串
        :raises NetdiskError: 登录态失效或接口失败时抛出
        """
        payload = self.http.get_json(
            _TEMPLATE_URL,
            account=account,
            params={
                "clienttype": 0,
                "app_id": _APP_ID,
                "web": 1,
                "fields": '["username"]',
            },
            headers={"Cookie": cookie, "User-Agent": _WEB_UA},
        )
        self._check(payload, "校验百度网盘登录态")
        result = payload.get("result")
        return as_text(result.get("username")) if isinstance(result, Mapping) else ""

    def _nickname(self, cookie: str, account: Optional[NetdiskAccountRecord] = None) -> str:
        """
        调用 xpan 账号信息接口读取昵称。

        `xpan/nas?method=uinfo` 只依赖 BDUSS，即使网页会话（STOKEN/bdstoken）已失效
        也能返回昵称，适合作为展示名来源。

        :param cookie: Cookie 头
        :param account: 账号记录；传入时允许回写响应 Cookie
        :return: 昵称；接口未返回时为空串
        :raises NetdiskError: 接口失败时抛出
        """
        payload = self.http.get_json(
            _UINFO_URL,
            account=account,
            params={"method": "uinfo"},
            headers={"Cookie": cookie, "User-Agent": _WEB_UA},
        )
        self._check(payload, "读取百度网盘昵称")
        return as_text(payload.get("netdisk_name")) or as_text(payload.get("baidu_name"))

    def login_ready(self, credential: Mapping[str, str]) -> bool:
        """
        判断百度网页登录是否已拿到可进入校验的登录态。

        百度登录页一打开就会写入 `BAIDUID` 等匿名 Cookie，只有 `BDUSS` 与
        `STOKEN` 同时出现才代表网页会话完整；只抓 BDUSS 会得到只能调 xpan、
        无法调网页接口（bdstoken/目录/配额）的半截会话。

        :param credential: 浏览器抓到的凭据
        :return: 是否已拿到完整的网页登录态
        """
        cookie = _credential_cookie(credential)
        return "BDUSS=" in cookie and "STOKEN=" in cookie

    def validate_credential(
        self,
        credential: Mapping[str, str],
        account: Optional[NetdiskAccountRecord] = None,
    ) -> AccountProbe:
        """
        校验凭据：先检查 BDUSS，再调用账号信息接口确认。

        :param credential: 明文凭据字段，支持 `{"cookie": ...}` 或 `{"BDUSS": ...}`
        :param account: 账号记录；探测已保存账号时传入以回写轮换后的 Cookie
        :return: 探测结果；接口不可用时返回 `unknown`
        """
        cookie = _credential_cookie(credential)
        if "BDUSS=" not in cookie:
            return AccountProbe(status="expired", message="凭据缺少必填字段：BDUSS")
        try:
            nickname = self._username(cookie, account)
        except NetdiskError as error:
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message="百度网盘登录态已失效")
            return AccountProbe(status="unknown", message=error.message)
        message = f"百度网盘登录态有效：{nickname}" if nickname else "百度网盘登录态有效"
        return AccountProbe(status="active", message=message)

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从百度账号信息接口读取账号昵称。

        :param credential: 浏览器抓到的凭据
        :return: 昵称；接口不可用时为空串
        """
        cookie = _credential_cookie(credential)
        if "BDUSS=" not in cookie:
            return ""
        try:
            return self._nickname(cookie)
        except NetdiskError:
            return ""

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {}, account)
