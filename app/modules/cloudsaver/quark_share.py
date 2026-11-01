"""
夸克官方分享接口客户端，用于列出分享内的全部文件及其大小与时长。

CloudSaver 的列目录端点每次只返回 50 条且不支持翻页，无法覆盖多集分享；
本客户端直接调用夸克分享页使用的公开接口，按目录翻页收集完整文件树。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from app.adapters.network.http import RequestUtils

# 换取分享访问令牌与列目录的公开接口，无需账号 Cookie。
_TOKEN_URL = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
_DETAIL_URL = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail"
# 分享页请求所需的浏览器标识，缺少 Referer 时接口会拒绝。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://pan.quark.cn/",
    "Accept": "application/json, text/plain, */*",
}
# 单页上限与单条分享的翻页预算。
PAGE_SIZE = 100
MAX_PAGES = 3
# 排序规则与分享页保持一致，保证翻页顺序稳定。
_SORT = "file_type:asc,updated_at:desc"


def _as_text(value: Any) -> str:
    """
    将接口字段转换为去除首尾空白的文本。
    """
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: Any) -> int:
    """
    将接口字段转换为非负整数，非法值按 0 处理。
    """
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _normalize_entries(items: Any) -> List[Dict[str, Any]]:
    """
    归一分享目录条目，保留文件名、大小、时长与是否目录。

    :param items: 接口返回的 list 字段
    :return: 每项包含 name/size/duration/is_dir 的列表，非法条目被丢弃
    """
    if not isinstance(items, list):
        return []
    entries: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _as_text(item.get("file_name") or item.get("fileName") or item.get("name"))
        if not name:
            continue
        entries.append({
            "fid": _as_text(item.get("fid")),
            "name": name,
            "size": _as_int(item.get("size")),
            "duration": _as_int(item.get("duration")),
            "is_dir": bool(item.get("dir")),
        })
    return entries


class QuarkShareClient:
    """
    夸克分享只读客户端，负责换取访问令牌与翻页列出分享内文件。

    客户端不保存账号 Cookie，也不提供转存与下载能力。
    """

    def __init__(self, timeout: int = 10) -> None:
        """
        保存超时配置，构造时不发起网络请求。

        :param timeout: 单次请求超时秒数
        """
        self.timeout = timeout if timeout and timeout > 0 else 10
        self._http: Optional[RequestUtils] = None

    def _get_http(self) -> RequestUtils:
        """
        惰性创建并复用网络适配器实例。
        """
        if self._http is None:
            self._http = RequestUtils(headers=_HEADERS, timeout=self.timeout)
        return self._http

    def _request(self, method: str, url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """
        发起一次请求并返回 JSON 字典，网络异常或非 JSON 响应返回 None。
        """
        try:
            http = self._get_http()
            if method.upper() == "POST":
                response = http.post_res(url, **kwargs)
            else:
                response = http.get_res(url, **kwargs)
            try:
                if response is None:
                    return None
                if int(getattr(response, "status_code", 0) or 0) >= 400:
                    return None
                payload = response.json()
                return payload if isinstance(payload, dict) else None
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            return None

    def fetch_stoken(self, pwd_id: str, passcode: str = "") -> str:
        """
        用分享码换取访问令牌，失败返回空字符串。

        :param pwd_id: 分享码
        :param passcode: 分享提取码，可为空
        :return: 访问令牌；换取失败时为空
        """
        code = _as_text(pwd_id)
        if not code:
            return ""
        payload = self._request("POST", _TOKEN_URL, json={"pwd_id": code, "passcode": _as_text(passcode)})
        if not payload or payload.get("status") not in (None, 200) or payload.get("code") not in (None, 0):
            return ""
        data = payload.get("data")
        return _as_text(data.get("stoken")) if isinstance(data, dict) else ""

    def list_dir(
        self,
        pwd_id: str,
        stoken: str,
        pdir_fid: str = "0",
        page: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        列出分享内指定目录的一页条目。

        :param pwd_id: 分享码
        :param stoken: 访问令牌
        :param pdir_fid: 目录标识，根目录为 "0"
        :param page: 页码，从 1 开始
        :return: 含 entries 与 total 的字典；请求失败返回 None
        """
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "pwd_id": _as_text(pwd_id),
            "stoken": _as_text(stoken),
            "pdir_fid": _as_text(pdir_fid) or "0",
            "force": "0",
            "_page": str(max(int(page), 1)),
            "_size": str(PAGE_SIZE),
            "_fetch_total": "1",
            "_sort": _SORT,
        }
        payload = self._request("GET", _DETAIL_URL, params=params)
        if not payload:
            return None
        if payload.get("status") not in (None, 200) or payload.get("code") not in (None, 0):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        raw_metadata = payload.get("metadata")
        metadata: Dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        return {
            "entries": _normalize_entries(data.get("list")),
            "total": _as_int(metadata.get("_total")),
        }

    def collect_files(
        self,
        pwd_id: str,
        stoken: str,
        max_pages: int = MAX_PAGES,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        翻页收集分享内全部文件（含子目录一层），返回文件名、大小与时长。

        页数用尽仍未读完时返回 None，由调用方按集数不清晰处理，避免用不完整的
        文件树推导出错误的集数区间。

        :param pwd_id: 分享码
        :param stoken: 访问令牌
        :param max_pages: 单条分享允许的最大翻页次数
        :return: 文件条目列表；失败或超出预算返回 None
        """
        code = _as_text(pwd_id)
        token = _as_text(stoken)
        if not code or not token:
            return None
        budget = max(int(max_pages), 1)
        # (目录标识, 层级)；根目录为 "0"，只下钻一层子目录。
        queue: List[Tuple[str, int]] = [("0", 0)]
        visited: Set[str] = set()
        files: List[Dict[str, Any]] = []
        while queue:
            pdir_fid, depth = queue.pop(0)
            if pdir_fid in visited:
                continue
            visited.add(pdir_fid)
            page = 1
            while True:
                if budget <= 0:
                    return None
                budget -= 1
                result = self.list_dir(code, token, pdir_fid, page)
                if result is None:
                    return None
                entries = result.get("entries") or []
                total = result.get("total") or 0
                for entry in entries:
                    if entry.get("is_dir"):
                        if depth == 0 and entry.get("fid"):
                            queue.append((entry["fid"], 1))
                        continue
                    files.append(entry)
                if not entries or len(entries) < PAGE_SIZE:
                    break
                if total and page * PAGE_SIZE >= total:
                    break
                page += 1
        return files
