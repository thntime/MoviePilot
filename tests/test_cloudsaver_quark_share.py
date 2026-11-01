"""
验证夸克分享客户端：换取令牌、翻页聚合、字段归一与失败回退。
"""

from typing import Any, Dict, List, Optional

import pytest

from app.modules.cloudsaver import quark_share


class FakeResponse:
    """
    提供夸克客户端所需的最小 HTTP 响应接口。
    """

    def __init__(self, payload: Optional[dict], status_code: int = 200) -> None:
        """
        保存响应体与状态码。
        """
        self._payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self) -> Optional[dict]:
        """
        返回预设 JSON 响应。
        """
        return self._payload

    def close(self) -> None:
        """
        标记响应已关闭。
        """
        self.closed = True


class FakeQuarkHttp:
    """
    按页码返回预设目录数据的假网络适配器。
    """

    def __init__(self) -> None:
        """
        初始化调用记录与预设数据。
        """
        self.calls: List[Dict[str, Any]] = []
        self.token_payload: Optional[dict] = {
            "status": 200,
            "code": 0,
            "data": {"stoken": "stoken-value"},
        }
        # 目录标识 -> 页码 -> 条目列表
        self.pages: Dict[str, Dict[int, List[dict]]] = {}
        self.totals: Dict[str, int] = {}
        self.fail_dirs: set = set()

    def post_res(self, url: str, **kwargs: Any) -> Optional[FakeResponse]:
        """
        模拟换取令牌接口。
        """
        self.calls.append({"method": "POST", "url": url, "kwargs": kwargs})
        if self.token_payload is None:
            return None
        return FakeResponse(dict(self.token_payload))

    def get_res(self, url: str, **kwargs: Any) -> Optional[FakeResponse]:
        """
        模拟列目录接口，按 pdir_fid 与页码返回预设条目。
        """
        params = kwargs.get("params") or {}
        pdir_fid = str(params.get("pdir_fid") or "0")
        page = int(params.get("_page") or 1)
        self.calls.append({"method": "GET", "url": url, "params": params})
        if pdir_fid in self.fail_dirs:
            return FakeResponse(None, 500)
        entries = (self.pages.get(pdir_fid) or {}).get(page) or []
        return FakeResponse({
            "status": 200,
            "code": 0,
            "data": {"list": entries},
            "metadata": {"_total": self.totals.get(pdir_fid, len(entries))},
        })


def install_http(monkeypatch: pytest.MonkeyPatch, http: FakeQuarkHttp) -> None:
    """
    把客户端使用的网络适配器替换为假实现。
    """
    monkeypatch.setattr(quark_share, "RequestUtils", lambda **kwargs: http)


def make_entry(name: str, size: int = 1024, duration: int = 60, is_dir: bool = False) -> dict:
    """
    构造一条夸克目录条目。
    """
    return {
        "fid": f"fid-{name}",
        "file_name": name,
        "size": size,
        "duration": duration,
        "dir": is_dir,
    }


def test_fetch_stoken_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    换取令牌成功时返回 stoken，请求体携带分享码与提取码。
    """
    http = FakeQuarkHttp()
    install_http(monkeypatch, http)

    token = quark_share.QuarkShareClient().fetch_stoken("abc123", "9527")

    assert token == "stoken-value"
    assert http.calls[0]["kwargs"]["json"] == {"pwd_id": "abc123", "passcode": "9527"}


def test_list_dir_normalizes_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    列目录应归一文件名、大小、时长与是否目录。
    """
    http = FakeQuarkHttp()
    http.pages["0"] = {1: [make_entry("第01集.mp4", size=2048, duration=1400), make_entry("花絮", is_dir=True)]}
    install_http(monkeypatch, http)

    result = quark_share.QuarkShareClient().list_dir("abc123", "token")

    assert result is not None
    assert result["total"] == 2
    assert result["entries"][0] == {
        "fid": "fid-第01集.mp4",
        "name": "第01集.mp4",
        "size": 2048,
        "duration": 1400,
        "is_dir": False,
    }
    assert result["entries"][1]["is_dir"] is True


def test_collect_files_pages_until_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    单页满额时继续翻页，直到拿满 metadata 给出的总数。
    """
    http = FakeQuarkHttp()
    first_page = [make_entry(f"第{index}集.mp4") for index in range(1, 101)]
    second_page = [make_entry(f"第{index}集.mp4") for index in range(101, 117)]
    http.pages["0"] = {1: first_page, 2: second_page}
    http.totals["0"] = 116
    install_http(monkeypatch, http)

    files = quark_share.QuarkShareClient().collect_files("abc123", "token")

    assert files is not None
    assert len(files) == 116
    assert [call["params"]["_page"] for call in http.calls] == ["1", "2"]


def test_collect_files_descends_one_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    根目录的文件夹下钻一层，更深层目录不再请求。
    """
    http = FakeQuarkHttp()
    http.pages["0"] = {1: [make_entry("第一季", is_dir=True)]}
    http.pages["fid-第一季"] = {1: [make_entry("第01集.mp4")]}
    install_http(monkeypatch, http)

    files = quark_share.QuarkShareClient().collect_files("abc123", "token")

    assert files is not None
    assert [entry["name"] for entry in files] == ["第01集.mp4"]
    assert [call["params"]["pdir_fid"] for call in http.calls] == ["0", "fid-第一季"]


def test_collect_files_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    任一层请求失败时返回 None，由调用方按集数不清晰处理。
    """
    http = FakeQuarkHttp()
    http.pages["0"] = {1: [make_entry("第一季", is_dir=True)]}
    http.fail_dirs.add("fid-第一季")
    install_http(monkeypatch, http)

    assert quark_share.QuarkShareClient().collect_files("abc123", "token") is None


def test_collect_files_respects_page_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    翻页预算用尽仍未读完时返回 None，不使用不完整的文件树。
    """
    http = FakeQuarkHttp()
    http.pages["0"] = {
        1: [make_entry(f"第{index}集.mp4") for index in range(1, 101)],
        2: [make_entry(f"第{index}集.mp4") for index in range(101, 201)],
    }
    http.totals["0"] = 300
    install_http(monkeypatch, http)

    assert quark_share.QuarkShareClient().collect_files("abc123", "token", max_pages=2) is None


def test_collect_files_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    缺少分享码或令牌时不发起请求，直接返回 None。
    """
    http = FakeQuarkHttp()
    install_http(monkeypatch, http)

    assert quark_share.QuarkShareClient().collect_files("", "token") is None
    assert quark_share.QuarkShareClient().collect_files("abc123", "") is None
    assert http.calls == []
