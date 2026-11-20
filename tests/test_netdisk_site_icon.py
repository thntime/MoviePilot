"""
网盘站点图标抓取回归：页面解析、魔数校验、大小上限与图片类型。
"""

import base64
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain.site import SiteChain, configure_site_ports, reset_site_ports
from app.db.oper.site import _icon_data_url

# 真实 ICO/PNG 文件头，够魔数校验用；HTML 取自站点把未知路径回落到首页的真实响应。
ICO_BYTES = b"\x00\x00\x01\x00\x01\x00\x20\x20" + b"\x00" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
HTML_BYTES = b"<!doctype html><html><head><title>PanSou</title></head><body></body></html>"
PAGE_WITH_ICON = '<html><head><link rel="icon" href="/assets/favicon.ico"></head><body></body></html>'
PAGE_WITHOUT_ICON = "<html><head><title>site</title></head><body></body></html>"
SITE_URL = "http://pansou.example:3300/"


class _FakeResponse:
    """
    模拟站点链消费的最小 HTTP 响应。
    """

    def __init__(self, status_code: int = 200, *, text: str = "", content: bytes = b"") -> None:
        """
        保存响应状态与正文。

        :param status_code: HTTP 状态码
        :param text: 解码后的文本正文
        :param content: 原始响应字节
        """
        self.status_code = status_code
        self.text = text
        self.content = content
        self.reason = "OK"

    def __bool__(self) -> bool:
        """
        模拟 requests.Response 的成功状态真假语义。

        :return: HTTP 状态小于 400 时为 True
        """
        return self.status_code < 400

    def json(self) -> dict[str, Any]:
        """
        返回空 JSON 对象，图标抓取不使用接口正文。

        :return: 空字典
        """
        return {}


class _FakeHttpPort:
    """
    按队列回放响应并记录每次请求参数。
    """

    def __init__(self, *responses: Optional[_FakeResponse]) -> None:
        """
        保存将被依次消费的响应。

        :param responses: 按请求顺序回放的响应
        """
        self.responses: List[Optional[_FakeResponse]] = list(responses)
        self.calls: List[dict[str, Any]] = []

    @contextmanager
    def open(self, **kwargs: Any) -> Iterator[Optional[_FakeResponse]]:
        """
        回放一个响应。

        :param kwargs: 站点链传入的请求参数
        :return: 单个响应的上下文
        """
        self.calls.append(kwargs)
        yield self.responses.pop(0)


def _configure(http: _FakeHttpPort) -> None:
    """
    装配一组完全离线的站点链假端口。

    :param http: 回放响应的 HTTP 端口
    """
    configure_site_ports(
        http=http,
        browser=SimpleNamespace(render=lambda **_kwargs: None),
        challenge=SimpleNamespace(detected=lambda _page: False),
        cookiecloud=SimpleNamespace(download=lambda: ({}, "")),
    )


@pytest.fixture(autouse=True)
def clean_site_ports() -> Iterator[None]:
    """
    隔离每个用例修改的站点链进程级端口。
    """
    reset_site_ports()
    yield
    reset_site_ports()


def _chain() -> SiteChain:
    """
    构造只带图标缓存所需仓储的 SiteChain。

    :return: 已注入假仓储的站点链
    """
    chain = object.__new__(SiteChain)
    chain.site_repository = Mock()
    chain.runtime_config = SimpleNamespace(user_agent="MoviePilot-Test")
    return chain


def test_cache_netdisk_icon_uses_page_link_and_stores_ico() -> None:
    """
    图标地址取页面声明的 link，ICO 内容按 Base64 写入站点图标。
    """
    http = _FakeHttpPort(_FakeResponse(text=PAGE_WITH_ICON), _FakeResponse(content=ICO_BYTES))
    _configure(http)
    chain = _chain()

    assert chain.cache_netdisk_icon({"kind": "ps", "url": SITE_URL, "domain": "pansou.example"}) is True

    kwargs = chain.site_repository.update_icon.call_args.kwargs
    assert kwargs["icon_url"] == "http://pansou.example:3300/assets/favicon.ico"
    assert base64.b64decode(kwargs["icon_base64"]) == ICO_BYTES


def test_cache_netdisk_icon_falls_back_to_favicon_ico() -> None:
    """
    页面没有声明图标时退回站点根目录的 favicon.ico。
    """
    http = _FakeHttpPort(_FakeResponse(text=PAGE_WITHOUT_ICON), _FakeResponse(content=PNG_BYTES))
    _configure(http)
    chain = _chain()

    assert chain.cache_netdisk_icon({"kind": "cd", "url": SITE_URL, "domain": "pansou.example"}) is True

    assert chain.site_repository.update_icon.call_args.kwargs["icon_url"] == (
        "http://pansou.example:3300/favicon.ico"
    )


def test_cache_netdisk_icon_rejects_html_payload() -> None:
    """
    favicon.ico 回落到首页 HTML 时判定为不是图标，不写库。
    """
    http = _FakeHttpPort(_FakeResponse(text=PAGE_WITH_ICON), _FakeResponse(text=HTML_BYTES.decode(), content=HTML_BYTES))
    _configure(http)
    chain = _chain()

    assert chain.cache_netdisk_icon({"kind": "cd", "url": SITE_URL, "domain": "pansou.example"}) is False

    chain.site_repository.update_icon.assert_not_called()


def test_cache_netdisk_icon_rejects_oversized_icon() -> None:
    """
    图标超过 256KB 上限时丢弃，避免把大文件塞进配置库。
    """
    oversized = ICO_BYTES + b"\x00" * (256 * 1024)
    http = _FakeHttpPort(_FakeResponse(text=PAGE_WITH_ICON), _FakeResponse(content=oversized))
    _configure(http)
    chain = _chain()

    assert chain.cache_netdisk_icon({"kind": "ps", "url": SITE_URL, "domain": "pansou.example"}) is False

    chain.site_repository.update_icon.assert_not_called()


def test_cache_netdisk_icon_ignores_pt_site() -> None:
    """
    PT 站点图标仍走官方索引器，这里不发请求也不写库。
    """
    http = _FakeHttpPort()
    _configure(http)
    chain = _chain()

    assert chain.cache_netdisk_icon({"kind": "pt", "url": SITE_URL, "domain": "pansou.example"}) is False

    assert http.calls == []
    chain.site_repository.update_icon.assert_not_called()


def test_icon_data_url_uses_detected_mime() -> None:
    """
    data URL 的图片类型按魔数决定，认不出时保持 ico 兼容既有数据。
    """
    assert _icon_data_url(base64.b64encode(PNG_BYTES).decode()) == (
        f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"
    )
    assert _icon_data_url(base64.b64encode(ICO_BYTES).decode()).startswith("data:image/ico;base64,")
    assert _icon_data_url("AAA") == "data:image/ico;base64,AAA"
    assert _icon_data_url("") == ""

@pytest.mark.asyncio
async def test_site_icon_backfills_missing_netdisk_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    库里没有图标时，网盘站点的图标接口补抓一次再返回，老站点不必重新保存。
    """
    from app.api.endpoints import site as site_endpoint
    from app.schemas.site import SiteIconData

    site = SimpleNamespace(id=9, kind="ps", url=SITE_URL, domain="pansou.example", name="PanSou")
    icons = [None, SiteIconData(icon="data:image/ico;base64,AAA")]
    query = SimpleNamespace(
        get=AsyncMock(return_value=site),
        icon=AsyncMock(side_effect=lambda _domain: icons.pop(0)),
    )
    chain = SimpleNamespace(cache_netdisk_icon=Mock(return_value=True))
    monkeypatch.setattr(site_endpoint, "SiteChain", lambda: chain)

    response = await site_endpoint.site_icon(9, query=query, _=None)

    assert response.success is True
    assert response.data == {"icon": "data:image/ico;base64,AAA"}
    chain.cache_netdisk_icon.assert_called_once_with({
        "kind": "ps",
        "url": SITE_URL,
        "domain": "pansou.example",
        "name": "PanSou",
    })


@pytest.mark.asyncio
async def test_site_icon_skips_backfill_when_icon_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    库里已有图标时不再抓取，PT 站点同样不走补抓。
    """
    from app.api.endpoints import site as site_endpoint
    from app.schemas.site import SiteIconData

    site = SimpleNamespace(id=10, kind="pt", url=SITE_URL, domain="pansou.example", name="PT")
    query = SimpleNamespace(
        get=AsyncMock(return_value=site),
        icon=AsyncMock(return_value=SiteIconData(icon="data:image/ico;base64,AAA")),
    )
    chain = SimpleNamespace(cache_netdisk_icon=Mock(return_value=True))
    monkeypatch.setattr(site_endpoint, "SiteChain", lambda: chain)

    response = await site_endpoint.site_icon(10, query=query, _=None)

    assert response.success is True
    chain.cache_netdisk_icon.assert_not_called()
