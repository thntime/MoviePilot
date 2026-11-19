"""
PanSou 网盘聚合搜索客户端，负责搜索、健康检查与平台分组链接的归一化。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.adapters.network.http import RequestUtils


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


class PansouClient:
    """
    PanSou 同步客户端，负责按关键词搜索与健康检查。

    token 可选，只随请求头携带，不落盘；服务端未开启认证时留空即可。
    """

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: int = 30) -> None:
        """
        保存连接配置，构造时不发起网络请求。

        :param base_url: PanSou 服务地址（API 根，不是前端页面地址）
        :param token: 可选的 Bearer token
        :param timeout: 单次请求超时秒数
        """
        self.base_url = _as_text(base_url).rstrip("/")
        self.token = _as_text(token)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._http: Optional[RequestUtils] = None

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

    def _request_json(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        发起一次 JSON GET 请求并返回状态码与响应体。

        :param path: 相对 PanSou 根地址的接口路径
        :param params: 查询参数
        :return: HTTP 状态码与 JSON 字典；响应体不是字典时为空
        :raises PansouError: 网络不可达或没有拿到响应
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self._get_http().get_res(
            f"{self.base_url}{path}",
            params=params,
            headers=headers,
        )
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

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        """
        搜索关键词，把按平台归并的链接展开成逐条条目。

        :param keyword: 搜索关键词
        :return: 条目列表，每条含 platform、url、password、title、source、pubdate
        :raises PansouError: 认证失败、HTTP 错误、接口返回失败或响应不是有效 JSON
        """
        status, payload = self._request_json(
            "/api/search",
            params={"kw": keyword, "res": "merge", "src": "all"},
        )
        if status in (401, 403):
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
        status, payload = self._request_json("/api/health")
        if status in (401, 403):
            raise PansouError("PanSou 认证失败：请在站点里填写 Bearer token")
        if status >= 400:
            raise PansouError(f"PanSou 连接失败：HTTP {status}")
        if payload is None:
            raise PansouError("PanSou 连接失败：响应不是有效 JSON")
        return payload
