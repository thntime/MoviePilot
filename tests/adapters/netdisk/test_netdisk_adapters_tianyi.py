"""
天翼云盘适配器的元数据、分享解析、转存取链与清理测试。

所有 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，Cookie 使用假值。
"""

import json

import pytest

from app.adapters.external.netdisk import tianyi
from app.domain.netdisk import (
    DriveCapabilities,
    LinkPolicy,
    NetdiskAccountRecord,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareFile,
)


def _account(credential: dict[str, str]) -> NetdiskAccountRecord:
    """
    构造天翼云盘测试账号。

    :param credential: 明文凭据
    :return: 账号记录
    """
    return NetdiskAccountRecord(id=1, platform=NetdiskPlatform.TIANYI, name="t", credential=credential)


def test_metadata() -> None:
    """适配器声明天翼平台能力与单流下载口径。"""
    adapter = tianyi.TianyiAdapter()
    assert adapter.platform is NetdiskPlatform.TIANYI
    assert adapter.link_policy is LinkPolicy.TRANSFER_REQUIRED
    assert adapter.single_stream is True
    assert adapter.max_concurrency == 1
    assert adapter.cookie_required_keys == ("COOKIE_LOGIN_USER",)
    assert "189.cn" in adapter.cookie_domains
    assert adapter.supports_password_login is False
    assert adapter.drive_capabilities == DriveCapabilities(True, True, True, True, True, True, True)


def test_validate_credential_requires_cookie() -> None:
    """缺少 COOKIE_LOGIN_USER 时直接判为过期。"""
    adapter = tianyi.TianyiAdapter()
    assert adapter.validate_credential({}).status == "expired"


def test_validate_credential_maps_invalid_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """列目录接口返回 InvalidSessionKey 时判为过期。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(adapter, "_log_failure_response", lambda *a, **k: None)
    monkeypatch.setattr(
        adapter.http,
        "get_json",
        lambda *a, **k: {"res_code": 1, "errorCode": "InvalidSessionKey"},
    )
    probe = adapter.validate_credential({"COOKIE_LOGIN_USER": "c"})
    assert probe.status == "expired"


def test_validate_credential_maps_error_code_without_res_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录时接口可能只返回 errorCode（无 res_code），同样必须判为过期。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(adapter, "_log_failure_response", lambda *a, **k: None)
    monkeypatch.setattr(
        adapter.http,
        "get_json",
        lambda *a, **k: {"errorCode": "InvalidSessionKey", "errorMsg": "cookieUserSession is null"},
    )
    probe = adapter.validate_credential({"COOKIE_LOGIN_USER": "c"})
    assert probe.status == "expired"


def test_validate_credential_active_when_root_listing_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cookie 只要能列个人盘根目录，就判为登录态有效。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(
        adapter.http,
        "get_json",
        lambda *a, **k: {"fileListAO": {"fileList": [], "folderList": []}},
    )
    probe = adapter.validate_credential({"COOKIE_LOGIN_USER": "c"})
    assert probe.status == "active"


def test_requests_declare_json_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    """天翼按 Accept 决定返回 JSON 还是 XML，请求必须显式要求 JSON。"""
    adapter = tianyi.TianyiAdapter()
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> dict:
        seen.update(kwargs)
        return {"fileListAO": {"fileList": [], "folderList": []}}

    monkeypatch.setattr(adapter.http, "get_json", fake_get)
    adapter.list_dir(_account({"COOKIE_LOGIN_USER": "c"}), "")

    headers = seen.get("headers")
    assert isinstance(headers, dict)
    assert headers["Accept"] == "application/json;charset=UTF-8"


def test_quota_maps_capacity_and_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """配额按总量减可用量计算。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(adapter.http, "get_json", lambda *a, **k: {"capacity": 1000, "available": 400})
    quota = adapter.quota(_account({"COOKIE_LOGIN_USER": "c"}))
    assert (quota.total, quota.used) == (1000, 600)


def test_list_dir_returns_drive_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """个人盘目录同时返回文件与子目录。"""
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
    files = adapter.list_dir(_account({"COOKIE_LOGIN_USER": "c"}), "-11")
    assert [(f.file_id, f.name, f.is_dir) for f in files] == [("11", "a.mp4", False), ("22", "dir", True)]


def test_share_detail_and_list_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """分享详情读取根目录，列目录透传 shareId 与提取码。"""
    adapter = tianyi.TianyiAdapter()
    calls: list[tuple[str, dict]] = []

    def fake_get(path, account=None, params=None, **kwargs):
        calls.append((path, dict(params or {})))
        if path.endswith("getShareInfoByCodeV2.action"):
            return {"shareId": "S1", "fileId": "F1", "shareMode": "1", "fileName": "剧集"}
        return {
            "fileListAO": {
                "fileList": [{"id": "f-1", "name": "S01E01.mp4", "size": 100}],
                "folderList": [{"id": "d-1", "name": "S01E02"}],
            }
        }

    monkeypatch.setattr(adapter.http, "get_json", fake_get)
    ref = adapter.parse_share("https://cloud.189.cn/web/share?code=CODE1", "pwd1")
    detail = adapter.share_detail(ref, _account({"COOKIE_LOGIN_USER": "c"}))
    assert detail.title == "剧集"
    assert detail.size == 100
    files = adapter.list_files(ref, "", _account({"COOKIE_LOGIN_USER": "c"}))
    assert [(f.file_id, f.is_dir, f.parent_id) for f in files] == [("f-1", False, "F1"), ("d-1", True, "F1")]
    assert calls[1][1]["shareId"] == "S1"
    assert calls[1][1]["accessCode"] == "pwd1"
    assert calls[1][1]["isFolder"] == "true"


def test_resolve_transfers_then_returns_direct_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """转存后按任务返回的文件标识取直链并携带清理句柄。"""
    adapter = tianyi.TianyiAdapter()
    calls: list[tuple[str, str, dict]] = []

    def fake_get(path, account=None, params=None, **kwargs):
        calls.append(("GET", path, dict(params or {})))
        if path.endswith("getShareInfoByCodeV2.action"):
            return {"shareId": "S1", "fileId": "F1", "shareMode": "1"}
        if path.endswith("getFileInfo.action"):
            return {"downloadUrl": "//download.cloud.189.cn/x"}
        if path.endswith("listFiles.action"):
            return {
                "fileListAO": {
                    "fileList": [{"id": "999", "name": "S01E01.mp4", "size": 100}],
                    "folderList": [],
                }
            }
        raise AssertionError(path)

    def fake_post(path, account=None, data=None, **kwargs):
        calls.append(("POST", path, dict(data or {})))
        if path.endswith("createFolder.action"):
            return {"id": "DIR1"}
        if path.endswith("createBatchTask.action"):
            return {"taskId": "T1"}
        if path.endswith("checkBatchTask.action"):
            return {"taskStatus": 4, "failedCount": 0, "successedFileIdList": ["999"]}
        raise AssertionError(path)

    monkeypatch.setattr(adapter.http, "get_json", fake_get)
    monkeypatch.setattr(adapter.http, "post_json", fake_post)
    ref = adapter.parse_share("https://cloud.189.cn/t/CODE1", None)
    file = ShareFile(file_id="f-1", name="S01E01.mp4", size=100)
    link = adapter.resolve(ref, file, _account({"COOKIE_LOGIN_USER": "c"}))

    assert link.url.startswith("https:") and "download.cloud.189.cn" in link.url
    assert link.cleanup_handle is not None
    assert link.cleanup_handle.directory_id == "DIR1"
    assert link.strategy == "transfer"
    save = next(c for c in calls if c[1].endswith("createBatchTask.action"))
    assert json.loads(save[2]["taskInfos"]) == [{"fileId": "f-1", "fileName": "S01E01.mp4", "isFolder": 0}]
    assert save[2]["shareId"] == "S1"


def test_resolve_raises_share_invalid_when_all_files_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """转存完成但 failedCount>0 视为分享失效。"""
    adapter = tianyi.TianyiAdapter()
    monkeypatch.setattr(adapter.http, "get_json", lambda *a, **k: {"shareId": "S1", "fileId": "F1", "shareMode": "1"})
    monkeypatch.setattr(
        adapter.http,
        "post_json",
        lambda path, **k: (
            {"id": "DIR1"}
            if path.endswith("createFolder.action")
            else {"taskId": "T1"}
            if path.endswith("createBatchTask.action")
            else {"taskStatus": 4, "failedCount": 1}
        ),
    )
    ref = adapter.parse_share("https://cloud.189.cn/t/CODE1", None)
    with pytest.raises(NetdiskError) as err:
        adapter.resolve(ref, ShareFile(file_id="f-1", name="x.mp4", size=1), _account({"COOKIE_LOGIN_USER": "c"}))
    assert err.value.code is NetdiskErrorCode.SHARE_INVALID


def test_resolve_requires_account() -> None:
    """没有账号时转存取链直接提示登录。"""
    adapter = tianyi.TianyiAdapter()
    ref = adapter.parse_share("https://cloud.189.cn/t/CODE1", None)
    with pytest.raises(NetdiskError) as err:
        adapter.resolve(ref, ShareFile(file_id="f-1", name="x.mp4"), None)
    assert err.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_cleanup_deletes_handle_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理只删除句柄记录的临时目录，且 isFolder=1。"""
    from app.domain.netdisk import TransferHandle

    adapter = tianyi.TianyiAdapter()
    posts: list[tuple[str, dict]] = []

    def fake_post(path, account=None, data=None, **kwargs):
        posts.append((path, dict(data or {})))
        return {"taskId": "T1"}

    monkeypatch.setattr(adapter.http, "post_json", fake_post)
    handle = TransferHandle(
        platform=NetdiskPlatform.TIANYI,
        account_id=1,
        directory_id="1234567890",
        file_ids=("999",),
    )
    adapter.cleanup(handle, _account({"COOKIE_LOGIN_USER": "c"}))

    create = posts[0]
    assert create[0].endswith("createBatchTask.action")
    assert create[1]["type"] == "DELETE"
    assert json.loads(create[1]["taskInfos"]) == [{"fileId": "1234567890", "fileName": "", "isFolder": 1}]

    posts.clear()
    empty = TransferHandle(platform=NetdiskPlatform.TIANYI, account_id=1)
    adapter.cleanup(empty, _account({"COOKIE_LOGIN_USER": "c"}))
    assert posts == []


def test_tianyi_is_registered() -> None:
    """组合根装配后天翼适配器可按平台查到。"""
    from app.application.netdisk.registry import find_adapter
    from app.startup.composition import netdisk as composition

    composition.configure_netdisk_composition()
    try:
        adapter = find_adapter("tianyi")
        assert adapter is not None and adapter.single_stream is True
    finally:
        composition.reset_netdisk_composition()
