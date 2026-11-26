"""
网盘取链与下载必须直连：宿主 HTTP_PROXY/HTTPS_PROXY 不能接管网盘 API 或 CDN 流量。
"""

import pytest

from app.adapters.external.netdisk.baidu import BaiduAdapter
from app.adapters.external.netdisk.c139 import C139Adapter
from app.adapters.external.netdisk.guangya import GuangYaAdapter
from app.adapters.external.netdisk.pan115 import Pan115Adapter
from app.adapters.external.netdisk.pan123 import Pan123Adapter
from app.adapters.external.netdisk.quark import QuarkAdapter
from app.adapters.external.netdisk.uc import UCAdapter
from app.adapters.external.netdisk.xunlei import XunleiAdapter
from app.modules.netdisk import engine

_DIRECT = {"http": "", "https": ""}


@pytest.mark.parametrize(
    "adapter_cls",
    [
        QuarkAdapter,
        UCAdapter,
        C139Adapter,
        Pan123Adapter,
        Pan115Adapter,
        GuangYaAdapter,
        BaiduAdapter,
        XunleiAdapter,
    ],
)
def test_netdisk_adapter_http_bypasses_proxy(adapter_cls: type) -> None:
    """
    所有平台适配器的 API 请求默认直连，不继承宿主代理。
    """
    assert adapter_cls().http.proxies == _DIRECT


def test_download_engine_bypasses_proxy() -> None:
    """
    分片/单流/HLS 下载引擎显式使用空代理。
    """
    assert engine._DIRECT_PROXIES == _DIRECT
