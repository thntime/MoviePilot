"""
115 网盘个人盘协议测试。

所有 HTTP 调用都在 `NetdiskHttp` 层替换为预设响应，Cookie 使用假值，不访问真实网络。
"""

from typing import Any

import pytest

from app.adapters.external.netdisk import pan115 as pan115_module
from app.adapters.external.netdisk.base import NetdiskHttp
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import DriveFile, DriveQuota, NetdiskError, NetdiskErrorCode, NetdiskPlatform

_COOKIE = "UID=103616022_A1_169; SEID=fake-seid; CID=fake-cid; KID=fake-kid"


def _account() -> NetdiskAccountRecord:
    """
    构造 115 账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=9,
        platform=NetdiskPlatform.PAN115,
        name="115 主号",
        credential={"cookie": _COOKIE},
        is_primary=True,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pan115_module.Pan115Adapter, list[dict[str, Any]], dict[str, list[Any]]]:
    """
    把适配器的 HTTP 客户端替换为按 URL 排队返回的替身。

    :param monkeypatch: pytest 补丁工具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    calls: list[dict[str, Any]] = []
    responses: dict[str, list[Any]] = {}

    def _next(url: str) -> Any:
        queue = responses.get(url)
        if queue is None:
            for key in sorted(responses, key=len, reverse=True):
                if url.startswith(key):
                    queue = responses[key]
                    break
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
                "data": dict(data or {}),
                "json": dict(json or {}),
                "headers": dict(headers or {}),
            }
        )
        result = _next(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    return pan115_module.Pan115Adapter(), calls, responses


def _file_item() -> dict[str, Any]:
    """
    构造一个带 pickcode 的个人盘文件条目。

    :return: files 接口条目
    """
    return {
        "fid": "f1",
        "n": "movie.mkv",
        "s": 2048,
        "cid": "0",
        "pc": "pick-code-1",
        "t": "2026-01-02 03:04:05",
    }


def test_drive_capabilities_declare_all_operations() -> None:
    """
    115 平台声明配额、浏览、下载、分享、移动、重命名与删除能力。
    """
    capabilities = pan115_module.Pan115Adapter().drive_capabilities

    assert capabilities.quota is True
    assert capabilities.browse is True
    assert capabilities.download is True
    assert capabilities.share is True
    assert capabilities.move is True
    assert capabilities.rename is True
    assert capabilities.delete is True


def test_quota_reads_space_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    配额读取 space_info 的已用与总空间。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._INDEX_INFO_URL] = [
        {
            "state": True,
            "data": {
                "space_info": {
                    "all_total": {"size": 1000},
                    "all_use": {"size": 400},
                }
            },
        }
    ]

    quota = adapter.quota(_account())

    assert quota == DriveQuota(used=400, total=1000)
    assert calls[0]["params"] == {}


def test_list_dir_maps_files_and_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    个人盘列表区分文件与目录，并保留父目录与修改时间。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._FILES_URL] = [
        {
            "state": True,
            "count": 2,
            "data": [
                _file_item(),
                {"cid": "d1", "pid": "0", "n": "folder"},
            ],
        }
    ]

    files = adapter.list_dir(_account(), "0")

    assert [item.file_id for item in files] == ["f1", "d1"]
    assert files[0].name == "movie.mkv"
    assert files[0].size == 2048
    assert files[0].is_dir is False
    assert files[0].updated_at is not None
    assert files[1].is_dir is True
    assert calls[0]["params"]["cid"] == "0"
    assert calls[0]["params"]["aid"] == "1"


def test_download_file_falls_back_to_web_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    客户端通道失败后回退网页端直链，并带上登录 Cookie。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._FILES_URL] = [{"state": True, "count": 1, "data": [_file_item()]}]
    responses[pan115_module._APP_DOWNLOAD_URL] = [
        NetdiskError(NetdiskErrorCode.UNKNOWN, "客户端通道不可用", platform=NetdiskPlatform.PAN115)
    ]
    responses[pan115_module._FILE_DOWNLOAD_URL] = [
        {
            "state": True,
            "data": {"file_url": "https://cdn.example.com/115", "file_name": "movie.mkv", "file_size": 2048},
        }
    ]

    link = adapter.download_file(
        _account(),
        DriveFile(file_id="f1", name="movie.mkv", size=2048, parent_id="0"),
    )

    assert link.url == "https://cdn.example.com/115"
    assert link.size == 2048
    assert "UID=103616022_A1_169" in link.headers["Cookie"]
    web_call = next(item for item in calls if item["url"] == pan115_module._FILE_DOWNLOAD_URL)
    assert web_call["params"]["pickcode"] == "pick-code-1"


def test_share_file_creates_and_updates_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    创建分享后按天数设置有效期，并使用服务端返回的分享码与提取码。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._ALLOW_PROTOCOL_URL] = [{"state": True}]
    responses[pan115_module._SHARE_SEND_URL] = [
        {
            "state": True,
            "data": {
                "share_code": "swabc",
                "share_url": "https://115cdn.com/s/swabc",
                "receive_code": "abcd",
            },
        }
    ]
    responses[pan115_module._SHARE_UPDATE_URL] = [{"state": True}]

    share = adapter.share_file(
        _account(),
        [DriveFile(file_id="f1", name="movie.mkv")],
        expires_in=7 * 86400,
    )

    assert share.url == "https://115cdn.com/s/swabc"
    assert share.password == "abcd"
    send_call = next(item for item in calls if item["url"] == pan115_module._SHARE_SEND_URL)
    assert send_call["data"]["user_id"] == "103616022"
    assert send_call["data"]["file_ids"] == "f1"
    update_call = next(item for item in calls if item["url"] == pan115_module._SHARE_UPDATE_URL)
    assert update_call["data"]["share_duration"] == "7"


def test_share_file_without_expiry_uses_long_term(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    有效期为 0 时提交长期档位 -1。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._ALLOW_PROTOCOL_URL] = [{"state": True}]
    responses[pan115_module._SHARE_SEND_URL] = [
        {"state": True, "data": {"share_code": "swabc", "share_url": "https://115cdn.com/s/swabc"}}
    ]
    responses[pan115_module._SHARE_UPDATE_URL] = [{"state": True}]

    share = adapter.share_file(_account(), [DriveFile(file_id="f1", name="movie.mkv")], password="pwd")

    assert share.password == "pwd"
    update_call = next(item for item in calls if item["url"] == pan115_module._SHARE_UPDATE_URL)
    assert update_call["data"]["share_duration"] == "-1"


def test_move_files_polls_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    移动提交 fid 数组，并在返回 move_proid 时轮询到完成。
    """
    adapter, calls, responses = _install(monkeypatch)
    monkeypatch.setattr(pan115_module.Pan115Adapter, "sleep", staticmethod(lambda _seconds: None))
    responses[pan115_module._FILE_MOVE_URL] = [{"state": True, "data": {"move_proid": "p1"}}]
    responses[pan115_module._MOVE_PROGRESS_URL] = [
        {"state": True, "data": {"percent": 100, "count": 0}}
    ]

    adapter.move_files(_account(), [DriveFile(file_id="f1", name="movie.mkv")], "d1")

    move_call = next(item for item in calls if item["url"] == pan115_module._FILE_MOVE_URL)
    assert move_call["data"]["pid"] == "d1"
    assert move_call["data"]["fid[0]"] == "f1"
    progress_call = next(item for item in calls if item["url"] == pan115_module._MOVE_PROGRESS_URL)
    assert progress_call["params"]["move_proid"] == "p1"


def test_rename_file_sends_batch_rename_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    重命名同时提交 fid 与 files_new_name 映射。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._FILE_RENAME_URL] = [{"state": True}]

    adapter.rename_file(_account(), DriveFile(file_id="f1", name="movie.mkv"), "new.mkv")

    rename_call = calls[0]
    assert rename_call["data"]["fid"] == "f1"
    assert rename_call["data"]["file_name"] == "new.mkv"
    assert rename_call["data"]["files_new_name[f1]"] == "new.mkv"


def test_delete_files_retries_after_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    删除遇到 errno=800007 时按文档重试一次。
    """
    adapter, calls, responses = _install(monkeypatch)
    responses[pan115_module._FILE_DELETE_URL] = [
        {"state": False, "errno": 800007, "error": "需要二次确认"},
        {"state": True},
    ]

    adapter.delete_files(_account(), [DriveFile(file_id="f1", name="movie.mkv")])

    delete_calls = [item for item in calls if item["url"] == pan115_module._FILE_DELETE_URL]
    assert len(delete_calls) == 2
    assert delete_calls[0]["data"]["ignore_warn"] == "1"
    assert delete_calls[0]["data"]["fid[0]"] == "f1"
