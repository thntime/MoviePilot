"""
天翼云盘个人盘协议方法测试。

所有 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，Cookie 使用假值。
"""

import json

import pytest

from app.adapters.external.netdisk import tianyi
from app.domain.netdisk import (
    DriveCapabilities,
    DriveFile,
    NetdiskAccountRecord,
    NetdiskPlatform,
)


def _account() -> NetdiskAccountRecord:
    """
    构造天翼云盘测试账号。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=1,
        platform=NetdiskPlatform.TIANYI,
        name="t",
        credential={"COOKIE_LOGIN_USER": "c"},
    )


def test_drive_capabilities_cover_all_operations() -> None:
    """天翼声明个人盘全部七项能力。"""
    capabilities = tianyi.TianyiAdapter.drive_capabilities
    assert capabilities == DriveCapabilities(True, True, True, True, True, True, True)


def test_quota_maps_capacity_and_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """配额按总量减可用量计算。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(adapter.http, "get_json", lambda *a, **k: {"capacity": 1000, "available": 400})
    quota = adapter.quota(_account())
    assert (quota.total, quota.used) == (1000, 600)


def test_list_dir_returns_drive_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """个人盘目录返回文件与子目录。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(
        adapter.http,
        "get_json",
        lambda *a, **k: {
            "fileListAO": {
                "fileList": [{"id": "11", "name": "a.mp4", "size": 10}],
                "folderList": [{"id": "22", "name": "dir"}],
            }
        },
    )
    files = adapter.list_dir(_account(), "")
    assert [(f.file_id, f.is_dir, f.parent_id) for f in files] == [("11", False, "-11"), ("22", True, "-11")]


def test_download_file_returns_direct_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """个人盘下载补全协议并带上账号请求头。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(
        adapter.http,
        "get_json",
        lambda *a, **k: {"downloadUrl": "//download.cloud.189.cn/x"},
    )
    link = adapter.download_file(_account(), DriveFile(file_id="11", name="a.mp4", size=10))
    assert link.url == "https://download.cloud.189.cn/x"
    assert link.size == 10


def test_share_file_builds_share_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建分享读取 shareUrl 与提取码。"""
    adapter = tianyi.TianyiAdapter()
    calls: list[dict] = []

    def fake_get(path, account=None, params=None, **kwargs):
        calls.append(dict(params or {}))
        return {"shareUrl": "https://cloud.189.cn/t/SH1", "accessCode": "ab12"}

    monkeypatch.setattr(adapter.http, "get_json", fake_get)
    share = adapter.share_file(_account(), [DriveFile(file_id="11", name="a.mp4")], expires_in=7)
    assert share.url == "https://cloud.189.cn/t/SH1"
    assert share.password == "ab12"
    assert calls[0]["fileId"] == "11"
    assert calls[0]["shareType"] == 3
    assert calls[0]["expireTime"] == 7


def test_batch_operations_send_string_ids_and_is_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量任务 fileId 使用字符串且 isFolder 必填。"""
    adapter = tianyi.TianyiAdapter()
    posts: list[tuple[str, dict]] = []

    def fake_post(path, account=None, data=None, **kwargs):
        posts.append((path, dict(data or {})))
        return {"taskId": "T1", "taskStatus": 4}

    monkeypatch.setattr(adapter.http, "post_json", fake_post)
    account = _account()
    files = [DriveFile(file_id="11", name="a.mp4"), DriveFile(file_id="22", name="dir", is_dir=True)]

    adapter.delete_files(account, files)
    delete = posts[0]
    assert delete[1]["type"] == "DELETE"
    assert json.loads(delete[1]["taskInfos"]) == [
        {"fileId": "11", "fileName": "a.mp4", "isFolder": 0},
        {"fileId": "22", "fileName": "dir", "isFolder": 1},
    ]

    adapter.move_files(account, files, "-11")
    assert posts[1][1]["type"] == "MOVE"
    assert posts[1][1]["targetFolderId"] == "-11"


def test_rename_routes_file_and_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """文件走 renameFile，目录走 renameFolder。"""
    adapter = tianyi.TianyiAdapter()
    posts: list[str] = []
    monkeypatch.setattr(adapter.http, "post_json", lambda path, **k: posts.append(path) or {})
    account = _account()
    adapter.rename_file(account, DriveFile(file_id="11", name="a"), "b")
    adapter.rename_file(account, DriveFile(file_id="22", name="d", is_dir=True), "e")
    assert posts[0].endswith("renameFile.action")
    assert posts[1].endswith("renameFolder.action")
