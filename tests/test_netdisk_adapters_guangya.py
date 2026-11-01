"""
光鸭云盘适配器的协议解析与取链流程测试。

全部 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，token 使用假值。
"""

from typing import Any

import pytest

from app.adapters.external.netdisk import base as base_module
from app.adapters.external.netdisk import guangya as guangya_module
from app.adapters.external.netdisk.base import NetdiskHttp
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import (
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareFile,
    ShareRef,
    TransferHandle,
)

_TOKEN = "fake-access-token"


def _account() -> NetdiskAccountRecord:
    """
    构造使用假 token 的光鸭账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=11,
        platform=NetdiskPlatform.GUANGYA,
        name="光鸭测试账号",
        credential={"token": _TOKEN},
        is_primary=True,
    )


def _ref(share_token: str = "share-token") -> ShareRef:
    """
    构造已持有分享访问令牌的分享引用。

    :param share_token: 分享访问令牌
    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.GUANGYA,
        share_id="abc123",
        password="x1y2",
        url="https://www.guangyapan.com/s/abc123",
        share_token=share_token,
    )


def _file() -> ShareFile:
    """
    构造分享根目录下的目标文件。

    :return: 文件条目
    """
    return ShareFile(file_id="f1", name="01.mkv", size=1024, is_dir=False, parent_id="")


def _patch_http(monkeypatch, handler) -> None:
    """
    用统一路由函数替换基类 HTTP 方法，并禁止文本请求。

    :param monkeypatch: pytest 打桩夹具
    :param handler: 接收 (方法, http 实例, 地址, 账号, 参数, 请求体, 请求头) 并返回 JSON 的函数
    """

    def get_json(self, url, *, account=None, params=None, headers=None):
        """
        转发 GET 请求到测试路由。
        """
        return handler("GET", self, url, account, params, None, headers)

    def post_json(self, url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        转发 POST 请求到测试路由。
        """
        return handler("POST", self, url, account, params, json if json is not None else data, headers)

    def get_text(self, url, *, account=None, params=None, headers=None):
        """
        光鸭适配器不应使用文本请求。
        """
        raise AssertionError("光鸭适配器不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)


def test_adapter_declares_guangya_capabilities() -> None:
    """
    适配器声明光鸭平台能力与固定请求头。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.platform is NetdiskPlatform.GUANGYA
    assert adapter.link_policy is LinkPolicy.DIRECT_WITH_FALLBACK
    assert adapter.requires_account is True
    assert adapter.guest_supported is True
    assert adapter.max_concurrency == 512
    assert adapter.cookie_required_keys == ("token",)
    assert adapter.cookie_domains == (".guangyapan.com",)
    assert adapter.login_url == "https://www.guangyapan.com/"
    assert adapter.supports_password_login is True
    assert adapter.referer == "https://www.guangyapan.com/"


def test_parse_share_link_keeps_share_id_and_passcode() -> None:
    """
    分享文案解析出光鸭分享码与提取码，其他平台链接被拒绝。
    """
    adapter = guangya_module.GuangYaAdapter()

    ref = adapter.parse_share("https://www.guangyapan.com/s/abc123 提取码：x1y2", None)

    assert ref.platform is NetdiskPlatform.GUANGYA
    assert ref.share_id == "abc123"
    assert ref.password == "x1y2"

    with pytest.raises(NetdiskError) as error:
        adapter.parse_share("https://115.com/s/swabc123", None)
    assert error.value.code is NetdiskErrorCode.UNSUPPORTED


def test_share_detail_returns_title_token_and_files(monkeypatch) -> None:
    """
    分享详情换取访问令牌、读取标题并解析根目录条目。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        分享令牌、摘要与目录接口分别返回固定数据。
        """
        if url == guangya_module._SHARE_TOKEN_URL:
            assert body["shareId"] == "abc123"
            assert body["code"] == "x1y2"
            return {"success": True, "code": "0", "data": {"accessToken": "share-token"}}
        if url == guangya_module._SHARE_SUMMARY_URL:
            return {"success": True, "code": "0", "data": {"title": "示例剧集"}}
        if url == guangya_module._SHARE_FILES_URL:
            assert body["accessToken"] == "share-token"
            return {
                "success": True,
                "code": "0",
                "data": {
                    "hasMore": False,
                    "list": [
                        {"fileId": "f1", "fileName": "01.mkv", "fileSize": "100", "resType": "1"},
                        {"fileId": "dir-1", "fileName": "花絮", "resType": "2"},
                    ],
                },
            }
        raise AssertionError(url)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    detail = adapter.share_detail(_ref(share_token=""), _account())

    assert detail.title == "示例剧集"
    assert detail.stoken == "share-token"
    assert detail.size == 100
    assert [item.file_id for item in detail.files] == ["f1", "dir-1"]
    assert detail.files[0].is_dir is False
    assert detail.files[1].is_dir is True


def test_list_files_follows_cursor(monkeypatch) -> None:
    """
    列目录按游标翻页，hasMore 为假时结束。
    """
    cursors = []

    def handler(method, http, url, account, params, body, headers):
        """
        第一页返回游标，第二页结束。
        """
        assert url == guangya_module._SHARE_FILES_URL
        cursors.append(body.get("cursor", ""))
        if body.get("cursor") is None:
            return {
                "success": True,
                "code": "0",
                "data": {
                    "hasMore": True,
                    "cursor": "c2",
                    "list": [{"fileId": "f1", "fileName": "01.mkv", "fileSize": "10"}],
                },
            }
        return {
            "success": True,
            "code": "0",
            "data": {
                "hasMore": False,
                "list": [{"fileId": "f2", "fileName": "02.mkv", "fileSize": "20"}],
            },
        }

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    files = adapter.list_files(_ref(), "", _account())

    assert [item.file_id for item in files] == ["f1", "f2"]
    assert cursors == ["", "c2"]


def test_resolve_returns_direct_link_with_account_token(monkeypatch) -> None:
    """
    免登录取链成功时不创建临时目录，下载头携带账号 token。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        分享取链接口直接返回签名地址。
        """
        assert url == guangya_module._SHARE_DOWNLOAD_URL
        assert body["fileId"] == "f1"
        assert body["accessToken"] == "share-token"
        return {
            "success": True,
            "code": "0",
            "data": {"signedURL": "https://cdn.guangyapan.com/a.mkv", "size": "2048"},
        }

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    link = adapter.resolve(_ref(), _file(), _account())

    assert link.url == "https://cdn.guangyapan.com/a.mkv"
    assert link.size == 2048
    assert link.cleanup_handle is None
    assert link.account_id == 11
    assert link.is_hls is False
    assert link.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert link.headers["User-Agent"] == guangya_module.GUANGYA_USER_AGENT
    assert link.headers["Referer"] == "https://www.guangyapan.com/"


def test_resolve_falls_back_to_transfer_when_share_restricted(monkeypatch) -> None:
    """
    分享者未开启免登录下载时转存到唯一临时目录，再从个人盘取链并返回清理句柄。
    """
    folder_names = []
    bodies = {}

    def handler(method, http, url, account, params, body, headers):
        """
        首次分享取链返回 207，随后完成建目录、转存、轮询与个人盘取链。
        """
        if url == guangya_module._SHARE_DOWNLOAD_URL:
            return {"code": "207", "msg": "分享下载受限"}
        if url == guangya_module._CREATE_DIR_URL:
            folder_names.append(body["dirName"])
            assert body["parentId"] == ""
            return {"success": True, "code": "0", "data": {"fileId": "dir-1"}}
        if url == guangya_module._RESTORE_SHARE_URL:
            assert body["accessToken"] == "share-token"
            assert body["fileIds"] == ["f1"]
            assert body["parentId"] == "dir-1"
            assert headers["Authorization"] == f"Bearer {_TOKEN}"
            return {"success": True, "code": "0", "data": {"taskId": ""}}
        if url == guangya_module._FILE_LIST_URL:
            return {
                "success": True,
                "code": "0",
                "data": {
                    "total": "1",
                    "list": [{"fileId": "new-1", "fileName": "01.mkv", "fileSize": "1024"}],
                },
            }
        if url == guangya_module._RES_DOWNLOAD_URL:
            bodies["res"] = body
            return {
                "success": True,
                "code": "0",
                "data": {"signedURL": "https://cdn.guangyapan.com/saved.mkv", "size": "1024"},
            }
        if url == guangya_module._DELETE_FILE_URL:
            bodies["delete"] = body
            return {"success": True, "code": "0", "data": {}}
        raise AssertionError(url)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    link = adapter.resolve(_ref(), _file(), _account())

    handle = link.cleanup_handle
    assert handle is not None
    assert handle.platform is NetdiskPlatform.GUANGYA
    assert handle.account_id == 11
    assert handle.directory_id == "dir-1"
    assert handle.file_ids == ("new-1",)
    assert link.url == "https://cdn.guangyapan.com/saved.mkv"
    assert link.size == 1024
    assert folder_names and folder_names[0].startswith("mp_tr_")
    assert bodies["res"] == {"fileId": "new-1"}
    assert "delete" not in bodies

    adapter.cleanup(handle, _account())

    assert bodies["delete"] == {"fileIds": ["dir-1"]}


def test_share_restricted_without_transfer_permission_raises_unsupported(monkeypatch) -> None:
    """
    未登录或不允许转存时给出可展示的未支持分类，且错误消息不含 token。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        分享取链返回受限。
        """
        return {"code": "207", "msg": "分享下载受限"}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), None, allow_transfer=True)

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert error.value.platform is NetdiskPlatform.GUANGYA
    assert "登录" in error.value.message
    assert _TOKEN not in str(error.value)


def test_password_login_returns_token(monkeypatch) -> None:
    """
    账号密码登录规范化手机号并返回可保存的 token。
    """
    sent = {}

    def handler(method, http, url, account, params, body, headers):
        """
        记录登录请求并返回令牌。
        """
        assert url == guangya_module._SIGNIN_URL
        sent["body"] = body
        sent["headers"] = headers
        return {
            "success": True,
            "code": "0",
            "data": {"access_token": "at-1", "refresh_token": "rt-1"},
        }

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    credential = adapter.password_login("13800138000", "secret")

    assert credential == {"token": "at-1"}
    assert sent["body"]["username"] == "+86 13800138000"
    assert sent["body"]["client_id"] == guangya_module._CLIENT_ID
    assert sent["headers"]["X-Client-Id"] == guangya_module._CLIENT_ID
    assert "X-Device-Sign" in sent["headers"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": "captcha_required", "msg": "需要人机验证"}, NetdiskErrorCode.RISK_BLOCKED),
        ({"code": "verification_required", "msg": "需要短信验证"}, NetdiskErrorCode.RISK_BLOCKED),
        ({"code": "invalid_password", "msg": "密码错误"}, NetdiskErrorCode.AUTH_EXPIRED),
        ({"code": "too_many_requests", "msg": "过于频繁"}, NetdiskErrorCode.RATE_LIMITED),
    ],
)
def test_password_login_maps_failures(monkeypatch, payload, expected) -> None:
    """
    登录失败按风控、凭据与限流分类。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        返回指定登录失败响应。
        """
        return payload

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.password_login("13800138000", "secret")

    assert error.value.code is expected
    assert error.value.message
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("209", NetdiskErrorCode.SHARE_INVALID),
        ("200", NetdiskErrorCode.SHARE_INVALID),
        ("157", NetdiskErrorCode.SPACE_INSUFFICIENT),
        ("504", NetdiskErrorCode.UNSUPPORTED),
        ("429", NetdiskErrorCode.RATE_LIMITED),
    ],
)
def test_business_code_maps_to_classified_errors(monkeypatch, code: str, expected) -> None:
    """
    业务码映射为统一错误分类。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        分享令牌接口返回指定业务码。
        """
        return {"success": False, "code": code, "msg": "失败原因"}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(share_token=""), _account())

    assert error.value.code is expected
    assert _TOKEN not in str(error.value)


def test_http_status_and_html_map_to_classified_errors(monkeypatch) -> None:
    """
    基类把 401/429 与 HTML 防盗链页面映射为统一错误分类。
    """
    responses: dict[str, Any] = {"status": 401, "content_type": "application/json", "payload": {}}

    class FakeResponse:
        """
        只实现基类解析所需的响应替身。
        """

        def __init__(self) -> None:
            """
            按当前场景设置状态码与响应头。
            """
            self.status_code = responses["status"]
            self.headers = {"Content-Type": responses["content_type"]}

        def json(self) -> dict[str, Any]:
            """
            返回预设 JSON；HTML 页面模拟解析失败。
            """
            if "text/html" in responses["content_type"]:
                raise ValueError("not json")
            return responses["payload"]

        @property
        def text(self) -> str:
            """
            返回空文本。
            """
            return ""

    class FakeRequest:
        """
        记录请求头并返回固定响应的传输替身。
        """

        def __init__(self, **kwargs) -> None:
            """
            保存构造参数。
            """
            self.kwargs = kwargs

        def post_res(self, url, data=None, params=None, allow_redirects=True, json=None, **kwargs):
            """
            返回当前场景的响应。
            """
            return FakeResponse()

    monkeypatch.setattr(base_module, "RequestUtils", FakeRequest)
    adapter = guangya_module.GuangYaAdapter()

    for status, expected in ((401, NetdiskErrorCode.AUTH_EXPIRED), (429, NetdiskErrorCode.RATE_LIMITED)):
        responses["status"] = status
        with pytest.raises(NetdiskError) as error:
            adapter.list_files(_ref(), "", _account())
        assert error.value.code is expected

    responses["status"] = 200
    responses["content_type"] = "text/html"
    with pytest.raises(NetdiskError) as error:
        adapter.list_files(_ref(), "", _account())
    assert error.value.code is NetdiskErrorCode.LINK_EXPIRED


def test_validate_credential_calls_user_me(monkeypatch) -> None:
    """
    凭据校验用 `/v1/user/me` 确认 token 可用；缺少 token 时不发请求。
    """
    calls = []

    def handler(method, http, url, account, params, body, headers):
        """
        账号信息接口返回昵称。
        """
        assert url == guangya_module._USER_ME_URL
        calls.append(headers["Authorization"])
        return {"success": True, "sub": "u1", "nickname": "鸭子"}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    probe = adapter.validate_credential({"token": _TOKEN})

    assert probe.status == "active"
    assert "鸭子" in probe.message
    assert calls == [f"Bearer {_TOKEN}"]

    missing = adapter.validate_credential({"cookie": "no-token-here"})
    assert missing.status == "expired"
    assert len(calls) == 1


def test_probe_account_maps_invalid_token_to_expired(monkeypatch) -> None:
    """
    账号探测把登录态失效归类为 expired。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        账号信息接口返回未登录。
        """
        raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "网盘登录态已失效", platform=NetdiskPlatform.GUANGYA)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    probe = adapter.probe_account(_account())

    assert probe.status == "expired"
    assert _TOKEN not in probe.message


def test_cleanup_swallows_delete_failure(monkeypatch) -> None:
    """
    清理删除失败只记录，不抛出。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        删除接口失败。
        """
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口无响应", platform=NetdiskPlatform.GUANGYA)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()
    handle = TransferHandle(
        platform=NetdiskPlatform.GUANGYA,
        account_id=11,
        directory_id="dir-1",
        file_ids=("new-1",),
    )

    adapter.cleanup(handle, _account())
