"""
网盘登录用例：粘贴 Cookie、CookieCloud 导入、账号密码与扫码会话。

本模块只编排端口，具体平台协议由 Adapter 与登录 Adapter 承担。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService, parse_cookie_header
from app.application.netdisk.contract import LoginResult, LoginSession, NetdiskAccountView
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform, normalize_platform


@dataclass(slots=True)
class _PendingLogin:
    """
    扫码会话的待落库上下文。

    :param platform: 平台标识
    :param name: 登录成功后写入的账号名称
    :param is_primary: 是否设为主账号
    :param account_id: 已有账号主键，新增账号时为 None
    """

    platform: NetdiskPlatform
    name: str
    is_primary: bool
    account_id: Optional[int] = None
    expires_at: float = 0.0


# 扫码会话的待落库上下文必须跨请求共享：接口每次请求都会新建用例实例，
# 若存在实例属性上，下一次轮询就找不到会话。
_QR_SESSION_TTL_SECONDS = 300
_PENDING: dict[str, _PendingLogin] = {}
_PENDING_LOCK = threading.RLock()


def reset_pending_sessions() -> None:
    """
    清空待完成扫码会话，供测试与生命周期重置使用。
    """
    with _PENDING_LOCK:
        _PENDING.clear()


class NetdiskLoginService:
    """
    网盘凭据获取与登录会话用例。
    """

    def __init__(
        self,
        accounts: Optional[NetdiskAccountService] = None,
        pending: Optional[dict[str, _PendingLogin]] = None,
    ) -> None:
        """
        保存账号服务与待完成会话。

        :param accounts: 账号用例；缺省时使用组合根注册的仓储
        :param pending: 待完成会话表；缺省时使用进程级共享表
        """
        self._accounts = accounts or NetdiskAccountService()
        self._pending = pending if pending is not None else _PENDING

    def paste_cookie(
        self,
        platform: object,
        cookie_text: str,
        *,
        name: Optional[str] = None,
        is_primary: bool = False,
    ) -> NetdiskAccountView:
        """
        保存用户粘贴的 Cookie 或令牌。

        :param platform: 平台标识
        :param cookie_text: Cookie 头或平台令牌
        :param name: 账号名称，缺省时使用平台名
        :param is_primary: 是否设为主账号
        :return: 新账号的脱敏投影
        """
        resolved = self._require_platform(platform)
        text = str(cookie_text or "").strip()
        if not text:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "请填写 Cookie 或令牌", platform=resolved)
        credential = self._credential_from_text(resolved, text)
        return self._accounts.create(
            resolved,
            name or self._default_name(resolved),
            credential,
            is_primary=is_primary,
        )

    def import_cookiecloud(
        self,
        platform: object,
        *,
        name: Optional[str] = None,
        is_primary: bool = False,
    ) -> NetdiskAccountView:
        """
        从 CookieCloud 按平台域名导入 Cookie。

        :param platform: 平台标识
        :param name: 账号名称
        :param is_primary: 是否设为主账号
        :return: 新账号的脱敏投影
        """
        resolved = self._require_platform(platform)
        adapter = registry.get_adapter(resolved)
        cookies = registry.get_cookie_import().import_cookies(adapter.cookie_domains)
        if not cookies:
            raise NetdiskError(
                NetdiskErrorCode.AUTH_EXPIRED,
                "CookieCloud 未找到该平台的 Cookie，请检查配置或改用粘贴",
                platform=resolved,
            )
        credential = {str(key): str(value) for key, value in cookies.items() if str(value).strip()}
        if not credential:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "CookieCloud 返回的 Cookie 为空", platform=resolved)
        return self._accounts.create(
            resolved,
            name or self._default_name(resolved),
            credential,
            is_primary=is_primary,
        )

    def password_login(
        self,
        platform: object,
        username: str,
        password: str,
        *,
        name: Optional[str] = None,
        is_primary: bool = False,
    ) -> NetdiskAccountView:
        """
        使用账号密码换取凭据并保存。

        :param platform: 平台标识
        :param username: 账号
        :param password: 密码
        :param name: 账号名称
        :param is_primary: 是否设为主账号
        :return: 新账号的脱敏投影
        """
        resolved = self._require_platform(platform)
        adapter = registry.get_adapter(resolved)
        if not adapter.supports_password_login:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持账号密码登录", platform=resolved)
        credential = adapter.password_login(str(username or "").strip(), str(password or ""))
        if not credential:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "账号或密码错误", platform=resolved)
        return self._accounts.create(
            resolved,
            name or str(username or "").strip() or self._default_name(resolved),
            credential,
            is_primary=is_primary,
        )

    def send_sms_code(self, platform: object, phone: str) -> dict[str, Any]:
        """
        发送短信验证码。

        :param platform: 平台标识
        :param phone: 手机号
        :return: 验证会话信息
        """
        resolved = self._require_platform(platform)
        adapter = registry.get_adapter(resolved)
        sender = getattr(adapter, "send_sms_code", None)
        if sender is None:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "该平台不支持短信验证码登录",
                platform=resolved,
            )
        return dict(sender(str(phone or "").strip()))

    def sms_login(
        self,
        platform: object,
        phone: str,
        code: str,
        verification_id: str,
        *,
        name: Optional[str] = None,
        is_primary: bool = False,
    ) -> NetdiskAccountView:
        """
        使用短信验证码登录并保存账号。

        :param platform: 平台标识
        :param phone: 手机号
        :param code: 短信验证码
        :param verification_id: 发码接口返回的验证会话
        :param name: 账号名称
        :param is_primary: 是否设为主账号
        :return: 新账号的脱敏投影
        """
        resolved = self._require_platform(platform)
        adapter = registry.get_adapter(resolved)
        login = getattr(adapter, "sms_login", None)
        if login is None:
            raise NetdiskError(
                NetdiskErrorCode.UNSUPPORTED,
                "该平台不支持短信验证码登录",
                platform=resolved,
            )
        credential = login(
            str(phone or "").strip(),
            str(code or "").strip(),
            str(verification_id or "").strip(),
        )
        if not credential:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "短信验证码登录失败", platform=resolved)
        return self._accounts.create(
            resolved,
            name or str(phone or "").strip() or self._default_name(resolved),
            credential,
            is_primary=is_primary,
            validate=False,
        )

    def start_qr(
        self,
        platform: object,
        *,
        name: Optional[str] = None,
        is_primary: bool = False,
        account_id: Optional[int] = None,
    ) -> LoginSession:
        """
        启动扫码登录会话。

        :param platform: 平台标识
        :param name: 登录成功后写入的账号名称
        :param is_primary: 是否设为主账号
        :param account_id: 已有账号主键，新增账号时为 None
        :return: 含二维码图片的会话
        """
        resolved = self._require_platform(platform)
        adapter = registry.get_adapter(resolved)
        session = registry.get_login_adapter().start(resolved, adapter.login_url, account_id)
        now = time.monotonic()
        with _PENDING_LOCK:
            expired = [key for key, item in self._pending.items() if item.expires_at and item.expires_at <= now]
            for key in expired:
                self._pending.pop(key, None)
            self._pending[session.session_id] = _PendingLogin(
                platform=resolved,
                name=name or self._default_name(resolved),
                is_primary=is_primary,
                account_id=account_id,
                expires_at=now + _QR_SESSION_TTL_SECONDS,
            )
        return session

    def poll_qr(self, session_id: str) -> LoginResult:
        """
        轮询扫码会话；成功时落库账号。

        :param session_id: 会话标识
        :return: 轮询结果
        """
        with _PENDING_LOCK:
            context = self._pending.get(session_id)
            if context is not None and context.expires_at and context.expires_at <= time.monotonic():
                self._pending.pop(session_id, None)
                context = None
        if context is None:
            return LoginResult(status="failed", message="登录会话不存在或已过期")
        result = registry.get_login_adapter().poll(session_id)
        if result.status != "success" or not result.credential:
            if result.status in {"failed", "expired"}:
                with _PENDING_LOCK:
                    self._pending.pop(session_id, None)
            return result
        try:
            adapter = registry.get_adapter(context.platform)
            probe = adapter.validate_credential(result.credential)
            if probe.status != "active":
                # 扫码成功后页面可能尚未完成跳转，__puus 等会话字段还没下发；
                # 此时保留会话继续轮询，避免刚扫完就提示登录失败。
                if "缺少必填字段" in (probe.message or ""):
                    return LoginResult(status="pending", message="正在等待登录完成")
                with _PENDING_LOCK:
                    self._pending.pop(session_id, None)
                return LoginResult(status="failed", message=probe.message or "登录态校验失败")
            if context.account_id:
                self._accounts.update(
                    context.account_id,
                    credential=result.credential,
                    validate=False,
                )
            else:
                self._accounts.create(
                    context.platform,
                    context.name,
                    result.credential,
                    is_primary=context.is_primary,
                    validate=False,
                )
        except Exception:
            with _PENDING_LOCK:
                self._pending.pop(session_id, None)
            registry.get_login_adapter().cancel(session_id)
            raise
        with _PENDING_LOCK:
            self._pending.pop(session_id, None)
        registry.get_login_adapter().cancel(session_id)
        return LoginResult(status="success", message="登录成功")

    def cancel_qr(self, session_id: str) -> None:
        """
        取消扫码会话并释放浏览器。

        :param session_id: 会话标识
        """
        with _PENDING_LOCK:
            self._pending.pop(session_id, None)
        registry.get_login_adapter().cancel(session_id)

    def _credential_from_text(
        self,
        platform: NetdiskPlatform,
        text: str,
    ) -> dict[str, str]:
        """
        按平台把粘贴文本转换为凭据字段。

        :param platform: 平台标识
        :param text: 用户输入
        :return: 凭据字段
        """
        if platform is NetdiskPlatform.PAN123 and "=" not in text and text.startswith("eyJ"):
            return {"token": text}
        fields = parse_cookie_header(text)
        if not fields and text:
            return {"token": text}
        return {"cookie": text}

    @staticmethod
    def _require_platform(platform: object) -> NetdiskPlatform:
        """
        解析平台标识。

        :param platform: 平台标识
        :return: 平台枚举
        """
        resolved = normalize_platform(platform)
        if resolved is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "未知的网盘平台")
        return resolved

    @staticmethod
    def _default_name(platform: NetdiskPlatform) -> str:
        """
        生成默认账号名称。

        :param platform: 平台标识
        :return: 账号名称
        """
        return f"{platform.value} 账号"
