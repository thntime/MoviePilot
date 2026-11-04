"""
网盘凭据保活测试：Set-Cookie 合并回写、非 Cookie 凭据跳过与惰性续期节流。

全部使用替身，不访问真实网络。
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.adapters.external.netdisk import base as base_module
from app.adapters.external.netdisk import guangya as guangya_module
from app.adapters.external.netdisk import xunlei as xunlei_module
from app.adapters.external.netdisk.base import NetdiskHttp, merge_cookie_header
from app.application.netdisk.keepalive import REFRESH_TTL_SECONDS, RefreshThrottle
from app.domain.netdisk import NetdiskAccountRecord, NetdiskPlatform


class _FakeHeaders:
    """
    兼容 httpx2 `headers.get_list` 的响应头替身。
    """

    def __init__(self, set_cookies: list[str]) -> None:
        self._set_cookies = set_cookies

    def get_list(self, name: str) -> list[str]:
        """
        按名称返回多值响应头。
        """
        return list(self._set_cookies) if name.lower() == "set-cookie" else []

    def get(self, name: str, default: Any = None) -> Any:
        """
        按名称返回单值响应头。
        """
        if name.lower() == "set-cookie" and self._set_cookies:
            return self._set_cookies[0]
        return default


class _FakeResponse:
    """
    最小 HTTP 响应替身。
    """

    def __init__(
        self,
        set_cookies: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        *,
        headers: Any = None,
        status: int = 200,
    ) -> None:
        self.status_code = status
        self._payload = payload or {"ok": True}
        self.headers = headers if headers is not None else _FakeHeaders(set_cookies or [])

    def json(self) -> dict[str, Any]:
        """
        返回固定 JSON 载荷。
        """
        return self._payload

    @property
    def text(self) -> str:
        """
        返回固定文本响应。
        """
        return "{}"


class _FakeRequestUtils:
    """
    固定返回预设响应的 RequestUtils 替身。
    """

    response: _FakeResponse | None = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def get_res(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        """
        返回预设响应。
        """
        assert self.response is not None
        return self.response

    def post_res(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        """
        返回预设响应。
        """
        assert self.response is not None
        return self.response


def _install_response(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> None:
    """
    用替身替换 RequestUtils 并注入响应。

    :param monkeypatch: pytest 打桩夹具
    :param response: 预设响应
    """
    _FakeRequestUtils.response = response
    monkeypatch.setattr(base_module, "RequestUtils", _FakeRequestUtils)


def _account(credential: dict[str, str]) -> NetdiskAccountRecord:
    """
    构造测试账号记录。

    :param credential: 凭据字段
    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=7,
        platform=NetdiskPlatform.QUARK,
        name="测试账号",
        credential=credential,
    )


def test_merge_cookie_header_adds_and_overwrites() -> None:
    """
    新 Cookie 追加，同名 Cookie 就地覆盖。
    """
    merged = merge_cookie_header("a=1; b=2", ["b=3; Path=/", "c=4; Max-Age=3600"])

    assert merged == "a=1; b=3; c=4"


def test_merge_cookie_header_deletes_max_age_zero() -> None:
    """
    `Max-Age=0` 的 Cookie 从结果中删除。
    """
    merged = merge_cookie_header("a=1; b=2", ["b=; Max-Age=0"])

    assert merged == "a=1"


def test_merge_cookie_header_deletes_expired() -> None:
    """
    `Expires` 已过期的 Cookie 从结果中删除。
    """
    merged = merge_cookie_header("a=1; b=2", ["b=x; Expires=Thu, 01 Jan 1970 00:00:00 GMT"])

    assert merged == "a=1"


def test_cookie_sink_writes_merged_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    响应带回新 Cookie 时，把合并后的完整凭据交给回写钩子。
    """
    _install_response(monkeypatch, _FakeResponse(["__puus=new; Path=/"]))
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)
    calls: list[tuple[int, dict[str, str]]] = []
    http.credential_sink = lambda account, credential: calls.append((account.id, dict(credential)))

    payload = http.get_json(
        "https://example.test/config",
        account=_account({"cookie": "__puus=old; __pus=keep", "note": "x"}),
    )

    assert payload == {"ok": True}
    assert calls == [(7, {"cookie": "__puus=new; __pus=keep", "note": "x"})]


def test_cookie_sink_skips_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Set-Cookie 与现有 Cookie 相同时不触发回写。
    """
    _install_response(monkeypatch, _FakeResponse(["__puus=old"]))
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)
    calls: list[dict[str, str]] = []
    http.credential_sink = lambda _account, credential: calls.append(dict(credential))

    http.get_json("https://example.test/config", account=_account({"cookie": "__puus=old"}))

    assert calls == []


def test_cookie_sink_skips_non_cookie_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    非 Cookie 凭据（如 123 的 token）完全跳过回写。
    """
    _install_response(monkeypatch, _FakeResponse(["session=1"]))
    http = NetdiskHttp(platform=NetdiskPlatform.PAN123)
    calls: list[dict[str, str]] = []
    http.credential_sink = lambda _account, credential: calls.append(dict(credential))

    http.get_json("https://example.test", account=_account({"token": "abc"}))

    assert calls == []


def test_cookie_sink_failure_does_not_break_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    回写钩子抛异常时原请求结果不受影响。
    """
    _install_response(monkeypatch, _FakeResponse(["a=2"]))
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)

    def _boom(_account: NetdiskAccountRecord, _credential: Any) -> None:
        raise RuntimeError("sink failed")

    http.credential_sink = _boom

    assert http.get_json("https://example.test", account=_account({"cookie": "a=1"})) == {"ok": True}


def test_cookie_sink_reads_single_set_cookie_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    只有单值 `Set-Cookie` header 时也能合并。
    """
    _install_response(
        monkeypatch,
        _FakeResponse(headers={"Set-Cookie": "b=9; Path=/"}),
    )
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)
    calls: list[dict[str, str]] = []
    http.credential_sink = lambda _account, credential: calls.append(dict(credential))

    http.get_text("https://example.test", account=_account({"cookie": "a=1; b=2"}))

    assert calls == [{"cookie": "a=1; b=9"}]


def test_cookie_sink_runs_for_post_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    POST 响应同样触发 Cookie 回写。
    """
    _install_response(monkeypatch, _FakeResponse(["a=2"]))
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)
    calls: list[dict[str, str]] = []
    http.credential_sink = lambda _account, credential: calls.append(dict(credential))

    http.post_json("https://example.test", account=_account({"cookie": "a=1"}), json={})

    assert calls == [{"cookie": "a=2"}]


def test_throttle_due_mark_and_boundary() -> None:
    """
    未记录的账号需要刷新，超过 TTL 边界后再次需要刷新。
    """
    throttle = RefreshThrottle()
    ttl = REFRESH_TTL_SECONDS[NetdiskPlatform.QUARK]

    assert throttle.due(1, NetdiskPlatform.QUARK, now=100.0) is True
    throttle.mark(1, NetdiskPlatform.QUARK, now=100.0)
    assert throttle.due(1, NetdiskPlatform.QUARK, now=100.0 + ttl - 1) is False
    assert throttle.due(1, NetdiskPlatform.QUARK, now=100.0 + ttl) is True


def test_throttle_reset() -> None:
    """
    reset 后所有账号重新视为未记录。
    """
    throttle = RefreshThrottle()
    throttle.mark(1, NetdiskPlatform.QUARK, now=0.0)

    throttle.reset()

    assert throttle.due(1, NetdiskPlatform.QUARK, now=0.0) is True


def test_throttle_custom_ttl_and_unknown_platform() -> None:
    """
    自定义 TTL 生效，未配置 TTL 的平台不触发刷新。
    """
    throttle = RefreshThrottle({NetdiskPlatform.UC: 10.0})
    throttle.mark(2, NetdiskPlatform.UC, now=0.0)

    assert throttle.due(2, NetdiskPlatform.UC, now=9.9) is False
    assert throttle.due(2, NetdiskPlatform.UC, now=10.0) is True
    assert throttle.due(3, NetdiskPlatform.PAN123, now=0.0) is False


def _jwt(exp: int) -> str:
    """
    构造只含 `exp` 的 JWT 文本。

    :param exp: 过期时间戳
    :return: JWT 文本
    """
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_jwt_expiring_uses_leeway() -> None:
    """
    JWT 剩余不足 60 秒时视为临期，无法解析时返回 False。
    """
    token = _jwt(1000)

    assert xunlei_module.jwt_expiring(token, now=950.0) is True
    assert xunlei_module.jwt_expiring(token, now=900.0) is False
    assert xunlei_module.jwt_expiring("not-a-jwt", now=0.0) is False


def _record(platform: NetdiskPlatform, credential: dict[str, str]) -> NetdiskAccountRecord:
    """
    构造指定平台的账号记录。

    :param platform: 平台标识
    :param credential: 凭据字段
    :return: 账号记录
    """
    return NetdiskAccountRecord(id=9, platform=platform, name="续期测试", credential=credential)


def test_xunlei_needs_refresh_when_jwt_expiring() -> None:
    """
    迅雷 JWT 剩余不足 60 秒时需要续期。
    """
    token = _jwt(int(time.time()) + 30)
    adapter = xunlei_module.XunleiAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.XUNLEI, {"access_token": token, "refresh_token": "r"})
    ) is True


def test_xunlei_needs_refresh_when_jwt_fresh() -> None:
    """
    迅雷 JWT 剩余充足时不需要续期。
    """
    token = _jwt(int(time.time()) + 3600)
    adapter = xunlei_module.XunleiAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.XUNLEI, {"access_token": token, "refresh_token": "r"})
    ) is False


def test_xunlei_needs_refresh_without_refresh_token() -> None:
    """
    没有 refresh_token 时无法续期，即使 JWT 已临期也返回 False。
    """
    token = _jwt(int(time.time()) + 30)
    adapter = xunlei_module.XunleiAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.XUNLEI, {"access_token": token})
    ) is False


def test_xunlei_needs_refresh_when_token_unparsable() -> None:
    """
    access_token 不是可解析的 JWT 时按需要续期处理。
    """
    adapter = xunlei_module.XunleiAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.XUNLEI, {"access_token": "opaque", "refresh_token": "r"})
    ) is True


def _iso_expires_in(seconds: float) -> str:
    """
    生成相对当前时间的 ISO 8601 过期时间。

    :param seconds: 相对当前时间的秒数
    :return: ISO 8601 字符串
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_guangya_needs_refresh_when_expiring() -> None:
    """
    光鸭 `expires_at` 剩余不足 60 秒时需要续期。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.GUANGYA, {"token": "t", "expires_at": _iso_expires_in(30)})
    ) is True


def test_guangya_needs_refresh_when_expired() -> None:
    """
    光鸭 `expires_at` 已过期时需要续期。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.GUANGYA, {"token": "t", "expires_at": _iso_expires_in(-10)})
    ) is True


def test_guangya_needs_refresh_when_fresh() -> None:
    """
    光鸭 `expires_at` 剩余充足时不需要续期。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.GUANGYA, {"token": "t", "expires_at": _iso_expires_in(3600)})
    ) is False


def test_guangya_needs_refresh_without_expires_at() -> None:
    """
    光鸭缺少 `expires_at` 时视为未知，不主动刷新。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.needs_refresh(_record(NetdiskPlatform.GUANGYA, {"token": "t"})) is False


def test_guangya_needs_refresh_with_invalid_expires_at() -> None:
    """
    光鸭 `expires_at` 无法解析时视为未知，不主动刷新。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.needs_refresh(
        _record(NetdiskPlatform.GUANGYA, {"token": "t", "expires_at": "not-a-date"})
    ) is False


class _SequenceRequestUtils:
    """
    按顺序返回预设响应并记录每次请求头的 RequestUtils 替身。
    """

    responses: list[_FakeResponse] = []
    requests: list[dict[str, str]] = []

    def __init__(self, **kwargs: Any) -> None:
        self._headers = dict(kwargs.get("headers") or {})

    def _next(self) -> _FakeResponse:
        """
        记录本次请求头并弹出下一个响应。
        """
        _SequenceRequestUtils.requests.append(dict(self._headers))
        assert _SequenceRequestUtils.responses
        return _SequenceRequestUtils.responses.pop(0)

    def get_res(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        """
        返回下一个响应。
        """
        return self._next()

    def post_res(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        """
        返回下一个响应。
        """
        return self._next()


def _install_sequence(monkeypatch: pytest.MonkeyPatch, responses: list[_FakeResponse]) -> None:
    """
    用顺序替身替换 RequestUtils。

    :param monkeypatch: pytest 打桩夹具
    :param responses: 依次返回的响应
    """
    _SequenceRequestUtils.responses = list(responses)
    _SequenceRequestUtils.requests = []
    monkeypatch.setattr(base_module, "RequestUtils", _SequenceRequestUtils)


def test_auth_failure_refreshes_and_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    鉴权失败时用续期钩子换取新凭据，并用新 Cookie 重试一次。
    """
    _install_sequence(
        monkeypatch,
        [_FakeResponse(status=401), _FakeResponse(payload={"ok": True})],
    )
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)
    refreshed: list[int] = []

    def refresher(record: NetdiskAccountRecord) -> dict[str, str]:
        """
        记录续期调用并返回新凭据。
        """
        refreshed.append(record.id)
        return {"cookie": "a=2"}

    http.credential_refresher = refresher

    assert http.get_json("https://example.invalid/config", account=_account({"cookie": "a=1"})) == {
        "ok": True
    }
    assert refreshed == [7]
    assert len(_SequenceRequestUtils.requests) == 2
    assert _SequenceRequestUtils.requests[0]["Cookie"] == "a=1"
    assert _SequenceRequestUtils.requests[1]["Cookie"] == "a=2"


def test_auth_failure_without_refresher_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    没有续期钩子时鉴权失败直接抛出，不重复请求。
    """
    from app.domain.netdisk import NetdiskError

    _install_sequence(monkeypatch, [_FakeResponse(status=401)])
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)

    with pytest.raises(NetdiskError):
        http.get_json("https://example.invalid/config", account=_account({"cookie": "a=1"}))
    assert len(_SequenceRequestUtils.requests) == 1


def test_server_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    服务端错误不触发续期，也不重试。
    """
    from app.domain.netdisk import NetdiskError

    _install_sequence(monkeypatch, [_FakeResponse(status=500)])
    http = NetdiskHttp(platform=NetdiskPlatform.QUARK)
    http.credential_refresher = lambda _record: {"cookie": "a=2"}

    with pytest.raises(NetdiskError):
        http.get_json("https://example.invalid/config", account=_account({"cookie": "a=1"}))
    assert len(_SequenceRequestUtils.requests) == 1
