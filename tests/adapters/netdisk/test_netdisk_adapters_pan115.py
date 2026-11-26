"""
115 网盘适配器的协议解析与取链流程测试。

全部 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，Cookie 使用假值。
"""

from typing import Any

import pytest

from app.adapters.external.netdisk import base as base_module
from app.adapters.external.netdisk import pan115 as pan115_module
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

_COOKIE = "UID=fake-uid; SEID=fake-seid; CID=fake-cid; KID=fake-kid"


def _account() -> NetdiskAccountRecord:
    """
    构造使用假 Cookie 的 115 账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=9,
        platform=NetdiskPlatform.PAN115,
        name="115测试账号",
        credential={"cookie": _COOKIE},
        is_primary=True,
    )


def _ref(share_token: str = "x1y2|42") -> ShareRef:
    """
    构造已持有分享会话令牌的分享引用，避免测试重复探测根目录。

    :param share_token: `<提取码>|<分享者 UID>` 形式的会话令牌
    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.PAN115,
        share_id="swabc123",
        password="x1y2",
        url="https://115.com/s/swabc123",
        share_token=share_token,
    )


def _file() -> ShareFile:
    """
    构造分享根目录下的目标文件。

    :return: 文件条目
    """
    return ShareFile(
        file_id="f1",
        name="01.mkv",
        size=1024,
        is_dir=False,
        parent_id="0",
    )


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
        115 适配器不应使用文本请求。
        """
        raise AssertionError("115 适配器不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)


def test_adapter_declares_pan115_capabilities() -> None:
    """
    适配器声明 115 平台能力与固定请求头。
    """
    adapter = pan115_module.Pan115Adapter()

    assert adapter.platform is NetdiskPlatform.PAN115
    assert adapter.link_policy is LinkPolicy.DIRECT_WITH_FALLBACK
    assert adapter.requires_account is True
    assert adapter.guest_supported is True
    assert adapter.max_concurrency == 512
    assert adapter.cookie_required_keys == ("UID", "SEID")
    assert adapter.cookie_domains == (".115.com", ".115cdn.com")
    assert adapter.login_url == "https://115.com/"
    assert adapter.referer == "https://115.com/"
    assert "115Browser" in adapter.user_agent


def test_parse_share_link_accepts_link_and_command() -> None:
    """
    分享链接与 115 口令都解析出分享码与提取码，其他平台链接被拒绝。
    """
    adapter = pan115_module.Pan115Adapter()

    ref = adapter.parse_share("https://115.com/s/swabc123 提取码：x1y2", None)
    assert ref.platform is NetdiskPlatform.PAN115
    assert ref.share_id == "swabc123"
    assert ref.password == "x1y2"

    command = adapter.parse_share("https://115.com/swabc12345-x1y2", None)
    assert command.share_id == "swabc12345"
    assert command.password == "x1y2"

    with pytest.raises(NetdiskError) as error:
        adapter.parse_share("https://www.guangyapan.com/s/abc123", None)
    assert error.value.code is NetdiskErrorCode.UNSUPPORTED


def test_share_detail_returns_title_user_token_and_files(monkeypatch) -> None:
    """
    分享详情返回标题、根目录条目、会话令牌与文件总大小。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        分享根目录返回分享者 UID、标题与两个条目。
        """
        assert url == pan115_module._SHARE_SNAP_URL
        assert (params or {}).get("cid") == "0"
        return {
            "state": True,
            "data": {
                "count": 2,
                "userinfo": {"user_id": "42"},
                "shareinfo": {"share_title": "示例剧集"},
                "list": [
                    {"fid": "f1", "n": "01.mkv", "s": 100, "fc": 1, "cid": "0"},
                    {"cid": "dir-1", "n": "花絮", "s": 0, "fc": 0, "pid": "0"},
                ],
            },
        }

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    detail = adapter.share_detail(_ref(share_token=""), _account())

    assert detail.title == "示例剧集"
    assert detail.stoken == "x1y2|42"
    assert detail.size == 100
    assert [item.file_id for item in detail.files] == ["f1", "dir-1"]
    assert detail.files[0].is_dir is False
    assert detail.files[1].is_dir is True


def test_list_files_paginates_share_directory(monkeypatch) -> None:
    """
    列目录按 offset 索引翻页，直到整页不足或取满总数。
    """
    monkeypatch.setattr(pan115_module, "_SHARE_PAGE_LIMIT", 2)
    offsets = []

    def handler(method, http, url, account, params, body, headers):
        """
        第一页返回两条，第二页返回一条。
        """
        assert url == pan115_module._SHARE_SNAP_URL
        offset = int((params or {}).get("offset", 0))
        offsets.append(offset)
        if offset == 0:
            items = [
                {"fid": "f1", "n": "01.mkv", "s": 10, "fc": 1, "cid": "dir-9"},
                {"fid": "f2", "n": "02.mkv", "s": 20, "fc": 1, "cid": "dir-9"},
            ]
        else:
            items = [{"fid": "f3", "n": "03.mkv", "s": 30, "fc": 1, "cid": "dir-9"}]
        return {"state": True, "data": {"count": 3, "list": items}}

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    files = adapter.list_files(_ref(), "dir-9", _account())

    assert [item.file_id for item in files] == ["f1", "f2", "f3"]
    assert offsets == [0, 2]
    assert files[0].parent_id == "dir-9"


def test_resolve_returns_direct_link_with_double_cookie(monkeypatch) -> None:
    """
    免转存取链成功时不创建临时目录，下载头拼接登录 Cookie 与 CDN Cookie。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        分享直链接口返回直链并模拟下发 CDN Cookie。
        """
        assert url == pan115_module._SHARE_DOWNLOAD_URL
        assert (params or {}).get("user_id") == "42"
        assert (params or {}).get("file_id") == "f1"
        http.cdn_cookie = "ab12cd34ef56=cdn-token"
        return {
            "state": True,
            "data": {
                "file_url_302": "https://cdn.115.com/a.mkv",
                "fn": "01.mkv",
                "fs": "2048",
            },
        }

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    link = adapter.resolve(_ref(), _file(), _account())

    assert link.url == "https://cdn.115.com/a.mkv"
    assert link.size == 2048
    assert link.cleanup_handle is None
    assert link.account_id == 9
    assert link.is_hls is False
    cookie = link.headers["Cookie"]
    assert "UID=fake-uid" in cookie
    assert "SEID=fake-seid" in cookie
    assert "ab12cd34ef56=cdn-token" in cookie
    assert cookie.count(";") == 4
    assert link.headers["User-Agent"] == pan115_module.CLIENT_USER_AGENT
    assert link.headers["Referer"] == "https://115.com/"


def test_resolve_falls_back_to_transfer_on_50029(monkeypatch) -> None:
    """
    网页端以 50029 拒绝取链时转存到唯一临时目录，再走客户端通道取链并返回清理句柄。
    """
    calls = []
    folder_names = []

    def handler(method, http, url, account, params, body, headers):
        """
        首次分享取链失败，随后完成建目录、转存、认领与客户端取链。
        """
        calls.append((method, url))
        if url == pan115_module._SHARE_DOWNLOAD_URL:
            return {"state": False, "errno": 50029, "error": "当前版本过低，请升级到最新版本下载"}
        if url == pan115_module._FILE_ADD_URL:
            folder_names.append(body["cname"])
            assert body["pid"] == "0"
            return {"state": True, "cid": "dir-1"}
        if url == pan115_module._SHARE_RECEIVE_URL:
            assert body["cid"] == "dir-1"
            assert body["file_id"] == "f1"
            return {"state": True}
        if url == pan115_module._FILES_URL:
            assert (params or {}).get("cid") == "dir-1"
            return {
                "state": True,
                "count": 1,
                "data": [{"n": "01.mkv", "fid": "new-1", "pc": "pick-1", "s": 2048, "cid": "dir-1"}],
            }
        if url.startswith(pan115_module._APP_DOWNLOAD_URL):
            assert body["data"] == "encrypted"
            return {"state": True, "data": "cipher"}
        raise AssertionError(url)

    monkeypatch.setattr(pan115_module, "_encode_payload", lambda payload, key: "encrypted")
    monkeypatch.setattr(
        pan115_module,
        "_decode_payload",
        lambda data, key: {
            "new-1": {
                "file_name": "01.mkv",
                "file_size": "2048",
                "url": {"url": "https://cdn.115.com/saved.mkv"},
            }
        },
    )
    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    link = adapter.resolve(_ref(), _file(), _account())

    handle = link.cleanup_handle
    assert handle is not None
    assert handle.platform is NetdiskPlatform.PAN115
    assert handle.account_id == 9
    assert handle.directory_id == "dir-1"
    assert handle.file_ids == ("new-1",)
    assert link.url == "https://cdn.115.com/saved.mkv"
    assert link.size == 2048
    assert folder_names and folder_names[0].startswith("mp_tr_")
    assert all(url != pan115_module._FILE_DELETE_URL for _, url in calls)


def test_cleanup_deletes_transfer_directory_and_swallows_failure(monkeypatch) -> None:
    """
    清理删除临时目录；删除失败只记录不抛出。
    """
    bodies = []
    should_fail = {"value": False}

    def handler(method, http, url, account, params, body, headers):
        """
        记录删除请求，可切换为失败响应。
        """
        assert url == pan115_module._FILE_DELETE_URL
        bodies.append(body)
        if should_fail["value"]:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口无响应", platform=NetdiskPlatform.PAN115)
        return {"state": True}

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()
    handle = TransferHandle(
        platform=NetdiskPlatform.PAN115,
        account_id=9,
        directory_id="dir-1",
        file_ids=("new-1",),
    )

    adapter.cleanup(handle, _account())

    assert bodies[0] == {"pid": "0", "ignore_warn": "1", "fid[0]": "dir-1"}

    should_fail["value"] = True
    adapter.cleanup(handle, _account())
    assert len(bodies) == 2


def test_50029_without_transfer_permission_raises_unsupported(monkeypatch) -> None:
    """
    不允许转存时把 50029 作为未支持分类抛出，且错误消息不含 Cookie。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        网页端拒绝下发直链。
        """
        return {"state": False, "errno": 50029, "error": "当前版本过低"}

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), _account(), allow_transfer=False)

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert error.value.platform is NetdiskPlatform.PAN115
    assert "fake-uid" not in str(error.value)


@pytest.mark.parametrize(
    ("errno", "expected"),
    [
        (990001, NetdiskErrorCode.AUTH_EXPIRED),
        (4100012, NetdiskErrorCode.SHARE_INVALID),
        (4100008, NetdiskErrorCode.SHARE_INVALID),
        (4100026, NetdiskErrorCode.SHARE_INVALID),
        (4100010, NetdiskErrorCode.SHARE_INVALID),
        (4100018, NetdiskErrorCode.LINK_EXPIRED),
        (990009, NetdiskErrorCode.RATE_LIMITED),
    ],
)
def test_business_errno_maps_to_classified_codes(monkeypatch, errno: int, expected) -> None:
    """
    服务端业务码映射为统一错误分类。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        返回指定错误码。
        """
        return {"state": False, "errno": errno, "error": "失败原因"}

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    with pytest.raises(NetdiskError) as error:
        adapter.list_files(_ref(), "0", _account())

    assert error.value.code is expected
    assert "fake-uid" not in str(error.value)


def test_http_status_and_html_map_to_classified_errors(monkeypatch) -> None:
    """
    基类把 401/429 与 HTML 防盗链页面映射为统一错误分类。
    """
    responses: dict[str, Any] = {
        "status": 401,
        "content_type": "application/json",
        "payload": {},
        "json_ok": True,
    }

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
            返回预设 JSON；HTML 场景模拟解析失败。
            """
            if not responses["json_ok"]:
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

        def get_res(self, url, params=None, allow_redirects=True):
            """
            返回当前场景的响应。
            """
            return FakeResponse()

    monkeypatch.setattr(base_module, "RequestUtils", FakeRequest)
    adapter = pan115_module.Pan115Adapter()

    for status, expected in ((401, NetdiskErrorCode.AUTH_EXPIRED), (429, NetdiskErrorCode.RATE_LIMITED)):
        responses["status"] = status
        with pytest.raises(NetdiskError) as error:
            adapter.share_detail(_ref(), _account())
        assert error.value.code is expected

    responses["status"] = 200
    responses["content_type"] = "text/html"
    responses["json_ok"] = False
    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(_ref(), _account())
    assert error.value.code is NetdiskErrorCode.LINK_EXPIRED


def test_validate_credential_filters_session_cookies(monkeypatch) -> None:
    """
    凭据校验只保留会话字段，并用过滤后的 Cookie 调用 `/user/info`。
    """
    sent_cookies = []

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求 Cookie 并返回登录态有效。
        """
        assert url == pan115_module._USER_INFO_URL
        sent_cookies.append(account.credential["cookie"])
        return {"state": True, "data": "138****0000"}

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    probe = adapter.validate_credential(
        {"cookie": "UID=fake-uid; SEID=fake-seid; acw_tc=drop-me; __puus=drop-me"}
    )

    assert probe.status == "active"
    assert "138****0000" in probe.message
    assert "acw_tc" not in sent_cookies[0]
    assert "__puus" not in sent_cookies[0]
    assert "UID=fake-uid" in sent_cookies[0]


def test_validate_credential_rejects_missing_seid_without_request(monkeypatch) -> None:
    """
    缺少 SEID 时直接判定为过期，不发起请求。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        不应被调用。
        """
        raise AssertionError("缺少必填字段时不应发起请求")

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    probe = adapter.validate_credential({"cookie": "UID=fake-uid"})

    assert probe.status == "expired"
    assert "SEID" in probe.message


def test_probe_account_maps_login_timeout_to_expired(monkeypatch) -> None:
    """
    账号探测把 errno 990001 归类为登录态失效。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        `/user/info` 返回登录超时。
        """
        assert url == pan115_module._USER_INFO_URL
        return {"state": False, "errno": 990001, "error": "登录超时"}

    _patch_http(monkeypatch, handler)
    adapter = pan115_module.Pan115Adapter()

    probe = adapter.probe_account(_account())

    assert probe.status == "expired"
    assert "fake-uid" not in probe.message


def test_rsa_encrypt_produces_fixed_length_blocks() -> None:
    """
    电脑端取链的 RSA 分块输出为定长 128 字节。
    """
    encrypted = pan115_module._rsa_encrypt(b"0123456789abcdef")

    assert len(encrypted) == 128
    assert int.from_bytes(encrypted, "big") < pan115_module._RSA_MODULUS


def test_download_headers_include_login_and_cdn_cookie() -> None:
    """
    下载头由登录 Cookie 与 CDN Cookie 双拼，并带客户端 UA。
    """
    adapter = pan115_module.Pan115Adapter()

    headers = adapter._download_headers(_account(), "ab12=cdn")

    assert headers["Cookie"] == f"{_COOKIE}; ab12=cdn"
    assert headers["User-Agent"] == pan115_module.CLIENT_USER_AGENT
    assert headers["X-Requested-With"] == "XMLHttpRequest"


def test_html_content_type_with_json_body_is_accepted(monkeypatch) -> None:
    """
    115 会用 text/html 返回合法 JSON，解析必须优先于内容类型判断。
    """
    class FakeResponse:
        """
        返回 text/html 但正文为合法 JSON 的响应替身。
        """

        status_code = 200
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def json(self) -> dict[str, Any]:
            """
            返回登录态信息。
            """
            return {"state": True, "data": "137*****542"}

        @property
        def text(self) -> str:
            """
            返回原始 JSON 文本。
            """
            return '{"state":true,"data":"137*****542"}'

    class FakeRequest:
        """
        返回固定响应的传输替身。
        """

        def __init__(self, **kwargs) -> None:
            """
            保存构造参数。
            """
            self.kwargs = kwargs

        def get_res(self, url, params=None, allow_redirects=True):
            """
            返回合法 JSON。
            """
            return FakeResponse()

    monkeypatch.setattr(base_module, "RequestUtils", FakeRequest)
    adapter = pan115_module.Pan115Adapter()

    probe = adapter.validate_credential({"cookie": "UID=u; SEID=s"})

    assert probe.status == "active"
    assert "137*****542" in probe.message
