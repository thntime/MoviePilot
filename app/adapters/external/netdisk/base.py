"""
网盘平台适配器的共享 HTTP 封装与默认实现。

具体平台适配器只实现协议差异，请求头装配、响应解析与错误映射在此收敛。
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from app.adapters.network.http import RequestUtils
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
    parse_share_link,
)
from app.runtime.log import logger


def as_text(value: Any) -> str:
    """
    把接口字段转换为去除首尾空白的文本。

    :param value: 原始字段
    :return: 文本
    """
    if value is None:
        return ""
    return str(value).strip()


def as_int(value: Any) -> int:
    """
    把接口字段转换为非负整数。

    :param value: 原始字段
    :return: 非负整数
    """
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def cookie_header(account: Optional[NetdiskAccountRecord]) -> str:
    """
    从账号凭据中取出 Cookie 头。

    :param account: 账号记录
    :return: Cookie 头文本
    """
    if account is None:
        return ""
    credential = account.credential or {}
    for key in ("cookie", "cookie_header", "cookies"):
        value = credential.get(key)
        if value:
            return str(value)
    return "; ".join(f"{key}={value}" for key, value in credential.items())


def credential_value(account: Optional[NetdiskAccountRecord], *names: str) -> str:
    """
    按名称读取账号凭据字段。

    :param account: 账号记录
    :param names: 候选字段名
    :return: 字段值
    """
    if account is None:
        return ""
    credential = account.credential or {}
    for name in names:
        value = credential.get(name)
        if value:
            return str(value)
    return ""


class NetdiskHttp:
    """
    按平台装配请求头的轻量 HTTP 客户端。
    """

    def __init__(
        self,
        *,
        platform: NetdiskPlatform,
        user_agent: str = "",
        referer: str = "",
        timeout: float = 20.0,
    ) -> None:
        """
        保存平台默认请求头。

        :param platform: 平台标识
        :param user_agent: 默认 UA
        :param referer: 默认 Referer
        :param timeout: 请求超时，单位秒
        """
        self.platform = platform
        self.user_agent = user_agent
        self.referer = referer
        self.timeout = timeout

    def headers(
        self,
        account: Optional[NetdiskAccountRecord] = None,
        *,
        extra: Optional[Mapping[str, str]] = None,
        include_cookie: bool = True,
    ) -> dict[str, str]:
        """
        组装请求头。

        :param account: 账号记录
        :param extra: 额外请求头
        :param include_cookie: 是否带上账号 Cookie
        :return: 请求头字典
        """
        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.referer:
            headers["Referer"] = self.referer
        if include_cookie:
            cookie = cookie_header(account)
            if cookie:
                headers["Cookie"] = cookie
        if extra:
            headers.update({str(key): str(value) for key, value in extra.items()})
        return headers

    def get_json(
        self,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """
        发起 GET 请求并解析 JSON。

        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param headers: 额外请求头
        :return: JSON 对象
        """
        request = RequestUtils(headers=self.headers(account, extra=headers), timeout=self.timeout)
        response = request.get_res(url, params=dict(params or {}), allow_redirects=True)
        return self._json(response)

    def post_json(
        self,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord] = None,
        json: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """
        发起 POST 请求并解析 JSON。

        :param url: 请求地址
        :param account: 账号记录
        :param json: JSON 请求体
        :param data: 表单请求体
        :param params: 查询参数
        :param headers: 额外请求头
        :return: JSON 对象
        """
        request = RequestUtils(headers=self.headers(account, extra=headers), timeout=self.timeout)
        response = request.post_res(
            url,
            data=dict(data) if data is not None else None,
            json=dict(json) if json is not None else None,
            params=dict(params or {}),
            allow_redirects=True,
        )
        return self._json(response)

    def get_text(
        self,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        """
        发起 GET 请求并返回文本。

        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param headers: 额外请求头
        :return: 响应文本
        """
        request = RequestUtils(headers=self.headers(account, extra=headers), timeout=self.timeout)
        response = request.get_res(url, params=dict(params or {}), allow_redirects=True)
        if response is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口无响应", platform=self.platform)
        if response.status_code >= 400:
            raise self.error_for_status(response.status_code)
        return str(response.text)

    def _json(self, response: Any) -> dict[str, Any]:
        """
        校验响应并解析 JSON。

        :param response: HTTP 响应
        :return: JSON 对象
        """
        if response is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口无响应", platform=self.platform)
        if response.status_code in (401, 403):
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "网盘登录态已失效", platform=self.platform)
        if response.status_code == 429:
            raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "网盘接口请求过于频繁", platform=self.platform)
        if response.status_code >= 500:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘服务暂时不可用", platform=self.platform)
        if response.status_code >= 400:
            raise self.error_for_status(response.status_code)
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - 非 JSON 响应统一归类
            # 115 等平台会用 text/html 返回合法 JSON，必须先尝试解析，
            # 只有解析失败时才按 HTML 防盗链页处理。
            content_type = str(response.headers.get("Content-Type", "")).lower()
            text = str(getattr(response, "text", "") or "").lstrip().lower()
            if "text/html" in content_type or text.startswith(("<!doctype", "<html")):
                raise NetdiskError(
                    NetdiskErrorCode.LINK_EXPIRED,
                    "网盘返回了登录或防盗链页面",
                    platform=self.platform,
                ) from error
            raise NetdiskError(
                NetdiskErrorCode.UNKNOWN,
                "网盘接口返回了非 JSON 响应",
                platform=self.platform,
            ) from error
        if not isinstance(payload, dict):
            return {"data": payload}
        return payload

    def error_for_status(self, status_code: int) -> NetdiskError:
        """
        把 HTTP 状态码映射为分类异常。

        :param status_code: 状态码
        :return: 分类异常
        """
        if status_code in (401, 403):
            return NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "网盘登录态已失效", platform=self.platform)
        if status_code == 429:
            return NetdiskError(NetdiskErrorCode.RATE_LIMITED, "网盘接口请求过于频繁", platform=self.platform)
        return NetdiskError(
            NetdiskErrorCode.UNKNOWN,
            f"网盘接口返回 HTTP {status_code}",
            platform=self.platform,
        )


class BaseNetdiskAdapter:
    """
    平台适配器的默认实现。

    子类至少需要覆盖 `share_detail`、`list_files` 与 `resolve`。
    """

    platform: NetdiskPlatform = NetdiskPlatform.QUARK
    link_policy: LinkPolicy = LinkPolicy.DIRECT
    requires_account: bool = True
    guest_supported: bool = False
    max_concurrency: int = 512
    cookie_required_keys: tuple[str, ...] = ()
    cookie_domains: tuple[str, ...] = ()
    login_url: str = ""
    supports_password_login: bool = False
    user_agent: str = ""
    referer: str = ""

    def __init__(self) -> None:
        """
        初始化共享 HTTP 客户端。
        """
        self.http = NetdiskHttp(
            platform=self.platform,
            user_agent=self.user_agent,
            referer=self.referer,
        )

    def parse_share(self, url: str, password: Optional[str]) -> ShareRef:
        """
        解析分享链接并校验平台。

        :param url: 分享链接或文案
        :param password: 已知提取码
        :return: 分享引用
        """
        ref = parse_share_link(url, password)
        if ref is None or ref.platform is not self.platform:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "分享链接与平台不匹配", platform=self.platform)
        return ref

    def share_detail(self, ref: ShareRef, account: Optional[NetdiskAccountRecord]) -> ShareDetail:
        """
        获取分享详情；子类必须实现。

        :param ref: 分享引用
        :param account: 取链账号
        :return: 分享详情
        """
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台暂不支持分享详情", platform=self.platform)

    def list_files(
        self,
        ref: ShareRef,
        parent_id: str,
        account: Optional[NetdiskAccountRecord],
    ) -> list[ShareFile]:
        """
        列出分享目录；子类必须实现。

        :param ref: 分享引用
        :param parent_id: 父目录标识
        :param account: 取链账号
        :return: 目录条目
        """
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台暂不支持列目录", platform=self.platform)

    def resolve(
        self,
        ref: ShareRef,
        file: ShareFile,
        account: Optional[NetdiskAccountRecord],
        *,
        allow_transfer: bool = True,
    ) -> DirectLink:
        """
        获取直链；子类必须实现。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底
        :return: 直链
        """
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台暂不支持取链", platform=self.platform)

    def cleanup(self, handle: TransferHandle, account: Optional[NetdiskAccountRecord]) -> None:
        """
        清理转存内容；默认无操作。

        :param handle: 转存句柄
        :param account: 转存账号
        """
        return None

    def validate_credential(self, credential: Mapping[str, str]) -> AccountProbe:
        """
        按必填字段校验凭据；子类可覆盖为调用平台接口。

        :param credential: 明文凭据
        :return: 探测结果
        """
        fields: dict[str, str] = {}
        for key, value in credential.items():
            if key in {"cookie", "cookie_header", "cookies"}:
                for part in str(value).split(";"):
                    item = part.strip()
                    if "=" in item:
                        name, field_value = item.split("=", 1)
                        fields[name.strip()] = field_value.strip()
            else:
                fields[str(key)] = str(value)
        missing = [key for key in self.cookie_required_keys if not fields.get(key)]
        if missing:
            return AccountProbe(status="expired", message="凭据缺少必填字段：" + "、".join(missing))
        return AccountProbe(status="active", message="凭据字段完整")

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态；默认按字段完整性判断。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential)

    def refresh_account(self, account: NetdiskAccountRecord) -> Optional[Mapping[str, str]]:
        """
        刷新账号令牌；默认不支持。

        :param account: 账号记录
        :return: 新凭据或 None
        """
        return None

    def password_login(self, username: str, password: str) -> Mapping[str, str]:
        """
        账号密码登录；默认不支持。

        :param username: 账号
        :param password: 密码
        :return: 凭据字段
        """
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持账号密码登录", platform=self.platform)

    def download_headers(
        self,
        account: Optional[NetdiskAccountRecord],
        *,
        extra: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """
        组装下载请求头，供直链携带。

        :param account: 账号记录
        :param extra: 额外请求头
        :return: 请求头
        """
        return self.http.headers(account, extra=extra)

    def log_failure(self, action: str, error: Exception, account: Optional[NetdiskAccountRecord]) -> None:
        """
        记录不含凭据的失败日志。

        :param action: 动作描述
        :param error: 异常
        :param account: 账号记录
        """
        logger.warning(
            f"网盘{action}失败：platform={self.platform.value} "
            f"account={account.name if account else '-'} error={type(error).__name__}"
        )

    @staticmethod
    def sleep(seconds: float) -> None:
        """
        在协议轮询中等待。

        :param seconds: 等待秒数
        """
        time.sleep(max(0.0, seconds))
