"""
115 回收站列表与清空的适配器测试。

全部 HTTP 调用都在 `NetdiskHttp` 层打桩，不访问真实网络，Cookie 使用假值。
"""

import pytest

from app.adapters.external.netdisk import pan115 as pan115_module
from app.adapters.external.netdisk.base import NetdiskHttp
from app.application.netdisk.contract import NetdiskAccountRecord
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform

_COOKIE = "UID=fake-uid; SEID=fake-seid; CID=fake-cid; KID=fake-kid"


def _account() -> NetdiskAccountRecord:
    """
    构造使用假 Cookie 的 115 账号记录。

    :return: 账号记录
    """
    return NetdiskAccountRecord(
        id=9,
        platform=NetdiskPlatform.PAN115,
        name="115测试账号",
        credential={"cookie": _COOKIE},
    )


def _patch_http(monkeypatch, handler) -> None:
    """
    用统一路由函数替换基类 HTTP 方法，并禁止文本请求。

    :param monkeypatch: pytest 打桩夹具
    :param handler: 接收 (方法, http 实例, 地址, 账号, 参数, 请求体, 请求头) 并返回 JSON 的函数
    """

    def get_json(self, url, *, account=None, params=None, headers=None):
        """
        转发 GET 请求到测试路由。
        """
        return handler("GET", self, url, account, params, None, headers)

    def post_json(self, url, *, account=None, json=None, data=None, params=None, headers=None):
        """
        转发 POST 请求到测试路由。
        """
        return handler("POST", self, url, account, params, json if json is not None else data, headers)

    def get_text(self, url, *, account=None, params=None, headers=None):
        """
        115 适配器不应使用文本请求。
        """
        raise AssertionError("115 适配器不应发起文本请求")

    monkeypatch.setattr(NetdiskHttp, "get_json", get_json)
    monkeypatch.setattr(NetdiskHttp, "post_json", post_json)
    monkeypatch.setattr(NetdiskHttp, "get_text", get_text)


def test_list_recycle_bin_reads_page_and_reports_safe_password(monkeypatch) -> None:
    """
    列表请求带上回收站分页参数，并透出总数、安全密钥标记与条目。
    """
    seen = {}

    def handler(method, http, url, account, params, body, headers):
        """
        记录请求并返回两条回收站条目。
        """
        seen.update(method=method, url=url, params=params, body=body)
        return {
            "state": True,
            "count": 2,
            "rb_pass": 0,
            "data": [{"id": "1", "n": "a.mkv"}, {"id": "2", "n": "b.mkv"}],
        }

    _patch_http(monkeypatch, handler)
    result = pan115_module.Pan115Adapter().list_recycle_bin(_account())

    assert seen["method"] == "GET"
    assert seen["url"] == pan115_module._RECYCLE_LIST_URL
    assert seen["params"] == {
        "aid": "7",
        "cid": "0",
        "offset": "0",
        "limit": "100",
        "format": "json",
    }
    assert result["count"] == 2
    assert result["rb_pass"] == 0
    assert [item["id"] for item in result["items"]] == ["1", "2"]


def test_list_recycle_bin_accepts_numeric_state(monkeypatch) -> None:
    """
    `state` 为数字 1 时同样视为成功。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        返回数字状态与空列表。
        """
        return {"state": 1, "count": 0, "rb_pass": 0, "data": []}

    _patch_http(monkeypatch, handler)
    result = pan115_module.Pan115Adapter().list_recycle_bin(_account())

    assert result["count"] == 0
    assert result["items"] == []


def test_clear_recycle_bin_posts_empty_form_after_reading_password_flag(monkeypatch) -> None:
    """
    未开启安全密钥时先读回收站再提交空表单清空。
    """
    posts = []

    def handler(method, http, url, account, params, body, headers):
        """
        GET 返回待清空条目，POST 记录清空请求。
        """
        if method == "GET":
            assert url == pan115_module._RECYCLE_LIST_URL
            return {"state": True, "count": 2, "rb_pass": 0, "data": []}
        posts.append((url, body))
        return {"state": True}

    _patch_http(monkeypatch, handler)
    result = pan115_module.Pan115Adapter().clear_recycle_bin(_account())

    assert posts == [(pan115_module._RECYCLE_CLEAN_URL, {})]
    assert result == {"cleared": True, "count": 2}


def test_clear_recycle_bin_skips_when_safe_password_enabled(monkeypatch) -> None:
    """
    账号开启回收站安全密钥时不猜密码、不发送清空请求。
    """
    posts = []

    def handler(method, http, url, account, params, body, headers):
        """
        GET 标记安全密钥已开启；POST 不应被调用。
        """
        if method == "GET":
            return {"state": True, "count": 1, "rb_pass": 1, "data": []}
        posts.append(url)
        return {"state": True}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        pan115_module.Pan115Adapter().clear_recycle_bin(_account())

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert "账号开启回收站安全密钥，暂不支持自动清空" == error.value.message
    assert posts == []


@pytest.mark.parametrize(
    ("errno", "message"),
    [
        (990009, "上一次操作尚未完成"),
        (590075, "操作太频繁"),
    ],
)
def test_clear_recycle_bin_maps_busy_errors_to_rate_limited(monkeypatch, errno, message) -> None:
    """
    删除未完成与操作过频都归为限流，供调用方跳过或退避。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        GET 正常，POST 返回指定错误码。
        """
        if method == "GET":
            return {"state": True, "count": 1, "rb_pass": 0, "data": []}
        return {"state": False, "errno": errno, "error": message}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        pan115_module.Pan115Adapter().clear_recycle_bin(_account())

    assert error.value.code is NetdiskErrorCode.RATE_LIMITED


def test_clear_recycle_bin_maps_safe_password_error_to_unsupported(monkeypatch) -> None:
    """
    服务端返回安全密钥错误时归类为未支持，提示不要自动重试。
    """

    def handler(method, http, url, account, params, body, headers):
        """
        GET 正常，POST 返回安全密钥错误。
        """
        if method == "GET":
            return {"state": True, "count": 1, "rb_pass": 0, "data": []}
        return {"state": False, "errno": 320001, "error": "安全密钥不正确"}

    _patch_http(monkeypatch, handler)

    with pytest.raises(NetdiskError) as error:
        pan115_module.Pan115Adapter().clear_recycle_bin(_account())

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert "安全密钥" in error.value.message
