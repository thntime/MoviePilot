"""
迅雷昵称解析测试：about 接口的多种嵌套结构与凭据优先级。

全部使用替身，不访问真实网络。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.external.netdisk import xunlei as xunlei_module
from app.adapters.external.netdisk.base import NetdiskHttp


def _patch_about(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    """
    用固定响应替换 about 接口请求。

    :param monkeypatch: pytest 打桩夹具
    :param payload: 假响应
    """

    def fake_get_json(self, url, *, account=None, params=None, headers=None):
        del self, account, params, headers
        assert url == xunlei_module._ABOUT_URL
        return payload

    monkeypatch.setattr(NetdiskHttp, "get_json", fake_get_json)


def test_login_account_name_from_data_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    优先解析 `data.user.nickname`。
    """
    _patch_about(monkeypatch, {"data": {"user": {"nickname": "雷友甲"}}})

    adapter = xunlei_module.XunleiAdapter()
    assert adapter.login_account_name({"access_token": "a", "deviceid": "d"}) == "雷友甲"


def test_login_account_name_from_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    解析 `data.nickname`。
    """
    _patch_about(monkeypatch, {"data": {"nickname": "雷友乙"}})

    adapter = xunlei_module.XunleiAdapter()
    assert adapter.login_account_name({"access_token": "a", "deviceid": "d"}) == "雷友乙"


def test_login_account_name_from_top_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    解析响应顶层的昵称字段。
    """
    _patch_about(monkeypatch, {"nickName": "雷友丙"})

    adapter = xunlei_module.XunleiAdapter()
    assert adapter.login_account_name({"access_token": "a", "deviceid": "d"}) == "雷友丙"


def test_login_account_name_prefers_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    凭据里已有昵称时不再请求接口。
    """

    def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("凭据已有昵称时不应请求 about 接口")

    monkeypatch.setattr(NetdiskHttp, "get_json", fail)

    adapter = xunlei_module.XunleiAdapter()
    credential = {"access_token": "a", "deviceid": "d", "nickname": "凭据昵称"}
    assert adapter.login_account_name(credential) == "凭据昵称"


def test_login_account_name_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    所有候选结构都缺失昵称时返回空串。
    """
    _patch_about(monkeypatch, {"data": {"user": {"uid": "1"}}, "foo": "bar"})

    adapter = xunlei_module.XunleiAdapter()
    assert adapter.login_account_name({"access_token": "a", "deviceid": "d"}) == ""
