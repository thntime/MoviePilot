"""
百度网盘适配器的协议、转存取链与错误分类测试。

所有 HTTP 调用都由替身响应覆盖，不访问真实网络，Cookie 全部为假值。
"""

import json
from typing import Any, Optional

import pytest

from app.adapters.external.netdisk import baidu
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


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[baidu.BaiduAdapter, list[dict[str, Any]], dict]:
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
    return baidu.BaiduAdapter(), calls, responses


def _account() -> NetdiskAccountRecord:
    """
    构造百度网盘账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=11,
        platform=NetdiskPlatform.BAIDU,
        name="百度主号",
        credential={"cookie": "BDUSS=fake-bduss; STOKEN=fake-stoken"},
    )


def _ref(password: Optional[str] = "abcd", token: str = "") -> ShareRef:
    """
    构造测试用分享引用。

    :param password: 提取码
    :param token: 分享令牌
    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.BAIDU,
        share_id="12345",
        password=password,
        url="https://pan.baidu.com/s/1abc12345",
        share_token=token,
    )


def _file(file_id: str = "900", name: str = "a.mkv", size: int = 1024) -> ShareFile:
    """
    构造测试用分享文件。

    :param file_id: 文件标识
    :param name: 文件名
    :param size: 文件大小
    :return: 文件条目
    """
    return ShareFile(file_id=file_id, name=name, size=size)


def _list_payload() -> dict:
    """
    构造分享根目录响应。

    :return: 分享列表响应
    """
    return {
        "errno": 0,
        "title": "电影合集",
        "share_id": "12345",
        "uk": "999",
        "list": [
            {
                "fs_id": 900,
                "server_filename": "a.mkv",
                "size": 1024,
                "isdir": "0",
                "path": "/a.mkv",
            },
            {
                "fs_id": 901,
                "server_filename": "子目录",
                "size": 0,
                "isdir": "1",
                "path": "/子目录",
            },
        ],
    }


def test_platform_metadata() -> None:
    """
    平台能力声明与需求矩阵一致。
    """
    adapter = baidu.BaiduAdapter()

    assert adapter.platform is NetdiskPlatform.BAIDU
    assert adapter.link_policy.value == "transfer_required"
    assert adapter.requires_account is True
    assert adapter.guest_supported is False
    assert adapter.max_concurrency == 512
    assert adapter.cookie_required_keys == ("BDUSS",)
    assert adapter.cookie_domains == (".baidu.com",)
    assert adapter.login_url == "https://pan.baidu.com/"
    assert adapter.user_agent == baidu._WEB_UA


def test_parse_share_link() -> None:
    """
    分享链接解析出分享码与提取码。
    """
    adapter = baidu.BaiduAdapter()

    ref = adapter.parse_share("https://pan.baidu.com/s/1abc12345?pwd=abcd", None)

    assert ref.platform is NetdiskPlatform.BAIDU
    assert ref.share_id == "abc12345"
    assert ref.password == "abcd"


def test_share_detail_verifies_and_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情先用提取码换 sekey，再以根目录模式列文件。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("POST", baidu._VERIFY_URL)] = [{"errno": 0, "randsk": "sekey-1"}]
    responses[("GET", baidu._SHARE_LIST_URL)] = [_list_payload()]

    detail = adapter.share_detail(_ref(), _account())

    assert detail.title == "电影合集"
    assert detail.stoken == "sekey-1"
    assert [item.name for item in detail.files] == ["a.mkv", "子目录"]
    assert detail.files[0].file_id == "900"
    assert detail.files[1].file_id == "/子目录"
    assert detail.files[1].is_dir is True
    assert calls[0]["data"]["pwd"] == "abcd"
    assert calls[1]["params"]["root"] == "1"
    assert calls[1]["params"]["sekey"] == "sekey-1"
    assert calls[1]["params"]["shorturl"] == "12345"
    assert "BDCLND=sekey-1" in calls[1]["headers"]["Cookie"]


def test_list_files_uses_subdir_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    子目录列表使用 root=0 并携带目录路径。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", baidu._SHARE_LIST_URL)] = [_list_payload()]

    files = adapter.list_files(_ref(password=None, token="sekey-1"), "/子目录", _account())

    assert len(files) == 2
    assert calls[0]["params"]["root"] == "0"
    assert calls[0]["params"]["dir"] == "/子目录"


def test_space_insufficient_stops_before_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    配额不足时抛 SPACE_INSUFFICIENT，且不发起转存。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", baidu._QUOTA_URL)] = [{"errno": 0, "total": 1000, "used": 950}]

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(size=1024), _account())

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert [call["url"] for call in calls] == [baidu._QUOTA_URL]


def test_resolve_transfers_locates_and_cleans(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    转存后取 appall 明文直链，下载头带 BDUSS 与手机端 UA，清理删除转存内容。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", baidu._QUOTA_URL)] = [{"errno": 0, "total": 10_000_000, "used": 0}]
    responses[("POST", baidu._VERIFY_URL)] = [{"errno": 0, "randsk": "sekey-1"}]
    responses[("GET", baidu._SHARE_LIST_URL)] = [_list_payload()]
    responses[("GET", baidu._TEMPLATE_URL)] = [{"errno": 0, "result": {"bdstoken": "tok-1"}}]
    responses[("POST", baidu._CREATE_URL)] = [{"errno": 0}]
    responses[("POST", baidu._TRANSFER_URL)] = [
        {
            "errno": 0,
            "extra": {"list": [{"to_fs_id": 777, "to": "/MoviePilot临时转存x/a.mkv"}]},
        }
    ]
    responses[("POST", baidu._LOCATE_URL)] = [
        {
            "errno": 0,
            "urls": [
                {"url": "https://d2-ant.baidu.com/encrypted", "encrypt": 1},
                {"url": "https://appall01.baidupcs.com/plain", "encrypt": 0},
            ],
        }
    ]
    responses[("POST", baidu._DELETE_URL)] = [{"errno": 0}]

    link = adapter.resolve(_ref(), _file(), _account())

    assert link.url == "https://appall01.baidupcs.com/plain"
    assert link.size == 1024
    assert link.account_id == 11
    assert link.cleanup_handle is not None
    assert link.cleanup_handle.directory_id.startswith("/MoviePilot临时转存")
    assert link.cleanup_handle.file_ids == ("777",)
    assert "BDUSS=fake-bduss" in link.headers["Cookie"]
    assert link.headers["User-Agent"] == baidu._CLIENT_UA
    locate = next(call for call in calls if call["url"] == baidu._LOCATE_URL)
    assert locate["params"]["method"] == "locatedownload"
    assert locate["headers"]["User-Agent"] == baidu._CLIENT_UA
    transfer = next(call for call in calls if call["url"] == baidu._TRANSFER_URL)
    assert transfer["data"]["fsidlist"] == '["900"]'
    assert "BDCLND=sekey-1" in transfer["headers"]["Cookie"]

    adapter.cleanup(link.cleanup_handle, _account())

    delete = next(call for call in calls if call["url"] == baidu._DELETE_URL)
    deleted = json.loads(delete["data"]["filelist"])
    assert "/MoviePilot临时转存x/a.mkv" in deleted
    assert link.cleanup_handle.directory_id in deleted


def test_resolve_failure_cleans_temp_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    取链失败时删除已创建的临时目录并保留原始错误。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", baidu._QUOTA_URL)] = [{"errno": 0, "total": 10_000_000, "used": 0}]
    responses[("POST", baidu._VERIFY_URL)] = [{"errno": 0, "randsk": "sekey-1"}]
    responses[("GET", baidu._SHARE_LIST_URL)] = [_list_payload()]
    responses[("GET", baidu._TEMPLATE_URL)] = [{"errno": 0, "result": {"bdstoken": "tok-1"}}]
    responses[("POST", baidu._CREATE_URL)] = [{"errno": 0}]
    responses[("POST", baidu._TRANSFER_URL)] = [
        {"errno": 0, "extra": {"list": [{"to_fs_id": 777, "to": "/tmp/a.mkv"}]}}
    ]
    responses[("POST", baidu._LOCATE_URL)] = [{"errno": -9, "err_msg": "文件不存在"}]
    responses[("POST", baidu._DELETE_URL)] = [{"errno": 0}]

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), _account())

    assert error.value.code is NetdiskErrorCode.UNKNOWN
    delete = next(call for call in calls if call["url"] == baidu._DELETE_URL)
    assert delete["data"]["filelist"].startswith('["/')


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"errno": -6}, NetdiskErrorCode.AUTH_EXPIRED),
        ({"errno": 8888}, NetdiskErrorCode.RATE_LIMITED),
        ({"errno": -12}, NetdiskErrorCode.SHARE_INVALID),
        ({"errno": -9, "err_msg": "触发安全验证"}, NetdiskErrorCode.RISK_BLOCKED),
        ({"errno": -9, "err_msg": "剩余空间不足"}, NetdiskErrorCode.SPACE_INSUFFICIENT),
    ],
)
def test_business_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    expected: NetdiskErrorCode,
) -> None:
    """
    百度 errno 与提示映射为统一错误码。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", baidu._SHARE_LIST_URL)] = [payload]

    with pytest.raises(NetdiskError) as error:
        adapter.list_files(_ref(password=None, token="sekey-1"), "", _account())

    assert error.value.code is expected
    assert "fake-bduss" not in error.value.message


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
    adapter = baidu.BaiduAdapter()

    def get_json(_self, url: str, **_kwargs):
        return _self._json(_Response(status_code=status, content_type=content_type))

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)

    with pytest.raises(NetdiskError) as error:
        adapter.list_files(_ref(password=None, token="sekey-1"), "", _account())

    assert error.value.code is expected


def test_validate_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    凭据校验接受 Cookie 形态并调用账号信息接口。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", baidu._TEMPLATE_URL)] = [{"errno": 0, "result": {"username": "阿度"}}]

    assert adapter.validate_credential({}).status == "expired"
    probe = adapter.validate_credential({"BDUSS": "fake-bduss"})

    assert probe.status == "active"
    assert "阿度" in probe.message


def test_validate_credential_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    账号接口返回未登录时判为过期。
    """
    adapter, _calls, responses = _install(monkeypatch)
    responses[("GET", baidu._TEMPLATE_URL)] = [{"errno": -6}]

    probe = adapter.validate_credential({"cookie": "BDUSS=fake-bduss"})

    assert probe.status == "expired"


def test_probe_account_passes_account_for_cookie_writeback(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    探测已保存账号时必须带上账号记录，才能把百度轮换后的 Cookie 回写。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("GET", baidu._TEMPLATE_URL)] = [{"errno": 0, "result": {"username": "阿度"}}]
    account = _account()

    probe = adapter.probe_account(account)

    assert probe.status == "active"
    assert calls[0]["account"] is account


def test_baidu_http_uses_direct_connection_like_login_browser() -> None:
    """
    百度 API 与登录浏览器都走直连，保持同一出口 IP。
    """
    assert baidu.BaiduAdapter().http.proxies == {"http": "", "https": ""}


def test_verify_retries_without_cookie_on_risk_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    带账号 Cookie 校验被风控拒绝时，去掉 Cookie 重试并接受公开校验结果。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[("POST", baidu._VERIFY_URL)] = [
        {"errno": 200025, "err_msg": "提取码输入错误，请重试"},
        {"errno": 0, "randsk": "sekey-1"},
    ]
    ref = ShareRef(platform=NetdiskPlatform.BAIDU, share_id="-o-abc", password="nhf3")
    account = NetdiskAccountRecord(
        id=1, platform=NetdiskPlatform.BAIDU, name="百度测试账号",
        credential={"cookie": "BDUSS=fake"},
    )

    sekey = adapter._secret(ref, account)  # pylint: disable=protected-access

    assert sekey == "sekey-1"
    assert "Cookie" in calls[0]["headers"]
    assert "Cookie" not in calls[1]["headers"]


def test_baidu_share_surl_strips_leading_one() -> None:
    """
    百度校验接口的 surl 必须去掉分享码首位的 1。
    """
    from app.adapters.external.netdisk import baidu as baidu_module

    assert baidu_module._share_surl("1AbCdEf") == "AbCdEf"
    assert baidu_module._share_surl("AbCdEf") == "AbCdEf"
    assert baidu_module._share_surl("") == ""
