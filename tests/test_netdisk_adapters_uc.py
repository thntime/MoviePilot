"""
UC 网盘适配器的协议、错误分类与刷新逻辑单元测试。

所有 HTTP 都在 `NetdiskHttp` 层被替换为假响应，测试不访问真实网络。
"""

from typing import Any, Callable, Optional

import pytest

from app.adapters.external.netdisk import base as base_module
from app.adapters.external.netdisk.uc import (
    UC_ACCOUNT_INFO_URL,
    UCAdapter,
    merge_set_cookies,
    strip_puus,
)
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import (
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareFile,
    ShareRef,
)

REF = ShareRef(
    platform=NetdiskPlatform.UC,
    share_id="share123",
    password="abcd",
    url="https://drive.uc.cn/s/share123",
)
ACCOUNT = NetdiskAccountRecord(
    id=7,
    platform=NetdiskPlatform.UC,
    name="测试账号",
    credential={"cookie": "__pus=old-pus; __puus=old-puus; uid=42"},
)
TOKEN_PAYLOAD = {
    "status": 200,
    "code": 0,
    "data": {"stoken": "st-abc", "title": "测试分享", "first_fid": "f-root"},
}
EMPTY_LIST_PAYLOAD = {"status": 200, "code": 0, "data": {"list": []}}


class _FakeResponse:
    """
    供基类 `_json` 解析的最小 HTTP 响应替身。
    """

    def __init__(
        self,
        status_code: int,
        payload: Optional[Any] = None,
        content_type: str = "application/json",
    ) -> None:
        """
        保存状态码、响应体与内容类型。

        :param status_code: HTTP 状态码
        :param payload: JSON 响应体
        :param content_type: Content-Type 头
        """
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._payload = payload
        self.text = ""
        self.history: list[Any] = []
        self.raw = None

    def json(self) -> Any:
        """
        返回预设的 JSON 响应体；HTML 页面模拟解析失败。
        """
        if "text/html" in str(self.headers.get("Content-Type", "")).lower():
            raise ValueError("not json")
        return self._payload


@pytest.fixture()
def adapter() -> UCAdapter:
    """
    构造一个使用假 Cookie 的适配器实例。
    """
    return UCAdapter()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    禁止任何测试触发真实文本请求。
    """
    def forbidden(self, url: str, **kwargs: Any) -> str:  # noqa: ARG001
        raise AssertionError(f"测试禁止真实网络请求：{url}")

    monkeypatch.setattr(base_module.NetdiskHttp, "get_text", forbidden)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    routes: list[tuple[str, Any]],
) -> list[dict[str, Any]]:
    """
    按 URL 片段安装假响应，并记录每次调用。

    :param monkeypatch: pytest 打桩工具
    :param routes: `(URL 片段, 响应体或处理函数)` 列表，按顺序匹配
    :return: 调用记录列表
    """
    calls: list[dict[str, Any]] = []

    def make(method: str) -> Callable[..., dict[str, Any]]:
        def fake(self, url: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"method": method, "url": url, "kwargs": kwargs})
            for marker, handler in routes:
                if marker in url:
                    if callable(handler):
                        return handler(self, url, kwargs)
                    return handler
            raise AssertionError(f"未预期的 {method} 请求：{url}")

        return fake

    monkeypatch.setattr(base_module.NetdiskHttp, "get_json", make("GET"))
    monkeypatch.setattr(base_module.NetdiskHttp, "post_json", make("POST"))
    return calls


def _detail_pages(pages: dict[int, dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """
    构造按 `_page` 返回不同条目的目录处理函数。

    :param pages: 页码到响应体的映射
    :return: 处理函数
    """

    def handler(http: Any, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        page = int(kwargs["params"]["_page"])
        return pages.get(page, EMPTY_LIST_PAYLOAD)

    return handler


def _item(fid: str, name: str, size: int, *, is_dir: bool = False, token: str = "") -> dict[str, Any]:
    """
    构造一条分享条目。

    :param fid: 文件标识
    :param name: 文件名
    :param size: 文件大小
    :param is_dir: 是否目录
    :param token: 文件令牌
    :return: 接口条目字典
    """
    return {
        "fid": fid,
        "file_name": name,
        "size": size,
        "dir": is_dir,
        "pdir_fid": "0",
        "share_fid_token": token,
    }


def test_parse_share_matches_uc(adapter: UCAdapter) -> None:
    """
    分享链接解析应落在 UC 平台并带上提取码。
    """
    ref = adapter.parse_share("https://drive.uc.cn/s/share123", "abcd")
    assert ref.platform is NetdiskPlatform.UC
    assert ref.share_id == "share123"
    assert ref.password == "abcd"


def test_share_detail_returns_root_and_stoken(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    详情接口应返回标题、总大小、根目录条目与 stoken。
    """
    root_payload = {
        "status": 200,
        "code": 0,
        "data": {
            "total_size": 300,
            "list": [
                _item("f1", "影片.mp4", 200, token="tok-1"),
                _item("d1", "子目录", 0, is_dir=True, token="tok-d"),
            ],
        },
    }
    calls = _install(
        monkeypatch,
        [
            ("sharepage/token", TOKEN_PAYLOAD),
            ("transfer_share/detail", root_payload),
        ],
    )

    detail = adapter.share_detail(REF, ACCOUNT)

    assert detail.title == "测试分享"
    assert detail.size == 300
    assert detail.stoken == "st-abc"
    assert [item.name for item in detail.files] == ["影片.mp4", "子目录"]
    assert detail.files[0].token == "tok-1"
    assert detail.files[1].is_dir is True
    token_call = next(call for call in calls if "sharepage/token" in call["url"])
    assert token_call["kwargs"]["json"]["pwd_id"] == "share123"
    assert token_call["kwargs"]["json"]["passcode"] == "abcd"


def test_list_files_paginates_until_empty(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    列目录应翻页到空页为止，并把父目录标识传给接口。
    """
    pages = {
        1: {
            "status": 200,
            "code": 0,
            "data": {
                "list": [
                    _item("f1", "第一集.mkv", 10, token="tok-1"),
                    _item("f2", "第二集.mkv", 20, token="tok-2"),
                ]
            },
        },
    }
    calls = _install(
        monkeypatch,
        [
            ("sharepage/token", TOKEN_PAYLOAD),
            ("transfer_share/detail", _detail_pages(pages)),
        ],
    )

    files = adapter.list_files(REF, "d1", ACCOUNT)

    assert [item.file_id for item in files] == ["f1", "f2"]
    detail_calls = [call for call in calls if "transfer_share/detail" in call["url"]]
    assert [call["kwargs"]["params"]["_page"] for call in detail_calls] == [1, 2]
    assert all(call["kwargs"]["params"]["pdir_fid"] == "d1" for call in detail_calls)


def test_resolve_regular_file_returns_direct_link(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    普通文件应走下载接口，返回带 Cookie 的直链且忽略转存开关。
    """
    download_payload = {
        "status": 200,
        "code": 0,
        "data": [{"download_url": "https://cdn.uc.cn/a.pdf", "size": 99, "file_name": "a.pdf"}],
    }
    _install(
        monkeypatch,
        [
            ("sharepage/token", TOKEN_PAYLOAD),
            ("file/download", download_payload),
        ],
    )

    link = adapter.resolve(
        REF,
        ShareFile(file_id="f1", name="文档.pdf", size=99, token="tok-1"),
        ACCOUNT,
        allow_transfer=False,
    )

    assert link.url == "https://cdn.uc.cn/a.pdf"
    assert link.size == 99
    assert link.is_hls is False
    assert link.account_id == 7
    assert link.cleanup_handle is None
    assert "__pus=old-pus" in link.headers["Cookie"]
    assert link.headers["Referer"] == "https://drive.uc.cn/"


def test_resolve_video_prefers_direct_preview(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    视频应走预览接口取原画直链，并按 YunX 语义标记为普通直链（非 HLS）。
    """
    preview_payload = {
        "status": 200,
        "code": 0,
        "data": {"play_info": {"url": "https://cdn.uc.cn/v/index.mp4", "size": 2048}},
    }
    calls = _install(
        monkeypatch,
        [
            ("sharepage/token", TOKEN_PAYLOAD),
            ("sharepage/video_preview", preview_payload),
        ],
    )

    link = adapter.resolve(REF, ShareFile(file_id="f9", name="影片.mp4", size=1024, token="tok-9"), ACCOUNT)

    assert link.url == "https://cdn.uc.cn/v/index.mp4"
    assert link.is_hls is False
    assert link.size == 2048
    assert not any("file/download" in call["url"] for call in calls)


def test_resolve_video_falls_back_to_download(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    预览不可用时视频回退到普通直链。
    """
    download_payload = {
        "status": 200,
        "code": 0,
        "data": [{"download_url": "https://cdn.uc.cn/v.mp4", "size": 1024}],
    }
    _install(
        monkeypatch,
        [
            ("sharepage/token", TOKEN_PAYLOAD),
            ("sharepage/video_preview", {"status": 400, "code": 41020}),
            ("file/download", download_payload),
        ],
    )

    link = adapter.resolve(REF, ShareFile(file_id="f9", name="影片.mp4", size=1024, token="tok-9"), ACCOUNT)

    assert link.url == "https://cdn.uc.cn/v.mp4"
    assert link.is_hls is False


def test_resolve_rejects_directory(adapter: UCAdapter) -> None:
    """
    目录条目不应被当作可下载文件。
    """
    with pytest.raises(NetdiskError) as error:
        adapter.resolve(REF, ShareFile(file_id="d1", name="子目录", is_dir=True), ACCOUNT)
    assert error.value.code is NetdiskErrorCode.UNSUPPORTED


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, NetdiskErrorCode.AUTH_EXPIRED),
        (429, NetdiskErrorCode.RATE_LIMITED),
    ],
)
def test_http_status_maps_to_error_code(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected: NetdiskErrorCode,
) -> None:
    """
    401/429 应分别归类为登录失效与限流。
    """
    response = _FakeResponse(status_code)

    def fake(self: Any, url: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001
        return self._json(response)

    monkeypatch.setattr(base_module.NetdiskHttp, "post_json", fake)

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(REF, ACCOUNT)
    assert error.value.code is expected
    assert "old-pus" not in str(error.value)


def test_html_response_maps_to_link_expired(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    HTML 登录页或防盗链页应归类为直链过期。
    """
    response = _FakeResponse(200, payload={}, content_type="text/html; charset=utf-8")

    def fake(self: Any, url: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001
        return self._json(response)

    monkeypatch.setattr(base_module.NetdiskHttp, "post_json", fake)

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(REF, ACCOUNT)
    assert error.value.code is NetdiskErrorCode.LINK_EXPIRED


def test_business_code_maps_to_share_invalid(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    提取码错误等业务码应归类为分享失效。
    """
    _install(
        monkeypatch,
        [("sharepage/token", {"status": 400, "code": 14001, "message": "passcode error"})],
    )

    with pytest.raises(NetdiskError) as error:
        adapter.share_detail(REF, ACCOUNT)
    assert error.value.code is NetdiskErrorCode.SHARE_INVALID
    assert "stoken" not in str(error.value)


def test_refresh_account_merges_puus_and_pus(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    刷新应剥掉旧 `__puus` 请求配置接口，并合并服务端新下发字段。
    """
    seen: dict[str, Any] = {}

    def handler(http: Any, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        seen["cookie"] = kwargs["account"].credential["cookie"]
        http.last_set_cookies = [
            "__puus=new-puus; Path=/; Domain=.uc.cn",
            "__pus=new-pus; Path=/; Domain=.uc.cn",
            "other=ignored; Path=/",
        ]
        return {"status": 200, "code": 0, "data": {}}

    _install(monkeypatch, [("/config", handler)])

    refreshed = adapter.refresh_account(ACCOUNT)

    assert refreshed is not None
    assert "__puus" not in seen["cookie"]
    assert refreshed["cookie"] == "__pus=new-pus; __puus=new-puus; uid=42"


def test_refresh_account_without_new_cookie_returns_none(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    服务端未下发新 Cookie 时刷新返回 None。
    """
    _install(monkeypatch, [("/config", {"status": 200, "code": 0, "data": {}})])

    assert adapter.refresh_account(ACCOUNT) is None


def test_validate_credential_rejects_missing_keys(adapter: UCAdapter) -> None:
    """
    缺少 `__pus`/`__puus` 的凭据不应发起请求。
    """
    probe = adapter.validate_credential({"cookie": "__pus=only"})
    assert probe.status == "expired"
    assert "__puus" in probe.message


def test_validate_credential_confirms_login(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    账号信息接口成功时凭据有效。
    """
    calls = _install(
        monkeypatch,
        [(UC_ACCOUNT_INFO_URL, {"success": True, "data": {"nickname": "小U"}})],
    )

    probe = adapter.validate_credential({"cookie": "__pus=1; __puus=2"})

    assert probe.status == "active"
    assert "小U" in probe.message
    assert calls[0]["url"] == UC_ACCOUNT_INFO_URL


def test_probe_account_marks_expired_on_rejection(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    账号信息接口明确失败时应标记登录失效。
    """
    _install(monkeypatch, [(UC_ACCOUNT_INFO_URL, {"success": False, "data": {}})])

    probe = adapter.probe_account(ACCOUNT)

    assert probe.status == "expired"


def test_probe_account_maps_auth_error(
    adapter: UCAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    账号接口抛登录失效时探测结果应为 expired 而非异常。
    """
    def handler(http: Any, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "网盘登录态已失效", platform=NetdiskPlatform.UC)

    _install(monkeypatch, [(UC_ACCOUNT_INFO_URL, handler)])

    probe = adapter.probe_account(ACCOUNT)

    assert probe.status == "expired"
    assert "old-pus" not in probe.message


def test_cookie_helpers_keep_unrelated_fields() -> None:
    """
    Cookie 工具只处理 `__puus`/`__pus`，其余字段原样保留。
    """
    assert strip_puus("__pus=1; __puus=2; uid=3") == "__pus=1; uid=3"
    merged = merge_set_cookies("__pus=1; __puus=2; uid=3", ["__puus=9; Path=/"])
    assert merged == "__pus=1; __puus=9; uid=3"
