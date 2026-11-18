"""
补充网盘相关模块的边界分支测试，覆盖异常、降级与清理路径。

覆盖对象：CloudSaver 客户端、网盘账号仓储、网盘组合根装配与本地已有文件检查。
所有网络、数据库与文件系统访问都使用替身或临时目录，不访问真实服务。
"""

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.modules.cloudsaver.cloudsaver as cloudsaver_module
import app.modules.netdisk as netdisk_module
from app.application.netdisk.contract import NetdiskAccountWrite
from app.application.netdisk.credentials import FernetCredentialCipher
from app.db.adapters.netdiskaccount import TransactionalNetdiskAccountRepository
from app.db.base import Base
from app.db.models.netdiskaccount import NetdiskAccount
from app.domain.netdisk import (
    AccountProbe,
    DirectLink,
    DriveFile,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    NetdiskTuning,
    ShareFile,
    TransferHandle,
)
from app.modules.cloudsaver.cloudsaver import CloudSaverClient, CloudSaverError
from app.modules.netdisk import NetdiskModule, store
from app.modules.netdisk.engine import DownloadCancelled, DownloadError, NotHls
from app.schemas.types import TorrentQueryStatus

# ---------------------------------------------------------------------------
# CloudSaver 客户端
# ---------------------------------------------------------------------------


class _FakeResponse:
    """
    最小 HTTP 响应替身，可配置 JSON 解析失败与关闭失败。

    :param payload: 响应 JSON 体
    :param status: HTTP 状态码
    :param json_error: 解析 JSON 时是否抛异常
    :param close_error: 关闭响应时是否抛异常
    """

    def __init__(
        self,
        payload: Any = None,
        status: int = 200,
        json_error: bool = False,
        close_error: bool = False,
    ) -> None:
        """保存响应内容与行为开关。"""
        self.payload = payload
        self.status_code = status
        self.json_error = json_error
        self.close_error = close_error
        self.closed = False

    def json(self) -> Any:
        """返回预设 JSON；配置了失败时抛出异常。"""
        if self.json_error:
            raise ValueError("invalid json")
        return self.payload

    def close(self) -> None:
        """记录关闭调用；配置了失败时抛出异常。"""
        self.closed = True
        if self.close_error:
            raise RuntimeError("close failed")


class _FakeHttp:
    """
    按方法返回队列响应的网络适配器替身。

    :param posts: POST 响应队列
    :param gets: GET 响应队列
    :param get_error: GET 时是否直接抛出异常
    """

    def __init__(
        self,
        posts: Optional[List[Any]] = None,
        gets: Optional[List[Any]] = None,
        get_error: Optional[Exception] = None,
    ) -> None:
        """保存响应队列与异常开关。"""
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.get_error = get_error
        self.calls: List[Dict[str, Any]] = []

    def post_res(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        """记录 POST 调用并返回队首响应。"""
        self.calls.append({"method": "POST", "url": url, "json": json, "kwargs": kwargs})
        return self.posts.pop(0)

    def get_res(self, url: str, params: Any = None, **kwargs: Any) -> Any:
        """记录 GET 调用并返回队首响应。"""
        self.calls.append({"method": "GET", "url": url, "params": params, "kwargs": kwargs})
        if self.get_error is not None:
            raise self.get_error
        return self.gets.pop(0)


def _install_http(monkeypatch: pytest.MonkeyPatch, http: _FakeHttp) -> CloudSaverClient:
    """
    把客户端的网络适配器替换为替身并返回已登录的客户端。

    :param monkeypatch: pytest monkeypatch
    :param http: 网络替身
    :return: 客户端实例
    """
    monkeypatch.setattr(cloudsaver_module, "RequestUtils", lambda **kwargs: http)
    client = CloudSaverClient("http://cloudsaver.local", "user", "secret")
    client._token = "token"  # pylint: disable=protected-access
    return client


def test_as_flag_accepts_bool_number_and_text() -> None:
    """文件夹标记应兼容布尔、数字与文本三种形态。"""
    assert cloudsaver_module._as_flag(True) is True  # pylint: disable=protected-access
    assert cloudsaver_module._as_flag(0) is False  # pylint: disable=protected-access
    assert cloudsaver_module._as_flag(1) is True  # pylint: disable=protected-access
    assert cloudsaver_module._as_flag("yes") is True  # pylint: disable=protected-access
    assert cloudsaver_module._as_flag("no") is False  # pylint: disable=protected-access
    assert cloudsaver_module._as_flag(None) is False  # pylint: disable=protected-access


def test_normalize_share_entries_filters_invalid_items() -> None:
    """目录条目归一应丢弃非法项并兼容多种字段名。"""
    assert cloudsaver_module._normalize_share_entries("bad") == []  # pylint: disable=protected-access
    entries = cloudsaver_module._normalize_share_entries([  # pylint: disable=protected-access
        "bad",
        {"file_name": "a.mkv", "fid": "1", "dir": 1},
        {"name": "b.mkv", "file_id": "2"},
        {"name": "  "},
    ])
    assert entries == [
        {"name": "a.mkv", "file_id": "1", "is_folder": True},
        {"name": "b.mkv", "file_id": "2", "is_folder": False},
    ]


def test_platform_from_url_returns_empty_for_unknown_domain() -> None:
    """无法识别的分享域名返回空平台标识。"""
    assert cloudsaver_module._platform_from_url("https://example.com/s/abc") == ""  # pylint: disable=protected-access


def test_normalize_search_data_skips_invalid_groups() -> None:
    """搜索数据归一应跳过非法分组、非法条目与缺失列表。"""
    assert cloudsaver_module._normalize_search_data("bad") == []  # pylint: disable=protected-access
    entries = cloudsaver_module._normalize_search_data([  # pylint: disable=protected-access
        "bad",
        {"channel": "频道", "list": "bad"},
        {"channelInfo": "bad", "list": ["bad", {"title": "标题", "content": "描述：正文"}]},
    ])
    assert len(entries) == 1
    assert entries[0]["title"] == "标题"
    assert entries[0]["content"] == "正文"


def test_request_json_handles_bad_json_and_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """响应不是字典时返回 None，关闭响应异常不应向上抛出。"""
    response = _FakeResponse(json_error=True, close_error=True)
    http = _FakeHttp(gets=[response])
    client = _install_http(monkeypatch, http)

    status, payload = client._request_json("GET", "/api/search", timeout=5)  # pylint: disable=protected-access

    assert status == 200
    assert payload is None
    assert http.calls[0]["kwargs"]["timeout"] == 5
    assert response.closed is True


def test_request_json_raises_when_response_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有拿到响应时抛出可展示的网络错误。"""
    http = _FakeHttp(gets=[None])
    client = _install_http(monkeypatch, http)

    with pytest.raises(CloudSaverError):
        client._request_json("GET", "/api/search")  # pylint: disable=protected-access


def test_login_rejects_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录返回 4xx 时给出 HTTP 状态说明并清空 token。"""
    http = _FakeHttp(posts=[_FakeResponse({"message": "bad"}, 401)])
    monkeypatch.setattr(cloudsaver_module, "RequestUtils", lambda **kwargs: http)
    client = CloudSaverClient("http://cloudsaver.local", "user", "secret")

    with pytest.raises(CloudSaverError, match="HTTP 401"):
        client.login()
    assert client._token is None  # pylint: disable=protected-access


def test_login_rejects_invalid_payload_and_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录响应非 JSON 或缺少 token 时都按失败处理。"""
    http = _FakeHttp(posts=[_FakeResponse(json_error=True), _FakeResponse({"success": True, "data": {}})])
    monkeypatch.setattr(cloudsaver_module, "RequestUtils", lambda **kwargs: http)
    client = CloudSaverClient("http://cloudsaver.local", "user", "secret")

    with pytest.raises(CloudSaverError, match="不是有效 JSON"):
        client.login()
    with pytest.raises(CloudSaverError, match="缺少 token"):
        client.login()


def test_share_detail_empty_code_returns_invalid_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """分享码为空时直接返回失效详情，不发起请求。"""
    http = _FakeHttp()
    client = _install_http(monkeypatch, http)

    assert client.share_detail("  ") == {
        "alive": False, "file_size": 0, "file_names": [], "stoken": "",
    }
    assert http.calls == []


def test_share_detail_parses_files_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """分享详情应解析文件列表、大小与 stoken。"""
    payload = {
        "success": True,
        "data": {
            "list": [{"fileName": "第01集.mp4", "fileId": "f1"}, {"file_name": "第02集.mp4", "fid": "f2"}],
            "fileSize": "2048",
            "stoken": "st",
        },
    }
    http = _FakeHttp(gets=[_FakeResponse(payload)])
    client = _install_http(monkeypatch, http)

    detail = client.share_detail("abc", "9527")

    assert detail == {
        "alive": True,
        "file_size": 2048,
        "file_names": ["第01集.mp4", "第02集.mp4"],
        "stoken": "st",
    }
    assert http.calls[0]["params"] == {"shareCode": "abc", "receiveCode": "9527"}
    assert http.calls[0]["kwargs"]["timeout"] == 10


def test_share_detail_refreshes_token_after_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """分享详情返回 401 时应重新登录并重试一次。"""
    login = _FakeResponse({"success": True, "data": {"token": "new"}})
    http = _FakeHttp(
        posts=[login],
        gets=[
            _FakeResponse({"message": "unauthorized"}, 401),
            _FakeResponse({"success": True, "data": {"list": [], "fileSize": 0}}),
        ],
    )
    client = _install_http(monkeypatch, http)

    detail = client.share_detail("abc")

    assert detail is not None
    assert detail["alive"] is False
    assert [call["method"] for call in http.calls] == ["GET", "POST", "GET"]


def test_share_detail_returns_none_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络异常时无法判定分享有效性，返回 None。"""
    http = _FakeHttp(get_error=RuntimeError("boom"))
    client = _install_http(monkeypatch, http)

    assert client.share_detail("abc") is None


def test_share_detail_maps_server_error_to_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx 表示分享失效，按失效详情返回。"""
    http = _FakeHttp(gets=[_FakeResponse({"success": False}, 500)])
    client = _install_http(monkeypatch, http)

    assert client.share_detail("abc")["alive"] is False


def test_share_detail_returns_none_on_client_error_or_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx 或响应体不是 JSON 时无法判定，返回 None。"""
    http = _FakeHttp(gets=[_FakeResponse({"message": "bad"}, 404), _FakeResponse(json_error=True)])
    client = _install_http(monkeypatch, http)

    assert client.share_detail("abc") is None
    assert client.share_detail("abc") is None


def test_share_detail_handles_failed_payload_and_bad_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """success=false 与非字典 data 都按失效处理，非法大小回落为 0。"""
    http = _FakeHttp(gets=[
        _FakeResponse({"success": False}),
        _FakeResponse({"success": True, "data": "bad"}),
        _FakeResponse({"success": True, "data": {"list": [], "fileSize": "abc"}}),
    ])
    client = _install_http(monkeypatch, http)

    assert client.share_detail("abc")["alive"] is False
    assert client.share_detail("abc")["alive"] is False
    detail = client.share_detail("abc")
    assert detail["alive"] is False
    assert detail["file_size"] == 0


def test_share_alive_returns_tristate(monkeypatch: pytest.MonkeyPatch) -> None:
    """有效性检查应区分有效、失效与无法判定。"""
    http = _FakeHttp(get_error=RuntimeError("boom"))
    client = _install_http(monkeypatch, http)
    assert client.share_alive("abc") is None

    http.get_error = None
    http.gets = [
        _FakeResponse({"success": True, "data": {"list": [{"fileName": "a.mp4"}]}}),
        _FakeResponse({"success": True, "data": {"list": []}}),
    ]
    assert client.share_alive("abc") is True
    assert client.share_alive("abc") is False


def test_search_rejects_empty_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """空关键词不发起请求，直接返回空列表。"""
    http = _FakeHttp()
    client = _install_http(monkeypatch, http)

    assert client.search("  ") == []
    assert http.calls == []


def test_search_reports_retry_and_rate_limit_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """重登后仍 401 与 429 都应给出明确的中文原因。"""
    login = _FakeResponse({"success": True, "data": {"token": "new"}})
    http = _FakeHttp(
        posts=[login],
        gets=[_FakeResponse({}, 401), _FakeResponse({}, 401)],
    )
    client = _install_http(monkeypatch, http)
    with pytest.raises(CloudSaverError, match="重新登录后仍返回 401"):
        client.search("关键词")

    http.gets = [_FakeResponse({}, 429)]
    with pytest.raises(CloudSaverError, match="HTTP 429"):
        client.search("关键词")


def test_search_reports_http_and_payload_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通 HTTP 错误、非 JSON 响应与接口 success=false 都抛出可展示原因。"""
    http = _FakeHttp(gets=[
        _FakeResponse({"message": "bad"}, 400),
        _FakeResponse(json_error=True),
        _FakeResponse({"success": False, "message": "接口拒绝"}),
    ])
    client = _install_http(monkeypatch, http)

    with pytest.raises(CloudSaverError, match="HTTP 400"):
        client.search("关键词")
    with pytest.raises(CloudSaverError, match="不是有效 JSON"):
        client.search("关键词")
    with pytest.raises(CloudSaverError, match="接口拒绝"):
        client.search("关键词")


# ---------------------------------------------------------------------------
# 网盘账号仓储
# ---------------------------------------------------------------------------


@pytest.fixture()
def repository() -> Any:
    """
    构造基于内存 SQLite 的账号仓储与引擎。

    :return: (仓储, 引擎)
    """
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[NetdiskAccount.__table__])
    factory = sessionmaker(bind=engine)
    cipher = FernetCredentialCipher("unit-test-secret")
    yield TransactionalNetdiskAccountRepository(sync_session=factory, cipher=cipher), engine
    engine.dispose()


def _create_account(repo: TransactionalNetdiskAccountRepository, name: str = "主账号") -> Any:
    """
    创建测试账号并返回脱敏投影。

    :param repo: 账号仓储
    :param name: 账号名称
    :return: 脱敏投影
    """
    return repo.create_account(
        NetdiskAccountWrite(
            platform=NetdiskPlatform.QUARK,
            name=name,
            credential={"cookie": "__pus=1"},
            is_primary=True,
        )
    )


def test_account_decrypt_returns_empty_for_invalid_json(repository: Any) -> None:
    """凭据密文解出非法 JSON 时返回空字段，不向上抛异常。"""
    repo, engine = repository
    view = _create_account(repo)
    cipher = FernetCredentialCipher("unit-test-secret")
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE netdiskaccount SET credential_enc = :value WHERE id = :id"),
            {"value": cipher.encrypt("{not json"), "id": view.id},
        )

    record = repo.get_record(view.id)

    assert record is not None
    assert record.credential == {}


def test_account_get_record_missing_and_invalid_platform(repository: Any) -> None:
    """不存在的账号返回 None，平台标识非法时抛出领域错误。"""
    repo, engine = repository
    assert repo.get_record(9999) is None

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO netdiskaccount "
                "(platform, name, credential_enc, is_primary, status, quota_used, quota_total) "
                "VALUES ('bogus', '坏账号', 'x', 0, 'active', 0, 0)"
            )
        )
    with engine.connect() as connection:
        account_id = connection.execute(
            sa.text("SELECT id FROM netdiskaccount WHERE platform = 'bogus'")
        ).scalar_one()

    with pytest.raises(NetdiskError, match="平台标识无效"):
        repo.get_record(account_id)


def test_account_update_all_fields(repository: Any) -> None:
    """更新账号应支持名称、凭据、状态、过期时间、昵称与配额。"""
    repo, _ = repository
    view = _create_account(repo)
    expires_at = datetime(2027, 1, 1, 12, 0, 0)

    updated = repo.update_account(
        view.id,
        name="改名",
        credential={"cookie": "__pus=2"},
        status="expired",
        expires_at=expires_at,
        nickname="昵称",
        quota_used=10,
        quota_total=100,
        is_primary=False,
    )

    assert updated is not None
    assert updated.name == "改名"
    assert updated.status == "expired"
    assert updated.expires_at == expires_at
    assert updated.nickname == "昵称"
    assert updated.quota_used == 10
    assert updated.quota_total == 100
    assert updated.is_primary is False
    record = repo.get_record(view.id)
    assert record is not None
    assert record.credential == {"cookie": "__pus=2"}

    promoted = repo.update_account(view.id, is_primary=True)
    assert promoted is not None and promoted.is_primary is True


def test_account_update_rejects_duplicate_name_and_missing_id(repository: Any) -> None:
    """改名撞同平台账号时拒绝，账号不存在时返回 None。"""
    repo, _ = repository
    _create_account(repo, "账号A")
    second = _create_account(repo, "账号B")

    with pytest.raises(NetdiskError, match="账号名称已存在"):
        repo.update_account(second.id, name="账号A")
    assert repo.update_account(9999, name="不存在") is None


def test_account_delete_missing_and_non_primary(repository: Any) -> None:
    """删除不存在账号返回 False，删除非主账号不提升其他账号。"""
    repo, _ = repository
    assert repo.delete_account(9999) is False

    _create_account(repo, "主账号")
    repo.create_account(
        NetdiskAccountWrite(
            platform=NetdiskPlatform.QUARK, name="备用", credential={"cookie": "c"},
        )
    )
    backup = repo.list_accounts(NetdiskPlatform.QUARK)[-1]
    assert repo.delete_account(backup.id) is True
    records = repo.list_accounts(NetdiskPlatform.QUARK)
    assert len(records) == 1


def test_account_touch_records_expiry_and_quota(repository: Any) -> None:
    """探测结果带过期时间时写回，配额更新应夹到非负值。"""
    repo, _ = repository
    view = _create_account(repo)
    expires_at = datetime(2027, 2, 2, 8, 0, 0)

    repo.touch_account(view.id, AccountProbe(status="expired", expires_at=expires_at))
    repo.update_quota(view.id, -5, 100)

    record = repo.get_record(view.id)
    assert record is not None
    assert record.status == "expired"
    assert record.expires_at == expires_at
    listed = repo.list_accounts(NetdiskPlatform.QUARK)[0]
    assert listed.quota_used == 0
    assert listed.quota_total == 100


def test_account_touch_and_quota_missing_account_are_noops(repository: Any) -> None:
    """账号不存在时探测与配额更新都不写库、不抛异常。"""
    repo, _ = repository
    repo.touch_account(9999, AccountProbe(status="expired"))
    repo.update_quota(9999, 1, 2)

    assert repo.list_accounts() == []


# ---------------------------------------------------------------------------
# 网盘组合根装配
# ---------------------------------------------------------------------------


class _FakeAccountRepository:
    """
    记录凭据回写的账号仓储替身。

    :param fail: 是否让更新抛出异常
    """

    calls: List[Dict[str, Any]] = []
    fail: bool = False

    def __init__(self, **kwargs: Any) -> None:
        """保存构造参数，兼容组合根的注入签名。"""
        self.kwargs = kwargs

    def update_account(self, account_id: int, **fields: Any) -> None:
        """记录回写调用；配置失败时抛出异常。"""
        if self.fail:
            raise RuntimeError("write failed")
        self.calls.append({"account_id": account_id, **fields})


def test_netdisk_composition_credential_sink_and_refresher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """组合根应把凭据回写与续期回调挂到适配器，并吞掉回写异常。"""
    from app.application.netdisk.registry import find_adapter
    from app.startup.composition import netdisk as composition

    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.db.adapters.netdiskaccount.TransactionalNetdiskAccountRepository",
        _FakeAccountRepository,
    )
    _FakeAccountRepository.calls = []
    _FakeAccountRepository.fail = False

    composition.configure_netdisk_composition()
    try:
        adapter = find_adapter(NetdiskPlatform.QUARK)
        assert adapter is not None
        record = SimpleNamespace(
            id=7, platform=NetdiskPlatform.QUARK, name="主账号",
        )

        adapter.http.credential_sink(record, {"cookie": "new"})
        assert _FakeAccountRepository.calls == [{"account_id": 7, "credential": {"cookie": "new"}}]

        _FakeAccountRepository.fail = True
        adapter.http.credential_sink(record, {"cookie": "new"})

        _FakeAccountRepository.fail = False
        adapter.supports_refresh = True
        adapter.refresh_account = lambda account: {"cookie": "refreshed"}
        assert adapter.http.credential_refresher(record) == {"cookie": "refreshed"}
        assert _FakeAccountRepository.calls[-1]["credential"] == {"cookie": "refreshed"}

        adapter.refresh_account = lambda account: {}
        assert adapter.http.credential_refresher(record) is None

        adapter.supports_refresh = False
        assert adapter.http.credential_refresher(record) is None
    finally:
        composition.reset_netdisk_composition()


# ---------------------------------------------------------------------------
# 本地已有完整文件检查
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_existing() -> Any:
    """
    清空本地检查的模块级缓存。

    :return: 模块对象
    """
    import app.chain.download.local_existing as module

    module._CACHE.clear()  # pylint: disable=protected-access
    yield module
    module._CACHE.clear()  # pylint: disable=protected-access


def _complete_file(directory: Path, name: str, size: int = 100) -> Path:
    """
    写入带完成标记的文件。

    :param directory: 目录
    :param name: 文件名
    :param size: 字节数
    :return: 文件路径
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"x" * size)
    target.with_name(target.name + ".done").touch()
    return target


def test_local_existing_empty_episodes_returns_none(local_existing: Any, tmp_path: Path) -> None:
    """没有目标集数时不扫描目录。"""
    assert local_existing.find_local_complete("Show", 1, set(), [str(tmp_path)]) is None


def test_local_existing_media_check_failure_continues(local_existing: Any, tmp_path: Path) -> None:
    """媒体库查询异常时继续扫描下载目录。"""
    target = _complete_file(tmp_path / "Show", "Show.S01E01.mkv")

    def broken() -> bool:
        """模拟媒体库查询失败。"""
        raise RuntimeError("db down")

    hit = local_existing.find_local_complete(
        "Show", 1, {1}, [str(tmp_path)], media_exists=broken,
    )
    assert hit is not None and hit.path == target


def test_local_existing_cache_expiry_and_prune(local_existing: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """过期缓存应被丢弃，缓存超限时应清理最旧条目。"""
    monkeypatch.setattr(local_existing.time, "monotonic", lambda: 1000.0)
    key = ("Show", 1, (1,), (str(tmp_path),))
    local_existing._CACHE[key] = (999.0, None)  # pylint: disable=protected-access

    assert local_existing._cached(key) is local_existing._MISS  # pylint: disable=protected-access
    assert key not in local_existing._CACHE  # pylint: disable=protected-access

    for index in range(local_existing._CACHE_MAX_ENTRIES):  # pylint: disable=protected-access
        local_existing._CACHE[(f"k{index}", None, (), ())] = (500.0, None)  # pylint: disable=protected-access
    local_existing._prune_cache()  # pylint: disable=protected-access
    assert len(local_existing._CACHE) < local_existing._CACHE_MAX_ENTRIES  # pylint: disable=protected-access


def test_local_existing_skips_other_season_and_unknown_episode(local_existing: Any, tmp_path: Path) -> None:
    """季号不一致或解析不出集数的文件都不登记。"""
    directory = tmp_path / "Show"
    _complete_file(directory, "Show.S02E01.mkv")
    _complete_file(directory, "花絮.mkv")

    assert local_existing.find_local_complete("Show", 1, {1}, [str(tmp_path)]) is None


def test_local_existing_budget_limits(local_existing: Any, tmp_path: Path) -> None:
    """目录数与文件数预算耗尽时按未命中处理。"""
    _complete_file(tmp_path / "Show", "Show.S01E01.mkv")

    assert local_existing.find_local_complete("Show", 1, {1}, [str(tmp_path)], max_dirs=0) is None
    assert local_existing.find_local_complete("Show", 1, {1}, [str(tmp_path)], max_files=0) is None


def test_local_existing_scandir_errors(local_existing: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """目录扫描失败时跳过该目录而不是抛出。"""
    def broken_scandir(path: Any) -> Any:
        """模拟目录不可读。"""
        raise OSError("denied")

    monkeypatch.setattr(local_existing.os, "scandir", broken_scandir)

    assert local_existing.find_local_complete("Show", 1, {1}, [str(tmp_path)]) is None


def test_local_existing_skips_non_file_entries(local_existing: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """既不是目录也不是普通文件的条目应被跳过。"""
    class _Entry:
        """scandir 条目替身。"""

        path = str(tmp_path / "special")

        @staticmethod
        def is_dir(follow_symlinks: bool = False) -> bool:
            """声明不是目录。"""
            return False

        @staticmethod
        def is_file(follow_symlinks: bool = False) -> bool:
            """声明不是普通文件。"""
            return False

    class _Scandir:
        """上下文管理器替身。"""

        def __enter__(self) -> List[Any]:
            """返回单条特殊条目。"""
            return [_Entry()]

        def __exit__(self, *args: Any) -> None:
            """无需清理。"""

    monkeypatch.setattr(local_existing.os, "scandir", lambda path: _Scandir())

    assert local_existing.find_local_complete("Show", 1, {1}, [str(tmp_path)]) is None


def test_local_existing_blank_dir_and_file_budget(local_existing: Any, tmp_path: Path) -> None:
    """空白目录字符串被忽略，直接传入文件时也受文件预算限制。"""
    target = _complete_file(tmp_path / "Show", "Show.S01E01.mkv")

    assert local_existing.find_local_complete("Show", 1, {1}, ["  "]) is None
    assert local_existing.find_local_complete("Show", 1, {1}, [str(target)], max_files=0) is None
    local_existing._CACHE.clear()  # pylint: disable=protected-access
    assert local_existing.find_local_complete("Show", 1, {1}, [str(target)]) is not None


# ---------------------------------------------------------------------------
# 通用网盘下载器模块：内部辅助函数
# ---------------------------------------------------------------------------


@pytest.fixture()
def netdisk_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    把任务存储指向临时配置目录。

    :param tmp_path: 临时目录
    :param monkeypatch: pytest monkeypatch
    :return: 临时目录
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_task_slots_limit_falls_back_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """系统配置不可读时任务槽上限回落到 3。"""
    import app.application.configuration as configuration

    def broken() -> Any:
        """模拟配置读取失败。"""
        raise RuntimeError("config down")

    monkeypatch.setattr(configuration, "get_configured_system_config", broken)

    assert netdisk_module._TaskSlots._limit() == 3  # pylint: disable=protected-access


def test_probe_video_duration_failure_modes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ffprobe 失败、输出不可解析与不可执行时都返回 None。"""
    monkeypatch.setattr(
        netdisk_module.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert netdisk_module._probe_video_duration(tmp_path / "a.mp4") is None  # pylint: disable=protected-access

    monkeypatch.setattr(
        netdisk_module.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="abc"),
    )
    assert netdisk_module._probe_video_duration(tmp_path / "a.mp4") is None  # pylint: disable=protected-access

    def boom(*args: Any, **kwargs: Any) -> Any:
        """模拟 ffprobe 不存在。"""
        raise OSError("no ffprobe")

    monkeypatch.setattr(netdisk_module.subprocess, "run", boom)
    assert netdisk_module._probe_video_duration(tmp_path / "a.mp4") is None  # pylint: disable=protected-access


def test_pause_for_disabled_platform_writes_reason(netdisk_env: Path) -> None:
    """禁用平台的残留任务应写入暂停原因。"""
    netdisk_module._pause_for_disabled_platform("h1", NetdiskPlatform.QUARK)  # pylint: disable=protected-access

    record = store.get_task("h1")
    assert record is not None
    assert record["state"] == "paused"
    assert record["paused_reason"] == "platform_disabled"


def test_remove_empty_dir_handles_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """目录不存在或删除失败时返回 False。"""
    assert netdisk_module._remove_empty_dir(tmp_path / "missing") is False  # pylint: disable=protected-access

    target = tmp_path / "only-sidecar"
    target.mkdir()
    (target / "a.mp4.done").touch()

    def broken_rmdir(self: Path) -> None:
        """模拟目录被占用。"""
        raise OSError("busy")

    monkeypatch.setattr(Path, "rmdir", broken_rmdir)
    assert netdisk_module._remove_empty_dir(target) is False  # pylint: disable=protected-access


def test_no_target_files_error_messages() -> None:
    """没有目标文件的错误应优先列出季级目标，其次集数，最后通用文案。"""
    targeted = netdisk_module._no_target_files_error(None, {1: {1, 2}})  # pylint: disable=protected-access
    assert "1, 2" in targeted.message

    episodic = netdisk_module._no_target_files_error({3}, None)  # pylint: disable=protected-access
    assert "3" in episodic.message

    generic = netdisk_module._no_target_files_error(None, None)  # pylint: disable=protected-access
    assert "视频文件" in generic.message


def test_file_progress_writer_smooths_speed_and_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    """进度写入应按时间窗口节流，并用 EWMA 平滑瞬时速度。"""
    writes: List[Any] = []
    monkeypatch.setattr(netdisk_module.store, "update_progress", lambda *args, **kwargs: writes.append(args))
    state: Dict[str, Any] = {
        "completed": 0.0,
        "active": {},
        "at": time.monotonic() - 2.0,
        "done": 0.0,
        "speed": 0.0,
        "write_at": 0.0,
        "percent": -100.0,
    }
    writer = netdisk_module._FileProgressWriter("t1", 1000, state, threading.Lock())  # pylint: disable=protected-access

    writer("a.mp4", 50.0, 1000)
    assert state["speed"] > 0
    assert len(writes) == 1

    writer("a.mp4", 50.1, 1000)
    assert len(writes) == 1


# ---------------------------------------------------------------------------
# 通用网盘下载器模块：任务恢复与个人盘下载
# ---------------------------------------------------------------------------


def _module_with_config(monkeypatch: pytest.MonkeyPatch) -> NetdiskModule:
    """
    构造已配置下载器实例的模块替身。

    :param monkeypatch: pytest monkeypatch
    :return: 网盘模块
    """
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    return module


def test_module_test_and_stop(netdisk_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有实例时自检返回 None；停止应置位信号并登记进程停止原因。"""
    module = NetdiskModule()
    monkeypatch.setattr(module, "get_configs", lambda: {})
    assert module.test() is None

    monkeypatch.setattr(module, "get_configs", lambda: {"a": object()})
    assert module.test() == (True, "")

    event = threading.Event()
    module._stops["h1"] = event  # pylint: disable=protected-access
    module.stop()
    assert event.is_set()
    assert module._pause_reasons["h1"] == "shutdown"  # pylint: disable=protected-access
    assert module._stops == {}  # pylint: disable=protected-access


def test_resume_task_restores_existing_files(netdisk_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已有文件任务与分享任务都应能按记录恢复，缺少信息时返回 False。"""
    module = _module_with_config(monkeypatch)
    monkeypatch.setattr(module, "_run_existing_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_run_task", lambda *args, **kwargs: None)

    existing_record = {
        "netdisk_platform": "quark",
        "save_path": str(netdisk_env),
        "account_id": 7,
        "downloader": "网盘",
        "media_runtime": 45,
        "source": "Subscribe|x",
        "existing_files": [{"file_id": "f1", "name": "a.mkv", "size": 4}],
    }
    assert module._resume_task("h-existing", existing_record) is True  # pylint: disable=protected-access
    assert store.get_task("h-existing")["state"] == "downloading"

    assert module._resume_task("h-no-account", {**existing_record, "account_id": 0}) is False  # pylint: disable=protected-access
    assert module._resume_task(  # pylint: disable=protected-access
        "h-dir-only", {**existing_record, "existing_files": [{"file_id": "f", "name": "d", "is_dir": True}]}
    ) is False

    monkeypatch.setattr(module, "get_configs", lambda: {})
    assert module._resume_task("h-no-config", existing_record) is False  # pylint: disable=protected-access
    monkeypatch.setattr(
        module, "get_configs", lambda: {"网盘": SimpleNamespace(name="网盘", config={})},
    )

    share_record = {
        "netdisk_platform": "quark",
        "save_path": str(netdisk_env),
        "share_url": "https://pan.quark.cn/s/abc",
        "share_code": "abc",
        "episodes": [1],
        "season": 1,
        "episode_targets": {"1": [1]},
    }
    assert module._resume_task("h-share", share_record) is True  # pylint: disable=protected-access
    assert module._resume_task("h-no-url", {**share_record, "share_url": ""}) is False  # pylint: disable=protected-access


class _QuarkService:
    """返回夸克平台的个人盘服务替身。"""

    def account(self, account_id: int) -> Any:
        """返回夸克账号。"""
        return SimpleNamespace(platform="quark")


def test_download_existing_error_paths(netdisk_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """个人盘下载入口应覆盖空文件、账号异常、未知平台、禁用平台与缺实例。"""
    module = _module_with_config(monkeypatch)
    files = [DriveFile(file_id="f1", name="a.mkv", size=4)]

    empty = module.download_existing(account_id=7, files=[], download_dir=netdisk_env)
    assert empty is not None and empty[3] == "没有可下载的个人盘文件"

    class _BrokenAccountService:
        """账号查询失败的个人盘服务替身。"""

        def account(self, account_id: int) -> Any:
            """模拟账号不可用。"""
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "账号不可用")

    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", _BrokenAccountService)
    broken = module.download_existing(account_id=7, files=files, download_dir=netdisk_env)
    assert broken is not None and "账号不可用" in broken[3]

    class _NoPlatformService:
        """平台信息缺失的个人盘服务替身。"""

        def account(self, account_id: int) -> Any:
            """返回缺少平台的账号。"""
            return SimpleNamespace(platform=None)

    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", _NoPlatformService)
    unknown = module.download_existing(account_id=7, files=files, download_dir=netdisk_env)
    assert unknown is not None and unknown[3] == "未知的网盘平台"

    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", _QuarkService)
    monkeypatch.setattr(netdisk_module, "_platform_enabled", lambda platform: False)
    disabled = module.download_existing(account_id=7, files=files, download_dir=netdisk_env)
    assert disabled is not None and "平台已禁用" in disabled[3]

    monkeypatch.setattr(netdisk_module, "_platform_enabled", lambda platform: True)
    monkeypatch.setattr(module, "get_configs", lambda: {})
    no_config = module.download_existing(account_id=7, files=files, download_dir=netdisk_env)
    assert no_config is not None and no_config[3] == "未配置网盘下载器实例"


def test_download_existing_dedupes_and_tolerates_mkdir_failure(
    netdisk_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一批文件重复提交应复用任务标识，目录创建失败不阻断任务登记。"""
    module = _module_with_config(monkeypatch)
    monkeypatch.setattr(module, "_run_existing_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", _QuarkService)
    files = [DriveFile(file_id="f1", name="a.mkv", size=4)]

    first = module.download_existing(account_id=7, files=files, download_dir=netdisk_env)
    second = module.download_existing(account_id=7, files=files, download_dir=netdisk_env)
    assert first is not None and first[1]
    assert second is not None and second[1] == first[1]
    assert second[3] == "下载任务已存在"

    blocked_root = netdisk_env / "blocked"
    blocked_root.write_text("file", encoding="utf-8")
    module._tasks.clear()  # pylint: disable=protected-access
    tolerated = module.download_existing(account_id=8, files=files, download_dir=blocked_root)
    assert tolerated is not None and tolerated[1]


def test_existing_entries_normalizes_inputs() -> None:
    """个人盘条目应兼容映射与对象，并跳过目录、空标识、非法名与重名。"""
    entries = NetdiskModule._existing_entries([  # pylint: disable=protected-access
        {"file_id": "f1", "name": "a.mkv", "size": 5, "parent_id": "p"},
        SimpleNamespace(file_id="f2", name="b.mkv", size=3),
        DriveFile(file_id="", name="c.mkv"),
        DriveFile(file_id="f3", name=""),
        DriveFile(file_id="f4", name="d", is_dir=True),
        DriveFile(file_id="f5", name="../evil.mkv"),
        DriveFile(file_id="f6", name="a.mkv", size=9),
    ])

    assert [(item.name, item.size) for item in entries] == [
        ("a.mkv", 5), ("b.mkv", 3), ("evil.mkv", 0), ("f6_a.mkv", 9),
    ]
    assert entries[0].parent_id == "p"


def test_run_existing_task_skips_completed_and_throttles(
    netdisk_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已完成文件计入进度不重复下载，重复进度回调应被节流。"""
    module = NetdiskModule()
    completed_target = netdisk_env / "a.mkv"
    completed_target.write_bytes(b"xxxx")
    completed_target.with_name("a.mkv.done").touch()
    calls: List[Any] = []

    def fake_one(service: Any, account_id: int, platform: Any, item: Any, target: Path,
                 tuning: Any, stop_event: Any, on_progress: Any, media_runtime: Any) -> None:
        """回报两次相同进度以验证节流。"""
        calls.append(item.name)
        on_progress(item.name, 10.0, 100)
        on_progress(item.name, 10.0, 100)

    monkeypatch.setattr(module, "_download_existing_one", fake_one)
    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", lambda: SimpleNamespace())

    module._run_existing_task(  # pylint: disable=protected-access
        "t-existing",
        NetdiskPlatform.QUARK,
        7,
        [
            DriveFile(file_id="f1", name="a.mkv", size=4),
            DriveFile(file_id="f2", name="b.mkv", size=100),
        ],
        netdisk_env,
        NetdiskTuning(),
    )

    assert calls == ["b.mkv"]
    record = store.get_task("t-existing")
    assert record is not None
    assert record["partial"] is True
    assert record["missing_episodes"] == []


def test_run_existing_task_handles_mkdir_and_outer_error(
    netdisk_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务目录创建失败只记日志，服务构造失败应把任务标记为失败。"""
    module = NetdiskModule()
    blocked = netdisk_env / "blocked"
    blocked.write_text("file", encoding="utf-8")
    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", lambda: SimpleNamespace())

    module._run_existing_task(  # pylint: disable=protected-access
        "t-mkdir", NetdiskPlatform.QUARK, 7, [], blocked / "task", NetdiskTuning(),
    )
    assert store.get_task("t-mkdir")["state"] == "completed"

    class _BrokenService:
        """构造即失败的个人盘服务替身。"""

        def __init__(self) -> None:
            """模拟服务不可用。"""
            raise RuntimeError("service down")

    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", _BrokenService)
    module._run_existing_task(  # pylint: disable=protected-access
        "t-error",
        NetdiskPlatform.QUARK,
        7,
        [DriveFile(file_id="f1", name="a.mkv", size=4)],
        netdisk_env / "task2",
        NetdiskTuning(),
    )
    assert store.get_task("t-error")["state"] == "error"


def test_record_existing_outcome_all_failed(netdisk_env: Path) -> None:
    """全部文件失败时写入 error 并带上失败原因。"""
    module = NetdiskModule()
    entries = [
        DriveFile(file_id="f1", name="a.mkv", size=4),
        DriveFile(file_id="f2", name="b.mkv", size=4),
    ]
    module._record_existing_outcome(  # pylint: disable=protected-access
        "t-all-failed",
        entries,
        {"a.mkv": "直链失效", "b.mkv": "磁盘写入失败"},
        netdisk_env,
        None,
    )

    record = store.get_task("t-all-failed")
    assert record is not None
    assert record["state"] == "error"
    assert "直链失效" in record["error"]


def test_record_existing_outcome_skips_discarded(netdisk_env: Path) -> None:
    """已删除的任务不再写回终态。"""
    module = NetdiskModule()
    module._discarded.add("t-gone")  # pylint: disable=protected-access
    module._record_existing_outcome(  # pylint: disable=protected-access
        "t-gone",
        [DriveFile(file_id="f1", name="a.mkv", size=4)],
        {"a.mkv": "失败"},
        netdisk_env,
        None,
    )
    assert store.get_task("t-gone") is None


class _FakeHlsDownloader:
    """
    写出完成标记的 HLS 下载器替身。

    :param fail_not_hls: 是否抛出 NotHls 触发回退
    """

    fail_not_hls = False

    def __init__(self, **kwargs: Any) -> None:
        """保存构造参数。"""
        self.kwargs = kwargs

    def run(self, url_provider: Any) -> None:
        """写出内容或抛出 NotHls。"""
        if self.fail_not_hls:
            raise NotHls("not m3u8")
        target = self.kwargs["target"]
        target.write_bytes(b"hls")
        target.with_name(target.name + ".done").touch()
        callback = self.kwargs.get("on_progress")
        if callback:
            callback(100.0)


class _FakeChunkedDownloader:
    """
    写出指定长度内容的分片下载器替身。

    :param refresh: 是否在下载中请求刷新直链
    """

    refresh = False
    calls: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        """保存构造参数。"""
        self.kwargs = kwargs
        _FakeChunkedDownloader.calls.append(kwargs)

    def run(self, url_provider: Any) -> None:
        """写出内容并可选刷新直链。"""
        if self.refresh:
            url_provider(True)
        target = self.kwargs["target"]
        target.write_bytes(b"d" * int(self.kwargs["total"]))
        target.with_name(target.name + ".done").touch()
        callback = self.kwargs.get("on_progress")
        if callback:
            callback(100.0)


class _FakeFilesService:
    """
    返回固定直链的个人盘服务替身。

    :param link: 返回的直链
    """

    def __init__(self, link: DirectLink) -> None:
        """保存直链并初始化调用计数。"""
        self.link = link
        self.calls = 0

    def download(self, account_id: int, item: DriveFile) -> DirectLink:
        """记录调用并返回固定直链。"""
        self.calls += 1
        return self.link


def _download_existing_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                           service: _FakeFilesService, item: DriveFile) -> None:
    """
    以最小参数调用单文件下载。

    :param monkeypatch: pytest monkeypatch
    :param tmp_path: 任务目录
    :param service: 个人盘服务替身
    :param item: 文件条目
    """
    module = NetdiskModule()
    monkeypatch.setattr(netdisk_module, "_validate_video_duration", lambda *args: None)
    module._download_existing_one(  # pylint: disable=protected-access
        service, 7, NetdiskPlatform.QUARK, item, tmp_path / item.name,
        NetdiskTuning(), threading.Event(), lambda *args: None, None,
    )


def test_download_existing_one_hls_and_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HLS 直链走 HLS 下载器，NotHls 时回退普通分片下载。"""
    item = DriveFile(file_id="f1", name="a.mkv", size=4)
    service = _FakeFilesService(DirectLink(url="u", headers={}, size=4, is_hls=True))
    monkeypatch.setattr(netdisk_module, "HlsDownloader", _FakeHlsDownloader)
    monkeypatch.setattr(_FakeHlsDownloader, "fail_not_hls", False)
    _download_existing_one(monkeypatch, tmp_path, service, item)
    assert (tmp_path / "a.mkv").read_bytes() == b"hls"

    monkeypatch.setattr(_FakeHlsDownloader, "fail_not_hls", True)
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", _FakeChunkedDownloader)
    (tmp_path / "fallback").mkdir()
    _download_existing_one(monkeypatch, tmp_path / "fallback", service, item)
    assert (tmp_path / "fallback" / "a.mkv").read_bytes() == b"dddd"


def test_download_existing_one_refreshes_links(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """下载器请求刷新时应重新向个人盘取链。"""
    item = DriveFile(file_id="f1", name="a.mkv", size=4)
    service = _FakeFilesService(DirectLink(url="u", headers={}, size=4))
    monkeypatch.setattr(netdisk_module, "ChunkedDownloader", _FakeChunkedDownloader)
    monkeypatch.setattr(_FakeChunkedDownloader, "refresh", True)
    try:
        _download_existing_one(monkeypatch, tmp_path, service, item)
    finally:
        monkeypatch.setattr(_FakeChunkedDownloader, "refresh", False)

    assert service.calls == 2


def test_download_existing_one_rejects_unknown_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """无法确定大小时抛出下载错误，避免写出空壳文件。"""
    item = DriveFile(file_id="f1", name="a.mkv", size=0)
    service = _FakeFilesService(DirectLink(url="u", headers={}, size=0))

    with pytest.raises(DownloadError):
        _download_existing_one(monkeypatch, tmp_path, service, item)


def test_instance_tuning_ignores_invalid_overrides() -> None:
    """非法平台覆盖值与非法整型配置回落到默认值，文本布尔值可解析。"""
    config = SimpleNamespace(
        config={"platform_threads": {"quark": "bad"}, "threads": "bad", "preempt": "on"},
    )

    tuning = NetdiskModule._instance_tuning(config, NetdiskPlatform.QUARK)  # pylint: disable=protected-access

    assert tuning.preempt_enabled is True
    assert tuning.threads >= 1


# ---------------------------------------------------------------------------
# 通用网盘下载器模块：文件收集与并发
# ---------------------------------------------------------------------------


def test_pending_files_skips_completed(tmp_path: Path) -> None:
    """已带完成标记的文件不再进入待下载列表，但计入初始进度。"""
    target = tmp_path / "a.mkv"
    target.write_bytes(b"xxxx")
    target.with_name("a.mkv.done").touch()
    state: Dict[str, Any] = {"completed": 0.0}

    pending = NetdiskModule._pending_files(  # pylint: disable=protected-access
        [ShareFile(file_id="f1", name="a.mkv", size=4)], tmp_path, state,
    )

    assert pending == []
    assert state["completed"] == 4.0


def test_download_pending_files_propagates_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """用户暂停应向上抛出取消异常，而不是记成文件失败。"""
    module = NetdiskModule()

    def cancel(*args: Any, **kwargs: Any) -> None:
        """模拟下载被取消。"""
        raise DownloadCancelled("stopped")

    monkeypatch.setattr(module, "_download_one", cancel)
    writer = SimpleNamespace(discard=lambda name: None, complete=lambda name, size: None)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)

    with pytest.raises(DownloadCancelled):
        module._download_pending_files(  # pylint: disable=protected-access
            [file],
            files=[file],
            resolver=SimpleNamespace(tuning=NetdiskTuning()),
            platform=NetdiskPlatform.QUARK,
            share_url="u",
            passcode="",
            task_dir=tmp_path,
            task_key="t1",
            stop_event=threading.Event(),
            share_token="",
            task_tuning=NetdiskTuning(),
            progress_writer=writer,
            episode_runtimes=None,
            media_runtime=None,
        )


def test_discard_partial_outputs_ignores_unlink_error(tmp_path: Path) -> None:
    """残片是目录导致删除失败时只跳过，不中断任务收尾。"""
    target = tmp_path / "a.mkv"
    target.mkdir()
    (target / "child").write_text("x", encoding="utf-8")

    NetdiskModule._discard_partial_outputs(  # pylint: disable=protected-access
        tmp_path, [ShareFile(file_id="f1", name="a.mkv", size=1)],
    )

    assert target.exists()


def test_ensure_local_space_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """无需写入时跳过检查，读取余量失败也跳过，余量不足时抛出。"""
    NetdiskModule._ensure_local_space(tmp_path, 0)  # pylint: disable=protected-access

    def broken(path: Any) -> Any:
        """模拟磁盘信息不可读。"""
        raise OSError("denied")

    monkeypatch.setattr(netdisk_module.shutil, "disk_usage", broken)
    NetdiskModule._ensure_local_space(tmp_path, 10)  # pylint: disable=protected-access

    monkeypatch.setattr(netdisk_module.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(DownloadError):
        NetdiskModule._ensure_local_space(tmp_path, 10)  # pylint: disable=protected-access


def test_task_dir_keeps_same_named_directory(tmp_path: Path) -> None:
    """下载根目录已以分享码命名时直接复用，不再嵌套一层。"""
    root = tmp_path / "abc"
    assert NetdiskModule._task_dir(root, "abc") == root  # pylint: disable=protected-access
    assert NetdiskModule._task_dir(tmp_path, "abc") == tmp_path / "abc"  # pylint: disable=protected-access


def test_collect_files_dedupes_duplicate_names() -> None:
    """递归拍平后的同名文件应加唯一前缀而不是静默丢弃。"""
    module = NetdiskModule()
    files = [
        ShareFile(file_id=f"abcdef{index}", name="01.mkv", size=1)
        for index in range(1, 4)
    ]

    result = module._collect_files(files)  # pylint: disable=protected-access

    assert [item.name for item in result] == ["01.mkv", "abcdef_01.mkv", "_abcdef_01.mkv"]


def test_collect_files_ignores_invalid_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """清洗后为空的名字应被忽略。"""
    module = NetdiskModule()
    monkeypatch.setattr(netdisk_module, "_safe_filename", lambda name: "")

    assert module._collect_files([ShareFile(file_id="f1", name="01.mkv", size=1)]) == []  # pylint: disable=protected-access


def test_filter_by_episode_targets_fallback() -> None:
    """目标季无精确命中时用无法解析集数的文件兜底，特典始终排除。"""
    fallback = [ShareFile(file_id="f1", name="Movie.mp4", size=1)]
    assert len(NetdiskModule._filter_by_episode_targets(fallback, {1: {1}})) == 1  # pylint: disable=protected-access

    special = [ShareFile(file_id="f2", name="花絮.mp4", size=1)]
    assert NetdiskModule._filter_by_episode_targets(special, {1: {1}}) == []  # pylint: disable=protected-access


def test_season_from_path_variants() -> None:
    """季号可从中文目录名、标准季集标记中提取，无法识别时返回 None。"""
    assert NetdiskModule._season_from_path("第2季/05.mkv") == 2  # pylint: disable=protected-access
    assert NetdiskModule._season_from_path("Show.S03E01.mkv") == 3  # pylint: disable=protected-access
    assert NetdiskModule._season_from_path("") is None  # pylint: disable=protected-access


def test_assign_accounts_requires_two_active_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有一个可用账号时不按容量拆分文件。"""
    module = NetdiskModule()
    views = [SimpleNamespace(id=1, status="active", quota_total=100, quota_used=0)]
    monkeypatch.setattr(
        netdisk_module, "NetdiskAccountService",
        lambda *args, **kwargs: SimpleNamespace(list=lambda platform: views),
    )
    files = [ShareFile(file_id="1", name="01.mp4", size=10), ShareFile(file_id="2", name="02.mp4", size=10)]

    assert module._assign_accounts(NetdiskPlatform.QUARK, files) == {}  # pylint: disable=protected-access


def test_limit_workers_for_task_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配器不可用或免转存时不限制并发，转存平台按剩余空间夹紧。"""
    module = NetdiskModule()
    files = [ShareFile(file_id="1", name="a.mp4", size=100)]
    tuning = NetdiskTuning(auto_transfer=True, threads=2)

    def broken(platform: Any) -> Any:
        """模拟适配器查找失败。"""
        raise RuntimeError("registry down")

    monkeypatch.setattr(netdisk_module, "find_adapter", broken)
    assert module._limit_workers_for_task(4, files, NetdiskPlatform.QUARK, tuning) == 4  # pylint: disable=protected-access

    monkeypatch.setattr(
        netdisk_module, "find_adapter",
        lambda platform: SimpleNamespace(link_policy=LinkPolicy.DIRECT),
    )
    assert module._limit_workers_for_task(4, files, NetdiskPlatform.QUARK, tuning) == 4  # pylint: disable=protected-access

    monkeypatch.setattr(
        netdisk_module, "find_adapter",
        lambda platform: SimpleNamespace(link_policy=LinkPolicy.TRANSFER_REQUIRED),
    )
    monkeypatch.setattr(module, "_most_free_bytes", lambda platform: 0)
    assert module._limit_workers_for_task(4, files, NetdiskPlatform.QUARK, tuning) == 4  # pylint: disable=protected-access

    monkeypatch.setattr(module, "_most_free_bytes", lambda platform: 50)
    assert module._limit_workers_for_task(4, files, NetdiskPlatform.QUARK, tuning) == 1  # pylint: disable=protected-access


def test_most_free_bytes_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """账号读取失败返回 0，否则返回各账号剩余空间最大值。"""
    def broken(*args: Any, **kwargs: Any) -> Any:
        """模拟账号服务不可用。"""
        raise RuntimeError("accounts down")

    monkeypatch.setattr(netdisk_module, "NetdiskAccountService", broken)
    assert NetdiskModule._most_free_bytes(NetdiskPlatform.QUARK) == 0  # pylint: disable=protected-access

    views = [
        SimpleNamespace(quota_total=100, quota_used=40),
        SimpleNamespace(quota_total=0, quota_used=0),
    ]
    monkeypatch.setattr(
        netdisk_module, "NetdiskAccountService",
        lambda *args, **kwargs: SimpleNamespace(list=lambda platform: views),
    )
    assert NetdiskModule._most_free_bytes(NetdiskPlatform.QUARK) == 60  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# 通用网盘下载器模块：清理与任务抽象
# ---------------------------------------------------------------------------


def test_active_temp_directories_collects_handles(netdisk_env: Path) -> None:
    """应收集所有任务登记的转存目录标识，跳过非法条目与空标识。"""
    store.upsert_task("h1", {"cleanup_handles": [
        {"directory_id": "d1"}, "bad", {"directory_id": ""},
    ]})

    assert NetdiskModule._active_temp_directories() == {"d1"}  # pylint: disable=protected-access


def test_cleanup_links_removes_cleaned_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认清理成功的句柄应从任务记录中注销。"""
    handle = TransferHandle(platform=NetdiskPlatform.QUARK, account_id=1, directory_id="d1")
    link = DirectLink(url="", headers={}, cleanup_handle=handle)
    resolver = SimpleNamespace(cleanup=lambda item: True)
    calls: List[Any] = []
    monkeypatch.setattr(store, "remove_cleanup_handles", lambda key, ids: calls.append((key, ids)))

    NetdiskModule._cleanup_links(resolver, [link, DirectLink(url="", headers={})], "h1")  # pylint: disable=protected-access

    assert calls == [("h1", ["d1"])]


def test_cleanup_persisted_handles_skips_invalid_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """持久化句柄中非法平台、缺失目录的条目被跳过，有效句柄仍会清理。"""
    module = NetdiskModule()
    cleaned: List[Any] = []
    monkeypatch.setattr(
        netdisk_module, "NetdiskResolveService",
        lambda: SimpleNamespace(cleanup=lambda link: cleaned.append(link) or True),
    )
    monkeypatch.setattr(store, "remove_cleanup_handles", lambda key, ids: None)
    record = {"cleanup_handles": [
        "bad",
        {"platform": "nope", "directory_id": "d0"},
        {"platform": "quark"},
        {"platform": "quark", "account_id": 1, "directory_id": "d1", "file_ids": ["f1"]},
    ]}

    module._cleanup_persisted_handles("h1", record)  # pylint: disable=protected-access

    assert len(cleaned) == 1
    assert cleaned[0].cleanup_handle.directory_id == "d1"


def test_list_torrents_filters_wanted_and_downloader(netdisk_env: Path) -> None:
    """按 hash、下载器与状态过滤任务列表，默认隐藏已整理任务。"""
    store.upsert_task("h1", {"state": "downloading", "downloader": "A"})
    store.upsert_task("h2", {"state": "completed", "downloader": "B"})
    store.upsert_task("h3", {"state": "transferred", "downloader": "B"})
    module = NetdiskModule()

    assert [item.hash for item in module.list_torrents(hashs="h1") or []] == ["h1"]
    assert [item.hash for item in module.list_torrents(downloader="A") or []] == ["h1"]
    assert [item.hash for item in module.list_torrents(status=TorrentQueryStatus.COMPLETED) or []] == ["h2"]


def test_torrent_files_returns_none_for_missing(netdisk_env: Path) -> None:
    """任务不存在时文件清单返回 None，存在时按记录投影。"""
    module = NetdiskModule()
    assert module.torrent_files("nope") is None

    store.upsert_task("h1", {"files": [{"name": "a.mp4", "size": 10, "progress": 0.5}]})
    files = module.torrent_files("h1")
    assert files is not None and files[0].name == "a.mp4"


def test_remove_torrents_handles_missing_and_stop(netdisk_env: Path) -> None:
    """删除任务应置位停止信号，并在文件删除失败时继续收尾。"""
    module = NetdiskModule()
    assert module.remove_torrents("nope") is True

    event = threading.Event()
    module._stops["h1"] = event  # pylint: disable=protected-access
    target_dir = netdisk_env / "abc"
    target_dir.mkdir()
    (target_dir / "d").mkdir()
    store.upsert_task("h1", {"path": str(target_dir), "files": [{"name": "d"}]})

    assert module.remove_torrents("h1") is True
    assert event.is_set()
    assert store.get_task("h1") is None


def test_cleanup_orphans_edge_branches(
    netdisk_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """孤儿清理应跳过缺失根目录、符号链接与未确认整理的非空目录。"""
    module = NetdiskModule()
    root = netdisk_env / "downloads"
    root.mkdir()
    real = netdisk_env / "real"
    real.mkdir()
    (root / "link").symlink_to(real, target_is_directory=True)

    child = root / "old"
    child.mkdir()
    (child / ".netdisk-task").touch()
    (child / "a.mp4").write_bytes(b"x")
    os.utime(child, (0, 0))

    assert module.cleanup_orphans([str(netdisk_env / "missing")])["empty_removed"] == 0
    module.cleanup_orphans([str(root)], empty_grace_seconds=0, nonempty_grace_seconds=0)
    assert child.exists()
    module.cleanup_orphans(
        [str(root)], nonempty_grace_seconds=0, is_transferred=lambda path: False,
    )
    assert child.exists()

    def broken_rmtree(path: Any) -> None:
        """模拟目录删除失败。"""
        raise OSError("busy")

    monkeypatch.setattr(netdisk_module.shutil, "rmtree", broken_rmtree)
    stats = module.cleanup_orphans(
        [str(root)], nonempty_grace_seconds=0, is_transferred=lambda path: True,
    )
    assert stats["transferred_removed"] == 0
    assert child.exists()

    original_iterdir = Path.iterdir

    def broken_iterdir(self: Path) -> Any:
        """模拟根目录不可读。"""
        if self == root:
            raise OSError("denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", broken_iterdir)
    assert module.cleanup_orphans([str(root)])["empty_removed"] == 0


def test_cleanup_terminal_handles_counts_records(netdisk_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """终态任务记录中残留的转存句柄应被清理并计数。"""
    module = NetdiskModule()
    calls: List[str] = []
    monkeypatch.setattr(module, "_cleanup_persisted_handles", lambda key, record: calls.append(key))
    store.upsert_task("h1", {"state": "completed", "cleanup_handles": [{"directory_id": "d"}]})
    store.upsert_task("h2", {"state": "downloading", "cleanup_handles": [{"directory_id": "d"}]})

    assert module._cleanup_terminal_handles() == 1  # pylint: disable=protected-access
    assert calls == ["h1"]


def test_mark_reconciled_and_transfer_completed(netdisk_env: Path) -> None:
    """对账标记与整理完成标记应只更新存在的任务。"""
    module = NetdiskModule()
    store.upsert_task("h1", {"state": "error"})
    store.upsert_task("h2", {"state": "completed"})

    module.mark_reconciled(["h1", "nope"])
    module.transfer_completed("h2")

    assert store.get_task("h1")["reconciled"] is True
    assert store.get_task("h2")["state"] == "transferred"
    assert store.get_task("nope") is None


def test_start_torrents_branches(netdisk_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复任务应跳过缺失记录与信息不全的任务，禁用平台改为暂停。"""
    module = _module_with_config(monkeypatch)
    monkeypatch.setattr(module, "_run_task", lambda *args, **kwargs: None)
    record = {
        "state": "paused",
        "share_code": "abc",
        "share_url": "https://pan.quark.cn/s/abc",
        "netdisk_platform": "quark",
        "path": str(netdisk_env),
        "downloader": "网盘",
    }
    store.upsert_task("h1", record)
    store.upsert_task("h2", {**record, "share_url": ""})

    assert module.start_torrents("h1") is True
    assert module.start_torrents(["nope", "h2"]) is None
    assert module.start_torrents("h1") is None

    store.upsert_task("h3", record)
    monkeypatch.setattr(netdisk_module, "_platform_enabled", lambda platform: False)
    assert module.start_torrents("h3") is None
    assert store.get_task("h3")["paused_reason"] == "platform_disabled"


def test_stop_torrents_branches(netdisk_env: Path) -> None:
    """停止任务应优先置位内存信号，线程已退出时把记录改为暂停。"""
    module = NetdiskModule()
    event = threading.Event()
    module._stops["h1"] = event  # pylint: disable=protected-access
    store.upsert_task("h2", {"state": "downloading"})

    assert module.stop_torrents(["h1", "h2"]) is True
    assert event.is_set()
    assert module._pause_reasons["h1"] == "user"  # pylint: disable=protected-access
    assert store.get_task("h2")["state"] == "paused"
    assert module.stop_torrents("nope") is False


def test_downloader_info_sums_speed(netdisk_env: Path) -> None:
    """下载器概览应累加在跑任务速度，并按实例名过滤。"""
    store.upsert_task("h1", {"state": "downloading", "downloader": "网盘", "dlspeed_bytes": 100})
    store.upsert_task("h2", {"state": "downloading", "downloader": "网盘", "dlspeed_bytes": 50})
    store.upsert_task("h3", {"state": "completed", "downloader": "网盘", "dlspeed_bytes": 999})
    store.upsert_task("h4", {"state": "downloading", "downloader": "其它", "dlspeed_bytes": 7})
    module = NetdiskModule()

    info = module.downloader_info("网盘")

    assert info is not None
    assert info[0].download_speed == 150
