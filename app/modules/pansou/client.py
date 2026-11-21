"""
PanSou 网盘聚合搜索客户端，负责登录、搜索、健康检查与平台分组链接的归一化。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.adapters.network.http import RequestUtils

# 登录令牌提前该秒数视为过期，避免临界点拿将失效的令牌去搜索。
_TOKEN_REFRESH_MARGIN = 300
# 未提供过期时间时按一天处理，与 PanSou 默认签发有效期一致。
_DEFAULT_TOKEN_TTL = 86400


class PansouError(Exception):
    """
    PanSou 请求失败，异常消息为可直接展示的中文原因。
    """


def _as_text(value: Any) -> str:
    """
    将接口字段转换为去除首尾空白的文本。

    :param value: 原始字段值
    :return: 文本；空值返回空字符串
    """
    if value is None:
        return ""
    return str(value).strip()


def _as_timestamp(value: Any) -> Optional[float]:
    """
    将接口返回的过期时间转换为时间戳。

    :param value: 原始字段值，可能是秒级时间戳或数字文本
    :return: 时间戳；无法解析时返回 None
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PansouClient:
    """
    PanSou 同步客户端，负责按关键词搜索、健康检查与账号密码登录。

    凭据有两种：显式 Bearer token，或站点账号密码（登录换取 token 并缓存在内存）。
    显式 token 优先；token 只随请求头携带，不落盘。
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        """
        保存连接配置，构造时不发起网络请求。

        :param base_url: PanSou 服务地址（API 根，不是前端页面地址）
        :param token: 可选的 Bearer token
        :param username: 可选的登录用户名，与密码同时提供时按需登录
        :param password: 可选的登录密码
        :param timeout: 单次请求超时秒数
        """
        self.base_url = _as_text(base_url).rstrip("/")
        self.token = _as_text(token)
        self.username = _as_text(username)
        self.password = password or ""
        self.timeout = timeout
        self._lock = threading.RLock()
        self._http: Optional[RequestUtils] = None
        self._session_token = ""
        self._session_expires_at = 0.0

    def _get_http(self) -> RequestUtils:
        """
        惰性创建并复用网络适配器实例。
        """
        with self._lock:
            if self._http is None:
                self._http = RequestUtils(
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=self.timeout,
                )
            return self._http

    def _clear_token(self) -> None:
        """
        清空内存中的登录令牌与过期时间。
        """
        with self._lock:
            self._session_token = ""
            self._session_expires_at = 0.0

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        发起一次 JSON 请求并返回状态码与响应体。

        :param method: HTTP 方法，支持 GET 与 POST
        :param path: 相对 PanSou 根地址的接口路径
        :param params: GET 查询参数
        :param json_body: POST JSON 请求体
        :param token: 可选的 Bearer token
        :return: HTTP 状态码与 JSON 字典；响应体不是字典时为空
        :raises PansouError: 网络不可达或没有拿到响应
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http = self._get_http()
        url = f"{self.base_url}{path}"
        if method.upper() == "POST":
            response = http.post_res(url, json=json_body, headers=headers)
        else:
            response = http.get_res(url, params=params, headers=headers)
        try:
            if response is None:
                raise PansouError("PanSou 网络请求失败，请检查地址是否可访问")
            status = int(getattr(response, "status_code", 0) or 0)
            content = getattr(response, "content", b"") or b""
            try:
                # 服务端 Content-Type 不带 charset，只有按 UTF-8 解码才能拿到正确的中文标题。
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload = None
            return status, payload if isinstance(payload, dict) else None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响本次结果
                    pass

    def login(self) -> str:
        """
        使用站点账号密码登录并缓存令牌。

        :return: 新签发的令牌
        :raises PansouError: 未配置账号密码、网络失败、凭据错误或响应缺少令牌
        """
        if not self.username or not self.password:
            raise PansouError("PanSou 登录失败：未配置用户名或密码")
        try:
            status, payload = self._request_json(
                "POST",
                "/api/auth/login",
                json_body={"username": self.username, "password": self.password},
            )
        except PansouError:
            self._clear_token()
            raise
        if status in (401, 403):
            self._clear_token()
            raise PansouError("PanSou 登录失败：用户名或密码错误")
        if status >= 400:
            self._clear_token()
            raise PansouError(f"PanSou 登录失败：HTTP {status}")
        if payload is None:
            self._clear_token()
            raise PansouError("PanSou 登录失败：响应不是有效 JSON")
        token = _as_text(payload.get("token"))
        if not token:
            self._clear_token()
            raise PansouError("PanSou 登录失败：响应缺少 token")
        expires_at = _as_timestamp(payload.get("expires_at")) or time.time() + _DEFAULT_TOKEN_TTL
        with self._lock:
            self._session_token = token
            self._session_expires_at = expires_at
        return token

    def _bearer(self) -> str:
        """
        返回本次请求应携带的令牌，必要时先登录。

        :return: Bearer 令牌；没有可用凭据时返回空字符串
        """
        if self.token:
            return self.token
        if not (self.username and self.password):
            return ""
        with self._lock:
            if self._session_token and time.time() < self._session_expires_at - _TOKEN_REFRESH_MARGIN:
                return self._session_token
        return self.login()

    def _authorized_json(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        携带令牌请求受保护接口，令牌被拒且配置了账号密码时重登一次再试。

        :param path: 相对 PanSou 根地址的接口路径
        :param params: 查询参数
        :return: HTTP 状态码与 JSON 字典
        :raises PansouError: 网络不可达或登录失败
        """
        status, payload = self._request_json("GET", path, params=params, token=self._bearer())
        if status in (401, 403) and self.username and self.password:
            self._clear_token()
            status, payload = self._request_json("GET", path, params=params, token=self.login())
        return status, payload

    def verify_credentials(self) -> None:
        """
        校验站点凭据是否被服务端接受，用于站点连接测试。

        配置了账号密码时以登录结果为准；只有令牌时打一次不命中任何资源的单频道
        搜索，用状态码判断令牌是否被接受。

        :raises PansouError: 凭据缺失、被服务端拒绝或请求失败
        """
        if self.username and self.password:
            self.login()
            return
        if not self.token:
            raise PansouError("PanSou 未配置凭据：请填写用户名密码或 Bearer token")
        status, payload = self._request_json(
            "GET",
            "/api/search",
            params={"kw": uuid.uuid4().hex[:8], "res": "merge", "src": "tg"},
            token=self.token,
        )
        if status in (401, 403):
            raise PansouError("PanSou 认证失败：Bearer token 无效或已过期")
        if status >= 400:
            raise PansouError(f"PanSou 连接失败：HTTP {status}")
        if payload is None:
            raise PansouError("PanSou 连接失败：响应不是有效 JSON")

    def search(self, keyword: str, cloud_types: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        搜索关键词，把按平台归并的链接展开成逐条条目。

        :param keyword: 搜索关键词
        :param cloud_types: 可选的 PanSou 网盘平台取值，多个用逗号分隔；为空时不带该参数
        :return: 条目列表，每条含 platform、url、password、title、source、pubdate
        :raises PansouError: 认证失败、HTTP 错误、接口返回失败或响应不是有效 JSON
        """
        params: Dict[str, Any] = {"kw": keyword, "res": "merge", "src": "all"}
        if cloud_types:
            # 服务端只认逗号分隔的取值，写成竖线或空格会静默返回 0 条，故非空才加。
            params["cloud_types"] = cloud_types
        status, payload = self._authorized_json("/api/search", params=params)
        if status in (401, 403):
            if self.username and self.password:
                raise PansouError("PanSou 认证失败：用户名或密码无效")
            raise PansouError("PanSou 认证失败：请在站点里填写 Bearer token")
        if status == 429:
            raise PansouError("PanSou 搜索请求过于频繁（HTTP 429），请稍后重试")
        if status >= 400:
            raise PansouError(f"PanSou 搜索失败：HTTP {status}")
        if payload is None:
            raise PansouError("PanSou 搜索失败：响应不是有效 JSON")
        if payload.get("code") not in (0, None):
            message = _as_text(payload.get("message")) or "接口返回失败"
            raise PansouError(f"PanSou 搜索失败：{message}")
        data = payload.get("data")
        merged = data.get("merged_by_type") if isinstance(data, dict) else None
        entries: List[Dict[str, Any]] = []
        if not isinstance(merged, dict):
            return entries
        for platform, links in merged.items():
            if not isinstance(links, list):
                continue
            for link in links:
                if not isinstance(link, dict):
                    continue
                entries.append(
                    {
                        "platform": _as_text(platform),
                        "url": _as_text(link.get("url")),
                        "password": _as_text(link.get("password")),
                        "title": _as_text(link.get("note")),
                        "source": _as_text(link.get("source")),
                        "pubdate": _as_text(link.get("datetime")),
                    }
                )
        return entries

    def health(self) -> Dict[str, Any]:
        """
        读取服务健康状态，用于站点连接测试。

        :return: 健康检查响应字典
        :raises PansouError: 认证失败、HTTP 错误或响应不是有效 JSON
        """
        status, payload = self._request_json("GET", "/api/health")
        if status in (401, 403):
            raise PansouError("PanSou 认证失败：请在站点里填写 Bearer token")
        if status >= 400:
            raise PansouError(f"PanSou 连接失败：HTTP {status}")
        if payload is None:
            raise PansouError("PanSou 连接失败：响应不是有效 JSON")
        return payload
