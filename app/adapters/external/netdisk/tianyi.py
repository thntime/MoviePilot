"""
天翼云盘（189）协议适配器。

协议依据天翼云盘网页端 Web API：登录态是域 `189.cn` 的 `COOKIE_LOGIN_USER` Cookie，
业务接口统一在 `https://cloud.189.cn/api/` 下，响应以 `res_code == 0` 判定成功。
天翼没有免转存分享直链，分享下载一律「转存到按文件隔离的临时目录 → 取直链 → 下载完成后
删除该目录」；个人盘提供配额、浏览、下载、分享、移动、重命名与删除。
"""

from __future__ import annotations

import json
import re
import threading
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

_WEB = "https://cloud.189.cn"
_API = "https://cloud.189.cn/api"
# 个人盘根目录固定为 -11。
_ROOT_FOLDER_ID = "-11"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 临时目录前缀；目录名按分享与文件隔离，避免并发任务互相覆盖。
_TEMP_DIR_PREFIX = "mp_tr_tianyi_"

_SHARE_INFO_PATH = "/open/share/getShareInfoByCodeV2.action"
_SHARE_LIST_PATH = "/open/share/listShareDir.action"
_FILE_LIST_PATH = "/open/file/listFiles.action"
_BATCH_CREATE_PATH = "/open/batch/createBatchTask.action"
_BATCH_CHECK_PATH = "/open/batch/checkBatchTask.action"
_CONFLICT_INFO_PATH = "/open/batch/getConflictTaskInfo.action"
_MANAGE_BATCH_PATH = "/open/batch/manageBatchTask.action"
_CREATE_FOLDER_PATH = "/open/file/createFolder.action"
_RENAME_FILE_PATH = "/open/file/renameFile.action"
_RENAME_FOLDER_PATH = "/open/file/renameFolder.action"
_FILE_INFO_PATH = "/portal/getFileInfo.action"
_USER_INFO_PATH = "/open/user/getUserInfo.action"
_CREATE_SHARE_PATH = "/open/share/createShareLink.action"

# 转存任务轮询：最多 60 次、间隔 1 秒。
_TASK_POLL_TIMES = 60
_TASK_POLL_INTERVAL = 1.0
# 天翼接口按 Accept 决定返回 JSON 还是 XML；默认 Accept 会拿到 XML，必须显式声明。
_ACCEPT_HEADERS: dict[str, str] = {"Accept": "application/json;charset=UTF-8"}
# 列表接口单页条数；天翼返回全部条目时一次取完。
_PAGE_SIZE = 1000

# 登录态失效的响应信号。
_AUTH_SIGNALS = ("InvalidSessionKey", "userSessionBO is null", "not login", "登录已失效")
# 分享本身不可用的响应信号；命中后按 SHARE_INVALID 处理，进入分享黑名单。
_SHARE_INVALID_SIGNALS = (
    "分享不存在",
    "已失效",
    "提取码",
    "access code",
    "ShareInfoNotFound",
    "AccessCodeError",
    "ShareFileNotFound",
)
# 频控与转存槽位占用的响应信号。
_RATE_SIGNALS = (
    "频繁",
    "限流",
    "请稍后",
    "ShareDumpFileOverload",
    "TooManyRequests",
)
# 空间不足的响应信号。
_SPACE_SIGNALS = ("空间不足", "容量不足", "空间已满", "InsufficientSpace")

# 账号级转存互斥锁：天翼同一账号只允许一个转存槽位。
_TRANSFER_LOCKS: dict[int, threading.Lock] = {}
_TRANSFER_LOCKS_GUARD = threading.Lock()


def _int(value: Any) -> int:
    """
    把接口字段转换为整数，允许负数。

    :param value: 原始字段
    :return: 整数；无法转换时为 0
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _transfer_lock(account_id: int) -> threading.Lock:
    """
    取得账号级转存互斥锁。

    :param account_id: 账号主键，无账号时使用 0
    :return: 该账号专属的锁
    """
    with _TRANSFER_LOCKS_GUARD:
        lock = _TRANSFER_LOCKS.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _TRANSFER_LOCKS[account_id] = lock
        return lock


def _credential_has_cookie(credential: Mapping[str, str]) -> bool:
    """
    判断凭据中是否存在 `COOKIE_LOGIN_USER`。

    兼容显式字段与整段 Cookie 头两种保存形式。

    :param credential: 明文凭据字段
    :return: 存在返回 True
    """
    if as_text(credential.get("COOKIE_LOGIN_USER")):
        return True
    for key in ("cookie", "cookie_header", "cookies"):
        raw = as_text(credential.get(key))
        for part in raw.split(";"):
            item = part.strip()
            if "=" not in item:
                continue
            name = item.split("=", 1)[0].strip()
            if name == "COOKIE_LOGIN_USER":
                return True
    return False


def _is_folder(value: Any) -> bool:
    """
    判断条目字段是否表示目录。

    :param value: `isFolder` 字段
    :return: 目录返回 True
    """
    if isinstance(value, bool):
        return value
    return as_int(value) != 0


class TianyiAdapter(BaseNetdiskAdapter):
    """
    天翼云盘（189）协议适配器。

    分享下载必须转存；个人盘按七项能力提供配额、浏览、下载、分享、移动、重命名与删除。
    """

    platform = NetdiskPlatform.TIANYI
    link_policy = LinkPolicy.TRANSFER_REQUIRED
    requires_account = True
    guest_supported = False
    supports_refresh = False
    # 天翼直链不做分片，下载器按单流处理。
    single_stream = True
    max_concurrency = 1
    cookie_required_keys: tuple[str, ...] = ("COOKIE_LOGIN_USER",)
    cookie_domains = ("189.cn",)
    login_url = "https://cloud.189.cn/web/main"
    supports_password_login = False
    drive_capabilities: DriveCapabilities = DriveCapabilities(
        quota=True,
        browse=True,
        download=True,
        share=True,
        move=True,
        rename=True,
        delete=True,
    )
    user_agent = _UA
    referer = "https://cloud.189.cn/web/main/"

    # ---- 通用请求与解析 ----

    def _check(self, payload: Mapping[str, Any]) -> None:
        """
        校验业务响应码。

        :param payload: 响应 JSON
        :raises NetdiskError: `res_code` 非 0 时抛出
        """
        code = payload.get("res_code")
        if code is not None and as_text(code) not in ("", "0"):
            raise self._error(payload)
        # 部分接口（如未登录时的列目录）直接返回 HTTP 400 + errorCode，没有 res_code。
        if as_text(payload.get("errorCode")):
            raise self._error(payload)

    def _error(self, payload: Mapping[str, Any]) -> NetdiskError:
        """
        把业务错误映射为统一错误分类。

        异常消息只包含服务端返回的原因，不包含 Cookie。

        :param payload: 响应 JSON
        :return: 分类异常
        """
        message = as_text(payload.get("res_message")) or as_text(payload.get("errorCode"))
        message = message or "天翼云盘接口请求失败"
        error_code = as_text(payload.get("errorCode"))
        text = f"{message} {error_code}".lower()
        if any(signal.lower() in text for signal in _AUTH_SIGNALS):
            return NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "天翼云盘登录态已失效", platform=self.platform)
        if any(signal.lower() in text for signal in _SHARE_INVALID_SIGNALS):
            return NetdiskError(NetdiskErrorCode.SHARE_INVALID, message, platform=self.platform)
        if any(signal.lower() in text for signal in _RATE_SIGNALS):
            return NetdiskError(NetdiskErrorCode.RATE_LIMITED, message, platform=self.platform)
        if any(signal.lower() in text for signal in _SPACE_SIGNALS):
            return NetdiskError(NetdiskErrorCode.SPACE_INSUFFICIENT, message, platform=self.platform)
        return NetdiskError(NetdiskErrorCode.UNKNOWN, message, platform=self.platform)

    def _get(
        self,
        path: str,
        account: Optional[NetdiskAccountRecord],
        params: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        发起 GET 请求并校验业务码。

        :param path: 相对 `/api` 的接口路径
        :param account: 取链账号
        :param params: 查询参数
        :return: 响应 JSON
        """
        payload = self.http.get_json(
            f"{_API}{path}",
            account=account,
            params=dict(params or {}),
            headers=_ACCEPT_HEADERS,
        )
        self._check(payload)
        return payload

    def _post(
        self,
        path: str,
        account: Optional[NetdiskAccountRecord],
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        """
        发起 POST 请求并校验业务码。

        :param path: 相对 `/api` 的接口路径
        :param account: 取链账号
        :param data: 表单请求体
        :return: 响应 JSON
        """
        payload = self.http.post_json(
            f"{_API}{path}",
            account=account,
            data=dict(data),
            headers=_ACCEPT_HEADERS,
        )
        self._check(payload)
        return payload

    @staticmethod
    def _items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """
        合并分享或个人目录的文件与子目录条目。

        :param payload: 响应 JSON
        :return: 统一为 `id`/`name`/`size`/`isFolder` 的条目列表
        """
        file_ao = payload.get("fileListAO")
        if not isinstance(file_ao, Mapping):
            return []
        items: list[dict[str, Any]] = []
        for entry in file_ao.get("fileList") or []:
            if not isinstance(entry, Mapping):
                continue
            items.append(
                {
                    "id": as_text(entry.get("id") or entry.get("fileId")),
                    "name": as_text(entry.get("name") or entry.get("fileName")),
                    "size": as_int(entry.get("size")),
                    "isFolder": _is_folder(entry.get("isFolder")),
                }
            )
        for entry in file_ao.get("folderList") or []:
            if not isinstance(entry, Mapping):
                continue
            items.append(
                {
                    "id": as_text(entry.get("id") or entry.get("fileId")),
                    "name": as_text(entry.get("name") or entry.get("fileName")),
                    "size": 0,
                    "isFolder": True,
                }
            )
        return [item for item in items if item["id"]]

    # ---- 账号 ----

    def _user_info(self, account: Optional[NetdiskAccountRecord]) -> dict[str, Any]:
        """
        读取账号信息。

        :param account: 账号
        :return: 含 `capacity`/`available`/`nickName`/`loginName` 的字典
        """
        payload = self._get(_USER_INFO_PATH, account)
        nested = payload.get("userInfo")
        if isinstance(nested, Mapping):
            return dict(nested)
        return payload

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        校验 Cookie：用列个人盘根目录确认登录态。

        用户信息接口在纯 Web 会话下需要 sessionKey，列目录接口只需要 Cookie，
        因此登录态检测统一走列目录，避免合法的 Cookie 被误判为未登录。

        :param credential: 明文凭据字段
        :return: 探测结果；网络异常返回 `unknown`
        """
        if not _credential_has_cookie(credential):
            logger.warning("天翼云盘登录态检测：凭据缺少 COOKIE_LOGIN_USER")
            return AccountProbe(status="expired", message="缺少天翼云盘 Cookie")
        account = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="",
            credential=dict(credential),
        )
        try:
            self._list_files(account, _ROOT_FOLDER_ID)
        except NetdiskError as error:
            logger.warning(
                f"天翼云盘登录态检测失败：code={error.code.value} message={error.message}"
            )
            self._log_failure_response(account)
            if error.code is NetdiskErrorCode.AUTH_EXPIRED:
                return AccountProbe(status="expired", message=error.message)
            return AccountProbe(status="unknown", message=error.message)
        except Exception as error:  # noqa: BLE001 - 网络异常不能判定登录态
            logger.warning(f"天翼云盘账号校验异常：error={type(error).__name__}")
            return AccountProbe(status="unknown", message="天翼云盘接口暂时不可用")
        return AccountProbe(status="active", message="天翼云盘登录态有效")

    def _log_failure_response(self, account: NetdiskAccountRecord) -> None:
        """
        记录失败响应的状态与内容摘要，便于定位登录态问题。

        摘要会脱敏 Cookie，只保留状态码、内容类型与截断后的响应体。

        :param account: 账号
        """
        url = f"{_API}{_FILE_LIST_PATH}"
        params = {
            "folderId": _ROOT_FOLDER_ID,
            "mediaType": 0,
            "pageNum": 1,
            "pageSize": 1,
            "iconOption": 5,
            "orderBy": "lastOpTime",
            "descending": "true",
        }
        try:
            response = RequestUtils(
                headers=self.http.headers(account, extra=_ACCEPT_HEADERS),
                timeout=10,
                proxies=self.http.proxies,
            ).get_res(url, params=params)
        except Exception as error:  # noqa: BLE001 - 诊断失败不影响登录态判定
            logger.warning(f"天翼云盘失败响应诊断异常：error={type(error).__name__}")
            return
        if response is None:
            logger.warning("天翼云盘失败响应诊断：无响应")
            return
        body = str(getattr(response, "text", ""))[:160]
        body = re.sub(r"(?i)COOKIE_LOGIN_USER=[^;\s\"']*", "COOKIE_LOGIN_USER=***", body)
        content_type = ""
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            content_type = as_text(headers.get("Content-Type"))
        logger.warning(
            "天翼云盘失败响应诊断：status=%s content_type=%s body=%r"
            % (getattr(response, "status_code", 0), content_type, body)
        )

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential or {})

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从凭据读取昵称或登录名。

        :param credential: 浏览器抓到的凭据
        :return: 账号名；读取失败时为空
        """
        account = NetdiskAccountRecord(
            id=0,
            platform=self.platform,
            name="",
            credential=dict(credential),
        )
        try:
            info = self._user_info(account)
        except Exception:  # noqa: BLE001 - 账号名读取失败不影响登录
            return ""
        return as_text(info.get("nickName")) or as_text(info.get("loginName"))

    # ---- 分享 ----

    def _share_info(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> tuple[str, str, str, str]:
        """
        读取分享的标识、根节点、模式与标题。

        :param ref: 分享引用
        :param account: 取链账号
        :return: `(shareId, fileId, shareMode, fileName)`
        """
        payload = self._get(_SHARE_INFO_PATH, account, {"shareCode": ref.share_id})
        return (
            as_text(payload.get("shareId")),
            as_text(payload.get("fileId")),
            as_text(payload.get("shareMode")) or "1",
            as_text(payload.get("fileName")),
        )

    def _list_share_dir(
        self,
        ref: ShareRef,
        share_id: str,
        file_id: str,
        share_mode: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[dict[str, Any]]:
        """
        读取一页分享目录。

        :param ref: 分享引用
        :param share_id: 分享标识
        :param file_id: 目录标识
        :param share_mode: 分享模式
        :param account: 取链账号
        :return: 统一后的目录条目
        """
        payload = self._get(
            _SHARE_LIST_PATH,
            account,
            {
                "shareId": share_id,
                "fileId": file_id,
                "isFolder": "true",
                "shareMode": share_mode,
                "accessCode": ref.password or "",
                "pageNum": 1,
                "pageSize": _PAGE_SIZE,
                "iconOption": 5,
                "orderBy": "lastOpTime",
                "descending": "true",
            },
        )
        return self._items(payload)

    @staticmethod
    def _share_files(items: list[dict[str, Any]], parent_id: str) -> list[ShareFile]:
        """
        把目录条目转换为分享文件对象。

        :param items: 统一后的目录条目
        :param parent_id: 当前目录标识
        :return: 分享文件列表
        """
        return [
            ShareFile(
                file_id=str(item["id"]),
                name=str(item["name"]),
                size=int(item["size"]),
                is_dir=bool(item["isFolder"]),
                parent_id=parent_id,
            )
            for item in items
        ]

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        读取分享标题、总大小与根目录条目。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情
        """
        share_id, root_file_id, share_mode, title = self._share_info(ref, account)
        items = self._list_share_dir(ref, share_id, root_file_id, share_mode, account)
        files = self._share_files(items, root_file_id)
        return ShareDetail(
            title=title or ref.share_id,
            size=sum(item.size for item in files if not item.is_dir),
            files=tuple(files),
            stoken="",
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
        :param parent_id: 父目录标识，空串表示分享根目录
        :param account: 取链账号
        :return: 目录条目
        """
        share_id, root_file_id, share_mode, _title = self._share_info(ref, account)
        file_id = parent_id or root_file_id
        items = self._list_share_dir(ref, share_id, file_id, share_mode, account)
        return self._share_files(items, file_id)

    # ---- 转存取链与清理 ----

    def _list_files(self, account: NetdiskAccountRecord, folder_id: str) -> list[dict[str, Any]]:
        """
        列出个人盘目录。

        :param account: 账号
        :param folder_id: 目录标识
        :return: 统一后的目录条目
        """
        payload = self._get(
            _FILE_LIST_PATH,
            account,
            {
                "folderId": folder_id,
                "mediaType": 0,
                "pageNum": 1,
                "pageSize": _PAGE_SIZE,
                "iconOption": 5,
                "orderBy": "lastOpTime",
                "descending": "true",
            },
        )
        return self._items(payload)

    def _ensure_temp_dir(
        self,
        account: NetdiskAccountRecord,
        share_id: str,
        file_id: str,
    ) -> str:
        """
        确保该文件专属的临时目录存在。

        :param account: 账号
        :param share_id: 分享标识
        :param file_id: 分享内的文件标识
        :return: 临时目录标识
        """
        name = f"{_TEMP_DIR_PREFIX}{share_id[:8]}_{file_id[:8]}"
        for item in self._list_files(account, _ROOT_FOLDER_ID):
            if item["isFolder"] and item["name"] == name:
                return str(item["id"])
        payload = self._post(
            _CREATE_FOLDER_PATH,
            account,
            {"parentFolderId": _ROOT_FOLDER_ID, "folderName": name},
        )
        folder_id = as_text(payload.get("id"))
        if not folder_id:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "天翼云盘创建临时目录失败：响应缺少目录标识",
                platform=self.platform,
            )
        return folder_id

    def _wait_batch(
        self,
        account: NetdiskAccountRecord,
        type_: str,
        task_id: str,
        entries: list[dict[str, Any]],
        first_payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        轮询批量任务直到结束，必要时自动裁决冲突。

        :param account: 账号
        :param type_: 任务类型
        :param task_id: 任务标识
        :param entries: 原始任务条目，用于冲突裁决
        :param first_payload: 创建任务的响应；已带完成状态时直接返回
        :return: 完成状态的响应
        """
        if first_payload is not None and _int(first_payload.get("taskStatus")) == 4:
            return dict(first_payload)
        for attempt in range(_TASK_POLL_TIMES):
            if attempt:
                self.sleep(_TASK_POLL_INTERVAL)
            payload = self._post(_BATCH_CHECK_PATH, account, {"type": type_, "taskId": task_id})
            status = _int(payload.get("taskStatus"))
            if status == 2:
                self._resolve_conflict(account, type_, task_id, entries)
                continue
            if status == 4:
                return payload
        raise NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            "天翼云盘任务处理超时，请稍后重试",
            platform=self.platform,
        )

    def _resolve_conflict(
        self,
        account: NetdiskAccountRecord,
        type_: str,
        task_id: str,
        entries: list[dict[str, Any]],
    ) -> None:
        """
        裁决同名冲突：读取冲突详情后按 `dealWay=1` 跳过冲突项。

        :param account: 账号
        :param type_: 任务类型
        :param task_id: 任务标识
        :param entries: 原始任务条目
        """
        self._post(_CONFLICT_INFO_PATH, account, {"type": type_, "taskId": task_id})
        conflicts = [{**entry, "dealWay": 1, "isConflict": 1} for entry in entries]
        self._post(
            _MANAGE_BATCH_PATH,
            account,
            {
                "type": type_,
                "taskId": task_id,
                "taskInfos": json.dumps(conflicts, ensure_ascii=False),
            },
        )

    @staticmethod
    def _task_entries(files: list[Any]) -> list[dict[str, Any]]:
        """
        把分享文件或个人盘文件转换为批量任务条目。

        `fileId` 必须是字符串，`isFolder` 必填（0 文件 / 1 目录）。

        :param files: 文件条目
        :return: 批量任务条目
        """
        return [
            {
                "fileId": str(file.file_id),
                "fileName": file.name,
                "isFolder": 1 if file.is_dir else 0,
            }
            for file in files
        ]

    def _save_share(
        self,
        account: NetdiskAccountRecord,
        share_id: str,
        target_dir: str,
        file: ShareFile,
    ) -> tuple[str, ...]:
        """
        把分享文件转存到临时目录并等待完成。

        :param account: 账号
        :param share_id: 分享标识
        :param target_dir: 目标临时目录
        :param file: 目标文件
        :return: 转存后的新文件标识列表
        :raises NetdiskError: 槽位占用、分享失效或任务失败时抛出
        """
        entries = self._task_entries([file])
        payload = self._post(
            _BATCH_CREATE_PATH,
            account,
            {
                "type": "SHARE_SAVE",
                "taskInfos": json.dumps(entries, ensure_ascii=False),
                "targetFolderId": target_dir,
                "shareId": share_id,
            },
        )
        task_id = as_text(payload.get("taskId"))
        if not task_id:
            raise NetdiskError(
                NetdiskErrorCode.RATE_LIMITED,
                "天翼云盘转存槽位被占用，请稍后重试",
                platform=self.platform,
            )
        result = self._wait_batch(account, "SHARE_SAVE", task_id, entries)
        if _int(result.get("failedCount")) > 0:
            raise NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "天翼云盘转存失败：分享可能已失效",
                platform=self.platform,
            )
        saved = result.get("successedFileIdList") or []
        return tuple(as_text(item) for item in saved if as_text(item))

    def _locate_saved(
        self,
        account: NetdiskAccountRecord,
        target_dir: str,
        file: ShareFile,
        saved_ids: tuple[str, ...],
    ) -> str:
        """
        定位转存后的文件标识。

        优先使用任务返回的标识；缺失时按名称与大小在临时目录匹配，匹配不到唯一文件即报错。

        :param account: 账号
        :param target_dir: 临时目录
        :param file: 原分享文件
        :param saved_ids: 任务返回的新文件标识
        :return: 转存后的文件标识
        """
        if saved_ids:
            return saved_ids[0]
        matches = [
            item
            for item in self._list_files(account, target_dir)
            if not item["isFolder"]
            and item["name"] == file.name
            and (not file.size or int(item["size"]) == file.size)
        ]
        if len(matches) != 1:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "天翼云盘转存后无法唯一定位目标文件",
                platform=self.platform,
            )
        return str(matches[0]["id"])

    def _download_url(self, account: Optional[NetdiskAccountRecord], file_id: str) -> str:
        """
        获取文件直链。

        :param account: 账号
        :param file_id: 个人盘文件标识
        :return: 补全协议后的直链
        """
        payload = self._get(_FILE_INFO_PATH, account, {"fileId": file_id})
        url = as_text(payload.get("downloadUrl"))
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "天翼云盘未返回下载地址",
                platform=self.platform,
            )
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{_WEB}{url}"
        return url

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
        转存分享文件到按文件隔离的临时目录后取直链。

        天翼没有免转存直链，`allow_transfer` 与 `transfer_first` 仅用于对齐接口。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存，本平台固定需要转存
        :param transfer_first: 是否优先转存，本平台固定先转存
        :return: 直链及其清理句柄
        """
        del allow_transfer, transfer_first
        if account is None:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "天翼云盘取链需要登录账号",
                platform=self.platform,
            )
        share_id, _root_file_id, _share_mode, _title = self._share_info(ref, account)
        with _transfer_lock(account.id):
            temp_dir = self._ensure_temp_dir(account, share_id, file.file_id)
            saved_ids = self._save_share(account, share_id, temp_dir, file)
            file_id = self._locate_saved(account, temp_dir, file, saved_ids)
        url = self._download_url(account, file_id)
        return DirectLink(
            url=url,
            headers=self.download_headers(account),
            size=file.size,
            cleanup_handle=TransferHandle(
                platform=self.platform,
                account_id=account.id,
                directory_id=temp_dir,
                file_ids=(file_id,),
            ),
            account_id=account.id,
            strategy="transfer",
        )

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        删除转存产生的临时目录；失败只记录，不抛出。

        只删除句柄里记录的目录，不触碰账号内的其他文件。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        directory_id = handle.directory_id
        if not directory_id:
            return
        try:
            payload = self._post(
                _BATCH_CREATE_PATH,
                account,
                {
                    "type": "DELETE",
                    "taskInfos": json.dumps(
                        [{"fileId": directory_id, "fileName": "", "isFolder": 1}],
                        ensure_ascii=False,
                    ),
                    "targetFolderId": "",
                },
            )
            task_id = as_text(payload.get("taskId"))
            if task_id:
                self._post(_BATCH_CHECK_PATH, account, {"type": "DELETE", "taskId": task_id})
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载主流程
            logger.warning(
                f"天翼云盘清理临时目录失败：directory={directory_id} error={type(error).__name__}"
            )

    # ---- 个人盘 ----

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询个人盘空间。

        :param account: 账号
        :return: 已用与总空间，字节
        """
        info = self._user_info(account)
        total = _int(info.get("capacity"))
        available = _int(info.get("available"))
        return DriveQuota(used=max(total - available, 0), total=total)

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        列出个人盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串表示根目录
        :return: 目录条目
        """
        folder_id = parent_id or _ROOT_FOLDER_ID
        return [
            DriveFile(
                file_id=str(item["id"]),
                name=str(item["name"]),
                size=int(item["size"]),
                is_dir=bool(item["isFolder"]),
                parent_id=folder_id,
            )
            for item in self._list_files(account, folder_id)
        ]

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        """
        return DirectLink(
            url=self._download_url(account, file.file_id),
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
        创建个人盘分享。

        `expireTime` 的语义尚未实测，按计划传 `expires_in` 或平台默认值 2099；
        提取码以服务端返回为准，`password` 仅作兜底。

        :param account: 账号
        :param files: 文件列表，当前只分享第一个
        :param password: 提取码兜底值
        :param expires_in: 有效期秒数，0 表示平台默认
        :return: 分享结果
        """
        if not files:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "没有可分享的文件",
                platform=self.platform,
            )
        payload = self._get(
            _CREATE_SHARE_PATH,
            account,
            {
                "fileId": files[0].file_id,
                "shareType": 3,
                "expireTime": expires_in or 2099,
            },
        )
        url = (
            as_text(payload.get("shareUrl"))
            or as_text(payload.get("shareLink"))
            or as_text(payload.get("shortShareUrl"))
            or as_text(payload.get("url"))
        )
        share_id = as_text(payload.get("shareId"))
        if not url and share_id:
            url = f"{_WEB}/t/{share_id}"
        if not url:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "天翼云盘未返回分享链接",
                platform=self.platform,
            )
        access_code = (
            as_text(payload.get("accessCode"))
            or as_text(payload.get("sharePwd"))
            or as_text(payload.get("password"))
            or password
        )
        return DriveShare(url=url, password=access_code)

    def _batch(
        self,
        account: NetdiskAccountRecord,
        type_: str,
        files: list[DriveFile],
        target_folder_id: str = "",
    ) -> None:
        """
        执行批量任务并等待结束。

        :param account: 账号
        :param type_: 任务类型，`MOVE` 或 `DELETE`
        :param files: 文件条目
        :param target_folder_id: 目标目录，删除时为空
        """
        entries = self._task_entries(files)
        payload = self._post(
            _BATCH_CREATE_PATH,
            account,
            {
                "type": type_,
                "taskInfos": json.dumps(entries, ensure_ascii=False),
                "targetFolderId": target_folder_id,
            },
        )
        task_id = as_text(payload.get("taskId"))
        if not task_id:
            return
        result = self._wait_batch(account, type_, task_id, entries, payload)
        if _int(result.get("failedCount")) > 0:
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                f"天翼云盘{type_}任务部分失败",
                platform=self.platform,
            )

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动个人盘文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录
        """
        self._batch(account, "MOVE", files, target_id or _ROOT_FOLDER_ID)

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名个人盘文件或目录。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        """
        if file.is_dir:
            self._post(_RENAME_FOLDER_PATH, account, {"folderId": file.file_id, "destFolderName": name})
            return
        self._post(_RENAME_FILE_PATH, account, {"fileId": file.file_id, "destFileName": name})

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除个人盘文件。

        :param account: 账号
        :param files: 文件列表
        """
        self._batch(account, "DELETE", files)
