"""
aria2 JSON-RPC 2.0 同步客户端封装。
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

# 任务查询统一使用的字段，避免逐字段请求造成多次往返。
_STATUS_KEYS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed",
    "dir", "files", "bittorrent",
]


class Aria2:
    """
    aria2 下载器客户端，封装 JSON-RPC 调用与任务管理操作。

    连接无登录态，secret 仅作为 RPC 令牌放在每次请求的 params 首位。
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 secret: Optional[str] = None, use_ssl: Optional[bool] = False,
                 **kwargs: object) -> None:
        """
        根据显式配置创建 aria2 客户端。

        :param host: 服务主机，可带协议前缀；缺少地址时保留未连接状态
        :param port: RPC 端口，省略时使用 aria2 默认端口 6800
        :param secret: RPC 令牌；为空时不附加 token 参数
        :param use_ssl: 是否使用 https 协议
        :param kwargs: 其余配置当前不读取
        """
        self._secret = str(secret).strip() if secret else None
        self._base_url = self.__build_base_url(host, port, bool(use_ssl))
        self._client = self.__build_client()
        self._rpc_id = 0
        if not self._base_url:
            logger.error("aria2 配置不完整！")

    @staticmethod
    def __build_base_url(host: Optional[str], port: Optional[int], use_ssl: bool) -> Optional[str]:
        """
        拼接 JSON-RPC 基础地址，兼容只填主机或填完整 URL 两种配置方式。

        :param host: 服务主机或完整 URL
        :param port: RPC 端口，省略时使用 6800
        :param use_ssl: 是否优先使用 https
        :return: 不含 /jsonrpc 的基础地址；主机为空时返回 None
        """
        host_text = str(host or "").strip().rstrip("/")
        if not host_text:
            return None
        scheme = "https" if use_ssl else "http"
        if "://" in host_text:
            parsed = urlsplit(host_text)
            scheme = parsed.scheme or scheme
            host_text = parsed.netloc or parsed.path
        # 已有端口时不覆盖，允许通过主机字段直接指定非默认端口。
        if ":" not in host_text.rsplit("]", 1)[-1]:
            host_text = f"{host_text}:{port or 6800}"
        return f"{scheme}://{host_text}"

    def _call(self, method: str, params: List[Any]) -> Any:
        """
        发送 JSON-RPC 请求并返回 result 字段。

        任何连接、协议或服务端错误都记录中文说明并返回 None，不向调用方抛出。

        :param method: aria2 RPC 方法名
        :param params: 方法参数列表，secret 由本方法自动放到首位
        :return: RPC 返回的 result；调用失败时返回 None
        """
        if not self._base_url:
            logger.warn(f"aria2 未配置连接地址，跳过 RPC 调用：{method}")
            return None
        self._rpc_id += 1
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": str(self._rpc_id),
            "method": method,
            "params": ([f"token:{self._secret}"] if self._secret else []) + list(params),
        }
        try:
            response = self._client.post_res(f"{self._base_url}/jsonrpc", json=payload)
            if response is None:
                logger.warn(f"aria2 RPC 请求失败：{method} - 无法连接服务")
                return None
            data = response.json()
            if data.get("error"):
                error = data.get("error") or {}
                logger.warn(f"aria2 RPC 调用失败：{method} - {error.get('message') or error}")
                return None
            return data.get("result")
        except Exception as err:
            logger.warn(f"aria2 RPC 请求异常：{method} - {err}")
            return None

    def is_inactive(self) -> bool:
        """
        判断已配置的下载器是否需要重新连接。

        aria2 按请求建立 HTTP 连接，没有需要维持的登录会话，因此不会进入失活状态。

        :return: 恒为 False；连接可用性由 transfer_info 探测
        """
        return False

    def reconnect(self) -> None:
        """
        重建 HTTP 会话；aria2 无登录态，重连不改变连接配置。
        """
        self._client = self.__build_client()

    @staticmethod
    def __build_client() -> RequestUtils:
        """
        构造统一 HTTP 客户端，复用持久会话与项目传输策略。

        :return: 已配置 JSON 内容类型与超时的同步客户端
        """
        return RequestUtils(
            use_session=True,
            content_type="application/json",
            accept_type="application/json",
            timeout=10,
        )

    def transfer_info(self) -> Optional[Dict[str, Any]]:
        """
        读取下载器全局传输统计。

        :return: getGlobalStat 结果；未连接或调用失败时返回 None
        """
        result = self._call("aria2.getGlobalStat", [])
        return result if isinstance(result, dict) else None

    def add_uri(self, uris: List[str], options: Dict[str, Any]) -> Optional[str]:
        """
        添加 HTTP 直链或磁力任务。

        :param uris: 待下载地址列表
        :param options: aria2 任务选项
        :return: 新任务 gid；添加失败时返回 None
        """
        result = self._call("aria2.addUri", [list(uris), options or {}])
        return str(result) if result else None

    def add_torrent(self, content: bytes, options: Dict[str, Any]) -> Optional[str]:
        """
        以 base64 上传种子内容并添加任务。

        :param content: 种子文件原始字节
        :param options: aria2 任务选项
        :return: 新任务 gid；添加失败时返回 None
        """
        try:
            encoded = base64.b64encode(content).decode("ascii")
        except Exception as err:
            logger.warn(f"aria2 种子内容编码失败：{err}")
            return None
        result = self._call("aria2.addTorrent", [encoded, [], options or {}])
        return str(result) if result else None

    def tell_status(self, gid: str, keys: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """
        查询单个任务状态。

        :param gid: 任务 gid
        :param keys: 需要返回的字段列表，省略时使用统一字段集合
        :return: 任务状态字典；任务不存在或调用失败时返回 None
        """
        if not gid:
            return None
        result = self._call("aria2.tellStatus", [gid, keys or _STATUS_KEYS])
        return result if isinstance(result, dict) else None

    def tell_stopped(self, offset: int = 0, num: int = 1000) -> List[Dict[str, Any]]:
        """
        分页查询已停止任务。

        :param offset: 起始偏移
        :param num: 单次返回条数上限
        :return: 已停止任务列表；调用失败时返回空列表
        """
        result = self._call("aria2.tellStopped", [offset, num, _STATUS_KEYS])
        return result if isinstance(result, list) else []

    def get_files(self, gid: str) -> List[Dict[str, Any]]:
        """
        读取任务文件列表。

        :param gid: 任务 gid
        :return: getFiles 结果；任务不存在或调用失败时返回空列表
        """
        if not gid:
            return []
        result = self._call("aria2.getFiles", [gid])
        return result if isinstance(result, list) else []

    def remove(self, gid: str) -> bool:
        """
        删除任务，运行中任务先取消，已停止任务再清理下载结果。

        :param gid: 任务 gid
        :return: 任一删除步骤成功时返回 True
        """
        if not gid:
            return False
        if self._call("aria2.remove", [gid]):
            return True
        return bool(self._call("aria2.removeDownloadResult", [gid]))

    def pause(self, gid: str) -> bool:
        """
        暂停任务。

        :param gid: 任务 gid
        :return: 请求成功时返回 True
        """
        return bool(gid) and bool(self._call("aria2.pause", [gid]))

    def unpause(self, gid: str) -> bool:
        """
        恢复任务。

        :param gid: 任务 gid
        :return: 请求成功时返回 True
        """
        return bool(gid) and bool(self._call("aria2.unpause", [gid]))

    def change_option(self, gid: str, options: Dict[str, Any]) -> bool:
        """
        修改运行中或已暂停任务的选项。

        :param gid: 任务 gid
        :param options: 需要修改的选项
        :return: 请求成功时返回 True
        """
        if not gid or not options:
            return False
        return bool(self._call("aria2.changeOption", [gid, options]))

    def save_session(self) -> bool:
        """
        持久化当前会话，避免重启后丢失任务。

        :return: 请求成功时返回 True
        """
        return self._call("aria2.saveSession", []) is not None
