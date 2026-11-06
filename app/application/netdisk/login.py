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
from app.application.netdisk.contract import (
    LoginResult,
    LoginSession,
    NetdiskAccountRecord,
    NetdiskAccountView,
)
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
    credential: Optional[Mapping[str, str]] = None
    account_name: str = ""
    validation_failures: int = 0


# 扫码会话的待落库上下文必须跨请求共享：接口每次请求都会新建用例实例，
# 若存在实例属性上，下一次轮询就找不到会话。
_QR_SESSION_TTL_SECONDS = 900
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
        account_id: Optional[int] = None,
    ) -> NetdiskAccountView:
        """
        保存用户粘贴的 Cookie 或令牌；指定账号时更新已有账号。

        :param platform: 平台标识
        :param cookie_text: Cookie 头或平台令牌
        :param name: 账号名称；缺省时按平台昵称自动生成
        :param is_primary: 是否设为主账号
        :param account_id: 已有账号主键，传入时更新凭据而不新建
        :return: 新账号或更新后账号的脱敏投影
        """
        resolved, existing = self._target(platform, account_id)
        text = str(cookie_text or "").strip()
        if not text:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "请填写 Cookie 或令牌", platform=resolved)
        credential = self.credential_from_text(resolved, text)
        if existing is not None:
            return self._accounts.update(
                existing.id,
                name=name,
                credential=credential,
                is_primary=True if is_primary else None,
            )
        return self._accounts.create(
            resolved,
            name or "",
            credential,
            is_primary=is_primary,
        )

    def import_cookiecloud(
        self,
        platform: object,
        *,
        name: Optional[str] = None,
        is_primary: bool = False,
        account_id: Optional[int] = None,
    ) -> NetdiskAccountView:
        """
        从 CookieCloud 按平台域名导入 Cookie；指定账号时更新已有账号。

        :param platform: 平台标识
        :param name: 账号名称
        :param is_primary: 是否设为主账号
        :param account_id: 已有账号主键，传入时更新凭据而不新建
        :return: 新账号或更新后账号的脱敏投影
        """
        resolved, existing = self._target(platform, account_id)
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
        if existing is not None:
            return self._accounts.update(
                existing.id,
                name=name,
                credential=credential,
                is_primary=True if is_primary else None,
            )
        return self._accounts.create(
            resolved,
            name or "",
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
        account_id: Optional[int] = None,
    ) -> NetdiskAccountView:
        """
        使用账号密码换取凭据并保存；指定账号时更新已有账号。

        :param platform: 平台标识
        :param username: 账号
        :param password: 密码
        :param name: 账号名称
        :param is_primary: 是否设为主账号
        :param account_id: 已有账号主键，传入时更新凭据而不新建
        :return: 新账号或更新后账号的脱敏投影
        """
        resolved, existing = self._target(platform, account_id)
        adapter = registry.get_adapter(resolved)
        if not adapter.supports_password_login:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持账号密码登录", platform=resolved)
        credential = adapter.password_login(str(username or "").strip(), str(password or ""))
        if not credential:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "账号或密码错误", platform=resolved)
        if existing is not None:
            return self._accounts.update(
                existing.id,
                name=name,
                credential=credential,
                is_primary=True if is_primary else None,
            )
        return self._accounts.create(
            resolved,
            name or "",
            credential,
            is_primary=is_primary,
        )

    def send_sms_code(
        self,
        platform: object,
        phone: str,
        *,
        account_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        发送短信验证码。

        :param platform: 平台标识
        :param phone: 手机号
        :param account_id: 已有账号主键，用于在省略平台时定位平台
        :return: 验证会话信息
        """
        resolved, _ = self._target(platform, account_id)
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
        account_id: Optional[int] = None,
    ) -> NetdiskAccountView:
        """
        使用短信验证码登录并保存账号；指定账号时更新已有账号。

        :param platform: 平台标识
        :param phone: 手机号
        :param code: 短信验证码
        :param verification_id: 发码接口返回的验证会话
        :param name: 账号名称
        :param is_primary: 是否设为主账号
        :param account_id: 已有账号主键，传入时更新凭据而不新建
        :return: 新账号或更新后账号的脱敏投影
        """
        resolved, existing = self._target(platform, account_id)
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
        if existing is not None:
            return self._accounts.update(
                existing.id,
                name=name,
                credential=credential,
                is_primary=True if is_primary else None,
                validate=False,
            )
        return self._accounts.create(
            resolved,
            name or "",
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
        resolved, _ = self._target(platform, account_id)
        adapter = registry.get_adapter(resolved)
        session = registry.get_login_adapter().start(resolved, adapter.login_url, account_id)
        now = time.monotonic()
        with _PENDING_LOCK:
            expired = [key for key, item in self._pending.items() if item.expires_at and item.expires_at <= now]
            for key in expired:
                self._pending.pop(key, None)
            self._pending[session.session_id] = _PendingLogin(
                platform=resolved,
                name=str(name or "").strip(),
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
        if context.credential:
            # 已经检测到登录态后不再重复调用平台接口，等待用户确认或取消。
            return LoginResult(
                status="detected",
                message="已检测到登录态，请确认保存",
                account_name=context.account_name,
            )
        result = registry.get_login_adapter().poll(session_id)
        if result.status != "success" or not result.credential:
            if result.status == "expired":
                with _PENDING_LOCK:
                    self._pending.pop(session_id, None)
            return result
        adapter = registry.get_adapter(context.platform)
        ready = getattr(adapter, "login_ready", None)
        if ready is not None and not ready(result.credential):
            return LoginResult(status="pending", message="正在等待登录完成")
        probe = adapter.validate_credential(result.credential)
        if probe.status != "active":
            # 登录过程中 Cookie/凭据是分批下发的：百度先有 BDUSS 才可能有 STOKEN，
            # 迅雷要等页面把 localStorage 写完。缺字段只代表还没登录完成，不能判失败。
            if "缺少必填字段" in (probe.message or ""):
                return LoginResult(status="pending", message="正在等待登录完成")
            # 其他校验失败给一段宽限时间，避免网络抖动或页面跳转中途误判。
            with _PENDING_LOCK:
                context.validation_failures += 1
                failures = context.validation_failures
            if failures < 30:
                return LoginResult(status="pending", message="正在等待登录完成")
            with _PENDING_LOCK:
                self._pending.pop(session_id, None)
            return LoginResult(status="failed", message=probe.message or "登录态校验失败")
        with _PENDING_LOCK:
            context.validation_failures = 0
        account_name = ""
        name_getter = getattr(adapter, "login_account_name", None)
        if name_getter is not None:
            account_name = str(name_getter(result.credential) or "")
        with _PENDING_LOCK:
            context.credential = dict(result.credential)
            context.account_name = account_name
        # 登录态已由平台接口确认，再把画面切回主页面。
        # 默认只置前、不关闭弹窗；弹窗由确认或取消时的会话销毁释放。
        focus = getattr(registry.get_login_adapter(), "focus_main_page", None)
        if focus is not None:
            try:
                focus(session_id)
            except Exception:  # noqa: BLE001 - 切页失败不影响已检测到的登录态
                pass
        return LoginResult(
            status="detected",
            message="已检测到登录态，请确认保存",
            account_name=account_name,
        )

    def confirm_qr(self, session_id: str) -> NetdiskAccountView:
        """
        用户确认后保存已检测到的登录态。

        :param session_id: 会话标识
        :return: 新账号或更新后的账号
        """
        with _PENDING_LOCK:
            context = self._pending.pop(session_id, None)
        if context is None or not context.credential:
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "没有待确认的登录态，请重新登录")
        try:
            if context.account_id:
                view = self._accounts.update(
                    context.account_id,
                    name=context.name or None,
                    credential=context.credential,
                    nickname=context.account_name or None,
                    validate=False,
                )
            else:
                view = self._accounts.create(
                    context.platform,
                    context.name or "",
                    context.credential,
                    is_primary=context.is_primary,
                    validate=False,
                    nickname=context.account_name,
                )
        finally:
            registry.get_login_adapter().cancel(session_id)
        return view

    def cancel_qr(self, session_id: str) -> None:
        """
        取消扫码会话并释放浏览器。

        :param session_id: 会话标识
        """
        with _PENDING_LOCK:
            self._pending.pop(session_id, None)
        registry.get_login_adapter().cancel(session_id)

    def qr_frame(self, session_id: str) -> dict[str, Any]:
        """
        读取网页登录当前画面。

        :param session_id: 会话标识
        :return: 图片与视口尺寸
        """
        adapter = registry.get_login_adapter()
        frame = getattr(adapter, "frame", None)
        if frame is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "当前登录方式不支持页面交互")
        return dict(frame(session_id))

    def qr_input(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        """
        转发点击、输入、按键或滚动，并返回最新画面。

        :param session_id: 会话标识
        :param kwargs: 操作参数
        :return: 最新画面
        """
        adapter = registry.get_login_adapter()
        handler = getattr(adapter, "input", None)
        if handler is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "当前登录方式不支持页面交互")
        handler(session_id, **kwargs)
        return self.qr_frame(session_id)

    def qr_stream_start(self, session_id: str, on_frame: Any) -> None:
        """
        启动网页登录实时画面。

        :param session_id: 会话标识
        :param on_frame: 帧回调
        """
        adapter = registry.get_login_adapter()
        starter = getattr(adapter, "start_stream", None)
        if starter is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "当前登录方式不支持实时画面")
        starter(session_id, on_frame)

    def qr_stream_stop(self, session_id: str) -> None:
        """
        停止网页登录实时画面。

        :param session_id: 会话标识
        """
        adapter = registry.get_login_adapter()
        stopper = getattr(adapter, "stop_stream", None)
        if stopper is not None:
            stopper(session_id)

    def qr_send_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        """
        转发页面事件。

        :param session_id: 会话标识
        :param event: 事件描述
        """
        adapter = registry.get_login_adapter()
        handler = getattr(adapter, "send_event", None)
        if handler is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "当前登录方式不支持页面交互")
        handler(session_id, event)

    def credential_from_text(
        self,
        platform: NetdiskPlatform,
        text: str,
    ) -> dict[str, str]:
        """
        按平台把粘贴文本转换为凭据字段。

        123 与光鸭的裸 JWT 识别为 token，其余无 `=` 的文本同样按 token 处理。

        :param platform: 平台标识
        :param text: 用户输入
        :return: 凭据字段
        """
        if platform in {NetdiskPlatform.PAN123, NetdiskPlatform.GUANGYA} and "=" not in text:
            return {"token": text}
        fields = parse_cookie_header(text)
        if not fields and text:
            return {"token": text}
        return {"cookie": text}

    def _target(
        self,
        platform: object,
        account_id: Optional[int],
    ) -> tuple[NetdiskPlatform, Optional[NetdiskAccountRecord]]:
        """
        解析本次登录要写入的平台与已有账号。

        :param platform: 平台标识，传入账号时可省略
        :param account_id: 已有账号主键
        :return: 平台枚举与已有账号记录；新建账号时记录为 None
        :raises NetdiskError: 账号不存在或平台不匹配时抛出
        """
        if account_id is None:
            return self._require_platform(platform), None
        record = self._accounts.store.get_record(account_id)
        if record is None:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "账号不存在")
        if platform is not None and str(platform).strip():
            resolved = normalize_platform(platform)
            if resolved is not record.platform:
                raise NetdiskError(
                    NetdiskErrorCode.UNKNOWN,
                    "账号与平台不匹配",
                    platform=record.platform,
                )
        return record.platform, record

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
