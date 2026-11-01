"""
夸克网盘取链客户端。

只负责「分享 → 直链」这一段：分享令牌、目录详情、免转存取链、转存与清理、
会话 Cookie 刷新。分片下载由 engine 负责。

接口契约来自 YunX-Desktop 的抓包实现（见 docs/quark-download-acceleration.md）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

_API_BASE = "https://drive-pc.quark.cn"
_DOWNLOAD_URL = f"{_API_BASE}/1/clouddrive/file/download?pr=ucpro&fr=pc&sys=win32&ve=3.23.2"
_SAVE_URL = f"{_API_BASE}/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc"
_TASK_URL = f"{_API_BASE}/1/clouddrive/task?pr=ucpro&fr=pc"
_TOKEN_URL = f"{_API_BASE}/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
_DETAIL_URL = f"{_API_BASE}/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc"
_FILE_URL = f"{_API_BASE}/1/clouddrive/file?pr=ucpro&fr=pc"
_DELETE_URL = f"{_API_BASE}/1/clouddrive/file/delete?pr=ucpro&fr=pc&uc_param_str="
_CONFIG_URL = f"{_API_BASE}/1/clouddrive/config?pr=ucpro&fr=pc"
# 取链与下载必须使用客户端 UA 与固定 Referer，浏览器 UA 会被判为非客户端。
API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.12-a038f7b798 "
    "Safari/537.36 Channel/pckk_other_ch"
)
DOWNLOAD_REFERER = "https://pan.quark.cn/"
# __puus 约 3 小时过期，提前到 90 分钟刷新。
_PUUS_REFRESH_SECONDS = 90 * 60
# 转存任务轮询次数与间隔。
_TASK_POLL_TIMES = 10
_TASK_POLL_INTERVAL = 1.0
# 分享列表单页条数；服务端可能截断到 50，翻页以「整页无新条目」结束。
_SHARE_PAGE_SIZE = 100
_SHARE_MAX_PAGES = 20
# 登录态失效与游客大小限制的服务端错误码。
_CODE_LOGIN_REQUIRED = 31001
_CODE_GUEST_LIMIT = 23018


class QuarkError(Exception):
    """
    夸克接口调用失败，异常消息为可直接展示的中文原因。
    """


def _as_text(value: Any) -> str:
    """
    将接口字段转换为去除首尾空白的文本。
    """
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: Any) -> int:
    """
    将接口字段转换为非负整数。
    """
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _merge_cookie(cookie: str, response: Any) -> str:
    """
    把响应 Set-Cookie 中的 __puus/__pus 合并回 Cookie 串。

    :param cookie: 原 Cookie
    :param response: HTTP 响应对象
    :return: 合并后的 Cookie；响应没有新令牌时原样返回
    """
    raw = ""
    try:
        raw = str(response.headers.get("set-cookie") or "")
    except Exception:  # noqa: BLE001
        raw = ""
    if not raw:
        return cookie
    updates: Dict[str, str] = {}
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


def _strip_puus(cookie: str) -> str:
    """
    去掉 __puus，用于触发服务端重新下发。

    :param cookie: 原 Cookie
    :return: 不含 __puus 的 Cookie
    """
    return "; ".join(
        item.strip()
        for item in cookie.split(";")
        if item.strip() and not item.strip().startswith("__puus=")
    )


class QuarkClient:
    """
    夸克网盘取链客户端，按下载器实例配置创建。

    实例配置：cookie（账号 Cookie）、threads（并发连接数）、rate_limit（全局限速 KB/s）、
    preempt（是否启用慢连接抢占）。
    """

    def __init__(self, name: str = "", cookie: str = "", threads: int = 32,
                 rate_limit: int = 0, preempt: bool = True, **_: Any) -> None:
        """
        保存实例配置，构造时不发起网络请求。

        :param name: 下载器实例名称
        :param cookie: 夸克账号 Cookie
        :param threads: 并发连接数
        :param rate_limit: 全局限速，单位 KB/s，0 表示不限速
        :param preempt: 是否启用慢连接抢占
        """
        self.name = _as_text(name)
        self.cookie = _as_text(cookie)
        self.threads = max(int(threads or 32), 1)
        self.rate_limit = max(int(rate_limit or 0), 0)
        self.preempt = bool(preempt)
        self._http: Optional[RequestUtils] = None
        self._cookie_at = time.monotonic()

    # ---- 基础请求 ----

    def _get_http(self) -> RequestUtils:
        """
        惰性创建网络适配器。
        """
        if self._http is None:
            self._http = RequestUtils(timeout=20)
        return self._http

    def _headers(self) -> Dict[str, str]:
        """
        构造夸克接口请求头。
        """
        return {
            "User-Agent": API_USER_AGENT,
            "Referer": DOWNLOAD_REFERER,
            "Accept": "application/json, text/plain, */*",
            "Cookie": self.cookie,
        }

    def _parse(self, response: Any, action: str) -> Dict[str, Any]:
        """
        解析统一响应，失败时抛出带原因的错误。

        :param response: HTTP 响应
        :param action: 用于错误提示的动作名
        :return: 响应 JSON
        """
        if response is None:
            raise QuarkError(f"{action}失败：网络不可达")
        self.cookie = _merge_cookie(self.cookie, response)
        status = int(getattr(response, "status_code", 0) or 0)
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            raise QuarkError(f"{action}失败：响应不是 JSON（HTTP {status}，可能触发风控）")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        if not isinstance(payload, dict):
            raise QuarkError(f"{action}失败：响应结构异常")
        code = _as_int(payload.get("code"))
        if _as_int(payload.get("status")) != 200 and code != 0:
            message = _as_text(payload.get("message")) or f"HTTP {status}"
            if code == _CODE_LOGIN_REQUIRED:
                raise QuarkError(f"{action}失败：夸克登录态已失效，请更新下载器 Cookie")
            if code == _CODE_GUEST_LIMIT:
                raise QuarkError(f"{action}失败：超出游客取链大小限制")
            raise QuarkError(f"{action}失败：{message}")
        return payload

    def _post(self, url: str, body: Dict[str, Any], action: str) -> Dict[str, Any]:
        """
        发送 JSON POST 请求。
        """
        response = self._get_http().post_res(url, json=body, headers=self._headers())
        return self._parse(response, action)

    def _get(self, url: str, params: Optional[Dict[str, Any]], action: str) -> Dict[str, Any]:
        """
        发送 GET 请求。
        """
        response = self._get_http().get_res(url, params=params, headers=self._headers())
        return self._parse(response, action)

    # ---- 会话 ----

    def ensure_cookie(self) -> str:
        """
        按需刷新 __puus，返回可用的 Cookie。

        :return: 当前 Cookie
        """
        if not self.cookie:
            raise QuarkError("未配置夸克账号 Cookie")
        if time.monotonic() - self._cookie_at < _PUUS_REFRESH_SECONDS:
            return self.cookie
        return self.refresh_cookie()

    def refresh_cookie(self) -> str:
        """
        剥离 __puus 后请求配置接口，由响应重新下发并合并。

        :return: 刷新后的 Cookie
        """
        original = self.cookie
        self.cookie = _strip_puus(original)
        try:
            self._get(_CONFIG_URL, None, "刷新夸克会话")
        except QuarkError:
            self.cookie = original
            raise
        finally:
            self._cookie_at = time.monotonic()
        return self.cookie

    # ---- 连接巡检 ----

    def is_inactive(self) -> bool:
        """
        判断实例是否缺少可用凭据。

        :return: 未配置 Cookie 时返回 True
        """
        return not self.cookie

    def reconnect(self) -> None:
        """
        刷新夸克会话 Cookie，失败时保留原值。
        """
        try:
            self.refresh_cookie()
        except QuarkError:
            pass

    def transfer_info(self) -> Optional[Dict[str, Any]]:
        """
        返回连接状态，供模块巡检使用。

        :return: 已配置凭据时返回状态字典，否则返回 None
        """
        return {"cookie": bool(self.cookie)}

    # ---- 分享与取链 ----

    def share_token(self, share_code: str, passcode: str = "") -> str:
        """
        用分享码与提取码换取分享访问令牌。

        :param share_code: 分享码
        :param passcode: 提取码，可为空
        :return: stoken
        """
        payload = self._post(_TOKEN_URL, {"pwd_id": share_code, "passcode": passcode or ""}, "获取分享令牌")
        data = payload.get("data") or {}
        token = _as_text(data.get("stoken"))
        if not token:
            raise QuarkError("获取分享令牌失败：响应缺少 stoken")
        return token

    def share_files(self, share_code: str, stoken: str, pdir_fid: str = "0") -> List[Dict[str, Any]]:
        """
        列出分享指定目录的文件，自动翻页取全量。

        服务端单页可能被截断（常见 50 条），且忽略页码时会重复返回同一页，
        因此按 fid 去重并以「整页没有新条目」作为结束条件。

        :param share_code: 分享码
        :param stoken: 分享访问令牌
        :param pdir_fid: 目录标识，根目录为 "0"
        :return: 文件条目（fid/name/size/fid_token/is_dir）
        """
        entries: Dict[str, Dict[str, Any]] = {}
        for page in range(1, _SHARE_MAX_PAGES + 1):
            params = {
                "pr": "ucpro", "fr": "pc", "pwd_id": share_code, "stoken": stoken,
                "pdir_fid": pdir_fid or "0", "force": "0",
                "_page": str(page), "_size": str(_SHARE_PAGE_SIZE),
                "_fetch_total": "1", "_sort": "file_type:asc,updated_at:desc",
            }
            payload = self._get(_DETAIL_URL, params, "读取分享目录")
            data = payload.get("data") or {}
            before = len(entries)
            for item in data.get("list") or []:
                if not isinstance(item, dict):
                    continue
                name = _as_text(item.get("file_name") or item.get("fileName"))
                if not name:
                    continue
                fid = _as_text(item.get("fid"))
                entries.setdefault(fid or f"{pdir_fid}/{name}", {
                    "fid": fid,
                    "name": name,
                    "size": _as_int(item.get("size")),
                    "duration": _as_int(item.get("duration")),
                    "fid_token": _as_text(item.get("share_fid_token") or item.get("fid_token")),
                    "is_dir": bool(item.get("dir")),
                })
            if not entries or len(entries) == before:
                break
        return list(entries.values())

    def share_link(self, share_code: str, stoken: str, fid: str, fid_token: str) -> Dict[str, Any]:
        """
        免转存取链：直接把分享凭证交给下载接口换取直链。

        :param share_code: 分享码
        :param stoken: 分享访问令牌
        :param fid: 分享内文件标识
        :param fid_token: 分享内文件令牌
        :return: 含 url/name/size 的字典
        """
        body = {
            "fids": [fid],
            "fids_token": [fid_token],
            "pwd_id": share_code,
            "stoken": stoken,
            "speedup_session": "",
            "token": "",
        }
        payload = self._post(_DOWNLOAD_URL, body, "获取分享直链")
        return self._first_link(payload, "获取分享直链")

    def file_link(self, fid: str) -> Dict[str, Any]:
        """
        个人网盘文件取链（转存后使用）。

        :param fid: 文件标识
        :return: 含 url/name/size 的字典
        """
        payload = self._post(_DOWNLOAD_URL, {"fids": [fid]}, "获取文件直链")
        return self._first_link(payload, "获取文件直链")

    @staticmethod
    def _first_link(payload: Dict[str, Any], action: str) -> Dict[str, Any]:
        """
        从下载接口响应中取出第一条直链。
        """
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise QuarkError(f"{action}失败：未返回下载链接")
        item = data[0] if isinstance(data[0], dict) else {}
        url = _as_text(item.get("download_url"))
        if not url:
            raise QuarkError(f"{action}失败：下载链接为空")
        return {
            "url": url,
            "name": _as_text(item.get("file_name") or item.get("filename")),
            "size": _as_int(item.get("size")),
        }

    # ---- 转存与清理 ----

    def create_folder(self, name: str, parent_fid: str = "0") -> str:
        """
        在个人网盘创建目录。

        :param name: 目录名
        :param parent_fid: 父目录标识，根目录为 "0"
        :return: 新目录 fid
        """
        payload = self._post(_FILE_URL, {
            "pdir_fid": parent_fid,
            "file_name": name,
            "dir_path": "",
            "dir_init_lock": False,
        }, "创建临时目录")
        fid = _as_text((payload.get("data") or {}).get("fid"))
        if not fid:
            raise QuarkError("创建临时目录失败：响应缺少 fid")
        return fid

    def save_share(self, share_code: str, stoken: str, fid: str, fid_token: str,
                   to_pdir_fid: str) -> str:
        """
        把分享文件转存到个人网盘目录，返回异步任务标识。

        :param share_code: 分享码
        :param stoken: 分享访问令牌
        :param fid: 分享内文件标识
        :param fid_token: 分享内文件令牌
        :param to_pdir_fid: 目标目录标识
        :return: 转存任务标识
        """
        payload = self._post(_SAVE_URL, {
            "pwd_id": share_code,
            "stoken": stoken,
            "pdir_fid": "0",
            "to_pdir_fid": to_pdir_fid,
            "fid_list": [fid],
            "fid_token_list": [fid_token],
            "scene": "link",
        }, "转存分享文件")
        task_id = _as_text((payload.get("data") or {}).get("task_id"))
        if not task_id:
            raise QuarkError("转存分享文件失败：响应缺少 task_id")
        return task_id

    def poll_saved_fid(self, task_id: str) -> str:
        """
        轮询转存任务，返回转存后的新文件标识。

        :param task_id: 转存任务标识
        :return: 新 fid
        """
        params = {"pr": "ucpro", "fr": "pc", "task_id": task_id, "retry_index": 0}
        for _ in range(_TASK_POLL_TIMES):
            try:
                payload = self._get(_TASK_URL, params, "查询转存任务")
            except QuarkError:
                time.sleep(_TASK_POLL_INTERVAL)
                continue
            data = payload.get("data") or {}
            finished = (
                _as_int(data.get("finished_at")) > 0
                or _as_int(data.get("status")) == 2
                or _as_int(data.get("task_status")) == 2
            )
            if finished:
                saved = ((data.get("save_as") or {}).get("save_as_top_fids") or [])
                fid = _as_text(saved[0]) if saved else ""
                if fid:
                    return fid
                raise QuarkError("转存完成但未返回新文件标识")
            time.sleep(_TASK_POLL_INTERVAL)
        raise QuarkError("转存任务超时")

    def delete_files(self, fids: List[str]) -> None:
        """
        删除个人网盘文件或目录，失败只记录不抛出。

        :param fids: 待删除的标识列表
        """
        if not fids:
            return
        try:
            # 夸克删除接口的字段名是 filelist（不是 fid_list），且必须显式带 exclude_fids。
            self._post(
                _DELETE_URL,
                {"action_type": 2, "filelist": list(fids), "exclude_fids": []},
                "清理临时文件",
            )
            logger.info(f"夸克网盘已清理转存临时目录：{fids}")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"夸克网盘清理转存临时目录失败：{fids} - {err}")
