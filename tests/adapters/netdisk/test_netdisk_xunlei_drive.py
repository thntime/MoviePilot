"""
迅雷云盘个人盘协议方法测试。

所有 HTTP 调用都在 `NetdiskHttp` / `RequestUtils` 层打桩，不访问真实网络，令牌全部为假值。
"""

from typing import Any, Optional

import pytest

from app.adapters.external.netdisk import xunlei
from app.adapters.external.netdisk.base import NetdiskHttp
from app.domain.netdisk import (
    DriveFile,
    NetdiskAccountRecord,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
)

_ACCESS_TOKEN = "fake-access-token"
_DEVICE_ID = "fake-device-id"
_ABOUT_URL = "https://api-pan.xunlei.com/drive/v1/about"
_FILES_URL = "https://api-pan.xunlei.com/drive/v1/files"
_MOVE_URL = "https://api-pan.xunlei.com/drive/v1/files:batchMove"
_TRASH_URL = "https://api-pan.xunlei.com/drive/v1/files:batchTrash"
_SHARE_URL = "https://api-pan.xunlei.com/drive/v1/share"


class _Response:
    """
    最小 HTTP 响应替身，供 PATCH 请求测试使用。
    """

    def __init__(self, payload: Optional[dict[str, Any]] = None, status_code: int = 200) -> None:
        """
        保存状态码与响应体。

        :param payload: JSON 响应体
        :param status_code: HTTP 状态码
        """
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        """
        返回预设 JSON。

        :return: 响应体
        """
        return self._payload


def _account() -> NetdiskAccountRecord:
    """
    构造使用假令牌与假设备标识的迅雷账号。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=31,
        platform=NetdiskPlatform.XUNLEI,
        name="迅雷测试账号",
        credential={
            "access_token": _ACCESS_TOKEN,
            "deviceid": _DEVICE_ID,
            "captcha": "fake-captcha",
        },
        is_primary=True,
    )


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[xunlei.XunleiAdapter, list[dict[str, Any]], dict]:
    """
    把共享 HTTP 客户端替换为按 `(方法, URL)` 排队返回的替身。

    :param monkeypatch: pytest 打桩夹具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    calls: list[dict[str, Any]] = []
    responses: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _next(method: str, url: str) -> dict[str, Any]:
        queue = responses.get((method, url)) or []
        assert queue, f"未预设响应：{method} {url}"
        return queue.pop(0)

    def get_json(_self, url, *, account=None, params=None, headers=None):
        """
        记录并返回预设的 GET 响应。
        """
        calls.append({"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return _next("GET", url)

    def post_json(_self, url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        记录并返回预设的 POST 响应。
        """
        calls.append(
            {
                "method": "POST",
                "url": url,
                "json": dict(json or {}),
                "data": dict(data or {}),
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            }
        )
        return _next("POST", url)

    def get_text(_self, url, *, account=None, params=None, headers=None):
        """
        迅雷个人盘方法不应发起文本请求。
        """
        raise AssertionError("迅雷个人盘方法不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)
    # captcha 现签需要真实网络；单测统一返回固定 token，避免额外请求。
    xunlei.XunleiAdapter._captcha_cache.clear()
    monkeypatch.setattr(
        xunlei.XunleiAdapter,
        "_init_captcha",
        lambda self, device, access: ("fresh-captcha", 9e18),
    )
    return xunlei.XunleiAdapter(), calls, responses


def _queue(responses: dict, method: str, url: str, payload: dict[str, Any]) -> None:
    """
    追加一条预设响应。

    :param responses: 响应队列
    :param method: HTTP 方法
    :param url: 请求地址
    :param payload: 响应 JSON
    """
    responses.setdefault((method, url), []).append(payload)


def test_drive_capabilities_cover_all_operations() -> None:
    """
    迅雷声明个人盘全部七项能力。
    """
    capabilities = xunlei.XunleiAdapter.drive_capabilities

    assert capabilities.quota is True
    assert capabilities.browse is True
    assert capabilities.download is True
    assert capabilities.share is True
    assert capabilities.move is True
    assert capabilities.rename is True
    assert capabilities.delete is True


def test_quota_reads_about(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    空间查询读取 `drive/v1/about` 的 quota。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(responses, "GET", _ABOUT_URL, {"data": {"quota": {"limit": 1000, "usage": 250}}})

    quota = adapter.quota(_account())

    assert quota.total == 1000
    assert quota.used == 250
    assert calls[0]["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert calls[0]["headers"]["X-Device-Id"] == _DEVICE_ID


def test_list_dir_follows_page_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘列目录按 `next_page_token` 翻页并映射条目。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "GET",
        _FILES_URL,
        {
            "data": {
                "files": [
                    {"id": "f1", "name": "01.mkv", "size": 1024, "kind": "drive#file", "parent_id": ""},
                    {"id": "f2", "name": "02.mkv", "size": 2048, "kind": "drive#file", "parent_id": ""},
                ],
                "next_page_token": "page-2",
            }
        },
    )
    _queue(
        responses,
        "GET",
        _FILES_URL,
        {
            "data": {
                "files": [
                    {"id": "d1", "name": "合集", "size": 0, "kind": "drive#folder", "parent_id": ""},
                ],
                "next_page_token": "",
            }
        },
    )

    files = adapter.list_dir(_account(), "")

    assert [item.file_id for item in files] == ["f1", "f2", "d1"]
    assert files[0].size == 1024
    assert files[0].is_dir is False
    assert files[2].is_dir is True
    assert calls[0]["params"]["parent_id"] == ""
    assert calls[0]["params"]["limit"] == 50
    assert calls[1]["params"]["page_token"] == "page-2"


def test_download_file_returns_stream_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘取链读取文件详情中的 octet-stream 直链。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "GET",
        f"{_FILES_URL}/f1",
        {
            "data": {
                "id": "f1",
                "name": "01.mkv",
                "size": 2048,
                "links": {"application/octet-stream": {"url": "https://cdn.example.com/01.mkv"}},
            }
        },
    )

    link = adapter.download_file(_account(), DriveFile(file_id="f1", name="01.mkv", size=1024))

    assert link.url == "https://cdn.example.com/01.mkv"
    assert link.size == 2048
    assert link.headers["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert calls[0]["url"] == f"{_FILES_URL}/f1"


def test_download_file_rejects_directory() -> None:
    """
    目录没有直链，直接返回未支持。
    """
    adapter = xunlei.XunleiAdapter()

    with pytest.raises(NetdiskError) as captured:
        adapter.download_file(_account(), DriveFile(file_id="d1", name="合集", is_dir=True))

    assert captured.value.code is NetdiskErrorCode.UNSUPPORTED


def test_share_file_creates_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    创建分享提交文件列表、有效期与提取码。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "POST",
        _SHARE_URL,
        {"data": {"share_url": "https://pan.xunlei.com/s/abc", "pass_code": "x1y2"}},
    )

    share = adapter.share_file(
        _account(),
        [DriveFile(file_id="f1", name="01.mkv", size=1024)],
        password="x1y2",
    )

    assert share.url == "https://pan.xunlei.com/s/abc"
    assert share.password == "x1y2"
    body = calls[0]["json"]
    assert body["file_ids"] == ["f1"]
    assert body["expiration_days"] == "-1"
    assert body["params"]["pass_code"] == "x1y2"


def test_move_and_delete_use_batch_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    移动与删除分别调用 `batchMove` 与 `batchTrash`。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(responses, "POST", _MOVE_URL, {"data": {"task_id": "t1"}})
    _queue(responses, "POST", _TRASH_URL, {"data": {}})

    adapter.move_files(_account(), [DriveFile(file_id="f1", name="01.mkv")], "d1")
    adapter.delete_files(_account(), [DriveFile(file_id="f1", name="01.mkv")])

    assert calls[0]["json"] == {"ids": ["f1"], "to": {"parent_id": "d1", "space": ""}, "space": ""}
    assert calls[1]["json"] == {"ids": ["f1"], "space": ""}


def test_rename_file_uses_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    重命名使用 `PATCH /drive/v1/files/{id}`，请求体只带新名称。
    """
    adapter, _calls, _responses = _install(monkeypatch)
    recorded: dict[str, Any] = {}

    class _FakeRequestUtils:
        """
        记录 PATCH 请求的 `RequestUtils` 替身。
        """

        def __init__(self, **kwargs: Any) -> None:
            """
            保存构造参数。

            :param kwargs: 构造参数
            """
            recorded["headers"] = dict(kwargs.get("headers") or {})

        def request(self, method: str, url: str, **kwargs: Any) -> _Response:
            """
            记录请求并返回成功响应。

            :param method: HTTP 方法
            :param url: 请求地址
            :param kwargs: 请求参数
            :return: 响应替身
            """
            recorded["method"] = method
            recorded["url"] = url
            recorded["json"] = kwargs.get("json")
            return _Response({"data": {"id": "f1", "name": "02.mkv"}})

    monkeypatch.setattr(xunlei, "RequestUtils", _FakeRequestUtils)

    adapter.rename_file(_account(), DriveFile(file_id="f1", name="01.mkv"), "02.mkv")

    assert recorded["method"] == "patch"
    assert recorded["url"] == f"{_FILES_URL}/f1"
    assert recorded["json"] == {"name": "02.mkv"}
    assert recorded["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
