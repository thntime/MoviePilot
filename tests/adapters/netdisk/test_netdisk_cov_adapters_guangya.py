"""
光鸭云盘适配器的覆盖率补充测试。

覆盖既有测试未触达的分支：HTTP 都在 `NetdiskHttp` 层打桩，不访问网络。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.external.netdisk import guangya as guangya_module
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

_TOKEN = "fake-access-token"


def _account(credential: dict[str, str] | None = None) -> NetdiskAccountRecord:
    """
    构造光鸭测试账号。

    :param credential: 凭据字段，默认只含 token
    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=11,
        platform=NetdiskPlatform.GUANGYA,
        name="光鸭测试账号",
        credential=credential if credential is not None else {"token": _TOKEN},
    )


def _ref(share_token: str = "share-token") -> ShareRef:
    """
    构造光鸭分享引用。

    :param share_token: 分享访问令牌
    :return: 分享引用
    """
    return ShareRef(
        platform=NetdiskPlatform.GUANGYA,
        share_id="abc123",
        password="x1y2",
        url="https://www.guangyapan.com/s/abc123",
        share_token=share_token,
    )


def _file() -> ShareFile:
    """
    构造分享文件。

    :return: 文件条目
    """
    return ShareFile(file_id="f1", name="01.mkv", size=1024, is_dir=False, parent_id="")


def _patch_http(monkeypatch, handler) -> None:
    """
    用统一路由替换 NetdiskHttp 的 JSON 方法。

    :param monkeypatch: pytest 打桩夹具
    :param handler: 接收 (方法, http, 地址, 账号, 参数, 请求体, 请求头) 的路由函数
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
        return handler("GET", self, url, account, params, None, headers)

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
        del params
        return handler("POST", self, url, account, None, json if json is not None else data, headers)

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)


# ---- 纯函数 ----


def test_normalize_account_forms() -> None:
    """
    手机号补 `+86 `，已带前缀的去重，空值与非常规输入原样返回。
    """
    assert guangya_module._normalize_account("") == ""  # pylint: disable=protected-access
    assert guangya_module._normalize_account("13800138000") == "+86 13800138000"  # pylint: disable=protected-access
    assert guangya_module._normalize_account("+86 138-0013-8000") == "+86 13800138000"  # pylint: disable=protected-access
    assert guangya_module._normalize_account("user@example.com") == "user@example.com"  # pylint: disable=protected-access


def test_is_success_reads_error_field() -> None:
    """
    响应带 error 字段时视为失败。
    """
    assert guangya_module._is_success({"error": "bad"}) is False  # pylint: disable=protected-access
    assert guangya_module._is_success({"code": "0"}) is True  # pylint: disable=protected-access


def test_token_fields_handles_missing_and_absolute_expiry() -> None:
    """
    缺少 access_token 返回空字典；没有 expires_in 时使用绝对过期时间。
    """
    assert guangya_module._token_fields({}) == {}  # pylint: disable=protected-access
    assert guangya_module._token_fields({"expire_time": "2030-01-01T00:00:00"}) == {}  # pylint: disable=protected-access

    fields = guangya_module._token_fields({"accessToken": "t", "expires_at": "2030-01-01T00:00:00"})  # pylint: disable=protected-access

    assert fields == {"token": "t", "expires_at": "2030-01-01T00:00:00"}


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("unauthenticated", "", NetdiskErrorCode.AUTH_EXPIRED),
        ("invalid_token", "", NetdiskErrorCode.AUTH_EXPIRED),
        ("429", "", NetdiskErrorCode.RATE_LIMITED),
        ("too_many_requests", "", NetdiskErrorCode.RATE_LIMITED),
        ("", "操作频繁", NetdiskErrorCode.RATE_LIMITED),
        ("captcha_required", "", NetdiskErrorCode.RISK_BLOCKED),
        ("", "需要验证码", NetdiskErrorCode.RISK_BLOCKED),
        ("", "触发风控", NetdiskErrorCode.RISK_BLOCKED),
        ("", "未知错误", NetdiskErrorCode.UNKNOWN),
    ],
)
def test_classify_maps_auth_rate_and_risk_codes(code: str, message: str, expected: NetdiskErrorCode) -> None:
    """
    业务码与提示语收敛到统一分类。
    """
    assert guangya_module._classify(code, message) is expected  # pylint: disable=protected-access


def test_normalize_error_maps_antileech_message() -> None:
    """
    未分类异常带防盗链提示时归类为直链过期。
    """
    error = NetdiskError(NetdiskErrorCode.UNKNOWN, "被防盗链拦截", platform=NetdiskPlatform.GUANGYA)

    assert guangya_module._normalize_error(error).code is NetdiskErrorCode.LINK_EXPIRED  # pylint: disable=protected-access

    untouched = NetdiskError(NetdiskErrorCode.UNKNOWN, "其他错误", platform=NetdiskPlatform.GUANGYA)
    assert guangya_module._normalize_error(untouched) is untouched  # pylint: disable=protected-access


def test_list_array_and_parse_file_skip_unusable_values() -> None:
    """
    列表字段缺失返回空列表；非字典或缺少标识/名称的条目返回 None。
    """
    assert guangya_module._list_array({"other": []}) == []  # pylint: disable=protected-access
    assert guangya_module._list_array({"items": [1]}) == [1]  # pylint: disable=protected-access
    assert guangya_module._parse_file("bad") is None  # pylint: disable=protected-access
    assert guangya_module._parse_file({"fileId": "f1"}) is None  # pylint: disable=protected-access


def test_parse_file_marks_directories_by_flag() -> None:
    """
    目录可由 isFolder 标记识别，大小归零。
    """
    parsed = guangya_module._parse_file({"id": "d1", "name": "目录", "isFolder": True, "size": 99})  # pylint: disable=protected-access

    assert parsed is not None
    assert parsed.is_dir is True
    assert parsed.size == 0


# ---- 分享令牌与目录 ----


def test_share_token_requires_access_token(monkeypatch) -> None:
    """
    令牌接口成功但缺少 accessToken 时报分享失效。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        返回缺少 accessToken 的响应。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        guangya_module.GuangYaAdapter().share_detail(_ref(share_token=""), _account())

    assert error.value.code is NetdiskErrorCode.SHARE_INVALID


def test_share_title_swallows_failures(monkeypatch) -> None:
    """
    分享摘要接口失败时标题为空，不影响取链。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        令牌成功，摘要失败，目录为空。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._SHARE_TOKEN_URL:
            return {"success": True, "code": "0", "data": {"accessToken": "st"}}
        if url == guangya_module._SHARE_SUMMARY_URL:
            return {"success": False, "code": "500", "msg": "服务异常"}
        return {"success": True, "code": "0", "data": {"hasMore": False, "list": []}}

    _patch_http(monkeypatch, handler)

    detail = guangya_module.GuangYaAdapter().share_detail(_ref(share_token=""), _account())

    assert detail.title == "光鸭分享"
    assert detail.files == ()


def test_list_share_dedupes_and_stops_without_cursor(monkeypatch) -> None:
    """
    重复文件标识只保留一次；hasMore 为真但无游标时结束翻页。
    """
    pages = [
        {
            "hasMore": True,
            "cursor": "c2",
            "list": [
                {"fileId": "f1", "fileName": "01.mkv"},
                {"fileId": "f1", "fileName": "01.mkv"},
            ],
        },
        {"hasMore": False, "list": [{"fileId": "f2", "fileName": "02.mkv"}]},
    ]

    def handler(method, http, url, account, params, body, headers):
        """
        依次返回预设页。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": pages.pop(0)}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    files = adapter.list_files(_ref(), "", _account())

    assert [item.file_id for item in files] == ["f1", "f2"]


def test_list_share_stops_when_cursor_missing(monkeypatch) -> None:
    """
    hasMore 为真但没有游标时不再请求下一页。
    """
    calls: list[str] = []

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求并返回无游标的页。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        calls.append("page")
        return {
            "success": True,
            "code": "0",
            "data": {"hasMore": True, "list": [{"fileId": "f1", "fileName": "01.mkv"}]},
        }

    _patch_http(monkeypatch, handler)

    files = guangya_module.GuangYaAdapter().list_files(_ref(), "", _account())

    assert [item.file_id for item in files] == ["f1"]
    assert calls == ["page"]


def test_share_link_requires_download_url(monkeypatch) -> None:
    """
    免登录取链缺少地址时报错。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        返回空 data。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.resolve(_ref(), _file(), _account())

    assert "下载链接为空" in error.value.message


# ---- 转存链路 ----


def test_account_token_requires_token() -> None:
    """
    账号缺失或没有 token 时抛出登录失效。
    """
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as missing:
        adapter._account_token(None)  # pylint: disable=protected-access
    assert missing.value.code is NetdiskErrorCode.AUTH_EXPIRED

    with pytest.raises(NetdiskError):
        adapter._account_token(_account({"cookie": "a=1"}))  # pylint: disable=protected-access


def test_create_dir_requires_identifier(monkeypatch) -> None:
    """
    创建目录响应缺少标识或误返回文件类型时报错。
    """
    responses = [{"success": True, "code": "0", "data": {}}, {"success": True, "code": "0", "data": {"fileId": "x", "resType": "1"}}]

    def handler(method, http, url, account, params, body, headers):
        """
        返回预设响应。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return responses.pop(0)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    for _ in range(2):
        with pytest.raises(NetdiskError) as error:
            adapter._create_dir(_TOKEN)  # pylint: disable=protected-access
        assert "缺少目录标识" in error.value.message


def test_restore_waits_for_async_task(monkeypatch) -> None:
    """
    转存返回 taskId 时轮询任务状态。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        转存返回任务标识。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {"taskId": "task-1"}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()
    waited: list[str] = []
    monkeypatch.setattr(adapter, "_wait_task", lambda token, task_id: waited.append(task_id))

    adapter._restore(_TOKEN, "share-token", _file(), "dir-1")  # pylint: disable=protected-access

    assert waited == ["task-1"]


def test_wait_task_reports_failures(monkeypatch) -> None:
    """
    任务失败与超时都抛出未分类错误。
    """
    responses = [
        {"success": True, "code": "0", "data": {"status": 3, "detail": {"msg": "转存失败"}}},
        {"success": True, "code": "0", "data": {"status": 2, "detail": {"code": 7, "msg": "目标已存在"}}},
    ]

    def handler(method, http, url, account, params, body, headers):
        """
        返回预设任务状态。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return responses.pop(0)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)

    with pytest.raises(NetdiskError) as failed:
        adapter._wait_task(_TOKEN, "task-1")  # pylint: disable=protected-access
    assert "转存失败" in failed.value.message

    with pytest.raises(NetdiskError) as conflict:
        adapter._wait_task(_TOKEN, "task-1")  # pylint: disable=protected-access
    assert "目标已存在" in conflict.value.message


def test_wait_task_times_out(monkeypatch) -> None:
    """
    任务始终未完成时抛出超时提示。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回处理中状态。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {"status": 1}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)

    with pytest.raises(NetdiskError) as error:
        adapter._wait_task(_TOKEN, "task-1")  # pylint: disable=protected-access

    assert "仍在处理中" in error.value.message


def test_list_cloud_handles_non_mapping_and_page_stops(monkeypatch) -> None:
    """
    data 非字典时结束；无 total 且不足一页时结束；重复标识只保留一次。
    """
    responses: list[Any] = [
        {"success": True, "code": "0", "data": "bad"},
        {
            "success": True,
            "code": "0",
            "data": {"list": [{"fileId": "f1", "fileName": "a.mkv"}, {"fileId": "f1", "fileName": "a.mkv"}]},
        },
        {"success": True, "code": "0", "data": {"total": 1, "list": [{"fileId": "f1", "fileName": "a.mkv"}]}},
    ]

    def handler(method, http, url, account, params, body, headers):
        """
        依次返回预设响应。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return responses.pop(0)

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    assert adapter._list_cloud(_TOKEN, "") == []  # pylint: disable=protected-access
    files = adapter._list_cloud(_TOKEN, "")  # pylint: disable=protected-access
    assert [item.file_id for item in files] == ["f1"]


def test_wait_saved_retries_and_reports_conflicts(monkeypatch) -> None:
    """
    列目录异常时继续轮询；同名多文件与大小不一致都报错。
    """
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)
    target = ShareFile(file_id="new-1", name="01.mkv", size=1024)

    calls = {"count": 0}

    def flaky(token, dir_id):
        """
        第一次抛异常，之后返回目标文件。

        :param token: 令牌
        :param dir_id: 目录
        :return: 文件列表
        """
        del token, dir_id
        calls["count"] += 1
        if calls["count"] == 1:
            raise NetdiskError(NetdiskErrorCode.UNKNOWN, "网络抖动", platform=NetdiskPlatform.GUANGYA)
        return [target]

    monkeypatch.setattr(adapter, "_list_cloud", flaky)

    assert adapter._wait_saved(_TOKEN, "dir-1", _file()) is target  # pylint: disable=protected-access

    monkeypatch.setattr(adapter, "_list_cloud", lambda token, dir_id: [target, target])
    with pytest.raises(NetdiskError) as duplicate:
        adapter._wait_saved(_TOKEN, "dir-1", _file())  # pylint: disable=protected-access
    assert "同名文件" in duplicate.value.message

    monkeypatch.setattr(
        adapter,
        "_list_cloud",
        lambda token, dir_id: [ShareFile(file_id="new-1", name="01.mkv", size=1)],
    )
    with pytest.raises(NetdiskError) as mismatch:
        adapter._wait_saved(_TOKEN, "dir-1", _file())  # pylint: disable=protected-access
    assert "大小不一致" in mismatch.value.message


def test_wait_saved_returns_none_on_timeout(monkeypatch) -> None:
    """
    轮询次数用尽仍未出现目标文件时返回 None。
    """
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(adapter, "sleep", lambda seconds: None)
    monkeypatch.setattr(adapter, "_list_cloud", lambda token, dir_id: [])

    assert adapter._wait_saved(_TOKEN, "dir-1", _file()) is None  # pylint: disable=protected-access


def test_file_link_requires_download_url(monkeypatch) -> None:
    """
    个人盘取链缺少地址时报错。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回空 data。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter._file_link(_TOKEN, _file())  # pylint: disable=protected-access

    assert "下载链接为空" in error.value.message


def test_transfer_link_cleans_up_when_target_invisible(monkeypatch) -> None:
    """
    转存已提交但目标不可见时删除临时目录并报错。
    """
    adapter = guangya_module.GuangYaAdapter()
    deleted: list[list[str]] = []
    monkeypatch.setattr(adapter, "_create_dir", lambda token: "dir-1")
    monkeypatch.setattr(adapter, "_restore", lambda token, share_token, file, dir_id: None)
    monkeypatch.setattr(adapter, "_wait_saved", lambda token, dir_id, file: None)
    monkeypatch.setattr(adapter, "_safe_delete", lambda token, ids: deleted.append(list(ids)))

    with pytest.raises(NetdiskError) as error:
        adapter._transfer_link(_file(), "share-token", _account())  # pylint: disable=protected-access

    assert "暂不可见" in error.value.message
    assert deleted == [["dir-1"]]


def test_transfer_link_cleans_up_on_unexpected_error(monkeypatch) -> None:
    """
    转存链路抛出异常时删除临时目录并继续抛出。
    """
    adapter = guangya_module.GuangYaAdapter()
    deleted: list[list[str]] = []
    monkeypatch.setattr(adapter, "_create_dir", lambda token: "dir-1")

    def boom(token, share_token, file, dir_id):
        """
        模拟转存失败。

        :param token: 令牌
        :param share_token: 分享令牌
        :param file: 文件
        :param dir_id: 目录
        :return: 不会返回
        """
        del token, share_token, file, dir_id
        raise RuntimeError("boom")

    monkeypatch.setattr(adapter, "_restore", boom)
    monkeypatch.setattr(adapter, "_safe_delete", lambda token, ids: deleted.append(list(ids)))

    with pytest.raises(RuntimeError):
        adapter._transfer_link(_file(), "share-token", _account())  # pylint: disable=protected-access

    assert deleted == [["dir-1"]]


def test_resolve_transfer_first_succeeds(monkeypatch) -> None:
    """
    transfer_first 时直接转存取链。
    """
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(
        adapter,
        "_transfer_link",
        lambda file, share_token, account: (
            TransferHandle(platform=NetdiskPlatform.GUANGYA, account_id=11, directory_id="dir-1"),
            {"url": "https://cdn/saved.mkv", "size": 1024},
        ),
    )

    link = adapter.resolve(_ref(), _file(), _account(), transfer_first=True)

    assert link.url == "https://cdn/saved.mkv"
    assert link.cleanup_handle is not None


def test_resolve_transfer_first_error_paths(monkeypatch) -> None:
    """
    transfer_first 失败时按 allow_transfer 决定抛出还是回退免登录取链。
    """
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(
        adapter,
        "_transfer_link",
        lambda file, share_token, account: (_ for _ in ()).throw(
            NetdiskError(NetdiskErrorCode.UNKNOWN, "转存失败", platform=NetdiskPlatform.GUANGYA)
        ),
    )

    with pytest.raises(NetdiskError):
        adapter.resolve(_ref(), _file(), _account(), transfer_first=True, allow_transfer=False)

    monkeypatch.setattr(
        adapter,
        "_share_link",
        lambda file, share_token: {"url": "https://cdn/direct.mkv", "size": 1024},
    )

    link = adapter.resolve(_ref(), _file(), _account(), transfer_first=True)

    assert link.url == "https://cdn/direct.mkv"


def test_cleanup_handles_missing_directory_and_token(monkeypatch) -> None:
    """
    句柄无目录时直接返回；账号无令牌时记录失败。
    """
    adapter = guangya_module.GuangYaAdapter()
    calls: list[str] = []
    monkeypatch.setattr(adapter, "_safe_delete", lambda token, ids: calls.append("delete"))
    failures: list[str] = []
    monkeypatch.setattr(adapter, "log_failure", lambda action, error, account: failures.append(action))

    adapter.cleanup(TransferHandle(platform=NetdiskPlatform.GUANGYA, account_id=11), _account())
    adapter.cleanup(
        TransferHandle(platform=NetdiskPlatform.GUANGYA, account_id=11, directory_id="dir-1"),
        _account({"cookie": "a=1"}),
    )

    assert calls == []
    assert failures == ["清理临时目录"]


def test_delete_ignores_empty_list(monkeypatch) -> None:
    """
    空标识列表不发起删除请求。
    """
    calls: list[str] = []

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求。

        :return: 成功响应
        """
        del method, http, account, params, body, headers
        calls.append(url)
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)

    guangya_module.GuangYaAdapter()._delete(_TOKEN, [])  # pylint: disable=protected-access

    assert calls == []


# ---- 个人盘 ----


def test_drive_mutations_ignore_empty_lists(monkeypatch) -> None:
    """
    移动与删除在空列表时不发请求。
    """
    calls: list[str] = []

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求。

        :return: 成功响应
        """
        del method, http, account, params, body, headers
        calls.append(url)
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    adapter.move_files(_account(), [], "dir-1")
    adapter.delete_files(_account(), [])

    assert calls == []


def test_drive_mutations_wait_for_async_tasks(monkeypatch) -> None:
    """
    移动/重命名/删除返回 taskId 时轮询任务。
    """
    waited: list[str] = []

    def handler(method, http, url, account, params, body, headers):
        """
        返回任务标识。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {"taskId": "task-1"}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(adapter, "_wait_task", lambda token, task_id: waited.append(task_id))
    files = [DriveFile(file_id="f1", name="a.mkv")]

    adapter.move_files(_account(), files, "dir-1")
    adapter.rename_file(_account(), files[0], "b.mkv")
    adapter.delete_files(_account(), files)

    assert waited == ["task-1", "task-1", "task-1"]


def test_share_file_requires_files_and_link(monkeypatch) -> None:
    """
    空文件列表报错；响应缺少分享链接且没有 shareId 时报错。
    """
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as empty:
        adapter.share_file(_account(), [])
    assert empty.value.code is NetdiskErrorCode.UNSUPPORTED

    def handler(method, http, url, account, params, body, headers):
        """
        返回空 data。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as missing:
        adapter.share_file(_account(), [DriveFile(file_id="f1", name="a.mkv")])
    assert "缺少分享链接" in missing.value.message


def test_share_file_builds_url_from_share_id(monkeypatch) -> None:
    """
    只有 shareId 时按网页规则拼接分享地址。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回 shareId 与提取码。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {"shareId": "s1", "code": "abcd"}}

    _patch_http(monkeypatch, handler)

    share = guangya_module.GuangYaAdapter().share_file(_account(), [DriveFile(file_id="f1", name="a.mkv")])

    assert share.url == "https://www.guangyapan.com/s/s1"
    assert share.password == "abcd"


# ---- 账号 ----


def test_probe_token_maps_unknown_errors(monkeypatch) -> None:
    """
    非登录失效异常判为 unknown，失败响应判为 expired。
    """
    responses: list[Any] = [
        NetdiskError(NetdiskErrorCode.RATE_LIMITED, "限流", platform=NetdiskPlatform.GUANGYA),
        {"success": False, "code": "500", "msg": "服务异常"},
        {"success": True, "code": "0", "data": {"nick_name": "鸭子"}},
    ]

    def handler(method, http, url, account, params, body, headers):
        """
        依次返回异常或预设响应。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    assert adapter._probe_token(_TOKEN).status == "unknown"  # pylint: disable=protected-access
    assert adapter._probe_token(_TOKEN).status == "expired"  # pylint: disable=protected-access
    assert "鸭子" in adapter._probe_token(_TOKEN).message  # pylint: disable=protected-access


def test_validate_credential_requires_token_field(monkeypatch) -> None:
    """
    基础字段校验通过但缺少 token 时判为过期。
    """
    adapter = guangya_module.GuangYaAdapter()
    monkeypatch.setattr(adapter, "_probe_token", lambda token: SimpleNamespace(status="active", message=""))

    probe = adapter.validate_credential({"token": "  ", "cookie": "x=1"})

    assert probe.status == "expired"
    assert "token" in probe.message


def test_login_account_name_variants(monkeypatch) -> None:
    """
    缺少 token 或接口失败返回空串，成功时读取昵称。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.login_account_name({"cookie": "x=1"}) == ""

    responses: list[Any] = [
        NetdiskError(NetdiskErrorCode.UNKNOWN, "失败", platform=NetdiskPlatform.GUANGYA),
        {"success": False, "code": "500"},
        {"success": True, "code": "0", "data": "not-a-dict", "name": "扁平昵称"},
        {"success": True, "code": "0", "data": {"nickname": "嵌套昵称"}},
    ]

    def handler(method, http, url, account, params, body, headers):
        """
        依次返回异常或预设响应。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    _patch_http(monkeypatch, handler)

    assert adapter.login_account_name({"token": _TOKEN}) == ""
    assert adapter.login_account_name({"token": _TOKEN}) == ""
    assert adapter.login_account_name({"token": _TOKEN}) == "扁平昵称"
    assert adapter.login_account_name({"token": _TOKEN}) == "嵌套昵称"


def test_probe_account_reports_missing_token(monkeypatch) -> None:
    """
    账号缺少 token 时探测结果为过期。
    """
    adapter = guangya_module.GuangYaAdapter()

    probe = adapter.probe_account(_account({"cookie": "x=1"}))

    assert probe.status == "expired"
    assert "token" in probe.message


@pytest.mark.parametrize(
    ("credential", "expected"),
    [
        ({}, False),
        ({"expires_at": ""}, False),
        ({"expires_at": "not-a-date-not-a-number"}, False),
        ({"expires_at": "2999-01-01T00:00:00+00:00"}, False),
        ({"expires_at": "2000-01-01T00:00:00+00:00"}, True),
        ({"expires_at": "2999-01-01T00:00:00"}, False),
        ({"expires_at": str((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}, False),
        ({"expires_at": str((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000)}, True),
    ],
)
def test_needs_refresh_parses_multiple_formats(credential: dict[str, str], expected: bool) -> None:
    """
    过期时间支持 ISO、秒级与毫秒级时间戳，缺失或无法解析时不刷新。
    """
    adapter = guangya_module.GuangYaAdapter()

    assert adapter.needs_refresh(_account(credential)) is expected


def test_refresh_account_requires_refresh_token(monkeypatch) -> None:
    """
    没有 refresh_token 时不发起刷新。
    """
    adapter = guangya_module.GuangYaAdapter()
    calls: list[str] = []

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求。

        :return: 成功响应
        """
        del method, http, account, params, body, headers
        calls.append(url)
        return {"success": True, "code": "0", "data": {"access_token": "new"}}

    _patch_http(monkeypatch, handler)

    assert adapter.refresh_account(_account()) is None
    assert calls == []


def test_refresh_account_failure_paths(monkeypatch) -> None:
    """
    刷新异常、失败响应与缺少 access_token 都返回 None。
    """
    adapter = guangya_module.GuangYaAdapter()
    responses: list[Any] = [
        NetdiskError(NetdiskErrorCode.UNKNOWN, "失败", platform=NetdiskPlatform.GUANGYA),
        {"success": False, "code": "500"},
        {"success": True, "code": "0", "data": {}},
    ]

    def handler(method, http, url, account, params, body, headers):
        """
        依次返回异常或预设响应。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    _patch_http(monkeypatch, handler)
    account = _account({"token": "old", "refresh_token": "rt"})

    for _ in range(3):
        assert adapter.refresh_account(account) is None


def test_refresh_account_returns_new_credentials(monkeypatch) -> None:
    """
    刷新成功后保留旧 refresh_token 并写入新令牌与过期时间。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回新令牌。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {"accessToken": "new", "expires_in": 7200}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    credential = adapter.refresh_account(_account({"token": "old", "refresh_token": "rt", "expires_at": "old"}))

    assert credential is not None
    assert credential["token"] == "new"
    assert credential["refresh_token"] == "rt"
    assert credential["expires_at"] != "old"

    def handler_new_refresh(method, http, url, account, params, body, headers):
        """
        返回新 refresh_token。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0", "data": {"access_token": "new2", "refresh_token": "rt2"}}

    _patch_http(monkeypatch, handler_new_refresh)

    credential = adapter.refresh_account(_account({"token": "old", "refresh_token": "rt"}))

    assert credential is not None
    assert credential["refresh_token"] == "rt2"


# ---- 登录 ----


def test_password_login_rejects_blank_input() -> None:
    """
    账号或密码为空时提示补全。
    """
    adapter = guangya_module.GuangYaAdapter()

    for username, password in (("", "secret"), ("13800138000", "")):
        with pytest.raises(NetdiskError) as error:
            adapter.password_login(username, password)
        assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_captcha_init_requires_browser_verification(monkeypatch) -> None:
    """
    初始化返回验证地址时要求先在浏览器完成人机验证。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回验证地址。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"url": "https://captcha.example.com/x"}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        guangya_module.GuangYaAdapter().password_login("13800138000", "secret")

    assert error.value.code is NetdiskErrorCode.RISK_BLOCKED
    assert "captcha.example.com" in error.value.message


def test_password_login_handles_request_errors_and_missing_token(monkeypatch) -> None:
    """
    登录请求异常经归一化处理；响应缺少 access_token 时报登录失败。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        人机验证通过，登录返回空 data。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {}
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, handler)
    adapter = guangya_module.GuangYaAdapter()

    with pytest.raises(NetdiskError) as error:
        adapter.password_login("13800138000", "secret")
    assert "缺少 access_token" in error.value.message

    def raising_handler(method, http, url, account, params, body, headers):
        """
        登录接口抛出防盗链异常，人机验证正常。

        :return: 不会返回
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {}
        raise NetdiskError(NetdiskErrorCode.UNKNOWN, "防盗链拦截", platform=NetdiskPlatform.GUANGYA)

    _patch_http(monkeypatch, raising_handler)

    with pytest.raises(NetdiskError) as normalized:
        adapter.password_login("13800138000", "secret")
    assert normalized.value.code is NetdiskErrorCode.LINK_EXPIRED


def test_password_login_verification_required(monkeypatch) -> None:
    """
    服务端要求短信验证码时提示改用网页扫码。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回需要短信验证。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {}
        return {"success": False, "code": "verification_required", "msg": "需要短信验证"}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        guangya_module.GuangYaAdapter().password_login("13800138000", "secret")

    assert error.value.code is NetdiskErrorCode.RISK_BLOCKED
    assert "短信验证码" in error.value.message


def test_password_login_generic_failure_message(monkeypatch) -> None:
    """
    登录失败的提示语按错误码归一化。
    """
    def handler(method, http, url, account, params, body, headers):
        """
        返回无提示的失败响应。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {}
        return {"success": False, "code": "password_required"}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        guangya_module.GuangYaAdapter().password_login("13800138000", "secret")

    assert "尚未设置密码" in error.value.message


def test_send_sms_code_requires_phone() -> None:
    """
    手机号为空时直接报错。
    """
    with pytest.raises(NetdiskError) as error:
        guangya_module.GuangYaAdapter().send_sms_code("")

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_send_sms_code_retries_captcha_and_reports_failures(monkeypatch) -> None:
    """
    人机验证未通过时重试；失败响应与缺少验证会话都报错。
    """
    adapter = guangya_module.GuangYaAdapter()

    def handler(method, http, url, account, params, body, headers):
        """
        始终要求重新人机验证。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {}
        return {"code": "captcha_required"}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as exhausted:
        adapter.send_sms_code("13800138000")
    assert exhausted.value.code is NetdiskErrorCode.RISK_BLOCKED

    def failing_handler(method, http, url, account, params, body, headers):
        """
        发码接口返回业务失败。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {"captcha_token": "cap"}
        return {"success": False, "code": "too_many_requests"}

    _patch_http(monkeypatch, failing_handler)

    with pytest.raises(NetdiskError) as rate_limited:
        adapter.send_sms_code("13800138000")
    assert rate_limited.value.code is NetdiskErrorCode.RATE_LIMITED
    assert "频繁" in rate_limited.value.message

    def missing_session_handler(method, http, url, account, params, body, headers):
        """
        发码成功但缺少验证会话标识。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._CAPTCHA_INIT_URL:
            return {"captcha_token": "cap"}
        return {"success": True, "code": "0"}

    _patch_http(monkeypatch, missing_session_handler)

    with pytest.raises(NetdiskError) as missing:
        adapter.send_sms_code("13800138000")
    assert missing.value.code is NetdiskErrorCode.RATE_LIMITED


def test_sms_login_validates_arguments(monkeypatch) -> None:
    """
    手机号、验证码与验证会话缺一不可。
    """
    adapter = guangya_module.GuangYaAdapter()
    calls: list[str] = []

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求。

        :return: 成功响应
        """
        del method, http, account, params, body, headers
        calls.append(url)
        return {"success": True, "code": "0"}

    _patch_http(monkeypatch, handler)

    for phone, code, verification_id in (("", "1", "v"), ("13800138000", "", "v"), ("13800138000", "1", "")):
        with pytest.raises(NetdiskError):
            adapter.sms_login(phone, code, verification_id)

    assert calls == []


def test_sms_login_failure_paths(monkeypatch) -> None:
    """
    验证码校验失败、缺少验证令牌、登录失败与缺少 access_token 都报错。
    """
    adapter = guangya_module.GuangYaAdapter()

    def verify_fail(method, http, url, account, params, body, headers):
        """
        验证码校验失败。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": False, "code": "500"}

    _patch_http(monkeypatch, verify_fail)
    with pytest.raises(NetdiskError) as verify_error:
        adapter.sms_login("13800138000", "123456", "v-1")
    assert verify_error.value.code is NetdiskErrorCode.AUTH_EXPIRED

    def missing_token(method, http, url, account, params, body, headers):
        """
        校验成功但没有 verification_token。

        :return: 响应
        """
        del method, http, url, account, params, body, headers
        return {"success": True, "code": "0"}

    _patch_http(monkeypatch, missing_token)
    with pytest.raises(NetdiskError) as token_error:
        adapter.sms_login("13800138000", "123456", "v-1")
    assert token_error.value.code is NetdiskErrorCode.AUTH_EXPIRED

    def signin_fail(method, http, url, account, params, body, headers):
        """
        验证码正确但登录失败。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._VERIFICATION_VERIFY_URL:
            return {"success": True, "code": "0", "verification_token": "vt"}
        return {"success": False, "code": "too_many_requests", "message": "过于频繁"}

    _patch_http(monkeypatch, signin_fail)
    with pytest.raises(NetdiskError) as signin_error:
        adapter.sms_login("13800138000", "123456", "v-1")
    assert signin_error.value.code is NetdiskErrorCode.RATE_LIMITED
    assert "频繁" in signin_error.value.message

    def signin_no_token(method, http, url, account, params, body, headers):
        """
        登录成功但缺少 access_token。

        :return: 响应
        """
        del method, http, account, params, body, headers
        if url == guangya_module._VERIFICATION_VERIFY_URL:
            return {"success": True, "code": "0", "verification_token": "vt"}
        return {"success": True, "code": "0", "data": {}}

    _patch_http(monkeypatch, signin_no_token)
    with pytest.raises(NetdiskError) as missing_token_error:
        adapter.sms_login("13800138000", "123456", "v-1")
    assert "缺少 access_token" in missing_token_error.value.message


def test_signin_error_message_fallbacks() -> None:
    """
    未知错误码回退到服务端提示或默认文案。
    """
    assert guangya_module._signin_error_message("other", "服务端提示") == "服务端提示"  # pylint: disable=protected-access
    assert "请检查账号密码" in guangya_module._signin_error_message("other", "")  # pylint: disable=protected-access
    assert "频繁" in guangya_module._signin_error_message("too_many_requests", "")  # pylint: disable=protected-access


def test_token_fields_uses_expires_in() -> None:
    """
    expires_in 换算为绝对过期时间。
    """
    fields = guangya_module._token_fields({"access_token": "t", "expires_in": "60"})  # pylint: disable=protected-access

    expires_at = datetime.fromisoformat(fields["expires_at"])
    assert expires_at > datetime.now(timezone.utc) + timedelta(seconds=30)
