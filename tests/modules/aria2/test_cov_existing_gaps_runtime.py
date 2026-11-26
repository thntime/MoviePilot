"""
补齐存量文件未覆盖分支：aria2 RPC 客户端与插件运行时持久化端口。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.modules.aria2 import aria2 as aria2_client
from app.modules.aria2.aria2 import Aria2
from app.runtime.extensions.plugin.storage import (
    PluginConfigStore,
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)


class _FakeResponse:
    """
    只暴露 json() 的 HTTP 响应替身。
    """

    def __init__(self, payload) -> None:
        """
        保存待返回的 JSON 载荷。
        """
        self._payload = payload

    def json(self):
        """
        返回预设载荷。
        """
        return self._payload


class _FakeClient:
    """
    记录请求并按预设响应或异常作答的 HTTP 客户端替身。
    """

    def __init__(self) -> None:
        """
        初始化空请求记录与默认响应。
        """
        self.requests: list[tuple[str, dict]] = []
        self.response = None
        self.error: Exception | None = None

    def post_res(self, url, json=None):
        """
        记录请求后返回预设响应，必要时抛出预设异常。
        """
        self.requests.append((url, json))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture(name="aria2_env")
def fixture_aria2_env(monkeypatch):
    """
    用可观测的客户端替身替换 RequestUtils 构造。
    """
    created: list[_FakeClient] = []

    def factory(**_kwargs):
        client = _FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr(aria2_client, "RequestUtils", factory)
    return created


def _client(created: list[_FakeClient]) -> _FakeClient:
    """
    取出最近一次构造的客户端替身。
    """
    return created[-1]


@pytest.mark.parametrize(
    ("host", "port", "use_ssl", "expected"),
    [
        (None, None, False, None),
        ("  ", None, False, None),
        ("127.0.0.1", None, False, "http://127.0.0.1:6800"),
        ("10.0.0.2", 6800, True, "https://10.0.0.2:6800"),
        ("127.0.0.1:7000", 6800, False, "http://127.0.0.1:7000"),
        ("https://aria.example.com/rpc/", None, False, "https://aria.example.com:6800"),
        ("[::1]", 6800, True, "https://[::1]:6800"),
    ],
)
def test_base_url_normalizes_host_configuration(aria2_env, host, port, use_ssl, expected):
    """
    主机可省略协议与端口，完整 URL 与 IPv6 地址都保留原语义。
    """
    client = Aria2(host=host, port=port, use_ssl=use_ssl, secret=None)

    assert client._base_url == expected  # pylint: disable=protected-access


def test_missing_host_keeps_unconnected_state(aria2_env):
    """
    缺少主机时不构造可调用地址，RPC 调用直接跳过。
    """
    client = Aria2()

    assert client._call("aria2.getVersion", []) is None  # pylint: disable=protected-access
    assert _client(aria2_env).requests == []


def test_call_prefixes_secret_token_and_returns_result(aria2_env):
    """
    已配置令牌时放在参数首位，并返回 RPC 的 result 字段。
    """
    client = Aria2(host="127.0.0.1", port=6800, secret=" tok ")
    _client(aria2_env).response = _FakeResponse({"result": {"version": "1.37.0"}})

    assert client._call("aria2.getVersion", []) == {"version": "1.37.0"}  # pylint: disable=protected-access
    url, payload = _client(aria2_env).requests[0]
    assert url == "http://127.0.0.1:6800/jsonrpc"
    assert payload["id"] == "1"
    assert payload["params"] == ["token:tok"]


def test_call_returns_none_when_service_unreachable(aria2_env):
    """
    无法建立连接时返回 None 而不抛出。
    """
    client = Aria2(host="127.0.0.1")
    _client(aria2_env).response = None

    assert client._call("aria2.getVersion", []) is None  # pylint: disable=protected-access


def test_call_returns_none_on_rpc_error_payload(aria2_env):
    """
    RPC 返回 error 字段时返回 None。
    """
    client = Aria2(host="127.0.0.1")
    _client(aria2_env).response = _FakeResponse({"error": {"message": "invalid token"}})

    assert client._call("aria2.getVersion", []) is None  # pylint: disable=protected-access


def test_call_returns_none_on_unexpected_exception(aria2_env):
    """
    请求过程抛出异常时记录并返回 None。
    """
    client = Aria2(host="127.0.0.1")
    _client(aria2_env).error = RuntimeError("boom")

    assert client._call("aria2.getVersion", []) is None  # pylint: disable=protected-access


def test_is_inactive_and_reconnect_rebuild_client(aria2_env):
    """
    aria2 没有登录会话，重连只重建 HTTP 客户端。
    """
    client = Aria2(host="127.0.0.1")
    first = _client(aria2_env)

    assert client.is_inactive() is False
    client.reconnect()

    assert len(aria2_env) == 2
    assert client._client is not first  # pylint: disable=protected-access


def test_transfer_info_returns_mapping_only(aria2_env):
    """
    全局统计只接受字典结果。
    """
    client = Aria2(host="127.0.0.1")
    _client(aria2_env).response = _FakeResponse({"result": {"downloadSpeed": "10"}})
    assert client.transfer_info() == {"downloadSpeed": "10"}

    _client(aria2_env).response = _FakeResponse({"result": []})
    assert client.transfer_info() is None


def test_add_uri_and_add_torrent_return_gid(aria2_env):
    """
    直链与种子提交都返回字符串 gid，失败时返回 None。
    """
    client = Aria2(host="127.0.0.1")
    _client(aria2_env).response = _FakeResponse({"result": "gid-1"})

    assert client.add_uri(["https://example.com/a.mkv"], {"dir": "/tmp"}) == "gid-1"
    assert _client(aria2_env).requests[0][1]["params"] == [["https://example.com/a.mkv"], {"dir": "/tmp"}]

    assert client.add_torrent(b"torrent-bytes", {}) == "gid-1"
    assert _client(aria2_env).requests[1][1]["params"] == ["dG9ycmVudC1ieXRlcw==", [], {}]

    _client(aria2_env).response = _FakeResponse({"result": None})
    assert client.add_uri(["https://example.com/a.mkv"], {}) is None


def test_add_torrent_returns_none_when_content_not_bytes(aria2_env):
    """
    种子内容无法 base64 编码时返回 None，不发请求。
    """
    client = Aria2(host="127.0.0.1")

    assert client.add_torrent(object(), {}) is None
    assert _client(aria2_env).requests == []


def test_tell_status_requires_gid_and_returns_mapping(aria2_env):
    """
    空 gid 直接返回 None，正常结果只接受字典。
    """
    client = Aria2(host="127.0.0.1")
    assert client.tell_status("") is None

    _client(aria2_env).response = _FakeResponse({"result": {"gid": "gid-1"}})
    assert client.tell_status("gid-1") == {"gid": "gid-1"}

    _client(aria2_env).response = _FakeResponse({"result": "bad"})
    assert client.tell_status("gid-1") is None


def test_tell_stopped_returns_list_only(aria2_env):
    """
    已停止任务列表只接受数组结果。
    """
    client = Aria2(host="127.0.0.1")
    _client(aria2_env).response = _FakeResponse({"result": [{"gid": "gid-1"}]})
    assert client.tell_stopped() == [{"gid": "gid-1"}]

    _client(aria2_env).response = _FakeResponse({"result": {"gid": "gid-1"}})
    assert client.tell_stopped() == []


def test_get_files_requires_gid_and_returns_list(aria2_env):
    """
    空 gid 直接返回空列表，正常结果只接受数组。
    """
    client = Aria2(host="127.0.0.1")
    assert client.get_files("") == []

    _client(aria2_env).response = _FakeResponse({"result": [{"index": "1"}]})
    assert client.get_files("gid-1") == [{"index": "1"}]

    _client(aria2_env).response = _FakeResponse({"result": "bad"})
    assert client.get_files("gid-1") == []


def test_remove_falls_back_to_download_result(aria2_env):
    """
    取消运行中任务失败后继续清理已停止任务。
    """
    client = Aria2(host="127.0.0.1")
    assert client.remove("") is False

    _client(aria2_env).response = _FakeResponse({"result": "OK"})
    assert client.remove("gid-1") is True
    assert len(_client(aria2_env).requests) == 1

    _client(aria2_env).response = _FakeResponse({"result": None})
    assert client.remove("gid-1") is False


def test_delete_torrents_keeps_files_and_reports_success(aria2_env):
    """
    删除任务不删除文件，空 gid 与失败都返回 False。
    """
    client = Aria2(host="127.0.0.1")
    assert client.delete_torrents(True, "") is False

    _client(aria2_env).response = _FakeResponse({"result": "OK"})
    assert client.delete_torrents(True, "gid-1") is True

    _client(aria2_env).response = _FakeResponse({"result": None})
    assert client.delete_torrents(True, "gid-1") is False


def test_pause_unpause_change_option_and_save_session(aria2_env):
    """
    任务控制接口只在请求成功且参数完整时返回 True。
    """
    client = Aria2(host="127.0.0.1")

    assert client.pause("") is False
    assert client.unpause("") is False
    assert client.change_option("gid-1", {}) is False
    assert client.change_option("", {"max-download-limit": "1M"}) is False

    _client(aria2_env).response = _FakeResponse({"result": "OK"})
    assert client.pause("gid-1") is True
    assert client.unpause("gid-1") is True
    assert client.change_option("gid-1", {"max-download-limit": "1M"}) is True
    assert client.save_session() is True

    _client(aria2_env).response = _FakeResponse({"result": None})
    assert client.save_session() is False


def test_default_plugin_storage_ports_are_inert():
    """
    组合根未装配时读写端口返回空值与 False，不抛异常。
    """
    storage = PluginStorage()

    assert storage.read("key") is None
    assert storage.write("key", 1) is None
    assert asyncio.run(storage.async_write("key", 1)) is None
    assert storage.delete("key") is False
    assert storage.read_config("plugin") is None
    assert storage.write_config("plugin", {}) is None
    assert asyncio.run(storage.async_write_config("plugin", {})) is None
    assert storage.delete_config("plugin") is False
    assert storage.delete_data("plugin") is None
    assert storage.read_log_level("plugin") == (None, None)
    assert storage.write_log_level("plugin", "DEBUG", None) is None


def test_config_store_async_write_rejects_unknown_plugin():
    """
    异步保存与同步保存一样，默认拒绝未登记插件的配置写入。
    """
    storage = MagicMock()
    store = PluginConfigStore(
        storage=lambda: storage,
        database=lambda: MagicMock(),
        plugin_exists=lambda _plugin_id: False,
    )

    assert asyncio.run(store.async_write("missing", {})) is False
    storage.async_write_config.assert_not_called()


def test_config_store_splits_data_rows_from_database_destroy():
    """
    删除插件数据行与销毁自有数据库可以分别执行。
    """
    storage = MagicMock()
    database = MagicMock()
    store = PluginConfigStore(
        storage=lambda: storage,
        database=lambda: database,
        plugin_exists=lambda _plugin_id: True,
    )

    store.delete_data_rows("plugin")
    store.destroy_database("plugin")

    storage.delete_data.assert_called_once_with("plugin")
    database.destroy.assert_called_once_with("plugin")


def test_instance_directory_defaults_are_empty():
    """
    实例表端口未装配时读取返回空值、写入返回 False。
    """
    directory = PluginInstanceDirectory()

    assert directory.get("plugin") is None
    assert directory.list_all() == []
    assert directory.list_by_source("plugin") == []
    assert directory.save(SimpleNamespace(instance_id="plugin")) is None
    assert directory.delete("plugin") is False
    assert directory.list_enabled() == []
    assert directory.set_enabled("plugin", True) is False
    assert directory.purge_invalid_empty_host("plugin") is False


class _BootstrappedLock:
    """
    进入临界区时模拟另一线程已完成引导的锁替身。
    """

    def __init__(self, store: PluginInstanceStore) -> None:
        """
        绑定待改写完成标记的实例存储。
        """
        self._store = store

    def __enter__(self) -> "_BootstrappedLock":
        """
        在持锁前把完成标记置真。
        """
        self._store._bootstrap_checked = True  # pylint: disable=protected-access
        return self

    def __exit__(self, *_exc_info) -> bool:
        """
        不吞异常。
        """
        return False


def test_bootstrap_rechecks_completed_flag_under_lock(monkeypatch):
    """
    等锁期间另一线程已完成引导时，本次调用不再重复合并旧键。
    """
    store = PluginInstanceStore(storage=lambda: MagicMock(), directory=lambda: MagicMock())
    merged: list[int] = []
    monkeypatch.setattr(store, "_merge_legacy_instances", lambda: merged.append(1))
    store._bootstrap_lock = _BootstrappedLock(store)  # pylint: disable=protected-access

    store._ensure_bootstrapped()  # pylint: disable=protected-access

    assert merged == []


def test_legacy_instances_accepts_list_payload_and_skips_invalid_entries():
    """
    旧键为列表形态时按 instance_id 归集，非法条目被跳过。
    """
    storage = MagicMock()
    storage.read.return_value = [
        {"instance_id": "clone", "source_plugin_id": "host"},
        "not-a-dict",
        {"source_plugin_id": "host"},
        {"instance_id": "broken", "source_plugin_id": "host", "is_enabled": "maybe"},
    ]
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: MagicMock())

    instances = store._legacy_instances()  # pylint: disable=protected-access

    assert set(instances) == {"clone"}
    assert instances["clone"].instance.source_plugin_id == "host"


def test_legacy_instances_accepts_dict_payload_and_rejects_other_shapes():
    """
    旧键为字典形态时直接使用，其他形态视为没有历史实例。
    """
    storage = MagicMock()
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: MagicMock())

    storage.read.return_value = {"clone": {"source_plugin_id": "host"}}
    assert set(store._legacy_instances()) == {"clone"}  # pylint: disable=protected-access

    storage.read.return_value = "broken"
    assert store._legacy_instances() == {}  # pylint: disable=protected-access


def test_configure_plugin_storage_replaces_global_port():
    """
    组合根可替换插件运行时持久化实现，并可恢复原值。
    """
    original = get_plugin_storage()
    replacement = PluginStorage()

    try:
        configure_plugin_storage(replacement)
        assert get_plugin_storage() is replacement
    finally:
        configure_plugin_storage(original)
