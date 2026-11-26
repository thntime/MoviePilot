"""
光鸭云盘个人盘协议方法测试。

所有 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，令牌全部为假值。
"""

from typing import Any

import pytest

from app.adapters.external.netdisk import guangya
from app.adapters.external.netdisk.base import NetdiskHttp
from app.domain.netdisk import (
    DriveFile,
    NetdiskAccountRecord,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
)

_TOKEN = "fake-access-token"
_ASSETS_URL = "https://api.guangyapan.com/assets/v1/get_assets"
_FILE_LIST_URL = "https://api.guangyapan.com/userres/v1/file/get_file_list"
_RES_DOWNLOAD_URL = "https://api.guangyapan.com/userres/v1/get_res_download_url"
_SHARE_FILE_URL = "https://api.guangyapan.com/userres/v1/share_file"
_MOVE_FILE_URL = "https://api.guangyapan.com/userres/v1/file/move_file"
_RENAME_URL = "https://api.guangyapan.com/userres/v1/file/rename"
_DELETE_FILE_URL = "https://api.guangyapan.com/userres/v1/file/delete_file"
_TASK_STATUS_URL = "https://api.guangyapan.com/userres/v1/get_task_status"


def _account() -> NetdiskAccountRecord:
    """
    构造使用假 token 的光鸭账号。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=11,
        platform=NetdiskPlatform.GUANGYA,
        name="光鸭测试账号",
        credential={"token": _TOKEN},
        is_primary=True,
    )


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[guangya.GuangYaAdapter, list[dict[str, Any]], dict]:
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
        光鸭个人盘方法不应发起文本请求。
        """
        raise AssertionError("光鸭个人盘方法不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)
    adapter = guangya.GuangYaAdapter()
    monkeypatch.setattr(type(adapter), "sleep", staticmethod(lambda _seconds: None))
    return adapter, calls, responses


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
    光鸭声明个人盘全部七项能力。
    """
    capabilities = guangya.GuangYaAdapter.drive_capabilities

    assert capabilities.quota is True
    assert capabilities.browse is True
    assert capabilities.download is True
    assert capabilities.share is True
    assert capabilities.move is True
    assert capabilities.rename is True
    assert capabilities.delete is True


def test_quota_reads_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    空间查询走 `get_assets`，并携带账号令牌。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "POST",
        _ASSETS_URL,
        {"success": True, "data": {"totalSpaceSize": "1000", "usedSpaceSize": "250"}},
    )

    quota = adapter.quota(_account())

    assert quota.used == 250
    assert quota.total == 1000
    assert calls[0]["url"] == _ASSETS_URL
    assert calls[0]["headers"]["Authorization"] == f"Bearer {_TOKEN}"


def test_list_dir_maps_cloud_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘列目录把文件与目录映射为统一的 `DriveFile`。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "POST",
        _FILE_LIST_URL,
        {
            "success": True,
            "data": {
                "list": [
                    {"fileId": "f1", "fileName": "01.mkv", "resType": "1", "fileSize": "1024"},
                    {"fileId": "d1", "fileName": "合集", "resType": "2", "fileSize": "0"},
                ],
                "total": 2,
            },
        },
    )

    files = adapter.list_dir(_account(), "")

    assert [item.file_id for item in files] == ["f1", "d1"]
    assert files[0].name == "01.mkv"
    assert files[0].size == 1024
    assert files[0].is_dir is False
    assert files[1].is_dir is True
    assert calls[0]["json"]["parentId"] == ""
    assert calls[0]["json"]["page"] == 0


def test_download_file_returns_signed_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘取链使用 `get_res_download_url` 并携带下载请求头。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "POST",
        _RES_DOWNLOAD_URL,
        {"success": True, "data": {"signedURL": "https://cdn.example.com/01.mkv", "size": "2048"}},
    )

    link = adapter.download_file(
        _account(),
        DriveFile(file_id="f1", name="01.mkv", size=1024),
    )

    assert link.url == "https://cdn.example.com/01.mkv"
    assert link.size == 2048
    assert link.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert link.headers["User-Agent"] == guangya.GUANGYA_USER_AGENT
    assert calls[0]["json"] == {"fileId": "f1"}


def test_download_file_rejects_directory() -> None:
    """
    目录没有直链，直接返回未支持。
    """
    adapter = guangya.GuangYaAdapter()

    with pytest.raises(NetdiskError) as captured:
        adapter.download_file(_account(), DriveFile(file_id="d1", name="合集", is_dir=True))

    assert captured.value.code is NetdiskErrorCode.UNSUPPORTED
    assert "目录" in captured.value.message


def test_share_file_creates_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    创建分享按提取码切换分享类型，并回填分享链接。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "POST",
        _SHARE_FILE_URL,
        {"success": True, "data": {"shareId": "s1", "code": "a1b2"}},
    )

    share = adapter.share_file(
        _account(),
        [DriveFile(file_id="f1", name="01.mkv", size=1024)],
        password="a1b2",
        expires_in=86400,
    )

    assert share.url == "https://www.guangyapan.com/s/s1"
    assert share.password == "a1b2"
    body = calls[0]["json"]
    assert body["fileIds"] == ["f1"]
    assert body["validateDuration"] == 1
    assert body["shareType"] == 1
    assert body["code"] == "a1b2"


def test_move_rename_delete_wait_async_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    移动、重命名、删除都提交任务并等待任务状态完成。
    """
    adapter, calls, responses = _install(monkeypatch)
    for url in (_MOVE_FILE_URL, _RENAME_URL, _DELETE_FILE_URL):
        _queue(responses, "POST", url, {"success": True, "data": {"taskId": "t1"}})
        _queue(responses, "POST", _TASK_STATUS_URL, {"success": True, "data": {"status": 2, "detail": {"code": 0}}})

    adapter.move_files(_account(), [DriveFile(file_id="f1", name="01.mkv")], "d1")
    adapter.rename_file(_account(), DriveFile(file_id="f1", name="01.mkv"), "02.mkv")
    adapter.delete_files(_account(), [DriveFile(file_id="f1", name="01.mkv")])

    assert calls[0]["json"] == {"fileIds": ["f1"], "parentId": "d1"}
    assert calls[2]["json"] == {"fileId": "f1", "newName": "02.mkv"}
    assert calls[4]["json"] == {"fileIds": ["f1"]}
    assert [call["url"] for call in calls] == [
        _MOVE_FILE_URL,
        _TASK_STATUS_URL,
        _RENAME_URL,
        _TASK_STATUS_URL,
        _DELETE_FILE_URL,
        _TASK_STATUS_URL,
    ]
