"""
网盘平台适配器的共享 HTTP 封装与默认实现。

具体平台适配器只实现协议差异，请求头装配、响应解析与错误映射在此收敛。
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from app.adapters.network.http import RequestUtils

# 网盘业务 API 均为国内直连域名；宿主代理常按域名分流，某些域名（如 yun.123pan.cn）
# 经代理会直接失败。这里显式关闭代理，避免环境变量 HTTPS_PROXY 干扰取链与登录校验。
_DIRECT_PROXIES = {"http": "", "https": ""}
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


def _cookie_attributes(segments: Sequence[str]) -> dict[str, str]:
    """
    解析 `Set-Cookie` 中 Cookie 值之后的属性。

    :param segments: 按 `;` 拆分的属性片段
    :return: 小写属性名到属性值的映射
    """
    attributes: dict[str, str] = {}
    for segment in segments:
        item = str(segment).strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            attributes[key.strip().lower()] = value.strip()
        else:
            attributes[item.lower()] = ""
    return attributes


def _cookie_deleted(attributes: Mapping[str, str]) -> bool:
    """
    判断 `Set-Cookie` 是否要求删除该 Cookie。

    `Max-Age` 不大于 0 或 `Expires` 已过期时视为删除。

    :param attributes: `Set-Cookie` 属性映射
    :return: 是否删除
    """
    max_age = attributes.get("max-age")
    if max_age is not None:
        try:
            return float(max_age) <= 0
        except ValueError:
            pass
    expires = attributes.get("expires")
    if not expires:
        return False
    try:
        moment = parsedate_to_datetime(expires)
    except (TypeError, ValueError):
        return False
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


def merge_cookie_header(cookie_header: str, set_cookies: Sequence[str]) -> str:
    """
    把响应下发的 `Set-Cookie` 合并进原始 Cookie 头。

    同名 Cookie 以后下发的值为准；`Max-Age` 不大于 0 或 `Expires` 已过期的键会被删除。

    :param cookie_header: 原始 Cookie 头
    :param set_cookies: 响应下发的 `Set-Cookie` 行
    :return: 合并后的 Cookie 头
    """
    pairs: list[tuple[str, str]] = []
    for item in str(cookie_header or "").split(";"):
        part = item.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    order = [name for name, _value in pairs]
    values = dict(pairs)
    for header in set_cookies:
        segments = str(header).split(";")
        pair = segments[0].strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            continue
        if _cookie_deleted(_cookie_attributes(segments[1:])):
            values.pop(name, None)
            if name in order:
                order.remove(name)
            continue
        values[name] = value.strip()
        if name not in order:
            order.append(name)
    return "; ".join(f"{name}={values[name]}" for name in order if name in values)


def response_set_cookies(response: Any) -> list[str]:
    """
    读取响应下发的全部 `Set-Cookie` 行。

    兼容 httpx2 的 `headers.get_list`、旧版 `getlist` 与单值 header。

    :param response: HTTP 响应
    :return: `Set-Cookie` 行列表
    """
    headers = getattr(response, "headers", None)
    for getter_name in ("get_list", "getlist"):
        getter = getattr(headers, getter_name, None)
        if callable(getter):
            values = getter("Set-Cookie")
            if values:
                return [str(item) for item in values]
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    for getter_name in ("get_list", "getlist"):
        getter = getattr(raw_headers, getter_name, None)
        if callable(getter):
            values = getter("Set-Cookie")
            if values:
                return [str(item) for item in values]
    if headers is not None and hasattr(headers, "get"):
        value = headers.get("Set-Cookie")
        if value:
            return [str(value)]
    return []


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
        # 默认直连国内网盘域名；需要与登录浏览器保持同一出口 IP 的平台可改为 None（沿用环境代理）。
        self.proxies: Optional[dict[str, str]] = _DIRECT_PROXIES
        # 由装配层赋值的 Cookie 回写钩子；回调收到本次请求使用的账号与合并后的完整凭据。
        self.credential_sink: Optional[Callable[[NetdiskAccountRecord, Mapping[str, str]], None]] = None
        # 由装配层赋值的续期钩子；鉴权失败时换取新凭据后重试一次。
        self.credential_refresher: Optional[
            Callable[[NetdiskAccountRecord], Optional[Mapping[str, str]]]
        ] = None
        # 记录当前线程是否正在续期，避免续期请求自身再次触发续期。
        self._refresh_state = threading.local()

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

    def _write_back_cookies(self, account: Optional[NetdiskAccountRecord], response: Any) -> None:
        """
        把响应 `Set-Cookie` 合并回账号 Cookie 并交给回写钩子。

        仅处理凭据中含 `cookie`/`cookie_header`/`cookies` 的账号；合并结果与原凭据
        相同、或没有下发新 Cookie 时跳过。解析与回调异常只记录告警，不影响请求结果。

        :param account: 本次请求使用的账号
        :param response: HTTP 响应
        """
        if account is None or self.credential_sink is None:
            return
        credential = account.credential or {}
        key = next(
            (name for name in ("cookie", "cookie_header", "cookies") if credential.get(name)),
            "",
        )
        if not key:
            return
        try:
            set_cookies = response_set_cookies(response)
            if not set_cookies:
                return
            original = str(credential.get(key) or "")
            merged = merge_cookie_header(original, set_cookies)
            if not merged or merged == original:
                return
            refreshed = dict(credential)
            refreshed[key] = merged
            self.credential_sink(account, refreshed)
        except Exception as error:  # noqa: BLE001 - 回写失败不能影响原请求
            logger.warning(
                f"网盘响应 Cookie 回写失败：platform={self.platform.value} "
                f"field={key} error={type(error).__name__}"
            )

    def _refresh_credential(self, account: NetdiskAccountRecord) -> Optional[NetdiskAccountRecord]:
        """
        鉴权失败后就地续期一次，返回使用新凭据的账号记录。

        续期请求本身不再触发续期，避免递归；续期失败返回 None 并保留原凭据。

        :param account: 本次请求使用的账号
        :return: 使用新凭据的账号记录；无法续期时返回 None
        """
        if self.credential_refresher is None or getattr(self._refresh_state, "active", False):
            return None
        self._refresh_state.active = True
        try:
            credential = self.credential_refresher(account)
        except Exception as error:  # noqa: BLE001 - 续期失败保留旧凭据
            logger.warning(
                f"网盘鉴权失败后自动续期失败：platform={self.platform.value} "
                f"account={account.name} error={type(error).__name__}"
            )
            return None
        finally:
            self._refresh_state.active = False
        if not credential:
            return None
        return replace(account, credential=dict(credential))

    def _dispatch(
        self,
        method: str,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord],
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """
        使用指定账号发送一次请求。

        :param method: 请求方法，`GET` 或 `POST`
        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param json_body: JSON 请求体
        :param data: 表单请求体
        :param headers: 额外请求头
        :return: HTTP 响应
        """
        request = RequestUtils(
            headers=self.headers(account, extra=headers),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if method == "GET":
            return request.get_res(url, params=dict(params or {}), allow_redirects=True)
        return request.post_res(
            url,
            data=dict(data) if data is not None else None,
            json=dict(json_body) if json_body is not None else None,
            params=dict(params or {}),
            allow_redirects=True,
        )

    def _dispatch_with_retry(
        self,
        method: str,
        url: str,
        *,
        account: Optional[NetdiskAccountRecord],
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """
        发送请求；鉴权失败时续期一次并重试原请求。

        网络错误、限流与其他状态码不重试，也不修改凭据。

        :param method: 请求方法
        :param url: 请求地址
        :param account: 账号记录
        :param params: 查询参数
        :param json_body: JSON 请求体
        :param data: 表单请求体
        :param headers: 额外请求头
        :return: HTTP 响应
        """
        response = self._dispatch(
            method,
            url,
            account=account,
            params=params,
            json_body=json_body,
            data=data,
            headers=headers,
        )
        self._write_back_cookies(account, response)
        if response is None or account is None:
            return response
        if getattr(response, "status_code", 0) not in (401, 403):
            return response
        refreshed = self._refresh_credential(account)
        if refreshed is None:
            return response
        retried = self._dispatch(
            method,
            url,
            account=refreshed,
            params=params,
            json_body=json_body,
            data=data,
            headers=headers,
        )
        self._write_back_cookies(refreshed, retried)
        return retried

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
        response = self._dispatch_with_retry(
            "GET",
            url,
            account=account,
            params=params,
            headers=headers,
        )
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
        response = self._dispatch_with_retry(
            "POST",
            url,
            account=account,
            params=params,
            json_body=json,
            data=data,
            headers=headers,
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
        response = self._dispatch_with_retry(
            "GET",
            url,
            account=account,
            params=params,
            headers=headers,
        )
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
            # 403 不一定代表登录态失效：夸克等平台会用 403 返回业务错误（如文件 token 校验失败）。
            # 优先透出服务端 message，避免把账号正常的情况误报成「登录态已失效」。
            message = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    message = str(
                        payload.get("message")
                        or payload.get("error_description")
                        or payload.get("error")
                        or ""
                    ).strip()
            except Exception:  # noqa: BLE001 - 非 JSON 响应保持通用提示
                message = ""
            detail = f"：{message}" if message else ""
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                f"网盘接口拒绝（HTTP {response.status_code}）{detail}",
                platform=self.platform,
            )
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
        if status_code == 404 and self.platform is NetdiskPlatform.UC:
            return NetdiskError(
                NetdiskErrorCode.SHARE_INVALID,
                "UC 分享已失效或已被删除",
                platform=self.platform,
            )
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
    supports_refresh: bool = False
    drive_capabilities: DriveCapabilities = DriveCapabilities()
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
        transfer_first: bool = False,
    ) -> DirectLink:
        """
        获取直链；子类必须实现。

        :param ref: 分享引用
        :param file: 目标文件
        :param account: 取链账号
        :param allow_transfer: 是否允许转存兜底
        :param transfer_first: 是否优先转存；False 时保持免转存优先
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

    def quota(self, account: NetdiskAccountRecord) -> DriveQuota:
        """
        查询个人网盘空间。

        :param account: 账号
        :return: 空间配额
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持空间查询", platform=self.platform)

    def list_dir(self, account: NetdiskAccountRecord, parent_id: str) -> list[DriveFile]:
        """
        列出个人网盘目录。

        :param account: 账号
        :param parent_id: 父目录标识，空串表示根目录
        :return: 目录条目
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account, parent_id
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持目录浏览", platform=self.platform)

    def download_file(self, account: NetdiskAccountRecord, file: DriveFile) -> DirectLink:
        """
        获取个人盘文件直链。

        :param account: 账号
        :param file: 文件条目
        :return: 直链
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account, file
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持个人盘下载", platform=self.platform)

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

        :param account: 账号
        :param files: 文件列表
        :param password: 提取码
        :param expires_in: 有效期秒数，0 表示平台默认
        :return: 分享结果
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account, files, password, expires_in
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持创建分享", platform=self.platform)

    def move_files(self, account: NetdiskAccountRecord, files: list[DriveFile], target_id: str) -> None:
        """
        移动文件。

        :param account: 账号
        :param files: 文件列表
        :param target_id: 目标目录
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account, files, target_id
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持移动文件", platform=self.platform)

    def rename_file(self, account: NetdiskAccountRecord, file: DriveFile, name: str) -> None:
        """
        重命名文件。

        :param account: 账号
        :param file: 文件条目
        :param name: 新名称
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account, file, name
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持重命名", platform=self.platform)

    def delete_files(self, account: NetdiskAccountRecord, files: list[DriveFile]) -> None:
        """
        删除文件。

        :param account: 账号
        :param files: 文件列表
        :raises NetdiskError: 平台不支持时抛出 UNSUPPORTED
        """
        del account, files
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持删除", platform=self.platform)

    def login_ready(self, credential: Mapping[str, str]) -> bool:
        """
        判断网页登录是否已拿到可保存的登录态。

        默认只要适配器返回了 Cookie 就认为可保存；平台可覆盖为更严格的判断。

        :param credential: 浏览器抓到的凭据
        :return: 是否可进入待确认状态
        """
        del credential
        return True

    def login_account_name(self, credential: Mapping[str, str]) -> str:
        """
        从登录凭据中提取可展示的账号名。

        :param credential: 浏览器抓到的凭据
        :return: 账号名；无法提取时为空
        """
        del credential
        return ""

    def probe_account(self, account: NetdiskAccountRecord) -> AccountProbe:
        """
        探测账号登录态；默认按字段完整性判断。

        :param account: 账号记录
        :return: 探测结果
        """
        return self.validate_credential(account.credential)

    def needs_refresh(self, account: NetdiskAccountRecord) -> bool:
        """
        判断当前凭据是否已临期、需要在下次使用前续期。

        默认平台没有可解析的过期信息，返回 False；由应用层按 TTL 决定是否刷新。

        :param account: 账号记录
        :return: 是否需要续期
        """
        del account
        return False

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
