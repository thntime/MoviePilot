"""
139（中国移动云盘）个人盘协议方法测试。

所有 HTTP 都在 `NetdiskHttp` 层替换为假响应，测试不访问真实网络。
"""

import base64
from typing import Any

import pytest

from app.adapters.external.netdisk import base as base_module
from app.adapters.external.netdisk.c139 import C139Adapter
from app.domain.netdisk import DriveFile, NetdiskAccountRecord, NetdiskPlatform

_QUOTA_URL = "https://user-njs.yun.139.com/user/disk/quota/detail"
_LIST_URL = "https://personal-kd-njs.yun.139.com/hcy/file/list"
_UPDATE_URL = "https://personal-kd-njs.yun.139.com/hcy/file/update"
_MOVE_URL = "https://personal-kd-njs.yun.139.com/hcy/file/batchMove"
_TRASH_URL = "https://personal-kd-njs.yun.139.com/hcy/recyclebin/batchTrash"
_DOWNLOAD_URL = "https://personal-kd-njs.yun.139.com/hcy/file/getDownloadUrl"
_OUTLINK_URL = "https://yun.139.com/orchestration/personalCloud-rebuild/outlink/v1.0/getOutLink"


def _authorization(number: str = "13800138000") -> str:
    """
    构造 `Basic base64(pc:账号:token)` 形式的 authorization。

    :param number: 手机号
    :return: authorization 文本
    """
    payload = base64.b64encode(f"pc:{number}:auth-token".encode()).decode()
    return f"Basic {payload}"


def _account(number: str = "13800138000") -> NetdiskAccountRecord:
    """
    构造带 authorization 与 `skey` 的 139 账号记录。

    :param number: 手机号
    :return: 账号记录
    """
    cookie = (
        f"authorization={_authorization(number)}; skey=skey-123; "
        f"ORCHES-I-ACCOUNT-ENCRYPT={base64.b64encode(number.encode()).decode()}"
    )
    return NetdiskAccountRecord(
        id=9,
        platform=NetdiskPlatform.PAN139,
        name="139 主号",
        credential={"cookie": cookie},
    )


def _file(file_id: str, name: str = "影片.mp4", size: int = 100) -> DriveFile:
    """
    构造个人盘文件条目。

    :param file_id: 文件标识
    :param name: 文件名
    :param size: 文件大小
    :return: 文件条目
    """
    return DriveFile(file_id=file_id, name=name, size=size)


def _queue(responses: dict[str, list[Any]], url: str, *payloads: dict[str, Any]) -> None:
    """
    按 URL 排队假响应。

    :param responses: 响应队列
    :param url: 接口地址
    :param payloads: 依次返回的响应体
    """
    responses.setdefault(url, []).extend(payloads)


@pytest.fixture()
def env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]]:
    """
    安装假 HTTP 客户端并返回适配器、调用记录与响应队列。

    :param monkeypatch: pytest 打桩工具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    adapter = C139Adapter()
    calls: list[dict[str, Any]] = []
    responses: dict[str, list[Any]] = {}

    def _next(url: str) -> dict[str, Any]:
        queue = responses.get(url) or []
        assert queue, f"未预设响应：{url}"
        return queue.pop(0)

    def get_json(_self, url: str, *, account=None, params=None, headers=None):
        calls.append(
            {
                "method": "GET",
                "url": url,
                "account": account,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            }
        )
        return _next(url)

    def post_json(_self, url: str, *, account=None, json=None, data=None, params=None, headers=None):
        calls.append(
            {
                "method": "POST",
                "url": url,
                "account": account,
                "json": dict(json or {}),
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            }
        )
        return _next(url)

    monkeypatch.setattr(base_module.NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(base_module.NetdiskHttp, "post_json", post_json)
    return adapter, calls, responses


def test_drive_capabilities(env: tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]]) -> None:
    """
    139 个人盘应声明全部能力。
    """
    adapter, _calls, _responses = env
    capabilities = adapter.drive_capabilities
    assert capabilities.quota
    assert capabilities.browse
    assert capabilities.download
    assert capabilities.share
    assert capabilities.move
    assert capabilities.rename
    assert capabilities.delete


def test_quota_converts_megabytes(
    env: tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    空间接口应把 MB 换算为字节，并携带 Cookie 与 `mcloud-skey`。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        _QUOTA_URL,
        {
            "success": True,
            "code": "0",
            "data": {
                "diskSize": 1024,
                "freeDiskSize": 512,
                "quotaList": [{"usedSize": 256}],
            },
        },
    )
    quota = adapter.quota(_account())
    assert quota.total == 1024 * 1024 * 1024
    assert quota.used == 256 * 1024 * 1024
    assert calls[0]["headers"]["Cookie"]
    assert calls[0]["headers"]["mcloud-skey"] == "skey-123"
    assert calls[0]["json"]["commonAccountInfo"]["account"] == "13800138000"


def test_list_dir_follows_cursor(
    env: tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    目录列表应按 `nextPageCursor` 翻页并映射文件夹类型。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        _LIST_URL,
        {
            "success": True,
            "code": "0",
            "data": {
                "items": [
                    {
                        "fileId": "f1",
                        "name": "影片.mp4",
                        "size": 100,
                        "type": "file",
                        "updatedAt": "2026-01-01T00:00:00Z",
                    },
                    {"fileId": "d1", "name": "子目录", "size": 0, "type": "folder"},
                ],
                "nextPageCursor": "cursor-2",
            },
        },
        {
            "success": True,
            "code": "0",
            "data": {
                "items": [{"fileId": "f2", "name": "续集.mp4", "size": 200, "type": "file"}],
            },
        },
    )
    files = adapter.list_dir(_account(), "")
    assert [item.file_id for item in files] == ["f1", "d1", "f2"]
    assert files[1].is_dir
    assert calls[0]["json"]["parentFileId"] == "/"
    assert calls[0]["json"]["pageInfo"]["pageCursor"] is None
    assert calls[1]["json"]["pageInfo"]["pageCursor"] == "cursor-2"


def test_download_file_returns_direct_link(
    env: tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    个人盘下载应返回 OBS 直链且不泄露账号 Cookie。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        _DOWNLOAD_URL,
        {"success": True, "code": "0", "data": {"url": "https://obs.139/file", "size": 0}},
    )
    link = adapter.download_file(_account(), _file("f1", size=321))
    assert link.url == "https://obs.139/file"
    assert link.size == 321
    assert "Cookie" not in link.headers
    assert calls[0]["json"] == {"fileId": "f1"}


def test_share_file_returns_link_and_password(
    env: tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    创建分享应回传服务端生成的链接与提取码，并按天映射有效期。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        _OUTLINK_URL,
        {
            "success": True,
            "code": "0",
            "data": {
                "getOutLinkRes": {
                    "getOutLinkResSet": [
                        {
                            "linkUrl": "https://yun.139.com/shareweb/#/w/i/share-1",
                            "passwd": "xy12",
                        }
                    ]
                }
            },
        },
    )
    share = adapter.share_file(_account(), [_file("f1")], password="", expires_in=7 * 86400)
    assert share.url == "https://yun.139.com/shareweb/#/w/i/share-1"
    assert share.password == "xy12"
    assert share.expires_at is not None
    request = calls[0]["json"]["getOutLinkReq"]
    assert request["coIDLst"] == ["f1"]
    assert request["period"] == 7
    assert request["commonAccountInfo"]["account"] == "13800138000"
    assert calls[0]["headers"]["Cookie"]
    assert calls[0]["headers"]["mcloud-skey"] == "skey-123"


def test_move_rename_delete(
    env: tuple[C139Adapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    移动、重命名与删除应发送平台约定的请求体。
    """
    adapter, calls, responses = env
    success = {"success": True, "code": "0", "data": {"taskId": "task-1"}}
    _queue(responses, _MOVE_URL, success)
    _queue(responses, _UPDATE_URL, success)
    _queue(responses, _TRASH_URL, success)
    adapter.move_files(_account(), [_file("f1")], "")
    adapter.rename_file(_account(), _file("f1"), "新名称.mp4")
    adapter.delete_files(_account(), [_file("f1")])
    assert calls[0]["json"] == {"fileIds": ["f1"], "toParentFileId": "/"}
    assert calls[1]["json"] == {"fileId": "f1", "name": "新名称.mp4", "description": ""}
    assert calls[2]["json"] == {"fileIds": ["f1"]}
