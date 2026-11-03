"""
UC 网盘个人盘协议方法测试。

所有 HTTP 都在 `NetdiskHttp` 层替换为假响应，测试不访问真实网络。
"""

from typing import Any

import pytest

from app.adapters.external.netdisk import base as base_module
from app.adapters.external.netdisk import uc as uc_module
from app.adapters.external.netdisk.uc import (
    UC_CLOUD_DOWNLOAD_URL,
    UC_DELETE_URL,
    UC_FILE_SORT_URL,
    UC_MEMBER_URL,
    UC_MOVE_URL,
    UC_RENAME_URL,
    UC_SHARE_CREATE_URL,
    UC_TASK_URL,
    UCAdapter,
)
from app.domain.netdisk import DriveFile, NetdiskAccountRecord, NetdiskPlatform

ACCOUNT = NetdiskAccountRecord(
    id=7,
    platform=NetdiskPlatform.UC,
    name="测试账号",
    credential={"cookie": "__pus=pus; __puus=puus; uid=42"},
)


def _account() -> NetdiskAccountRecord:
    """
    构造 UC 测试账号。

    :return: 账号记录
    """
    return ACCOUNT


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
) -> tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]]:
    """
    安装假 HTTP 客户端并返回适配器、调用记录与响应队列。

    :param monkeypatch: pytest 打桩工具
    :return: `(适配器, 调用记录, 响应队列)`
    """
    adapter = UCAdapter()
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
    monkeypatch.setattr(uc_module.time, "sleep", lambda _seconds: None)
    return adapter, calls, responses


def test_drive_capabilities(env: tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]]) -> None:
    """
    UC 个人盘应声明全部能力。
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


def test_quota_reads_member_capacity(
    env: tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    空间接口应读取 `use_capacity` 与 `total_capacity`。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        UC_MEMBER_URL,
        {"status": 200, "code": 0, "data": {"use_capacity": 100, "total_capacity": 1000}},
    )
    quota = adapter.quota(_account())
    assert quota.used == 100
    assert quota.total == 1000
    assert calls[0]["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_list_dir_paginates(
    env: tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    目录列表应翻页合并并按文件标识去重。
    """
    adapter, calls, responses = env
    first_page = [
        {
            "fid": f"f{index}",
            "file_name": f"文件{index}.mp4",
            "size": index,
            "dir": False,
            "pdir_fid": "0",
            "updated_at": "1700000000",
        }
        for index in range(50)
    ]
    second_page = [
        {
            "fid": "d1",
            "file_name": "子目录",
            "size": 0,
            "dir": True,
            "pdir_fid": "0",
            "updated_at": "1700000001",
        }
    ]
    _queue(
        responses,
        UC_FILE_SORT_URL,
        {"status": 200, "code": 0, "data": {"list": first_page}},
        {"status": 200, "code": 0, "data": {"list": second_page}},
    )
    files = adapter.list_dir(_account(), "")
    assert len(files) == 51
    assert files[-1].is_dir
    assert files[-1].name == "子目录"
    assert [call["params"]["_page"] for call in calls] == [1, 2]
    assert calls[0]["params"]["pdir_fid"] == "0"


def test_download_file_returns_direct_link(
    env: tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    个人盘下载应返回直链并回退到条目大小。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        UC_CLOUD_DOWNLOAD_URL,
        {"status": 200, "code": 0, "data": [{"download_url": "https://dl.uc/file", "size": 0}]},
    )
    link = adapter.download_file(_account(), _file("f1", size=321))
    assert link.url == "https://dl.uc/file"
    assert link.size == 321
    assert calls[0]["json"] == {"fids": ["f1"]}


def test_share_file_polls_async_task(
    env: tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    创建分享应轮询任务直到拿到 `share_id`。
    """
    adapter, calls, responses = env
    _queue(
        responses,
        UC_SHARE_CREATE_URL,
        {"status": 200, "code": 0, "data": {"task_id": "task-1"}},
    )
    _queue(
        responses,
        UC_TASK_URL,
        {"status": 200, "code": 0, "data": {"status": 1}},
        {"status": 200, "code": 0, "data": {"status": 2, "share_id": "share-9"}},
    )
    share = adapter.share_file(_account(), [_file("f1", "影片.mp4")], password="abcd", expires_in=86400)
    assert share.url == "https://drive.uc.cn/s/share-9"
    assert share.password == "abcd"
    create_body = calls[0]["json"]
    assert create_body["fid_list"] == ["f1"]
    assert create_body["url_type"] == 2
    assert create_body["passcode"] == "abcd"
    assert create_body["expired_type"] == 2
    assert create_body["public_search"] == 0
    assert [call["params"]["task_id"] for call in calls[1:]] == ["task-1", "task-1"]


def test_move_rename_delete(
    env: tuple[UCAdapter, list[dict[str, Any]], dict[str, list[Any]]],
) -> None:
    """
    移动、重命名与删除应发送平台约定的请求体。
    """
    adapter, calls, responses = env
    success = {"status": 200, "code": 0, "data": {}}
    _queue(responses, UC_MOVE_URL, success)
    _queue(responses, UC_RENAME_URL, success)
    _queue(responses, UC_DELETE_URL, success)
    adapter.move_files(_account(), [_file("f1")], "")
    adapter.rename_file(_account(), _file("f1"), "新名称.mp4")
    adapter.delete_files(_account(), [_file("f1")])
    assert calls[0]["json"] == {
        "action_type": 1,
        "to_pdir_fid": "0",
        "filelist": ["f1"],
        "exclude_fids": [],
    }
    assert calls[1]["json"] == {"fid": "f1", "file_name": "新名称.mp4"}
    assert calls[2]["json"] == {
        "action_type": 2,
        "filelist": ["f1"],
        "exclude_fids": [],
    }
