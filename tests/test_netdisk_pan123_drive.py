"""
123 云盘个人盘协议测试。

所有 HTTP 调用都在 `NetdiskHttp` 层替换为预设响应，不访问真实网络。
"""

import base64
from typing import Any

import pytest

from app.adapters.external.netdisk import pan123
from app.adapters.external.netdisk.base import NetdiskHttp
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import DriveFile, DriveQuota, NetdiskPlatform


def _account() -> NetdiskAccountRecord:
    """
    构造 123 云盘账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=7,
        platform=NetdiskPlatform.PAN123,
        name="123 主号",
        credential={"token": "jwt-token"},
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pan123.Pan123Adapter, list[dict[str, Any]], dict[str, list[Any]]]:
    """
    把适配器的 HTTP 客户端替换为按 URL 排队返回的替身。

    :param monkeypatch: pytest 补丁工具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    calls: list[dict[str, Any]] = []
    responses: dict[str, list[Any]] = {}

    def _next(url: str) -> Any:
        queue = responses.get(url) or []
        assert queue, f"未预设响应：{url}"
        return queue.pop(0)

    def get_json(_self, url: str, *, account=None, params=None, headers=None):
        calls.append(
            {
                "method": "GET",
                "url": url,
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
                "json": dict(json or {}),
                "headers": dict(headers or {}),
            }
        )
        return _next(url)

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    return pan123.Pan123Adapter(), calls, responses


def _list_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    """
    构造个人盘列表响应。

    :param items: InfoList 条目
    :return: 响应 JSON
    """
    return {"code": 0, "data": {"Next": "-1", "InfoList": items}}


def _file_item() -> dict[str, Any]:
    """
    构造一个带取链元数据的文件条目。

    :return: InfoList 条目
    """
    return {
        "FileId": "11",
        "FileName": "movie.mkv",
        "Size": "2048",
        "Type": 0,
        "ParentFileId": "0",
        "UpdateAt": "2026-01-02T03:04:05+08:00",
        "S3KeyFlag": "s3-key",
        "Etag": "etag-value",
        "StorageNode": "node-1",
    }


def test_drive_capabilities_declare_all_operations() -> None:
    """
    123 平台声明配额、浏览、下载、分享、移动、重命名与删除能力。
    """
    capabilities = pan123.Pan123Adapter().drive_capabilities

    assert capabilities.quota is True
    assert capabilities.browse is True
    assert capabilities.download is True
    assert capabilities.share is True
    assert capabilities.move is True
    assert capabilities.rename is True
    assert capabilities.delete is True


def test_quota_reads_space_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    配额读取 SpaceUsed 与 SpacePermanent + SpaceTemp。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._USER_INFO_URL] = [
        {"code": 0, "data": {"SpaceUsed": 100, "SpacePermanent": 900, "SpaceTemp": 50}}
    ]

    quota = adapter.quota(_account())

    assert quota == DriveQuota(used=100, total=950)
    assert calls[0]["headers"]["authorization"] == "Bearer jwt-token"


def test_list_dir_maps_personal_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘列表映射为 DriveFile，并携带父目录与修改时间。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._FILE_LIST_URL] = [
        _list_payload(
            [
                _file_item(),
                {
                    "FileId": "12",
                    "FileName": "folder",
                    "Size": 0,
                    "Type": 1,
                    "ParentFileId": "0",
                },
            ]
        )
    ]

    files = adapter.list_dir(_account(), "0")

    assert [item.file_id for item in files] == ["11", "12"]
    assert files[0].name == "movie.mkv"
    assert files[0].size == 2048
    assert files[0].is_dir is False
    assert files[0].updated_at is not None
    assert files[1].is_dir is True
    assert calls[0]["params"]["parentFileId"] == "0"
    assert calls[0]["params"]["next"] == "0"


def test_download_file_uses_metadata_and_decodes_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    下载前按文件标识补齐 S3KeyFlag/Etag，并解码 Base64 直链。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._FILE_LIST_URL] = [_list_payload([_file_item()])]
    responses[pan123._FILE_DOWNLOAD_INFO_URL] = [
        {"code": 0, "data": {"DownloadUrl": base64.b64encode(b"https://cdn.example.com/f").decode()}}
    ]

    link = adapter.download_file(
        _account(),
        DriveFile(file_id="11", name="movie.mkv", size=2048, parent_id="0"),
    )

    assert link.url == "https://cdn.example.com/f"
    assert link.size == 2048
    download_call = next(item for item in calls if item["url"] == pan123._FILE_DOWNLOAD_INFO_URL)
    assert download_call["json"]["fileId"] == 11
    assert download_call["json"]["s3keyFlag"] == "s3-key"
    assert download_call["json"]["etag"] == "etag-value"
    assert download_call["headers"]["authorization"] == "Bearer jwt-token"


def test_share_file_creates_share_with_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    创建分享时提交单个文件 ID、提取码与绝对过期时间。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_CREATE_URL] = [
        {
            "code": 0,
            "data": {
                "ShareKey": "abc-123",
                "shareLinkList": {"list": ["https://www.123pan.com/s/abc-123"]},
            },
        }
    ]

    share = adapter.share_file(
        _account(),
        [DriveFile(file_id="11", name="movie.mkv")],
        password="xy",
        expires_in=7 * 86400,
    )

    assert share.url == "https://www.123pan.com/s/abc-123"
    assert share.password == "xy"
    assert share.expires_at is not None
    body = calls[0]["json"]
    assert body["fileIdList"] == 11
    assert body["sharePwd"] == "xy"
    assert body["expiration"] != pan123._EXPIRATION_FOREVER


def test_share_file_without_expiry_uses_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    有效期为 0 时提交永久过期时间。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._SHARE_CREATE_URL] = [{"code": 0, "data": {"ShareKey": "abc-123"}}]

    share = adapter.share_file(_account(), [DriveFile(file_id="11", name="movie.mkv")])

    assert share.url == "https://www.123pan.com/s/abc-123"
    assert share.expires_at is None
    assert calls[0]["json"]["expiration"] == pan123._EXPIRATION_FOREVER


def test_move_and_rename_send_expected_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    移动与重命名使用个人盘管理接口与数字文件标识。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._FILE_MOD_PID_URL] = [{"code": 0}]
    responses[pan123._FILE_RENAME_URL] = [{"code": 0}]

    adapter.move_files(_account(), [DriveFile(file_id="11", name="movie.mkv")], "99")
    adapter.rename_file(_account(), DriveFile(file_id="11", name="movie.mkv"), "new.mkv")

    move_call = next(item for item in calls if item["url"] == pan123._FILE_MOD_PID_URL)
    assert move_call["json"]["fileIdList"] == [{"FileId": 11}]
    assert move_call["json"]["parentFileId"] == 99
    rename_call = next(item for item in calls if item["url"] == pan123._FILE_RENAME_URL)
    assert rename_call["json"]["fileId"] == 11
    assert rename_call["json"]["fileName"] == "new.mkv"


def test_delete_files_sends_trash_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    删除前补齐回收站接口要求的 S3KeyFlag/Etag 元数据。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan123._FILE_LIST_URL] = [_list_payload([_file_item()])]
    responses[pan123._FILE_TRASH_URL] = [{"code": 0}]

    adapter.delete_files(_account(), [DriveFile(file_id="11", name="movie.mkv", size=2048)])

    trash_call = next(item for item in calls if item["url"] == pan123._FILE_TRASH_URL)
    entry = trash_call["json"]["fileTrashInfoList"][0]
    assert entry["FileId"] == 11
    assert entry["S3KeyFlag"] == "s3-key"
    assert entry["Etag"] == "etag-value"
