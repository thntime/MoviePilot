"""
139（中国移动云盘）适配器的协议、凭据与错误分类测试。

所有 HTTP 调用都由替身响应覆盖，不访问真实网络。
"""

import base64
import json
from typing import Any, Optional

import pytest

from app.adapters.external.netdisk import c139
from app.adapters.external.netdisk.base import NetdiskHttp
from app.adapters.network.http import RequestUtils
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import (
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareFile,
    ShareRef,
)


class _Response:
    """
    最小 HTTP 响应替身，供错误分类测试使用。
    """

    def __init__(
        self,
        payload: Optional[dict] = None,
        status_code: int = 200,
        content_type: str = "application/json",
    ) -> None:
        """
        保存状态码、响应头与响应体。

        :param payload: JSON 响应体
        :param status_code: HTTP 状态码
        :param content_type: 响应 Content-Type
        """
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.text = json.dumps(payload) if payload is not None else "<html></html>"
        self._payload = payload

    def json(self) -> dict:
        """
        返回预设 JSON。

        :return: 响应体
        """
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[c139.C139Adapter, list[dict[str, Any]], dict[str, list[Any]]]:
    """
    把适配器的 HTTP 客户端替换为按 URL 排队返回的替身。

    :param monkeypatch: pytest 补丁工具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    calls: list[dict[str, Any]] = []
    responses: dict[str, list[Any]] = {}

    def _next(url: str) -> Any:
        queue = responses.get(url) or []
        assert queue, f"未预设响应：{url}"
        return queue.pop(0)

    def get_json(_self, url: str, *, account=None, params=None, headers=None):
        calls.append(
            {
                "method": "GET",
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "account": account,
            }
        )
        return _next(url)

    def post_json(_self, url: str, *, account=None, json=None, data=None, params=None, headers=None):
        calls.append(
            {
                "method": "POST",
                "url": url,
                "json": dict(json or {}),
                "data": dict(data or {}),
                "headers": dict(headers or {}),
                "account": account,
            }
        )
        return _next(url)

    def get_text(_self, url: str, *, account=None, params=None, headers=None):
        calls.append({"method": "TEXT", "url": url, "account": account})
        return _next(url)

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)
    return c139.C139Adapter(), calls, responses


def _authorization(account: str = "13800138000") -> str:
    """
    构造 `Basic base64(pc:账号:token)` 形式的 authorization。

    :param account: 手机号
    :return: authorization 文本
    """
    payload = base64.b64encode(f"pc:{account}:auth-token".encode()).decode()
    return f"Basic {payload}"


def _cookie(account: str = "13800138000") -> str:
    """
    构造完整登录 Cookie。

    :param account: 手机号
    :return: Cookie 头文本
    """
    encrypted = base64.b64encode(account.encode()).decode()
    return (
        f"Os_Sso_Sid=sso; RMKEY=rmkey; ORCHES-I-ACCOUNT-ENCRYPT={encrypted}; "
        f"authorization={_authorization(account)}"
    )


def _account(credential: dict[str, str]) -> NetdiskAccountRecord:
    """
    构造 139 云盘账号记录。

    :param credential: 明文凭据字段
    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=9,
        platform=NetdiskPlatform.C139,
        name="139 主号",
        credential=credential,
    )


def _ref() -> ShareRef:
    """
    构造测试用分享引用。

    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.C139,
        share_id="2qieNwKvQ0G1o",
        url="https://yun.139.com/shareweb/#/w/i/2qieNwKvQ0G1o",
    )


def _general_payload(title: str = "合集", password: str = "abcd") -> dict:
    """
    构造分享信息响应。

    :param title: 分享标题
    :param password: 明文提取码
    :return: 响应 JSON
    """
    return {
        "success": True,
        "resultCode": "0",
        "data": {"getOutLinkGeneralResp": {"outLinkGeneral": [{"lkName": title, "passwd": password}]}},
    }


def _list_payload(folders: list[dict], files: list[dict]) -> dict:
    """
    构造分享目录响应。

    :param folders: caLst 条目
    :param files: coLst 条目
    :return: 响应 JSON
    """
    return {"success": True, "resultCode": "0", "data": {"caLst": folders, "coLst": files}}


def test_platform_metadata() -> None:
    """
    平台能力声明与需求矩阵一致。
    """
    adapter = c139.C139Adapter()

    assert adapter.platform is NetdiskPlatform.C139
    assert adapter.link_policy.value == "direct"
    assert adapter.requires_account is True
    assert adapter.guest_supported is False
    assert adapter.max_concurrency == 512
    assert adapter.cookie_required_keys == ("Os_Sso_Sid", "RMKEY")
    assert adapter.cookie_domains == (".139.com", ".10086.cn")
    assert adapter.login_url == "https://yun.139.com/"


def test_parse_share_link() -> None:
    """
    分享链接解析出分享标识。
    """
    adapter = c139.C139Adapter()

    ref = adapter.parse_share("https://yun.139.com/shareweb/#/w/i/2qieNwKvQ0G1o", None)

    assert ref.platform is NetdiskPlatform.C139
    assert ref.share_id == "2qieNwKvQ0G1o"


def test_share_detail_merges_folders_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情读取标题、提取码，并把 caLst 与 coLst 合并为目录条目。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[c139._SHARE_GENERAL_URL] = [_general_payload()]
    responses[c139._SHARE_LIST_URL] = [
        _list_payload(
            [{"caID": "ca-1", "caName": "子目录"}],
            [
                {"coID": "co-1", "coName": "a.mkv", "coSize": 2048, "coType": 1},
                {"coID": "co-2", "coName": "嵌套目录", "coSize": 0, "coType": 2},
            ],
        )
    ]

    detail = adapter.share_detail(_ref(), None)

    assert detail.title == "合集"
    assert detail.stoken == "abcd"
    assert detail.size == 2048
    assert [item.name for item in detail.files] == ["子目录", "a.mkv", "嵌套目录"]
    assert detail.files[0].is_dir is True
    assert detail.files[2].is_dir is True
    body = calls[1]["json"]["getOutLinkInfoReq"]
    assert body["pCaID"] == "root"
    assert body["passwd"] == "abcd"
    assert body["account"] == ""


def test_share_detail_prefers_explicit_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    调用方显式提供的提取码优先于接口回吐值。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[c139._SHARE_GENERAL_URL] = [_general_payload(password="leaked")]
    responses[c139._SHARE_LIST_URL] = [_list_payload([], [])]
    ref = ShareRef(platform=NetdiskPlatform.C139, share_id="abc", password="input")

    detail = adapter.share_detail(ref, None)

    assert detail.stoken == "input"
    assert calls[1]["json"]["getOutLinkInfoReq"]["passwd"] == "input"


def test_list_files_maps_parent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    根目录映射为 root，子目录沿用父级标识。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[c139._SHARE_LIST_URL] = [
        _list_payload([], []),
        _list_payload([], [{"coID": "co-9", "coName": "b.mkv", "coSize": 1}]),
    ]

    assert adapter.list_files(_ref(), "0", None) == []
    files = adapter.list_files(_ref(), "ca-1", None)

    assert calls[0]["json"]["getOutLinkInfoReq"]["pCaID"] == "root"
    assert calls[1]["json"]["getOutLinkInfoReq"]["pCaID"] == "ca-1"
    assert files[0].file_id == "co-9"


def test_resolve_returns_link_with_account_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    取链携带完整账号、Cookie 与客户端 UA。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[c139._SHARE_LINK_URL] = [{"success": True, "resultCode": "0", "data": {"redrUrl": "https://obs.example.com/f"}}]
    file = ShareFile(file_id="co-1", name="a.mkv", size=2048)

    link = adapter.resolve(_ref(), file, _account({"cookie": _cookie()}))

    assert link.url == "https://obs.example.com/f"
    assert link.size == 2048
    assert link.account_id == 9
    assert "authorization=" in link.headers["Cookie"]
    assert "Mobile Safari" in link.headers["User-Agent"]
    body = calls[0]["json"]["dlFromOutLinkReqV3"]
    assert body["account"] == "13800138000"
    assert body["linkID"] == "2qieNwKvQ0G1o"
    assert body["coIDLst"] == {"item": ["co-1"]}
    assert calls[0]["headers"]["Authorization"].startswith("Basic ")


def test_resolve_adds_authorization_field_to_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    凭据分开保存 cookie 与 authorization 时，下载头仍包含 authorization。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[c139._SHARE_LINK_URL] = [{"success": True, "resultCode": "0", "data": {"redrUrl": "https://obs/f"}}]
    credential = {"cookie": "Os_Sso_Sid=sso; RMKEY=rmkey", "authorization": _authorization()}

    link = adapter.resolve(_ref(), ShareFile(file_id="co-1", name="a.mkv"), _account(credential))

    assert "authorization=Basic" in link.headers["Cookie"]


def test_resolve_requires_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    缺少账号时取链直接返回登录态失效，不发起请求。
    """
    adapter, calls, _responses = _install(monkeypatch)

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), ShareFile(file_id="co-1", name="a.mkv"), None)

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert calls == []


def test_resolve_without_account_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    只有双字段、无法解析账号时取链返回登录态失效。
    """
    adapter, calls, _responses = _install(monkeypatch)
    credential = {"cookie": "Os_Sso_Sid=sso; RMKEY=rmkey"}

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), ShareFile(file_id="co-1", name="a.mkv"), _account(credential))

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert calls == []


def test_resolve_maps_password_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    提取码错误（9188）映射为分享无效。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[c139._SHARE_LINK_URL] = [{"success": False, "resultCode": "9188", "desc": "提取码错误"}]

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), ShareFile(file_id="co-1", name="a.mkv"), _account({"cookie": _cookie()}))

    assert error.value.code is NetdiskErrorCode.SHARE_INVALID


def test_validate_credential_calls_account_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    有 authorization 时调用个人网盘接口确认登录态。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[c139._CLOUD_LIST_URL] = [{"success": True, "code": "0000", "data": {"items": []}}]

    probe = adapter.validate_credential({"authorization": _authorization()})

    assert probe.status == "active"
    assert calls[0]["url"] == c139._CLOUD_LIST_URL
    assert calls[0]["headers"]["Authorization"].startswith("Basic ")
    assert calls[0]["headers"]["mcloud-sign"]


def test_validate_credential_accepts_fast_login_fields() -> None:
    """
    只有 Os_Sso_Sid 与 RMKEY 时按字段判定有效，不发起请求。
    """
    adapter = c139.C139Adapter()

    probe = adapter.validate_credential({"cookie": "Os_Sso_Sid=sso; RMKEY=rmkey"})

    assert probe.status == "active"


def test_validate_credential_rejects_empty() -> None:
    """
    缺少登录字段时判定过期。
    """
    adapter = c139.C139Adapter()

    probe = adapter.validate_credential({"cookie": "theme=dark"})

    assert probe.status == "expired"
    assert "Os_Sso_Sid" in probe.message


def test_validate_credential_maps_cloud_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    管理接口返回 04000005 时判定登录态失效。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[c139._CLOUD_LIST_URL] = [{"success": False, "code": "04000005", "message": "认证失败"}]

    probe = adapter.validate_credential({"authorization": _authorization()})

    assert probe.status == "expired"


def test_business_code_401_maps_auth_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享接口业务码 401 映射为 AUTH_EXPIRED。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[c139._SHARE_GENERAL_URL] = [{"success": False, "resultCode": "401"}]
    responses[c139._SHARE_LIST_URL] = [{"success": False, "resultCode": "401"}]

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), None)

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_business_code_9530_maps_risk_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    设备校验失败（9530）映射为风控拦截。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[c139._SHARE_GENERAL_URL] = [{"success": False, "resultCode": "9530"}]
    responses[c139._SHARE_LIST_URL] = [{"success": False, "resultCode": "9530", "desc": "设备异常"}]

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), None)

    assert error.value.code is NetdiskErrorCode.RISK_BLOCKED


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_Response(status_code=401), NetdiskErrorCode.AUTH_EXPIRED),
        (_Response(status_code=429), NetdiskErrorCode.RATE_LIMITED),
        (_Response(payload=None, content_type="text/html"), NetdiskErrorCode.LINK_EXPIRED),
    ],
)
def test_http_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
    expected: NetdiskErrorCode,
) -> None:
    """
    HTTP 401/429 与 HTML 防盗链页映射为统一错误码。
    """
    adapter = c139.C139Adapter()
    monkeypatch.setattr(RequestUtils, "post_res", lambda _self, url, **kwargs: response)

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), None)

    assert error.value.code is expected
