"""
123 云盘适配器的协议、凭据与错误分类测试。

所有 HTTP 调用都由替身响应覆盖，不访问真实网络。
"""

import base64
import json
from typing import Any, Optional

import pytest

from app.adapters.external.netdisk import pan123
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


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[pan123.Pan123Adapter, list[dict[str, Any]], dict[str, list[Any]]]:
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
    return pan123.Pan123Adapter(), calls, responses


def _account(credential: dict[str, str]) -> NetdiskAccountRecord:
    """
    构造 123 云盘账号记录。

    :param credential: 明文凭据字段
    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=7,
        platform=NetdiskPlatform.PAN123,
        name="123 主号",
        credential=credential,
    )


def _ref() -> ShareRef:
    """
    构造测试用分享引用。

    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.PAN123,
        share_id="2785Vv-T4Ded",
        password="abcd",
        url="https://www.123pan.com/s/2785Vv-T4Ded",
    )


def _share_payload(files: list[dict], next_cursor: str = "-1") -> dict:
    """
    构造分享列表响应。

    :param files: InfoList 条目
    :param next_cursor: Next 字段
    :return: 响应 JSON
    """
    return {"code": 0, "data": {"InfoList": files, "Next": next_cursor}}


def test_platform_metadata() -> None:
    """
    平台能力声明与需求矩阵一致。
    """
    adapter = pan123.Pan123Adapter()

    assert adapter.platform is NetdiskPlatform.PAN123
    assert adapter.link_policy.value == "direct"
    assert adapter.requires_account is True
    assert adapter.guest_supported is False
    assert adapter.max_concurrency == 512
    assert adapter.cookie_required_keys == ("token",)
    assert adapter.cookie_domains == (".123pan.com", ".123952.com")
    assert adapter.login_url == "https://www.123pan.com/"
    assert adapter.supports_password_login is True


def test_parse_share_link() -> None:
    """
    分享链接解析出分享码与提取码。
    """
    adapter = pan123.Pan123Adapter()

    ref = adapter.parse_share("https://www.123pan.com/s/2785Vv-T4Ded?pwd=abcd", None)

    assert ref.platform is NetdiskPlatform.PAN123
    assert ref.share_id == "2785Vv-T4Ded"
    assert ref.password == "abcd"


def test_share_detail_reads_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情读取根目录并用首个条目作为标题。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_GET_URL] = [
        _share_payload(
            [
                {
                    "FileId": "100",
                    "FileName": "电影合集",
                    "Size": 0,
                    "Type": 1,
                    "ParentFileId": "0",
                    "S3KeyFlag": "1816216065-0",
                    "Etag": "etag-1",
                    "StorageNode": "node-1",
                },
                {
                    "FileId": "101",
                    "FileName": "a.mkv",
                    "Size": 1024,
                    "Type": 0,
                    "ParentFileId": "0",
                    "S3KeyFlag": "1816216065-0",
                    "Etag": "etag-2",
                    "StorageNode": "node-1",
                },
            ]
        )
    ]

    detail = adapter.share_detail(_ref(), None)

    assert detail.title == "电影合集"
    assert detail.stoken == "abcd"
    assert detail.size == 1024
    assert [item.name for item in detail.files] == ["电影合集", "a.mkv"]
    assert detail.files[1].token == "1816216065-0|etag-2|node-1"
    assert calls[0]["params"]["SharePwd"] == "abcd"
    assert calls[0]["params"]["ParentFileId"] == "0"


def test_share_detail_omits_empty_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    无提取码时不传 SharePwd，避免接口报错。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_GET_URL] = [_share_payload([])]
    ref = ShareRef(platform=NetdiskPlatform.PAN123, share_id="2785Vv-T4Ded")

    detail = adapter.share_detail(ref, None)

    assert detail.title == "2785Vv-T4Ded"
    assert "SharePwd" not in calls[0]["params"]


def test_list_files_paginates_until_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    空串游标表示还有下一页，`-1` 表示末页。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_GET_URL] = [
        _share_payload([{"FileId": "1", "FileName": "第一页", "Type": 0}], next_cursor=""),
        _share_payload([{"FileId": "2", "FileName": "第二页", "Type": 0}], next_cursor="-1"),
    ]

    files = adapter.list_files(_ref(), "0", None)

    assert [item.name for item in files] == ["第一页", "第二页"]
    assert [call["params"]["Page"] for call in calls] == [1, 2]


def test_resolve_returns_direct_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    取链返回解码后的 CDN 地址并携带 Bearer、UA 与 Referer。
    """
    adapter, calls, responses = _install(monkeypatch)
    target = "https://cdn.example.com/file.mkv?sign=1"
    wrapped = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    responses[pan123._SHARE_DOWNLOAD_URL] = [
        {"code": 0, "data": {"DownloadURL": f"https://download-v2.example.com/x?params={wrapped}"}}
    ]
    file = ShareFile(
        file_id="101",
        name="a.mkv",
        size=1024,
        parent_id="0",
        token="1816216065-0|etag-2|node-1",
    )

    link = adapter.resolve(_ref(), file, _account({"token": "jwt-token"}))

    assert link.url == target
    assert link.size == 1024
    assert link.account_id == 7
    assert link.headers["Authorization"] == "Bearer jwt-token"
    assert link.headers["Referer"] == "https://yun.123pan.cn/"
    assert "Dart" in link.headers["User-Agent"]
    body = calls[0]["json"]
    assert body["ShareKey"] == "2785Vv-T4Ded"
    assert body["FileID"] == 101
    assert body["S3KeyFlag"] == "1816216065-0"
    assert body["Etag"] == "etag-2"
    assert calls[0]["headers"]["authorization"] == "Bearer jwt-token"
    assert calls[0]["headers"]["auth-key"]
    assert calls[0]["headers"]["auth-value"]


def test_resolve_accepts_author_token_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Cookie 头中的 authorToken 可作为 JWT 使用。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_DOWNLOAD_URL] = [
        {"code": 0, "data": {"DownloadURL": "https://cdn.example.com/file.mkv"}}
    ]
    file = ShareFile(file_id="1", name="a.mkv", size=1, token="s3|etag|node")

    link = adapter.resolve(_ref(), file, _account({"cookie": "authorToken=cookie-token; theme=dark"}))

    assert link.headers["Authorization"] == "Bearer cookie-token"
    assert calls[0]["headers"]["authorization"] == "Bearer cookie-token"


def test_resolve_requires_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    缺少账号时取链直接返回登录态失效，不发起请求。
    """
    adapter, calls, _responses = _install(monkeypatch)
    file = ShareFile(file_id="1", name="a.mkv", size=1, token="s3|etag|node")

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), file, None)

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert calls == []


def test_password_login_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    账号密码登录按 sign_in 协议提交并返回 JWT。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._LOGIN_URL] = [{"code": 200, "data": {"token": "new-jwt"}}]

    credential = adapter.password_login(" user ", "pwd 1")

    assert credential == {"token": "new-jwt"}
    assert calls[0]["json"] == {"passport": "user", "password": "pwd 1", "remember": False}
    assert calls[0]["headers"]["app-version"] == "132"


def test_password_login_maps_bad_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    登录成功码但缺少 token 时按凭据无效处理。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[pan123._LOGIN_URL] = [{"code": 200, "data": {}}]

    with pytest.raises(NetdiskError) as error:
        adapter.password_login("user", "pwd")

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_validate_credential_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    凭据校验会调用账号信息接口确认 token。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._USER_INFO_URL] = [{"code": 0, "data": {"Nickname": "阿三"}}]

    probe = adapter.validate_credential({"token": "jwt"})

    assert probe.status == "active"
    assert "阿三" in probe.message
    assert calls[0]["url"] == pan123._USER_INFO_URL


def test_validate_credential_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    缺少 token 时按字段直接判定过期，不发起请求。
    """
    adapter, calls, _responses = _install(monkeypatch)

    probe = adapter.validate_credential({"cookie": "theme=dark"})

    assert probe.status == "expired"
    assert "token" in probe.message
    assert calls == []


def test_validate_credential_reports_expired_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    账号信息接口返回 401 时判定登录态失效。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[pan123._USER_INFO_URL] = [{"code": 401, "message": "token 已过期"}]

    probe = adapter.validate_credential({"token": "jwt"})

    assert probe.status == "expired"


def test_business_code_401_maps_auth_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    业务码 401 映射为 AUTH_EXPIRED。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_GET_URL] = [{"code": 401, "message": "请先登录"}]

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), None)

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


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
    adapter = pan123.Pan123Adapter()
    monkeypatch.setattr(RequestUtils, "get_res", lambda _self, url, **kwargs: response)

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), None)

    assert error.value.code is expected
