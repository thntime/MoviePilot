"""
迅雷云盘适配器的协议、转存取链与错误分类测试。

所有 HTTP 调用都由替身响应覆盖，不访问真实网络，token 全部为假值。
"""

import json
from typing import Any, Optional

import pytest

from app.adapters.external.netdisk import xunlei
from app.adapters.external.netdisk.base import NetdiskHttp
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


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[xunlei.XunleiAdapter, list[dict[str, Any]], dict]:
    """
    把适配器的 HTTP 客户端替换为按 `(方法, URL)` 排队返回的替身。

    :param monkeypatch: pytest 补丁工具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    calls: list[dict[str, Any]] = []
    responses: dict[tuple[str, str], list[Any]] = {}

    def _next(method: str, url: str) -> Any:
        queue = responses.get((method, url)) or []
        assert queue, f"未预设响应：{method} {url}"
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
        return _next("GET", url)

    def post_json(_self, url: str, *, account=None, json=None, data=None, params=None, headers=None):
        calls.append(
            {
                "method": "POST",
                "url": url,
                "json": dict(json or {}),
                "data": dict(data or {}),
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "account": account,
            }
        )
        return _next("POST", url)

    def get_text(_self, url: str, *, account=None, params=None, headers=None):
        calls.append({"method": "TEXT", "url": url, "account": account})
        return _next("TEXT", url)

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)
    return xunlei.XunleiAdapter(), calls, responses


def _account() -> NetdiskAccountRecord:
    """
    构造迅雷云盘账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=21,
        platform=NetdiskPlatform.XUNLEI,
        name="迅雷主号",
        credential={
            "access_token": "fake-access-token-1234567890",
            "deviceid": "fake-device-id",
            "captcha": "fake-captcha-token",
        },
    )


def _ref(password: Optional[str] = "efgh", token: str = "pct-1") -> ShareRef:
    """
    构造测试用分享引用。

    :param password: 提取码
    :param token: 分享令牌
    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.XUNLEI,
        share_id="VNDlLXkbF2N5Bg",
        password=password,
        url="https://pan.xunlei.com/s/VNDlLXkbF2N5Bg",
        share_token=token,
    )


def _file(file_id: str = "f1", name: str = "a.mkv", size: int = 2048) -> ShareFile:
    """
    构造测试用分享文件。

    :param file_id: 文件标识
    :param name: 文件名
    :param size: 文件大小
    :return: 文件条目
    """
    return ShareFile(file_id=file_id, name=name, size=size)


def _share_payload() -> dict:
    """
    构造分享根目录响应。

    :return: 分享响应
    """
    return {
        "title": "合集",
        "pass_code_token": "pct-1",
        "files": [
            {"id": "f1", "name": "a.mkv", "size": 2048, "kind": "drive#file", "parent_id": ""},
            {"id": "d1", "name": "目录", "size": 0, "kind": "drive#folder", "parent_id": ""},
        ],
    }


def test_platform_metadata() -> None:
    """
    平台能力声明与需求矩阵一致。
    """
    adapter = xunlei.XunleiAdapter()

    assert adapter.platform is NetdiskPlatform.XUNLEI
    assert adapter.link_policy.value == "transfer_required"
    assert adapter.requires_account is True
    assert adapter.guest_supported is False
    assert adapter.max_concurrency == 8
    assert adapter.cookie_required_keys == ("access_token", "deviceid")
    assert adapter.cookie_domains == (".xunlei.com",)
    assert adapter.login_url == "https://pan.xunlei.com/"
    assert adapter.user_agent == xunlei._CLIENT_UA


def test_parse_share_link() -> None:
    """
    分享链接解析出分享码与提取码。
    """
    adapter = xunlei.XunleiAdapter()

    ref = adapter.parse_share("https://pan.xunlei.com/s/VNDlLXkbF2N5Bg?pwd=efgh", None)

    assert ref.platform is NetdiskPlatform.XUNLEI
    assert ref.share_id == "VNDlLXkbF2N5Bg"
    assert ref.password == "efgh"


def test_share_detail_reads_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情读取根目录并携带账号身份请求头。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._SHARE_URL)] = [_share_payload()]

    detail = adapter.share_detail(_ref(), _account())

    assert detail.title == "合集"
    assert detail.stoken == "pct-1"
    assert [item.name for item in detail.files] == ["a.mkv", "目录"]
    assert detail.files[1].is_dir is True
    assert calls[0]["params"]["share_id"] == "VNDlLXkbF2N5Bg"
    assert calls[0]["params"]["pass_code"] == "efgh"
    assert calls[0]["headers"]["Authorization"] == "Bearer fake-access-token-1234567890"
    assert calls[0]["headers"]["X-Device-Id"] == "fake-device-id"
    assert calls[0]["headers"]["User-Agent"] == xunlei._CLIENT_UA


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("PASS_CODE_EMPTY", "请输入迅雷云盘提取码"),
        ("PASS_CODE_ERROR", "迅雷云盘提取码错误"),
        ("PASS_CODE_NEED", "该迅雷云盘分享需要提取码"),
    ],
)
def test_share_status_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    message: str,
) -> None:
    """
    提取码状态映射为 SHARE_INVALID，避免被误判为空目录。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._SHARE_URL)] = [{"share_status": status, "files": []}]

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), _account())

    assert error.value.code is NetdiskErrorCode.SHARE_INVALID
    assert error.value.message == message


def test_list_files_uses_share_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    子目录列表使用 share/detail 并携带父目录与分享令牌。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._SHARE_DETAIL_URL)] = [
        {"files": [{"id": "f2", "name": "b.mkv", "size": 10, "kind": "drive#file"}], "next_page_token": ""}
    ]

    files = adapter.list_files(_ref(), "d1", _account())

    assert [item.file_id for item in files] == ["f2"]
    assert calls[0]["params"]["parent_id"] == "d1"
    assert calls[0]["params"]["pass_code_token"] == "pct-1"


def test_space_insufficient_stops_before_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    配额不足时抛 SPACE_INSUFFICIENT，且不发起转存。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._ABOUT_URL)] = [{"quota": {"limit": 1000, "usage": 950}}]

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(size=2048), _account())

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert [call["url"] for call in calls] == [xunlei._ABOUT_URL]


def test_resolve_restores_and_cleans(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    转存后读取文件详情直链，下载头带令牌、设备与验证码，清理删除临时内容。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._ABOUT_URL)] = [{"quota": {"limit": 10_000_000, "usage": 0}}]
    responses[("POST", xunlei._FILES_URL)] = [{"id": "dir-1"}]
    responses[("POST", xunlei._RESTORE_URL)] = [
        {"params": {"trace_file_ids": json.dumps({"f1": "new-1"})}}
    ]
    responses[("GET", f"{xunlei._FILES_URL}/new-1")] = [
        {
            "id": "new-1",
            "name": "a.mkv",
            "size": 2048,
            "links": {"application/octet-stream": {"url": "https://cdn.xunlei.com/a.mkv"}},
        }
    ]
    responses[("POST", xunlei._BATCH_DELETE_URL)] = [{"id": "ok"}]

    link = adapter.resolve(_ref(), _file(), _account())

    assert link.url == "https://cdn.xunlei.com/a.mkv"
    assert link.size == 2048
    assert link.account_id == 21
    assert link.cleanup_handle is not None
    assert link.cleanup_handle.directory_id == "dir-1"
    assert link.cleanup_handle.file_ids == ("new-1",)
    assert link.headers["Authorization"] == "Bearer fake-access-token-1234567890"
    assert link.headers["X-Device-Id"] == "fake-device-id"
    assert link.headers["X-Captcha-Token"] == "fake-captcha-token"
    assert link.headers["User-Agent"] == xunlei._CLIENT_UA
    restore = next(call for call in calls if call["url"] == xunlei._RESTORE_URL)
    assert restore["json"]["parent_id"] == "dir-1"
    assert restore["json"]["file_ids"] == ["f1"]

    adapter.cleanup(link.cleanup_handle, _account())

    delete = next(call for call in calls if call["url"] == xunlei._BATCH_DELETE_URL)
    assert set(delete["json"]["ids"]) == {"new-1", "dir-1"}


def test_resolve_failure_cleans_temp_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    取链失败时删除已创建的临时目录并保留原始错误。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._ABOUT_URL)] = [{"quota": {"limit": 10_000_000, "usage": 0}}]
    responses[("POST", xunlei._FILES_URL)] = [{"id": "dir-1"}]
    responses[("POST", xunlei._RESTORE_URL)] = [
        {"params": {"trace_file_ids": json.dumps({"f1": "new-1"})}}
    ]
    responses[("GET", f"{xunlei._FILES_URL}/new-1")] = [{"error": "unauthenticated"}]
    responses[("POST", xunlei._BATCH_DELETE_URL)] = [{"id": "ok"}]

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), _account())

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    delete = next(call for call in calls if call["url"] == xunlei._BATCH_DELETE_URL)
    assert delete["json"]["ids"] == ["dir-1"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "unauthenticated"}, NetdiskErrorCode.AUTH_EXPIRED),
        ({"error": "captcha_invalid"}, NetdiskErrorCode.RISK_BLOCKED),
        ({"error": "quota_exceeded"}, NetdiskErrorCode.SPACE_INSUFFICIENT),
        ({"error": "share_not_found"}, NetdiskErrorCode.SHARE_INVALID),
        ({"error": "too_many_requests"}, NetdiskErrorCode.RATE_LIMITED),
    ],
)
def test_business_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    expected: NetdiskErrorCode,
) -> None:
    """
    迅雷业务错误字段映射为统一错误码。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._SHARE_URL)] = [payload]

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), _account())

    assert error.value.code is expected
    assert "fake-access-token" not in error.value.message


@pytest.mark.parametrize(
    ("status", "content_type", "expected"),
    [
        (401, "application/json", NetdiskErrorCode.AUTH_EXPIRED),
        (429, "application/json", NetdiskErrorCode.RATE_LIMITED),
        (200, "text/html", NetdiskErrorCode.LINK_EXPIRED),
    ],
)
def test_http_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content_type: str,
    expected: NetdiskErrorCode,
) -> None:
    """
    HTTP 状态码与 HTML 响应由共享层映射为统一错误码。
    """
    adapter = xunlei.XunleiAdapter()

    def get_json(_self, url: str, **_kwargs):
        return _self._json(_Response(status_code=status, content_type=content_type))

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), _account())

    assert error.value.code is expected


def test_validate_credential_accepts_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    凭据校验接受 Cookie 形态并调用 about 接口。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._ABOUT_URL)] = [{"kind": "drive#about"}]

    assert adapter.validate_credential({}).status == "expired"
    probe = adapter.validate_credential(
        {"cookie": "access_token=fake-access-token-1234567890; deviceid=fake-device-id"}
    )

    assert probe.status == "active"


def test_validate_credential_accepts_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    凭据校验接受字段字典形态。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._ABOUT_URL)] = [{"kind": "drive#about"}]

    probe = adapter.validate_credential(
        {"access_token": "fake-access-token-1234567890", "deviceid": "fake-device-id"}
    )

    assert probe.status == "active"


def test_validate_credential_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    about 接口返回未认证时判为过期。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", xunlei._ABOUT_URL)] = [{"error": "unauthenticated"}]

    probe = adapter.validate_credential(
        {"access_token": "fake-access-token-1234567890", "deviceid": "fake-device-id"}
    )

    assert probe.status == "expired"
