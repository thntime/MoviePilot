"""
验证夸克下载器模块的下载分流、任务抽象与取链客户端。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.modules.quark import QuarkModule, _share_code, store
from app.modules.quark import quark as quark_client
from app.modules.quark.quark import QuarkClient, QuarkError
from app.schemas.types import TorrentQueryStatus


class FakeResponse:
    """
    最小 HTTP 响应替身。
    """

    def __init__(self, payload: Optional[dict], status_code: int = 200,
                 set_cookie: str = "") -> None:
        """
        保存响应体与头部。
        """
        self._payload = payload
        self.status_code = status_code
        self.headers = {"set-cookie": set_cookie}
        self.closed = False

    def json(self) -> Optional[dict]:
        """
        返回预设 JSON。
        """
        return self._payload

    def close(self) -> None:
        """
        标记已关闭。
        """
        self.closed = True


class FakeHttp:
    """
    按路径返回预设响应的假适配器。
    """

    def __init__(self) -> None:
        """
        初始化调用记录与响应表。
        """
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, FakeResponse] = {}

    def post_res(self, url: str, **kwargs: Any) -> Optional[FakeResponse]:
        """
        记录并返回预设响应。
        """
        self.calls.append({"method": "POST", "url": url, "body": kwargs.get("json")})
        for key, response in self.responses.items():
            if key in url:
                return response
        return FakeResponse({"status": 200, "code": 0, "data": {}})

    def get_res(self, url: str, **kwargs: Any) -> Optional[FakeResponse]:
        """
        记录并返回预设响应。
        """
        self.calls.append({"method": "GET", "url": url, "params": kwargs.get("params")})
        for key, response in self.responses.items():
            if key in url:
                return response
        return FakeResponse({"status": 200, "code": 0, "data": {}})


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    把任务存储指向临时目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield


def test_share_code_parsing() -> None:
    """
    分享链接应解析出分享码，忽略锚点与查询参数。
    """
    assert _share_code("https://pan.quark.cn/s/abc123#/list/share") == "abc123"
    assert _share_code("https://pan.quark.cn/s/abc123?pwd=9527") == "abc123"
    assert _share_code("https://pan.quark.cn/other") == ""


def test_download_ignores_non_netdisk() -> None:
    """
    非网盘内容不归本模块处理，必须返回 None 交给其他下载器。
    """
    module = QuarkModule()
    assert module.download("magnet:?xt=urn:btih:abc", Path("/tmp"), "") is None
    assert module.download("magnet:?xt=urn:btih:abc", Path("/tmp"), "", download_type="torrent") is None


def test_download_rejects_unsupported_platform() -> None:
    """
    非夸克平台直接返回明确错误。
    """
    module = QuarkModule()
    result = module.download(
        "https://pan.quark.cn/s/abc", Path("/tmp"), "", download_type="netdisk",
        netdisk_platform="pan115",
    )
    assert result is not None
    assert "暂不支持" in result[3]


def test_download_requires_instance() -> None:
    """
    未配置下载器实例时返回明确错误。
    """
    module = QuarkModule()
    module._instances = {}  # pylint: disable=protected-access
    module._configs = {}  # pylint: disable=protected-access
    result = module.download(
        "https://pan.quark.cn/s/abc", Path("/tmp"), "", download_type="netdisk",
        netdisk_platform="quark",
    )
    assert result is not None
    assert result[3] == "未配置夸克下载器实例"


def test_task_store_and_list_torrents() -> None:
    """
    任务写入后应能被任务列表与文件清单读到，删除后消失。
    """
    module = QuarkModule()
    store.upsert_task("t1", {
        "title": "quark:abc",
        "name": "abc",
        "downloader": "q",
        "state": "completed",
        "progress": 100.0,
        "size": 1024.0,
        "path": "/tmp",
        "files": [{"name": "a.mp4", "size": 1024, "progress": 100.0}],
    })

    transfer = module.list_torrents(status=TorrentQueryStatus.TRANSFER)
    assert transfer is not None
    assert [item.hash for item in transfer] == ["t1"]
    assert transfer[0].progress == 100.0

    files = module.torrent_files("t1")
    assert files is not None
    assert files[0].name == "a.mp4"

    module.transfer_completed("t1")
    assert store.get_task("t1")["state"] == "transferred"

    assert module.remove_torrents("t1") is True
    assert store.get_task("t1") is None


def test_list_torrents_filters_unfinished() -> None:
    """
    未完成的任务不出现在整理查询结果中。
    """
    module = QuarkModule()
    store.upsert_task("t2", {"state": "downloading", "progress": 10.0, "files": []})
    assert module.list_torrents(status=TorrentQueryStatus.TRANSFER) == []


def test_client_share_token_and_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    取令牌与免转存取链应使用约定的请求体。
    """
    http = FakeHttp()
    http.responses["sharepage/token"] = FakeResponse(
        {"status": 200, "code": 0, "data": {"stoken": "st"}}
    )
    http.responses["file/download"] = FakeResponse({
        "status": 200,
        "code": 0,
        "data": [{"download_url": "https://cdn/x", "file_name": "a.mp4", "size": 100}],
    })
    monkeypatch.setattr(quark_client, "RequestUtils", lambda **kwargs: http)
    client = QuarkClient(cookie="c=1")

    assert client.share_token("abc", "9527") == "st"
    link = client.share_link("abc", "st", "fid1", "tok1")
    assert link["url"] == "https://cdn/x"
    assert link["size"] == 100
    download_call = [call for call in http.calls if "file/download" in call["url"]][0]
    assert download_call["body"]["pwd_id"] == "abc"
    assert download_call["body"]["fids"] == ["fid1"]
    assert download_call["body"]["fids_token"] == ["tok1"]


def test_client_raises_on_login_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    31001 应提示更新 Cookie，而不是笼统失败。
    """
    http = FakeHttp()
    http.responses["sharepage/token"] = FakeResponse(
        {"status": 400, "code": 31001, "message": "require login"}
    )
    monkeypatch.setattr(quark_client, "RequestUtils", lambda **kwargs: http)
    client = QuarkClient(cookie="c=1")

    with pytest.raises(QuarkError) as excinfo:
        client.share_token("abc")
    assert "Cookie" in str(excinfo.value)


def test_client_merges_puus_from_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    响应下发的 __puus 必须合并回 Cookie，供后续直链签名使用。
    """
    http = FakeHttp()
    http.responses["config"] = FakeResponse(
        {"status": 200, "code": 0, "data": {}}, set_cookie="__puus=new-token; Path=/"
    )
    monkeypatch.setattr(quark_client, "RequestUtils", lambda **kwargs: http)
    client = QuarkClient(cookie="__pus=old; __puus=stale")

    client.refresh_cookie()

    assert "__puus=new-token" in client.cookie
    assert "stale" not in client.cookie
