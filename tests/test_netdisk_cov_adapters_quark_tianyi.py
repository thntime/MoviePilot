"""
夸克与天翼网盘适配器的覆盖率补充测试。

只覆盖既有测试未触达的分支：HTTP 都在 `NetdiskHttp` 层打桩，不访问网络。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.external.netdisk import quark as quark_module
from app.adapters.external.netdisk import tianyi as tianyi_module
from app.adapters.external.netdisk.base import NetdiskHttp
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import (
    DriveFile,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareFile,
    ShareRef,
    TransferHandle,
)

_QUARK_COOKIE = "__pus=fake-pus; __puus=fake-puus"


def _quark_account() -> NetdiskAccountRecord:
    """
    构造夸克测试账号。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=7,
        platform=NetdiskPlatform.QUARK,
        name="夸克测试账号",
        credential={"cookie": _QUARK_COOKIE},
    )


def _quark_ref(share_token: str = "st-1") -> ShareRef:
    """
    构造夸克分享引用。

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


def _quark_file() -> ShareFile:
    """
    构造夸克分享文件。

    :return: 文件条目
    """
    return ShareFile(file_id="f1", name="01.mkv", size=1024, is_dir=False, parent_id="0", token="ft1")


def _patch_quark_http(monkeypatch, handler) -> None:
    """
    用统一路由替换 NetdiskHttp 的 JSON 方法。

    :param monkeypatch: pytest 打桩夹具
    :param handler: 接收 (方法, http, 地址, 账号, 参数, 请求体) 的路由函数
    """

    def get_json(self, url, *, account=None, params=None, headers=None):
        """
        转发 GET 请求。

        :param self: HTTP 实例
        :param url: 地址
        :param account: 账号
        :param params: 查询参数
        :param headers: 请求头
        :return: 路由结果
        """
        del headers
        return handler("GET", self, url, account, params, None)

    def post_json(self, url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        转发 POST 请求。

        :param self: HTTP 实例
        :param url: 地址
        :param account: 账号
        :param json: JSON 请求体
        :param data: 表单请求体
        :param params: 查询参数
        :param headers: 请求头
        :return: 路由结果
        """
        del params, headers
        return handler("POST", self, url, account, None, json if json is not None else data)

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)


# ---- 夸克：Cookie 与错误分类工具 ----


def test_merge_cookie_skips_malformed_and_irrelevant_parts() -> None:
    """
    Set-Cookie 中没有等号的片段被跳过；没有新令牌时原样返回。
    """
    assert quark_module._merge_cookie("a=1", "broken; Path=/") == "a=1"  # pylint: disable=protected-access
    assert quark_module._merge_cookie("a=1", "other=2; Path=/") == "a=1"  # pylint: disable=protected-access
    assert quark_module._merge_cookie("a=1; __pus=old", "__pus=new; Path=/") == "a=1; __pus=new"  # pylint: disable=protected-access


def test_cookie_token_skips_malformed_and_missing_names() -> None:
    """
    解析 Set-Cookie 时跳过无等号片段，未命中名称时返回空串。
    """
    assert quark_module._cookie_token("__pugs=v; broken", "__pugs") == "v"  # pylint: disable=protected-access
    assert quark_module._cookie_token("__pus=v", "__pugs") == ""  # pylint: disable=protected-access
    assert quark_module._cookie_token("", "__pugs") == ""  # pylint: disable=protected-access


def test_set_cookie_header_swallows_mapping_errors() -> None:
    """
    响应头不可读时返回空串。
    """
    class _BadHeaders:
        """
        get 抛异常的请求头映射。
        """

        def get(self, name: str) -> str:
            """
            模拟读取失败。

            :param name: 头名称
            :return: 不会返回
            """
            raise RuntimeError(name)

    assert quark_module._set_cookie_header(SimpleNamespace(headers=_BadHeaders())) == ""  # pylint: disable=protected-access
    assert quark_module._set_cookie_header(SimpleNamespace(headers=None)) == ""  # pylint: disable=protected-access
    assert quark_module._set_cookie_header(SimpleNamespace(headers={"Set-Cookie": "__pus=x"})) == "__pus=x"  # pylint: disable=protected-access


@pytest.mark.parametrize(
    ("code", "status", "message", "expected"),
    [
        (0, 429, "", NetdiskErrorCode.RATE_LIMITED),
        (0, 412, "", NetdiskErrorCode.LINK_EXPIRED),
        (0, 400, "提取码错误", NetdiskErrorCode.SHARE_INVALID),
        (0, 400, "分享已失效", NetdiskErrorCode.SHARE_INVALID),
        (0, 400, "空间不足", NetdiskErrorCode.SPACE_INSUFFICIENT),
        (0, 400, "未知错误", NetdiskErrorCode.UNKNOWN),
    ],
)
def test_classify_maps_status_and_message(code: int, status: int, message: str, expected: NetdiskErrorCode) -> None:
    """
    业务码、HTTP 状态与提示语都收敛到统一错误分类。
    """
    assert quark_module._classify(code, status, message) is expected  # pylint: disable=protected-access


def test_normalize_error_maps_antileech_message() -> None:
    """
    未分类异常带 412/HTML 提示时归类为直链过期。
    """
    error = NetdiskError(NetdiskErrorCode.UNKNOWN, "网盘接口返回 HTTP 412", platform=NetdiskPlatform.QUARK)

    normalized = quark_module._normalize_error(error)  # pylint: disable=protected-access

    assert normalized.code is NetdiskErrorCode.LINK_EXPIRED

    untouched = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "登录失效", platform=NetdiskPlatform.QUARK)
    assert quark_module._normalize_error(untouched) is untouched  # pylint: disable=protected-access


def test_quark_http_ignores_non_json_error_bodies() -> None:
    """
    404/401 响应体不是 JSON 时交给基类处理，不误报分享或令牌失效。
    """
    class _NotJsonResponse:
        """
        只实现状态码与 headers 的响应替身。
        """

        def __init__(self, status_code: int) -> None:
            """
            保存状态码。

            :param status_code: HTTP 状态码
            """
            self.status_code = status_code
            self.headers: dict[str, str] = {}
            self.text = ""

        def json(self) -> dict[str, Any]:
            """
            模拟非 JSON 响应。

            :return: 不会返回
            """
            raise ValueError("not json")

    http = quark_module._QuarkHttp()  # pylint: disable=protected-access
    with pytest.raises(NetdiskError):
        http._json(_NotJsonResponse(404))  # pylint: disable=protected-access
    with pytest.raises(NetdiskError):
        http._json(_NotJsonResponse(401))  # pylint: disable=protected-access


def test_quark_get_json_normalizes_unknown_errors(monkeypatch) -> None:
    """
    请求层的未分类异常经 `_normalize_error` 收敛。
    """

    def handler(method, http, url, account, params, body):
        """
        抛出防盗链提示的未分类异常。

        :return: 不会返回
        """
        del method, http, url, account, params, body
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "返回了防盗链页面", platform=NetdiskPlatform.QUARK)

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.list_files(_quark_ref(), "0", _quark_account())

    assert error.value.code is NetdiskErrorCode.LINK_EXPIRED


# ---- 夸克：分享令牌与目录 ----


def test_stoken_cache_avoids_repeated_token_requests(monkeypatch) -> None:
    """
    同一分享与账号在 TTL 内复用缓存令牌。
    """
    token_calls: list[str] = []

    def handler(method, http, url, account, params, body):
        """
        记录令牌请求。

        :return: 令牌响应
        """
        del method, http, account, params, body
        token_calls.append(url)
        return {"status": 200, "data": {"stoken": "st-1", "title": "标题"}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    first = adapter._stoken(_quark_ref(), _quark_account())  # pylint: disable=protected-access
    second = adapter._stoken(_quark_ref(), _quark_account())  # pylint: disable=protected-access

    assert first == second == ("st-1", "标题")
    assert token_calls == [quark_module._TOKEN_URL]


def test_stoken_falls_back_to_transferred_token(monkeypatch) -> None:
    """
    令牌接口失败或缺少 stoken 时退回透传令牌；都没有则报分享失效。
    """
    state = {"fail": True}

    def handler(method, http, url, account, params, body):
        """
        按开关返回失败或缺少 stoken 的响应。

        :return: 响应
        """
        del method, http, url, account, params, body
        if state["fail"]:
            return {"status": 400, "code": 400, "message": "失败"}
        return {"status": 200, "data": {}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    assert adapter._stoken(_quark_ref("fallback"), _quark_account()) == ("fallback", "")  # pylint: disable=protected-access

    state["fail"] = False
    assert adapter._stoken(_quark_ref("fallback"), _quark_account()) == ("fallback", "")  # pylint: disable=protected-access

    with pytest.raises(NetdiskError) as error:
        adapter._stoken(_quark_ref(""), _quark_account())  # pylint: disable=protected-access

    assert error.value.code is NetdiskErrorCode.SHARE_INVALID


def test_list_share_skips_unusable_entries(monkeypatch) -> None:
    """
    跳过非字典条目与缺少文件名的条目。
    """

    def handler(method, http, url, account, params, body):
        """
        第一页返回混合条目，第二页为空。

        :return: 目录响应
        """
        del method, http, account, body
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1"}}
        page = int((params or {}).get("_page", 1))
        items = ["bad", {"fid": "f2"}, {"fid": "f1", "file_name": "01.mkv", "size": 10}] if page == 1 else []
        return {"status": 200, "data": {"list": items}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    files = adapter.list_files(_quark_ref(), "0", _quark_account())

    assert [item.file_id for item in files] == ["f1"]


def test_first_link_rejects_missing_or_empty_payload() -> None:
    """
    下载接口未返回列表或链接为空时抛出未分类错误。
    """
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError) as missing:
        adapter._first_link({"data": {}}, "获取直链")  # pylint: disable=protected-access
    assert missing.value.code is NetdiskErrorCode.UNKNOWN

    with pytest.raises(NetdiskError) as empty:
        adapter._first_link({"data": [{"file_name": "x"}]}, "获取直链")  # pylint: disable=protected-access
    assert "下载链接为空" in empty.value.message


# ---- 夸克：转存与清理 ----


def test_create_folder_and_save_share_require_identifiers(monkeypatch) -> None:
    """
    创建目录与转存响应缺少标识时报错。
    """

    def handler(method, http, url, account, params, body):
        """
        两个接口都返回空 data。

        :return: 空响应
        """
        del method, http, url, account, params, body
        return {"status": 200, "data": {}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError) as folder:
        adapter._create_folder("mp_tr_1", _quark_account())  # pylint: disable=protected-access
    assert "缺少 fid" in folder.value.message

    with pytest.raises(NetdiskError) as save:
        adapter._save_share("abc123", "st-1", _quark_file(), "dir-1", _quark_account())  # pylint: disable=protected-access
    assert "缺少 task_id" in save.value.message


def test_poll_task_retries_then_times_out(monkeypatch) -> None:
    """
    任务查询异常时继续轮询；始终未完成时超时。
    """
    calls = {"count": 0}

    def handler(method, http, url, account, params, body):
        """
        前两次抛异常，之后返回未完成。

        :return: 任务响应
        """
        del method, http, url, account, params, body
        calls["count"] += 1
        if calls["count"] <= 2:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网络抖动", platform=NetdiskPlatform.QUARK)
        return {"status": 200, "data": {"status": 1}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)

    with pytest.raises(NetdiskError) as error:
        adapter._poll_task("task-1", _quark_account())  # pylint: disable=protected-access

    assert "超时" in error.value.message
    assert calls["count"] == quark_module._TASK_POLL_TIMES


def test_poll_task_requires_saved_file_id(monkeypatch) -> None:
    """
    任务完成但未返回新文件标识时报错。
    """

    def handler(method, http, url, account, params, body):
        """
        返回已完成但没有 save_as 的任务。

        :return: 任务响应
        """
        del method, http, url, account, params, body
        return {"status": 200, "data": {"finished_at": 1}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter._poll_task("task-1", _quark_account())  # pylint: disable=protected-access

    assert "未返回新文件标识" in error.value.message


def test_delete_files_ignores_empty_list(monkeypatch) -> None:
    """
    空列表不发起删除请求。
    """
    calls: list[str] = []

    def handler(method, http, url, account, params, body):
        """
        记录请求。

        :return: 成功响应
        """
        del method, http, account, params, body
        calls.append(url)
        return {"status": 200, "data": {}}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    adapter._delete_files([], _quark_account())  # pylint: disable=protected-access

    assert calls == []


def test_transfer_link_cleans_temp_dir_on_failure(monkeypatch) -> None:
    """
    转存中途失败时删除刚创建的临时目录并继续抛出。
    """
    deleted: list[list[str]] = []

    def handler(method, http, url, account, params, body):
        """
        建目录成功后转存失败。

        :return: 响应
        """
        del method, http, account, params, body
        if url == quark_module._FILE_URL:
            return {"status": 200, "data": {"fid": "dir-1"}}
        return {"status": 400, "code": 400, "message": "转存失败"}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()
    monkeypatch.setattr(
        adapter,
        "_delete_files",
        lambda fids, account: deleted.append(list(fids)),
    )

    with pytest.raises(NetdiskError):
        adapter._transfer_link("abc123", "st-1", _quark_file(), _quark_account())  # pylint: disable=protected-access

    assert deleted == [["dir-1"]]


def test_resolve_transfer_first_failure_falls_back_to_direct(monkeypatch) -> None:
    """
    transfer_first 转存失败且允许回退时，继续走免转存取链。
    """
    def handler(method, http, url, account, params, body):
        """
        建目录失败，免转存取链成功。

        :return: 响应
        """
        del method, http, account, params, body
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1"}}
        if url == quark_module._FILE_URL:
            return {"status": 400, "code": 400, "message": "目录已存在"}
        return {"status": 200, "data": [{"download_url": "https://dl.quark.cn/a.mkv"}]}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    link = adapter.resolve(_quark_ref(), _quark_file(), _quark_account(), transfer_first=True)

    assert link.url == "https://dl.quark.cn/a.mkv"
    assert link.cleanup_handle is None


def test_resolve_transfer_first_failure_raises_without_transfer(monkeypatch) -> None:
    """
    transfer_first 转存失败且禁止回退时直接抛出。
    """
    def handler(method, http, url, account, params, body):
        """
        建目录失败。

        :return: 响应
        """
        del method, http, account, params, body
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1"}}
        return {"status": 400, "code": 400, "message": "目录已存在"}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    with pytest.raises(NetdiskError):
        adapter.resolve(_quark_ref(), _quark_file(), _quark_account(), allow_transfer=False, transfer_first=True)


def test_resolve_refreshes_stoken_and_retries(monkeypatch) -> None:
    """
    免转存取链遇到直链过期时换新 stoken、重列目录并重试一次。
    """
    share_link_calls = {"count": 0}

    def handler(method, http, url, account, params, body):
        """
        第一次取链返回 412，重列目录后返回新的文件令牌，第二次成功。

        :return: 响应
        """
        del method, http, account, body
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "fresh"}}
        if url == quark_module._DETAIL_URL:
            assert (params or {}).get("stoken") == "fresh"
            return {"status": 200, "data": {"list": [{"fid": "f1", "file_name": "01.mkv", "share_fid_token": "ft2"}]}}
        share_link_calls["count"] += 1
        if share_link_calls["count"] == 1:
            return {"status": 412, "code": 412, "message": "防盗链"}
        return {"status": 200, "data": [{"download_url": "https://dl.quark.cn/retry.mkv"}]}

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    link = adapter.resolve(_quark_ref(), _quark_file(), _quark_account())

    assert link.url == "https://dl.quark.cn/retry.mkv"
    assert share_link_calls["count"] == 2


def test_resolve_retry_failure_falls_back_to_transfer(monkeypatch) -> None:
    """
    换新令牌后重试仍失败时转存兜底。
    """
    def handler(method, http, url, account, params, body):
        """
        免转存取链始终失败，转存链路成功。

        :return: 响应
        """
        del method, http, account, params
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "st-1"}}
        if url == quark_module._DETAIL_URL:
            return {"status": 200, "data": {"list": []}}
        if url == quark_module._DOWNLOAD_URL:
            if body and "pwd_id" in body:
                return {"status": 400, "code": 31001, "message": "请先登录"}
            return {
                "status": 200,
                "data": [{"download_url": "https://dl.quark.cn/saved.mkv", "file_name": "01.mkv", "size": 1024}],
            }
        if url == quark_module._FILE_URL:
            return {"status": 200, "data": {"fid": "dir-1"}}
        if url == quark_module._SAVE_URL:
            return {"status": 200, "data": {"task_id": "task-1"}}
        if url == quark_module._TASK_URL:
            return {"status": 200, "data": {"status": 2, "save_as": {"save_as_top_fids": ["new-1"]}}}
        raise AssertionError(url)

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    link = adapter.resolve(_quark_ref(), _quark_file(), _quark_account())

    assert link.cleanup_handle is not None
    assert link.cleanup_handle.directory_id == "dir-1"


def test_refresh_share_file_falls_back_when_listing_fails(monkeypatch) -> None:
    """
    重新列目录失败或找不到目标时保留原文件条目。
    """
    def handler(method, http, url, account, params, body):
        """
        令牌成功、列目录失败。

        :return: 响应
        """
        del method, http, account, params, body
        if url == quark_module._TOKEN_URL:
            return {"status": 200, "data": {"stoken": "fresh"}}
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "列目录失败", platform=NetdiskPlatform.QUARK)

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()
    file = _quark_file()

    stoken, refreshed = adapter._refresh_share_file(_quark_ref(), file, _quark_account())  # pylint: disable=protected-access

    assert stoken == "fresh"
    assert refreshed is file


def test_cleanup_ignores_handle_without_directory(monkeypatch) -> None:
    """
    句柄没有目录标识时不发起删除。
    """
    calls: list[str] = []
    monkeypatch.setattr(quark_module.QuarkAdapter, "_safe_delete", lambda self, fids, account: calls.append("delete"))
    adapter = quark_module.QuarkAdapter()

    adapter.cleanup(TransferHandle(platform=NetdiskPlatform.QUARK, account_id=7), _quark_account())

    assert calls == []


# ---- 夸克：账号与个人盘 ----


def test_probe_maps_unknown_and_code_results(monkeypatch) -> None:
    """
    探测把非登录失效异常归为 unknown，并用 success/code 判定状态。
    """
    payloads: list[dict[str, Any]] = [
        {"success": True, "code": "OK", "data": {"nickname": "小明"}},
        {"success": True, "code": "OK", "data": {}},
        {"success": None, "code": "OK", "data": {}},
        {"success": None, "code": "OTHER", "data": {}},
    ]

    def handler(method, http, url, account, params, body):
        """
        依次返回预设响应，或抛非登录失效异常。

        :return: 响应
        """
        del method, http, url, account, params, body
        payload = payloads.pop(0)
        if payload == "raise":
            raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "限流", platform=NetdiskPlatform.QUARK)
        return payload

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    assert adapter.probe_account(_quark_account()).status == "active"
    assert adapter.probe_account(_quark_account()).status == "active"
    assert adapter.probe_account(_quark_account()).status == "active"
    assert adapter.probe_account(_quark_account()).status == "expired"


def test_probe_reports_unknown_for_transient_errors(monkeypatch) -> None:
    """
    非登录失效异常无法确认登录态。
    """
    def handler(method, http, url, account, params, body):
        """
        抛出限流异常。

        :return: 不会返回
        """
        del method, http, url, account, params, body
        raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "限流", platform=NetdiskPlatform.QUARK)

    _patch_quark_http(monkeypatch, handler)

    probe = quark_module.QuarkAdapter().probe_account(_quark_account())

    assert probe.status == "unknown"


def test_login_account_name_handles_failure_and_shapes(monkeypatch) -> None:
    """
    账号信息接口失败或 data 不是映射时返回空串，正常时返回昵称。
    """
    responses: list[Any] = [
        NetdiskError(NetdiskErrorCode.UNKNOWN, "失败", platform=NetdiskPlatform.QUARK),
        {"success": True, "data": "not-a-mapping"},
        {"success": True, "data": {"nickname": "小明"}},
    ]

    def handler(method, http, url, account, params, body):
        """
        依次返回异常或预设响应。

        :return: 响应
        """
        del method, http, url, account, params, body
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    _patch_quark_http(monkeypatch, handler)
    adapter = quark_module.QuarkAdapter()

    assert adapter.login_account_name({"cookie": _QUARK_COOKIE}) == ""
    assert adapter.login_account_name({"cookie": _QUARK_COOKIE}) == ""
    assert adapter.login_account_name({"cookie": _QUARK_COOKIE}) == "小明"


def test_quota_maps_capacity(monkeypatch) -> None:
    """
    配额接口读取 use_capacity 与 total_capacity，data 非法时按 0 处理。
    """
    payloads = [{"data": {"use_capacity": 10, "total_capacity": 100}}, {"data": None}]

    def fake_get(url, *, account=None, params=None, headers=None):
        """
        返回预设响应。

        :param url: 地址
        :param account: 账号
        :param params: 查询参数
        :param headers: 请求头
        :return: 响应
        """
        del url, account, params, headers
        return payloads.pop(0)

    adapter = quark_module.QuarkAdapter()
    monkeypatch.setattr(adapter.http, "get_json", fake_get)

    quota = adapter.quota(_quark_account())
    assert (quota.used, quota.total) == (10, 100)

    empty = adapter.quota(_quark_account())
    assert (empty.used, empty.total) == (0, 0)


def test_list_dir_parses_items_and_skips_invalid(monkeypatch) -> None:
    """
    个人盘目录跳过非映射条目，空父目录回退根目录。
    """
    seen: list[str] = []

    def fake_get(url, *, account=None, params=None, headers=None):
        """
        记录地址并返回混合条目。

        :param url: 地址
        :param account: 账号
        :param params: 查询参数
        :param headers: 请求头
        :return: 响应
        """
        del account, params, headers
        seen.append(url)
        return {"data": {"list": ["bad", {"fid": "f1", "file_name": "a.mkv", "size": 5, "dir": False, "pdir_fid": "0"}]}}

    adapter = quark_module.QuarkAdapter()
    monkeypatch.setattr(adapter.http, "get_json", fake_get)

    files = adapter.list_dir(_quark_account(), "")

    assert [item.file_id for item in files] == ["f1"]
    assert "pdir_fid=0" in seen[0]

    monkeypatch.setattr(adapter.http, "get_json", lambda *a, **k: {"data": {"list": None}})
    assert adapter.list_dir(_quark_account(), "dir-1") == []


def test_download_file_requires_url(monkeypatch) -> None:
    """
    个人盘取链缺少地址时报错，正常时返回直链。
    """
    responses = [
        {"data": []},
        {"data": [{"download_url": "https://dl.quark.cn/f.mkv", "size": 9}]},
    ]

    def fake_post(url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        返回预设响应。

        :param url: 地址
        :param account: 账号
        :param json: JSON 请求体
        :param data: 表单请求体
        :param params: 查询参数
        :param headers: 请求头
        :return: 响应
        """
        del url, account, json, data, params, headers
        return responses.pop(0)

    adapter = quark_module.QuarkAdapter()
    monkeypatch.setattr(adapter.http, "post_json", fake_post)
    file = DriveFile(file_id="f1", name="a.mkv", size=1)

    with pytest.raises(NetdiskError) as error:
        adapter.download_file(_quark_account(), file)
    assert "未返回下载直链" in error.value.message

    link = adapter.download_file(_quark_account(), file)
    assert link.url == "https://dl.quark.cn/f.mkv"
    assert link.size == 9


def test_drive_mutations_post_expected_bodies(monkeypatch) -> None:
    """
    移动、重命名与删除都通过个人盘接口提交。
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        记录请求。

        :param url: 地址
        :param account: 账号
        :param json: JSON 请求体
        :param data: 表单请求体
        :param params: 查询参数
        :param headers: 请求头
        :return: 成功响应
        """
        del account, data, params, headers
        calls.append((url, dict(json or {})))
        return {"data": {}}

    adapter = quark_module.QuarkAdapter()
    monkeypatch.setattr(adapter.http, "post_json", fake_post)
    files = [DriveFile(file_id="f1", name="a.mkv", size=1)]

    adapter.move_files(_quark_account(), files, "")
    adapter.rename_file(_quark_account(), files[0], "b.mkv")
    adapter.delete_files(_quark_account(), files)

    assert calls[0][1]["to_pdir_fid"] == "0"
    assert calls[0][1]["filelist"] == ["f1"]
    assert calls[1][1] == {"fid": "f1", "file_name": "b.mkv"}
    assert calls[2][1]["action_type"] == 2


def test_refresh_account_without_cookie_returns_none(monkeypatch) -> None:
    """
    凭据中没有 Cookie 时不刷新。
    """
    adapter = quark_module.QuarkAdapter()
    account = NetdiskAccountRecord(id=7, platform=NetdiskPlatform.QUARK, name="q", credential={})

    assert adapter.refresh_account(account) is None


# ---- 天翼 ----


def _tianyi_account() -> NetdiskAccountRecord:
    """
    构造天翼测试账号。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=1,
        platform=NetdiskPlatform.TIANYI,
        name="t",
        credential={"COOKIE_LOGIN_USER": "fake"},
    )


def test_tianyi_int_and_folder_helpers() -> None:
    """
    数值转换失败返回 0；目录字段兼容布尔与数字。
    """
    assert tianyi_module._int("abc") == 0  # pylint: disable=protected-access
    assert tianyi_module._int(None) == 0  # pylint: disable=protected-access
    assert tianyi_module._int("-3") == -3  # pylint: disable=protected-access
    assert tianyi_module._is_folder(True) is True  # pylint: disable=protected-access
    assert tianyi_module._is_folder(False) is False  # pylint: disable=protected-access
    assert tianyi_module._is_folder("1") is True  # pylint: disable=protected-access
    assert tianyi_module._is_folder(0) is False  # pylint: disable=protected-access


def test_tianyi_credential_has_cookie_accepts_header_forms() -> None:
    """
    显式字段与整段 Cookie 头都能识别 COOKIE_LOGIN_USER。
    """
    assert tianyi_module._credential_has_cookie({"COOKIE_LOGIN_USER": "v"}) is True  # pylint: disable=protected-access
    assert tianyi_module._credential_has_cookie({"cookie": "a=1; COOKIE_LOGIN_USER=v"}) is True  # pylint: disable=protected-access
    assert tianyi_module._credential_has_cookie({"cookie_header": "a=1; b=2"}) is False  # pylint: disable=protected-access
    assert tianyi_module._credential_has_cookie({"cookies": "broken; COOKIE_LOGIN_USER=v"}) is True  # pylint: disable=protected-access


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"res_message": "登录已失效"}, NetdiskErrorCode.AUTH_EXPIRED),
        ({"res_message": "分享不存在"}, NetdiskErrorCode.SHARE_INVALID),
        ({"res_message": "请求频繁"}, NetdiskErrorCode.RATE_LIMITED),
        ({"res_message": "空间不足"}, NetdiskErrorCode.SPACE_INSUFFICIENT),
        ({"res_message": "未知原因"}, NetdiskErrorCode.UNKNOWN),
    ],
)
def test_tianyi_error_classification(payload: dict[str, str], expected: NetdiskErrorCode) -> None:
    """
    天翼错误提示收敛为统一分类，且消息不含 Cookie。
    """
    adapter = tianyi_module.TianyiAdapter()

    error = adapter._error(payload)  # pylint: disable=protected-access

    assert error.code is expected
    assert "COOKIE_LOGIN_USER" not in error.message


def test_tianyi_items_skips_non_mapping_entries() -> None:
    """
    文件与目录列表中的非映射条目被跳过，缺少标识的条目被过滤。
    """
    payload = {
        "fileListAO": {
            "fileList": ["bad", {"id": "f1", "name": "a.mp4", "size": 10}, {"name": "no-id"}],
            "folderList": ["bad", {"fileId": "d1", "fileName": "dir"}],
        }
    }

    items = tianyi_module.TianyiAdapter._items(payload)  # pylint: disable=protected-access

    assert [item["id"] for item in items] == ["f1", "d1"]
    assert items[1]["isFolder"] is True
    assert tianyi_module.TianyiAdapter._items({"fileListAO": None}) == []  # pylint: disable=protected-access


def test_tianyi_user_info_unwraps_nested_payload(monkeypatch) -> None:
    """
    userInfo 节点存在时返回其内容，否则返回原始响应。
    """
    adapter = tianyi_module.TianyiAdapter()
    responses = [{"userInfo": {"capacity": 10}}, {"capacity": 20}]

    monkeypatch.setattr(adapter.http, "get_json", lambda *a, **k: responses.pop(0))

    assert adapter._user_info(_tianyi_account()) == {"capacity": 10}  # pylint: disable=protected-access
    assert adapter._user_info(_tianyi_account()) == {"capacity": 20}  # pylint: disable=protected-access


def test_tianyi_validate_credential_maps_unknown_and_generic_errors(monkeypatch) -> None:
    """
    非登录失效错误判为 unknown，网络异常也判为 unknown。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "_log_failure_response", lambda account: None)

    def raise_rate(account, folder_id):
        """
        抛出限流异常。

        :param account: 账号
        :param folder_id: 目录
        :return: 不会返回
        """
        del account, folder_id
        raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "限流", platform=NetdiskPlatform.TIANYI)

    monkeypatch.setattr(adapter, "_list_files", raise_rate)
    assert adapter.validate_credential({"COOKIE_LOGIN_USER": "v"}).status == "unknown"

    def raise_network(account, folder_id):
        """
        抛出网络异常。

        :param account: 账号
        :param folder_id: 目录
        :return: 不会返回
        """
        del account, folder_id
        raise OSError("network down")

    monkeypatch.setattr(adapter, "_list_files", raise_network)
    probe = adapter.validate_credential({"COOKIE_LOGIN_USER": "v"})
    assert probe.status == "unknown"
    assert "暂时不可用" in probe.message


def test_tianyi_failure_diagnostics_are_sanitized(monkeypatch) -> None:
    """
    失败诊断记录状态与截断响应体，并脱敏 Cookie；诊断异常与空响应静默返回。
    """
    adapter = tianyi_module.TianyiAdapter()
    logs: list[str] = []

    class _Response:
        """
        最小响应替身。
        """

        status_code = 400
        headers = {"Content-Type": "application/json"}
        text = "COOKIE_LOGIN_USER=secret-value;" + "x" * 300

    class _RequestUtils:
        """
        RequestUtils 替身。
        """

        def __init__(self, **kwargs: Any) -> None:
            """
            记录构造参数。

            :param kwargs: 参数
            """
            del kwargs

        def get_res(self, url: str, params: Any = None) -> Any:
            """
            返回预设响应。

            :param url: 地址
            :param params: 查询参数
            :return: 响应
            """
            del url, params
            return _Response()

    monkeypatch.setattr(tianyi_module, "RequestUtils", _RequestUtils)
    monkeypatch.setattr(tianyi_module.logger, "warning", lambda message, *args: logs.append(str(message)))

    adapter._log_failure_response(_tianyi_account())  # pylint: disable=protected-access

    assert logs
    assert "secret-value" not in logs[-1]
    assert "COOKIE_LOGIN_USER=***" in logs[-1]

    class _FailingRequestUtils:
        """
        构造即失败的 RequestUtils 替身。
        """

        def __init__(self, **kwargs: Any) -> None:
            """
            模拟构造失败。

            :param kwargs: 参数
            """
            del kwargs
            raise RuntimeError("boom")

    monkeypatch.setattr(tianyi_module, "RequestUtils", _FailingRequestUtils)
    adapter._log_failure_response(_tianyi_account())  # pylint: disable=protected-access

    monkeypatch.setattr(tianyi_module, "RequestUtils", lambda **kwargs: SimpleNamespace(get_res=lambda *a, **k: None))
    adapter._log_failure_response(_tianyi_account())  # pylint: disable=protected-access


def test_tianyi_probe_and_login_name(monkeypatch) -> None:
    """
    探测转发凭据校验；昵称优先 nickName，失败返回空串。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "validate_credential", lambda credential: SimpleNamespace(status="active"))
    assert adapter.probe_account(_tianyi_account()).status == "active"

    monkeypatch.setattr(adapter, "_user_info", lambda account: {"nickName": "天翼用户"})
    assert adapter.login_account_name({"COOKIE_LOGIN_USER": "v"}) == "天翼用户"

    monkeypatch.setattr(adapter, "_user_info", lambda account: {"loginName": "login"})
    assert adapter.login_account_name({"COOKIE_LOGIN_USER": "v"}) == "login"

    def boom(account):
        """
        模拟读取失败。

        :param account: 账号
        :return: 不会返回
        """
        del account
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "失败", platform=NetdiskPlatform.TIANYI)

    monkeypatch.setattr(adapter, "_user_info", boom)
    assert adapter.login_account_name({"COOKIE_LOGIN_USER": "v"}) == ""


def test_tianyi_ensure_temp_dir_reuses_existing_folder(monkeypatch) -> None:
    """
    已存在同名临时目录时复用，不重复创建。
    """
    adapter = tianyi_module.TianyiAdapter()
    name = f"{tianyi_module._TEMP_DIR_PREFIX}share123_file123"
    monkeypatch.setattr(adapter, "_list_files", lambda account, folder: [{"id": "dir-1", "name": name, "isFolder": True}])
    posts: list[str] = []
    monkeypatch.setattr(adapter, "_post", lambda *a, **k: posts.append("post") or {"id": "new"})

    assert adapter._ensure_temp_dir(_tianyi_account(), "share123", "file123") == "dir-1"  # pylint: disable=protected-access
    assert posts == []


def test_tianyi_ensure_temp_dir_requires_identifier(monkeypatch) -> None:
    """
    创建目录响应缺少标识时报错。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "_list_files", lambda account, folder: [])
    monkeypatch.setattr(adapter, "_post", lambda *a, **k: {})

    with pytest.raises(NetdiskError) as error:
        adapter._ensure_temp_dir(_tianyi_account(), "share123", "file123")  # pylint: disable=protected-access

    assert "缺少目录标识" in error.value.message


def test_tianyi_wait_batch_accepts_completed_payload(monkeypatch) -> None:
    """
    创建任务的响应已是完成状态时不再轮询。
    """
    adapter = tianyi_module.TianyiAdapter()
    calls: list[str] = []
    monkeypatch.setattr(adapter, "_post", lambda path, account, data: calls.append(path) or {"taskStatus": 4})

    result = adapter._wait_batch(_tianyi_account(), "MOVE", "T1", [], {"taskStatus": 4})  # pylint: disable=protected-access

    assert result == {"taskStatus": 4}
    assert calls == []


def test_tianyi_wait_batch_resolves_conflicts(monkeypatch) -> None:
    """
    冲突状态会触发冲突裁决并继续轮询。
    """
    adapter = tianyi_module.TianyiAdapter()
    statuses = iter([{"taskStatus": 2}, {"taskStatus": 4}])
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(path, account, data):
        """
        记录请求并返回预设状态。

        :param path: 接口路径
        :param account: 账号
        :param data: 请求体
        :return: 响应
        """
        del account
        posts.append((path, dict(data)))
        if path.endswith("checkBatchTask.action"):
            return next(statuses)
        return {}

    monkeypatch.setattr(adapter, "_post", fake_post)
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)

    result = adapter._wait_batch(_tianyi_account(), "MOVE", "T1", [{"fileId": "f1"}])  # pylint: disable=protected-access

    assert result == {"taskStatus": 4}
    assert any(path.endswith("getConflictTaskInfo.action") for path, _ in posts)
    manage = next(data for path, data in posts if path.endswith("manageBatchTask.action"))
    assert json.loads(manage["taskInfos"])[0]["dealWay"] == 1


def test_tianyi_wait_batch_times_out(monkeypatch) -> None:
    """
    始终未完成时抛出超时错误。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "_post", lambda *a, **k: {"taskStatus": 1})
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)

    with pytest.raises(NetdiskError) as error:
        adapter._wait_batch(_tianyi_account(), "MOVE", "T1", [])  # pylint: disable=protected-access

    assert "超时" in error.value.message


def test_tianyi_save_share_requires_task_id(monkeypatch) -> None:
    """
    转存响应缺少 taskId 时提示槽位被占用。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "_post", lambda *a, **k: {})

    with pytest.raises(NetdiskError) as error:
        adapter._save_share(_tianyi_account(), "S1", "DIR1", ShareFile(file_id="f1", name="x.mp4"))  # pylint: disable=protected-access

    assert error.value.code is NetdiskErrorCode.RATE_LIMITED


def test_tianyi_locate_saved_matches_name_and_size(monkeypatch) -> None:
    """
    任务未返回标识时按名称与大小唯一定位，无法唯一确定时报错。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(
        adapter,
        "_list_files",
        lambda account, folder: [{"id": "999", "name": "x.mp4", "size": 10, "isFolder": False}],
    )

    assert adapter._locate_saved(_tianyi_account(), "DIR1", ShareFile(file_id="f1", name="x.mp4", size=10), ()) == "999"  # pylint: disable=protected-access

    monkeypatch.setattr(adapter, "_list_files", lambda account, folder: [])
    with pytest.raises(NetdiskError) as error:
        adapter._locate_saved(_tianyi_account(), "DIR1", ShareFile(file_id="f1", name="x.mp4", size=10), ())  # pylint: disable=protected-access
    assert "无法唯一定位" in error.value.message


def test_tianyi_download_url_requires_and_normalizes_url(monkeypatch) -> None:
    """
    直链支持协议相对与站内相对路径，缺少地址时报错。
    """
    adapter = tianyi_module.TianyiAdapter()
    responses = [
        {"downloadUrl": ""},
        {"downloadUrl": "//dl.example.com/x"},
        {"downloadUrl": "/web/x"},
        {"downloadUrl": "https://dl.example.com/y"},
    ]
    monkeypatch.setattr(adapter.http, "get_json", lambda *a, **k: responses.pop(0))

    with pytest.raises(NetdiskError) as error:
        adapter._download_url(_tianyi_account(), "f1")  # pylint: disable=protected-access
    assert "未返回下载地址" in error.value.message

    assert adapter._download_url(_tianyi_account(), "f1") == "https://dl.example.com/x"  # pylint: disable=protected-access
    assert adapter._download_url(_tianyi_account(), "f1") == "https://cloud.189.cn/web/x"  # pylint: disable=protected-access
    assert adapter._download_url(_tianyi_account(), "f1") == "https://dl.example.com/y"  # pylint: disable=protected-access


def test_tianyi_cleanup_swallows_errors(monkeypatch) -> None:
    """
    清理失败只记录，不抛出。
    """
    adapter = tianyi_module.TianyiAdapter()

    def boom(*args, **kwargs):
        """
        模拟删除失败。

        :param args: 位置参数
        :param kwargs: 关键字参数
        :return: 不会返回
        """
        del args, kwargs
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "删除失败", platform=NetdiskPlatform.TIANYI)

    monkeypatch.setattr(adapter, "_post", boom)
    handle = TransferHandle(platform=NetdiskPlatform.TIANYI, account_id=1, directory_id="DIR1")

    adapter.cleanup(handle, _tianyi_account())


def test_tianyi_download_file_returns_direct_link(monkeypatch) -> None:
    """
    个人盘取链复用直链接口并带上账号信息。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "_download_url", lambda account, file_id: "https://dl.example.com/x")

    link = adapter.download_file(_tianyi_account(), DriveFile(file_id="f1", name="a.mp4", size=5))

    assert link.url == "https://dl.example.com/x"
    assert link.size == 5


def test_tianyi_batch_handles_missing_task_and_failures(monkeypatch) -> None:
    """
    批量任务缺少 taskId 时直接返回；失败计数大于 0 时报错。
    """
    adapter = tianyi_module.TianyiAdapter()
    monkeypatch.setattr(adapter, "_post", lambda *a, **k: {})
    adapter._batch(_tianyi_account(), "DELETE", [])  # pylint: disable=protected-access

    monkeypatch.setattr(adapter, "_post", lambda *a, **k: {"taskId": "T1"})
    monkeypatch.setattr(adapter, "_wait_batch", lambda *a, **k: {"failedCount": 1})
    with pytest.raises(NetdiskError) as error:
        adapter._batch(_tianyi_account(), "DELETE", [DriveFile(file_id="f1", name="x")])  # pylint: disable=protected-access
    assert "部分失败" in error.value.message


def test_tianyi_drive_mutations(monkeypatch) -> None:
    """
    移动、重命名（目录/文件）与删除都转发到对应接口。
    """
    adapter = tianyi_module.TianyiAdapter()
    batches: list[tuple[str, str, list[str]]] = []
    posts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        adapter,
        "_batch",
        lambda account, type_, files, target="": batches.append((type_, target, [item.file_id for item in files])),
    )
    monkeypatch.setattr(adapter, "_post", lambda path, account, data: posts.append((path, dict(data))) or {})

    files = [DriveFile(file_id="f1", name="a.mp4")]
    adapter.move_files(_tianyi_account(), files, "")
    adapter.delete_files(_tianyi_account(), files)
    adapter.rename_file(_tianyi_account(), DriveFile(file_id="d1", name="dir", is_dir=True), "new-dir")
    adapter.rename_file(_tianyi_account(), DriveFile(file_id="f1", name="a.mp4", is_dir=False), "new.mp4")

    assert batches == [("MOVE", "-11", ["f1"]), ("DELETE", "", ["f1"])]
    assert posts[0][0].endswith("renameFolder.action")
    assert posts[0][1]["destFolderName"] == "new-dir"
    assert posts[1][0].endswith("renameFile.action")
    assert posts[1][1]["destFileName"] == "new.mp4"
