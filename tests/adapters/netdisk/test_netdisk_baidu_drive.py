"""
百度网盘个人盘协议方法测试。

所有 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，Cookie 全部为假值。
"""

import json
from typing import Any

import pytest

from app.adapters.external.netdisk import baidu
from app.adapters.external.netdisk.base import NetdiskHttp
from app.domain.netdisk import (
    DriveFile,
    NetdiskAccountRecord,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
)

_COOKIE = "BDUSS=fake-bduss"
_QUOTA_URL = "https://pan.baidu.com/api/quota"
_LIST_URL = "https://pan.baidu.com/api/list"
_TEMPLATE_URL = "https://pan.baidu.com/api/gettemplatevariable"
_LOCATE_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/file"
_SHARE_SET_URL = "https://pan.baidu.com/share/set"
_FILEMANAGER_URL = "https://pan.baidu.com/api/filemanager"


def _account() -> NetdiskAccountRecord:
    """
    构造使用假 Cookie 的百度账号。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=21,
        platform=NetdiskPlatform.BAIDU,
        name="百度测试账号",
        credential={"cookie": _COOKIE},
        is_primary=True,
    )


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[baidu.BaiduAdapter, list[dict[str, Any]], dict]:
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
        百度个人盘方法不应发起文本请求。
        """
        raise AssertionError("百度个人盘方法不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)
    return baidu.BaiduAdapter(), calls, responses


def _queue(responses: dict, method: str, url: str, payload: dict[str, Any]) -> None:
    """
    追加一条预设响应。

    :param responses: 响应队列
    :param method: HTTP 方法
    :param url: 请求地址
    :param payload: 响应 JSON
    """
    responses.setdefault((method, url), []).append(payload)


def _token(payload: str = "fake-bdstoken") -> dict[str, Any]:
    """
    构造 bdstoken 接口响应。

    :param payload: bdstoken 值
    :return: 响应 JSON
    """
    return {"errno": 0, "result": {"bdstoken": payload}}


def test_drive_capabilities_cover_all_operations() -> None:
    """
    百度声明个人盘全部七项能力。
    """
    capabilities = baidu.BaiduAdapter.drive_capabilities

    assert capabilities.quota is True
    assert capabilities.browse is True
    assert capabilities.download is True
    assert capabilities.share is True
    assert capabilities.move is True
    assert capabilities.rename is True
    assert capabilities.delete is True


def test_quota_reads_api_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    空间查询读取 `api/quota` 的 total/used。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(responses, "GET", _QUOTA_URL, {"errno": 0, "total": 1000, "used": 250})

    quota = adapter.quota(_account())

    assert quota.total == 1000
    assert quota.used == 250
    assert calls[0]["url"] == _QUOTA_URL
    assert calls[0]["headers"]["User-Agent"] == baidu._WEB_UA


def test_list_dir_maps_paths_and_fs_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘列目录：目录用完整路径作标识，文件用 `fs_id` 作标识。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(responses, "GET", _TEMPLATE_URL, _token())
    _queue(
        responses,
        "GET",
        _LIST_URL,
        {
            "errno": 0,
            "list": [
                {"fs_id": 100, "server_filename": "01.mkv", "size": 1024, "isdir": 0, "path": "/01.mkv"},
                {"fs_id": 200, "server_filename": "合集", "size": 0, "isdir": 1, "path": "/合集"},
            ],
        },
    )

    files = adapter.list_dir(_account(), "")

    assert [item.file_id for item in files] == ["100", "/合集"]
    assert files[0].name == "01.mkv"
    assert files[0].size == 1024
    assert files[0].is_dir is False
    assert files[0].parent_id == "/"
    assert files[1].is_dir is True
    assert calls[1]["params"]["dir"] == "/"
    assert calls[1]["params"]["bdstoken"] == "fake-bdstoken"


def test_download_file_uses_locatedownload_with_mobile_ua(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘取链走 `locatedownload`，按完整路径请求并优先选择明文直链。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(
        responses,
        "POST",
        _LOCATE_URL,
        {
            "errno": 0,
            "urls": [
                {"url": "https://d2-ant.baidu.com/encrypted", "encrypt": 1},
                {"url": "https://appall01.baidupcs.com/plain", "encrypt": 0},
            ],
        },
    )

    link = adapter.download_file(_account(), DriveFile(file_id="100", name="01.mkv", size=1024, parent_id="/"))

    assert link.url == "https://appall01.baidupcs.com/plain"
    assert link.size == 1024
    assert link.headers["Cookie"] == _COOKIE
    assert link.headers["User-Agent"] == baidu._CLIENT_UA
    assert calls[0]["params"]["method"] == "locatedownload"
    assert calls[0]["params"]["path"] == "/01.mkv"


def test_download_file_rejects_directory() -> None:
    """
    目录没有直链，直接返回未支持。
    """
    adapter = baidu.BaiduAdapter()

    with pytest.raises(NetdiskError) as captured:
        adapter.download_file(_account(), DriveFile(file_id="/合集", name="合集", is_dir=True))

    assert captured.value.code is NetdiskErrorCode.UNSUPPORTED


def test_share_file_creates_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    创建分享提交 `fs_id` 列表、有效期与提取码。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(responses, "GET", _TEMPLATE_URL, _token())
    _queue(responses, "POST", _SHARE_SET_URL, {"errno": 0, "shorturl": "1AbCdEf", "shareid": 123})

    share = adapter.share_file(
        _account(),
        [DriveFile(file_id="100", name="01.mkv", size=1024)],
        password="a1b2",
        expires_in=7 * 86400,
    )

    assert share.url == "https://pan.baidu.com/s/1AbCdEf"
    assert share.password == "a1b2"
    body = calls[1]["data"]
    assert json.loads(body["fid_list"]) == ["100"]
    assert body["period"] == 7
    assert body["pwd"] == "a1b2"


def test_move_rename_delete_use_filemanager(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    移动、重命名、删除都通过 `filemanager` 按完整路径操作。
    """
    adapter, calls, responses = _install(monkeypatch)
    _queue(responses, "GET", _TEMPLATE_URL, _token())
    for _ in range(3):
        _queue(responses, "POST", _FILEMANAGER_URL, {"errno": 0})

    file = DriveFile(file_id="100", name="01.mkv", size=1024, parent_id="/视频")
    adapter.move_files(_account(), [file], "/归档")
    adapter.rename_file(_account(), file, "02.mkv")
    adapter.delete_files(_account(), [file])

    move, rename, delete = calls[1], calls[2], calls[3]
    assert move["params"]["opera"] == "move"
    assert json.loads(move["data"]["filelist"]) == [
        {"path": "/视频/01.mkv", "dest": "/归档", "newname": "01.mkv"}
    ]
    assert rename["params"]["opera"] == "rename"
    assert json.loads(rename["data"]["filelist"]) == [{"path": "/视频/01.mkv", "newname": "02.mkv"}]
    assert delete["params"]["opera"] == "delete"
    assert json.loads(delete["data"]["filelist"]) == ["/视频/01.mkv"]
