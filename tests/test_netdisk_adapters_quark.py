"""
夸克网盘适配器的协议解析与取链流程测试。

全部 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，Cookie 使用假值。
"""

import pytest

from app.adapters.external.netdisk import quark as quark_module
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

_COOKIE = "__pus=fake-pus; __puus=fake-puus"


def _account() -> NetdiskAccountRecord:
    """
    构造使用假 Cookie 的夸克账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=7,
        platform=NetdiskPlatform.QUARK,
        name="夸克测试账号",
        credential={"cookie": _COOKIE},
        is_primary=True,
    )


def _ref(share_token: str = "st-1") -> ShareRef:
    """
    构造已持有 stoken 的分享引用，避免测试重复打令牌接口。

    :param share_token: 分享令牌
    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.QUARK,
        share_id="abc123",
        password="x1y2",
        url="https://pan.quark.cn/s/abc123",
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
        token="ft1",
    )


def _patch_http(monkeypatch, handler) -> None:
    """
    用统一路由函数替换基类 HTTP 方法，并禁止文本请求。

    :param monkeypatch: pytest 打桩夹具
    :param handler: 接收 (方法, http 实例, 地址, 账号, 参数, 请求体) 并返回 JSON 的函数
    """

    def get_json(self, url, *, account=None, params=None, headers=None):
        """
        转发 GET 请求到测试路由。
        """
        return handler("GET", self, url, account, params, None)

    def post_json(self, url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        转发 POST 请求到测试路由。
        """
        return handler("POST", self, url, account, params, json)

    def get_text(self, url, *, account=None, params=None, headers=None):
        """
        夸克适配器不应使用文本请求。
        """
        raise AssertionError("夸克适配器不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)


def test_adapter_declares_quark_capabilities() -> None:
    """
    适配器声明夸克平台能力与固定请求头。
    """
    adapter = quark_module.QuarkAdapter()

    assert adapter.platform is NetdiskPlatform.QUARK
    assert adapter.link_policy is LinkPolicy.DIRECT_WITH_FALLBACK
    assert adapter.requires_account is True
    assert adapter.guest_supported is True
    assert adapter.max_concurrency == 512
    assert adapter.cookie_required_keys == ("__pus", "__puus")
    assert adapter.cookie_domains == (".quark.cn",)
    assert adapter.login_url == "https://pan.quark.cn/"
    assert adapter.referer == "https://pan.quark.cn/"
    assert "quark-cloud-drive" in adapter.user_agent


def test_parse_share_link_keeps_share_id_and_passcode() -> None:
    """
    分享文案解析出夸克分享码与提取码，其他平台链接被拒绝。
    """
    adapter = quark_module.QuarkAdapter()

    ref = adapter.parse_share("https://pan.quark.cn/s/abc123 提取码：x1y2", None)

    assert ref.platform is NetdiskPlatform.QUARK
    assert ref.share_id == "abc123"
    assert ref.password == "x1y2"

    with pytest.raises(NetdiskError) as error:
        adapter.parse_share("https://drive.uc.cn/s/xyz789", None)
    assert error.value.code is NetdiskErrorCode.UNSUPPORTED


def test_share_detail_returns_title_size_and_root_files(monkeypatch) -> None:
    """
    分享详情返回标题、根目录条目、令牌与文件总大小。
    """

    def handler(method, http, url, account, params, body):
        """
        令牌接口返回标题，详情接口第一页返回两个根条目。
        """
        assert account is None or account.id == 7
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1", "title": "示例剧集"}}
        if url == quark_module._DETAIL_URL:
            page = int((params or {}).get("_page", 1))
            items = (
                [
                    {"fid": "f1", "file_name": "01.mkv", "size": 100, "dir": False,
                     "share_fid_token": "ft1"},
                    {"fid": "f2", "file_name": "花絮", "size": 0, "dir": True,
                     "share_fid_token": "ft2"},
                ]
                if page == 1
                else []
            )
            return {"status": 200, "data": {"list": items}}
        raise AssertionError(url)

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    detail = adapter.share_detail(_ref(share_token=""), _account())

    assert detail.title == "示例剧集"
    assert detail.stoken == "st-1"
    assert detail.size == 100
    assert [item.file_id for item in detail.files] == ["f1", "f2"]
    assert detail.files[1].is_dir is True
    assert detail.files[0].token == "ft1"


def test_list_files_paginates_until_page_without_new_entries(monkeypatch) -> None:
    """
    列目录按 pdir_fid 翻页，重复页视为结束且不产生重复条目。
    """
    pages = {
        1: [
            {"fid": "f1", "file_name": "01.mkv", "size": 10, "dir": False},
            {"fid": "f2", "file_name": "02.mkv", "size": 20, "dir": False},
        ],
        2: [{"fid": "f3", "file_name": "03.mkv", "size": 30, "dir": False}],
        3: [{"fid": "f3", "file_name": "03.mkv", "size": 30, "dir": False}],
    }
    seen_pages = []

    def handler(method, http, url, account, params, body):
        """
        记录翻页参数并返回对应页数据。
        """
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1", "title": "示例剧集"}}
        assert url == quark_module._DETAIL_URL
        # ver=2 是 share_fid_token 可用于取链的前提，不能被精简掉。
        assert (params or {}).get("ver") == "2"
        page = int((params or {}).get("_page", 1))
        seen_pages.append(page)
        return {"status": 200, "data": {"list": pages.get(page, [])}}

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    files = adapter.list_files(_ref(), "dir-9", _account())

    assert [item.file_id for item in files] == ["f1", "f2", "f3"]
    assert seen_pages == [1, 2, 3]
    assert files[0].parent_id == "dir-9"


def test_resolve_returns_direct_link_without_transfer(monkeypatch) -> None:
    """
    免转存取链成功时不创建临时目录，直链携带 Cookie、UA 与固定 Referer。
    """
    calls = []

    def handler(method, http, url, account, params, body):
        """
        下载接口直接返回直链。
        """
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1", "title": "示例剧集"}}
        calls.append((url, body))
        assert url == quark_module._DOWNLOAD_URL
        return {
            "status": 200,
            "data": [{"download_url": "https://dl.quark.cn/a.mkv", "file_name": "01.mkv", "size": 1024}],
        }

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    link = adapter.resolve(_ref(), _file(), _account())

    assert link.url == "https://dl.quark.cn/a.mkv"
    assert link.size == 1024
    assert link.cleanup_handle is None
    assert link.account_id == 7
    assert link.is_hls is False
    assert link.headers["Cookie"] == _COOKIE
    assert link.headers["User-Agent"] == quark_module.QUARK_USER_AGENT
    assert link.headers["Referer"] == "https://pan.quark.cn/"
    assert calls[0][1]["pwd_id"] == "abc123"
    assert calls[0][1]["fids_token"] == ["ft1"]


class _ErrorResponse:
    """带状态码与 JSON 体的最小响应替身。"""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self) -> dict:
        """返回预设 JSON。"""
        return self._payload


def test_404_cancelled_share_is_reported_as_share_invalid() -> None:
    """
    夸克分享被取消/过期时返回 404 + 41012/41011，应提示分享失效而不是裸 HTTP 404。
    """
    http = quark_module._QuarkHttp()

    with pytest.raises(NetdiskError) as error:
        http._json(_ErrorResponse(404, {"status": 404, "code": 41012, "message": "好友已取消了分享"}))

    assert error.value.code is NetdiskErrorCode.SHARE_INVALID
    assert "分享已失效" in error.value.message


def test_403_token_error_is_not_reported_as_login_expired() -> None:
    """
    夸克用 HTTP 403 + 41020 表示文件 token 失效，不能误报成账号登录态失效。
    """
    http = quark_module._QuarkHttp()

    with pytest.raises(NetdiskError) as error:
        http._json(_ErrorResponse(403, {"status": 403, "code": 41020, "message": "转存文件token校验异常"}))

    assert error.value.code is NetdiskErrorCode.LINK_EXPIRED
    assert "令牌" in error.value.message
    assert "登录态" not in error.value.message


def test_stale_share_token_is_refreshed(monkeypatch) -> None:
    """
    候选透传的 stoken 可能已过期；重试时必须换新，不能继续用旧 token 打详情接口。
    """
    token_calls = []

    def handler(method, http, url, account, params, body):
        """记录令牌请求并返回新 stoken。"""
        if url == quark_module._TOKEN_URL:
            token_calls.append(body)
            return {"status": 200, "data": {"stoken": "fresh", "title": "示例剧集"}}
        assert url == quark_module._DETAIL_URL
        assert (params or {}).get("stoken") == "fresh"
        return {"status": 200, "data": {"list": []}}

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    files = adapter.list_files(_ref(share_token="stale"), "0", _account())

    assert files == []
    assert len(token_calls) == 1


def test_resolve_falls_back_to_transfer_with_cleanup_handle(monkeypatch) -> None:
    """
    免转存失败时转存到唯一临时子目录再取链，并返回清理句柄。
    """
    calls = []
    folder_names = []

    def handler(method, http, url, account, params, body):
        """
        首次分享取链失败，随后完成建目录、转存、轮询与文件取链。
        """
        calls.append((method, url, body))
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1", "title": "示例剧集"}}
        if url == quark_module._DOWNLOAD_URL:
            if body and "pwd_id" in body:
                return {"status": 400, "code": 23018, "message": "超出游客大小限制"}
            return {
                "status": 200,
                "data": [{"download_url": "https://dl.quark.cn/saved.mkv", "file_name": "01.mkv", "size": 2048}],
            }
        if url == quark_module._FILE_URL:
            folder_names.append(body["file_name"])
            return {"status": 200, "data": {"fid": "dir-1"}}
        if url == quark_module._SAVE_URL:
            assert body["to_pdir_fid"] == "dir-1"
            assert body["fid_token_list"] == ["ft1"]
            return {"status": 200, "data": {"task_id": "task-1"}}
        if url == quark_module._TASK_URL:
            return {
                "status": 200,
                "data": {"status": 2, "finished_at": 1, "save_as": {"save_as_top_fids": ["new-1"]}},
            }
        raise AssertionError(url)

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    link = adapter.resolve(_ref(), _file(), _account())

    handle = link.cleanup_handle
    assert handle is not None
    assert handle.platform is NetdiskPlatform.QUARK
    assert handle.account_id == 7
    assert handle.directory_id == "dir-1"
    assert handle.file_ids == ("new-1",)
    assert link.url == "https://dl.quark.cn/saved.mkv"
    assert link.size == 2048
    assert folder_names and folder_names[0].startswith("mp_tr_")
    assert all(url != quark_module._DELETE_URL for _, url, _ in calls)


def test_cleanup_deletes_transfer_directory_and_swallows_failure(monkeypatch) -> None:
    """
    清理删除临时子目录；删除失败只记录不抛出。
    """
    bodies = []
    should_fail = {"value": False}

    def handler(method, http, url, account, params, body):
        """
        记录删除请求，可切换为失败响应。
        """
        assert url == quark_module._DELETE_URL
        bodies.append(body)
        if should_fail["value"]:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口无响应", platform=NetdiskPlatform.QUARK)
        return {"status": 200, "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()
    handle = TransferHandle(
        platform=NetdiskPlatform.QUARK,
        account_id=7,
        directory_id="dir-1",
        file_ids=("new-1",),
    )

    adapter.cleanup(handle, _account())

    assert bodies[0] == {"action_type": 2, "filelist": ["dir-1"], "exclude_fids": []}

    should_fail["value"] = True
    adapter.cleanup(handle, _account())
    assert len(bodies) == 2


def test_http_412_or_html_maps_to_link_expired(monkeypatch) -> None:
    """
    412 与非 JSON 页面统一归类为直链过期，且错误消息不含凭据。
    """
    failure = {"error": NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口返回 HTTP 412", platform=NetdiskPlatform.QUARK)}

    def handler(method, http, url, account, params, body):
        """
        抛出可切换的取链失败异常。
        """
        raise failure["error"]

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), _account(), allow_transfer=False)

    assert error.value.code is NetdiskErrorCode.LINK_EXPIRED
    assert "fake-pus" not in str(error.value)

    failure["error"] = NetdiskError(
        NetdiskErrorCode.LINK_EXPIRED,
        "网盘返回了登录或防盗链页面",
        platform=NetdiskPlatform.QUARK,
    )
    with pytest.raises(NetdiskError) as html_error:
        adapter.resolve(_ref(), _file(), _account(), allow_transfer=False)

    assert html_error.value.code is NetdiskErrorCode.LINK_EXPIRED


def test_login_required_maps_to_auth_expired(monkeypatch) -> None:
    """
    服务端 31001 归类为登录态失效，错误消息不泄漏 Cookie。
    """

    def handler(method, http, url, account, params, body):
        """
        返回需要登录的业务错误。
        """
        return {"status": 400, "code": 31001, "message": "请先登录"}

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), _account(), allow_transfer=False)

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert error.value.platform is NetdiskPlatform.QUARK
    assert "fake-pus" not in str(error.value)


def test_refresh_account_strips_puus_and_merges_set_cookie(monkeypatch) -> None:
    """
    刷新会话先剥离 __puus，再合并响应 Set-Cookie 下发的新令牌。
    """
    sent_cookies = []
    set_cookie = {"value": "__puus=new-puus; Path=/; Domain=.quark.cn"}

    def handler(method, http, url, account, params, body):
        """
        记录请求 Cookie 并模拟服务端下发新令牌。
        """
        assert url == quark_module._CONFIG_URL
        sent_cookies.append(account.credential["cookie"])
        http.last_set_cookie = set_cookie["value"]
        return {"status": 200, "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    credential = adapter.refresh_account(_account())

    assert sent_cookies == ["__pus=fake-pus"]
    assert credential == {"cookie": "__pus=fake-pus; __puus=new-puus"}

    set_cookie["value"] = ""
    assert adapter.refresh_account(_account()) is None


def test_probe_account_calls_account_info(monkeypatch) -> None:
    """
    账号探测走夸克账号信息接口；缺失必填 Cookie 时不发请求。
    """
    responses = {"payload": {"success": True, "code": "OK", "data": {"nickname": "小明"}}}
    calls = []

    def handler(method, http, url, account, params, body):
        """
        返回可切换的账号信息响应。
        """
        calls.append(url)
        assert url == quark_module._ACCOUNT_INFO_URL
        return responses["payload"]

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    probe = adapter.probe_account(_account())

    assert probe.status == "active"
    assert "小明" in probe.message
    assert calls == [quark_module._ACCOUNT_INFO_URL]

    responses["payload"] = {"success": False, "code": "OK", "data": {}}
    assert adapter.probe_account(_account()).status == "expired"

    calls.clear()
    missing = adapter.validate_credential({"cookie": "__pus=fake-pus"})
    assert missing.status == "expired"
    assert calls == []


def test_validate_credential_accepts_complete_cookie(monkeypatch) -> None:
    """
    字段完整的凭据经账号信息接口确认后判定为有效。
    """

    def handler(method, http, url, account, params, body):
        """
        账号信息接口返回登录成功。
        """
        assert url == quark_module._ACCOUNT_INFO_URL
        return {"success": True, "code": "OK", "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    probe = adapter.validate_credential({"cookie": _COOKIE})

    assert probe.status == "active"


def test_resolve_protocol_accepts_transfer_first() -> None:
    """
    协议层声明 transfer_first 关键字，默认 False 保持免转存优先。
    """
    import inspect

    from app.application.netdisk.contract import NetdiskAdapter

    signature = inspect.signature(NetdiskAdapter.resolve)
    parameter = signature.parameters.get("transfer_first")

    assert parameter is not None
    assert parameter.default is False


def test_resolve_transfer_first_saves_before_direct(monkeypatch) -> None:
    """
    transfer_first=True 时先转存再取链，即使免转存本可用。
    """
    urls = []

    def handler(method, http, url, account, params, body):
        """
        记录请求顺序，转存与免转存都返回可用直链。
        """
        urls.append(url)
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1", "title": "示例剧集"}}
        if url == quark_module._FILE_URL:
            return {"status": 200, "data": {"fid": "dir-1"}}
        if url == quark_module._SAVE_URL:
            return {"status": 200, "data": {"task_id": "task-1"}}
        if url == quark_module._TASK_URL:
            return {
                "status": 200,
                "data": {"status": 2, "finished_at": 1, "save_as": {"save_as_top_fids": ["new-1"]}},
            }
        if url == quark_module._DOWNLOAD_URL:
            if body and "pwd_id" in body:
                return {
                    "status": 200,
                    "data": [{"download_url": "https://dl.quark.cn/saved.mkv", "file_name": "01.mkv", "size": 2048}],
                }
            return {
                "status": 200,
                "data": [{"download_url": "https://dl.quark.cn/direct.mkv", "file_name": "01.mkv", "size": 2048}],
            }
        raise AssertionError(url)

    _patch_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    link = adapter.resolve(_ref(), _file(), _account(), transfer_first=True)

    assert quark_module._SAVE_URL in urls
    assert urls.index(quark_module._SAVE_URL) < urls.index(quark_module._DOWNLOAD_URL)
    assert link.cleanup_handle is not None
    assert link.cleanup_handle.file_ids == ("new-1",)
