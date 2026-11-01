"""
验证夸克取链客户端的响应解析、会话刷新、翻页与转存轮询。

所有网络调用都用假适配器替换，不访问外网。
"""

from typing import Any, Dict, List, Optional

import pytest

from app.modules.quark import quark as quark_client
from app.modules.quark.quark import (
    QuarkClient,
    QuarkError,
    _as_int,
    _merge_cookie,
    _strip_puus,
)


class FakeResponse:
    """
    最小 HTTP 响应替身。
    """

    def __init__(self, payload: Any = None, status_code: int = 200,
                 set_cookie: str = "", json_error: Optional[Exception] = None,
                 close_error: Optional[Exception] = None) -> None:
        """
        保存响应体、状态码与可选异常。
        """
        self._payload = payload
        self.status_code = status_code
        self.headers = {"set-cookie": set_cookie}
        self._json_error = json_error
        self._close_error = close_error
        self.closed = False

    def json(self) -> Any:
        """
        返回预设 JSON，或抛出预设异常。
        """
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def close(self) -> None:
        """
        标记关闭，或抛出预设异常。
        """
        if self._close_error is not None:
            raise self._close_error
        self.closed = True


class FakeHttp:
    """
    按 URL 关键字返回预设响应的假适配器。
    """

    def __init__(self) -> None:
        """
        初始化调用记录与响应表。
        """
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, Any] = {}
        self.default: Any = FakeResponse({"status": 200, "code": 0, "data": {}})

    def post_res(self, url: str, **kwargs: Any) -> Any:
        """
        记录并返回预设响应。
        """
        self.calls.append({"method": "POST", "url": url, "body": kwargs.get("json")})
        return self._pick(url)

    def get_res(self, url: str, **kwargs: Any) -> Any:
        """
        记录并返回预设响应。
        """
        self.calls.append({"method": "GET", "url": url, "params": kwargs.get("params")})
        return self._pick(url)

    def _pick(self, url: str) -> Any:
        """
        按关键字匹配响应，未命中时返回默认成功响应。
        """
        for key, response in self.responses.items():
            if key in url:
                return response
        return self.default


def _install(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    """
    用假适配器替换客户端内部的 RequestUtils。
    """
    http = FakeHttp()
    monkeypatch.setattr(quark_client, "RequestUtils", lambda **kwargs: http)
    return http


def test_as_int_handles_invalid_values() -> None:
    """非数字与负数都归一为 0。"""
    assert _as_int("12") == 12
    assert _as_int("abc") == 0
    assert _as_int(None) == 0
    assert _as_int(-5) == 0


def test_merge_cookie_ignores_missing_or_unrelated_headers() -> None:
    """没有新令牌时 Cookie 原样返回。"""
    class _NoHeaders:
        """headers 访问直接抛异常的响应。"""

        @property
        def headers(self):
            """模拟异常响应。"""
            raise RuntimeError("boom")

    assert _merge_cookie("a=1", FakeResponse(set_cookie="")) == "a=1"
    assert _merge_cookie("a=1", _NoHeaders()) == "a=1"
    assert _merge_cookie("a=1", FakeResponse(set_cookie="foo; bar=baz")) == "a=1"


def test_merge_cookie_replaces_existing_tokens() -> None:
    """新的 __puus/__pus 覆盖旧值，其余 Cookie 保留。"""
    cookie = "a=1; __puus=old; __pus=old2"

    merged = _merge_cookie(cookie, FakeResponse(set_cookie="__puus=new; Path=/\n__pus=new2"))

    assert "a=1" in merged
    assert "__puus=new" in merged
    assert "__pus=new2" in merged
    assert "old" not in merged


def test_strip_puus_removes_only_puus() -> None:
    """剥离 __puus 时保留其他 Cookie。"""
    assert _strip_puus("a=1; __puus=x; b=2") == "a=1; b=2"


def test_parse_rejects_missing_response() -> None:
    """网络不可达时给出明确原因。"""
    with pytest.raises(QuarkError, match="网络不可达"):
        QuarkClient(cookie="c=1")._parse(None, "测试")


def test_parse_rejects_non_json_body() -> None:
    """响应不是 JSON 时提示可能触发风控，并关闭响应。"""
    response = FakeResponse(json_error=ValueError("not json"), status_code=403)

    with pytest.raises(QuarkError, match="不是 JSON"):
        QuarkClient(cookie="c=1")._parse(response, "测试")
    assert response.closed


def test_parse_rejects_non_object_payload() -> None:
    """响应 JSON 不是对象时判为结构异常。"""
    with pytest.raises(QuarkError, match="响应结构异常"):
        QuarkClient(cookie="c=1")._parse(FakeResponse([1, 2]), "测试")


def test_parse_swallows_close_error() -> None:
    """关闭响应失败不应掩盖正常结果。"""
    payload = {"status": 200, "code": 0, "data": {"ok": True}}
    response = FakeResponse(payload, close_error=RuntimeError("boom"))

    assert QuarkClient(cookie="c=1")._parse(response, "测试") == payload


def test_parse_reports_guest_limit_and_generic_errors() -> None:
    """游客限制与服务端消息分别给出可读原因。"""
    client = QuarkClient(cookie="c=1")

    with pytest.raises(QuarkError, match="游客取链大小限制"):
        client._parse(FakeResponse({"status": 400, "code": 23018}), "测试")

    with pytest.raises(QuarkError, match="boom"):
        client._parse(FakeResponse({"status": 400, "code": 12345, "message": "boom"}), "测试")

    with pytest.raises(QuarkError, match="HTTP 500"):
        client._parse(FakeResponse({"status": 400, "code": 12345}, status_code=500), "测试")


def test_ensure_cookie_requires_configuration() -> None:
    """未配置 Cookie 时不能发起取链。"""
    with pytest.raises(QuarkError, match="未配置"):
        QuarkClient().ensure_cookie()


def test_ensure_cookie_returns_fresh_cookie_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cookie 未到刷新时间时直接返回，不发起请求。"""
    http = _install(monkeypatch)
    client = QuarkClient(cookie="c=1")

    assert client.ensure_cookie() == "c=1"
    assert http.calls == []


def test_ensure_cookie_refreshes_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cookie 过期后走刷新接口并合并新令牌。"""
    http = _install(monkeypatch)
    http.responses["config"] = FakeResponse({"status": 200, "code": 0, "data": {}},
                                            set_cookie="__puus=fresh")
    client = QuarkClient(cookie="c=1; __puus=stale")
    client._cookie_at -= quark_client._PUUS_REFRESH_SECONDS + 1  # pylint: disable=protected-access

    assert client.ensure_cookie() == "c=1; __puus=fresh"


def test_refresh_cookie_restores_original_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新失败时保留原 Cookie，避免把可用会话弄丢。"""
    http = _install(monkeypatch)
    http.responses["config"] = FakeResponse({"status": 400, "code": 31001})
    client = QuarkClient(cookie="c=1; __puus=stale")

    with pytest.raises(QuarkError):
        client.refresh_cookie()
    assert client.cookie == "c=1; __puus=stale"


def test_connection_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接巡检接口按凭据状态返回结果。"""
    _install(monkeypatch)
    client = QuarkClient(cookie="")

    assert client.is_inactive() is True
    assert client.transfer_info() == {"cookie": False}
    client.cookie = "c=1"
    assert client.is_inactive() is False
    assert client.transfer_info() == {"cookie": True}


def test_reconnect_swallows_refresh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """重连失败只保留原 Cookie，不向调度器抛异常。"""
    http = _install(monkeypatch)
    http.responses["config"] = FakeResponse({"status": 400, "code": 31001})
    client = QuarkClient(cookie="c=1; __puus=stale")

    client.reconnect()

    assert client.cookie == "c=1; __puus=stale"


def test_share_files_skips_invalid_items() -> None:
    """非对象条目与没有文件名的条目被忽略，缺失 fid 时用目录名兜底。"""
    client = QuarkClient(cookie="c=1")
    pages = {
        "1": ["bad", {"file_name": ""}, {"file_name": "a.mp4", "size": 1}],
        "2": [],
    }

    def fake_get(url: str, params: Any, action: str) -> Dict[str, Any]:
        """按页码返回预设列表。"""
        return {"status": 200, "code": 0, "data": {"list": pages[str((params or {}).get("_page"))]}}

    client._get = fake_get  # type: ignore[assignment]
    entries = client.share_files("code", "stoken")

    assert len(entries) == 1
    assert entries[0]["fid"] == ""
    assert entries[0]["name"] == "a.mp4"


def test_file_link_uses_first_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """转存后的取链接口按 fid 请求并返回直链。"""
    http = _install(monkeypatch)
    http.responses["file/download"] = FakeResponse({
        "status": 200,
        "code": 0,
        "data": [{"download_url": "https://cdn/x", "filename": "a.mp4", "size": 9}],
    })

    link = QuarkClient(cookie="c=1").file_link("fid1")

    assert link == {"url": "https://cdn/x", "name": "a.mp4", "size": 9}
    assert http.calls[0]["body"] == {"fids": ["fid1"]}


def test_first_link_rejects_malformed_payload() -> None:
    """取链响应结构异常或链接为空时给出明确错误。"""
    with pytest.raises(QuarkError, match="未返回下载链接"):
        QuarkClient._first_link({"data": []}, "获取直链")
    with pytest.raises(QuarkError, match="未返回下载链接"):
        QuarkClient._first_link({"data": "oops"}, "获取直链")
    with pytest.raises(QuarkError, match="下载链接为空"):
        QuarkClient._first_link({"data": [{"size": 1}]}, "获取直链")


def test_create_folder_returns_fid(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建临时目录返回新 fid。"""
    http = _install(monkeypatch)
    http.responses["clouddrive/file?"] = FakeResponse(
        {"status": 200, "code": 0, "data": {"fid": "folder1"}}
    )

    assert QuarkClient(cookie="c=1").create_folder("tmp") == "folder1"


def test_create_folder_requires_fid(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建目录响应缺少 fid 时判失败。"""
    _install(monkeypatch)

    with pytest.raises(QuarkError, match="缺少 fid"):
        QuarkClient(cookie="c=1").create_folder("tmp")


def test_save_share_returns_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """转存分享返回异步任务标识。"""
    http = _install(monkeypatch)
    http.responses["sharepage/save"] = FakeResponse(
        {"status": 200, "code": 0, "data": {"task_id": "task1"}}
    )

    assert QuarkClient(cookie="c=1").save_share("code", "st", "fid", "tok", "dir") == "task1"


def test_save_share_requires_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """转存响应缺少任务标识时判失败。"""
    _install(monkeypatch)

    with pytest.raises(QuarkError, match="缺少 task_id"):
        QuarkClient(cookie="c=1").save_share("code", "st", "fid", "tok", "dir")


def test_poll_saved_fid_retries_after_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮询遇到临时错误时继续，完成后返回新 fid。"""
    monkeypatch.setattr(quark_client.time, "sleep", lambda _seconds: None)
    client = QuarkClient(cookie="c=1")
    results = [
        QuarkError("临时失败"),
        {"data": {"status": 2, "save_as": {"save_as_top_fids": ["newfid"]}}},
    ]

    def fake_get(url: str, params: Any, action: str) -> Dict[str, Any]:
        """按序返回错误或结果。"""
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client._get = fake_get  # type: ignore[assignment]

    assert client.poll_saved_fid("task1") == "newfid"


def test_poll_saved_fid_reports_missing_fid(monkeypatch: pytest.MonkeyPatch) -> None:
    """转存完成但没有新文件标识时判失败。"""
    monkeypatch.setattr(quark_client.time, "sleep", lambda _seconds: None)
    client = QuarkClient(cookie="c=1")
    client._get = lambda *args, **kwargs: {  # type: ignore[assignment]
        "data": {"finished_at": 1, "save_as": {}},
    }

    with pytest.raises(QuarkError, match="未返回新文件标识"):
        client.poll_saved_fid("task1")


def test_poll_saved_fid_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮询次数耗尽后报告超时。"""
    monkeypatch.setattr(quark_client.time, "sleep", lambda _seconds: None)
    client = QuarkClient(cookie="c=1")
    client._get = lambda *args, **kwargs: {"data": {}}  # type: ignore[assignment]

    with pytest.raises(QuarkError, match="超时"):
        client.poll_saved_fid("task1")


def test_delete_files_skips_empty_list() -> None:
    """没有待删除文件时不发起请求。"""
    client = QuarkClient(cookie="c=1")
    client._post = lambda *args, **kwargs: pytest.fail("不应发起请求")  # type: ignore[assignment]

    client.delete_files([])


def test_delete_files_uses_expected_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """删除接口必须使用 filelist 与 exclude_fids 字段。"""
    http = _install(monkeypatch)

    QuarkClient(cookie="c=1").delete_files(["a", "b"])

    body = http.calls[0]["body"]
    assert body["filelist"] == ["a", "b"]
    assert body["exclude_fids"] == []
    assert body["action_type"] == 2


def test_delete_files_swallows_errors() -> None:
    """清理失败只记录，不影响下载结果。"""
    client = QuarkClient(cookie="c=1")

    def fail(*args: Any, **kwargs: Any) -> None:
        """模拟接口异常。"""
        raise QuarkError("boom")

    client._post = fail  # type: ignore[assignment]

    client.delete_files(["a"])
