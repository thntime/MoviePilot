"""
验证「搜索结果只留确认有效的网盘分享」这条闸门。

覆盖三件事：夸克/UC 把分享级失效认全（不再落到无法判定）、候选层丢弃无法
确认的分享、搜索阶段先查分享黑名单。全部用例使用本地替身，不访问真实网络。
"""

from typing import Any, Dict, List, Optional

import pytest

from app.adapters.external.netdisk import quark as quark_module
from app.adapters.external.netdisk import uc as uc_module
from app.domain.context import TorrentInfo
from app.domain.netdisk import NetdiskErrorCode, NetdiskPlatform, ShareFile
from app.modules.cloudsaver import CloudSaverModule
from app.modules.pansou import PansouModule


class _Response:
    """带状态码与 JSON 体的最小响应替身，供适配器分类测试使用。"""

    def __init__(self, status_code: int, payload: Optional[Dict[str, Any]] = None) -> None:
        """
        保存状态码与 JSON 体。

        :param status_code: HTTP 状态码
        :param payload: 响应 JSON；为空时 json() 抛异常，模拟非 JSON 响应
        """
        self.status_code = status_code
        self._payload = payload
        self.headers: Dict[str, str] = {}
        self.text = ""

    def json(self) -> Dict[str, Any]:
        """
        返回预设 JSON；没有预设时抛异常。

        :return: 预设 JSON
        """
        if self._payload is None:
            raise ValueError("非 JSON 响应")
        return self._payload


def _quark_detail(**kwargs: Any) -> Any:
    """
    构造一个可用的分享详情字典。

    :param kwargs: 需要覆盖的字段
    :return: 分享详情
    """
    detail: Dict[str, Any] = {
        "alive": True,
        "file_size": 2048,
        "file_names": ["第01集.mp4"],
        "stoken": "st",
        "_files": [{"name": "第01集.mp4", "size": 2048, "duration": 0}],
    }
    detail.update(kwargs)
    return detail


def _torrent(code: str, title: str) -> TorrentInfo:
    """
    构造一条夸克网盘候选。

    :param code: 分享码
    :param title: 候选标题
    :return: 网盘候选
    """
    return TorrentInfo(
        title=title,
        enclosure=f"https://pan.quark.cn/s/{code}",
        download_type="netdisk",
        netdisk_platform="quark",
        pubdate="2026-11-20T10:00:00+08:00",
    )


class _FakeBlacklist:
    """按平台与分享码判断是否命中的分享黑名单替身。"""

    def __init__(self, hits: set[tuple[str, str]]) -> None:
        """
        保存命中的平台与分享码组合。

        :param hits: 需要判定为命中的 (平台, 分享码) 集合
        """
        self._hits = hits

    def is_blacklisted(self, platform: str, share_code: str) -> Optional[object]:
        """
        命中时返回占位条目，未命中返回 None。

        :param platform: 平台标识
        :param share_code: 分享码
        :return: 命中条目或 None
        """
        return object() if (platform, share_code) in self._hits else None


# ---- 夸克分类 ----


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (404, {"status": 404, "code": 41004, "message": "文件不存在"}),
        (404, {"status": 404, "code": 41010, "message": "文件涉及违规内容"}),
        (404, None),
        (403, {"status": 403, "code": 41031, "message": "分享者用户封禁链接查看受限"}),
        # 文案不带任何分享级字样，只有业务码 41031 能救它，用来钉住按码判定的分支。
        (403, {"status": 403, "code": 41031, "message": "限制访问"}),
    ],
)
def test_quark_dead_share_responses_are_share_invalid(
    status: int,
    payload: Optional[Dict[str, Any]],
) -> None:
    """
    夸克分享失效的 404/403 形态一律归为 SHARE_INVALID，不能落到无法判定。
    """
    http = quark_module._QuarkHttp()

    with pytest.raises(Exception) as error:
        http._json(_Response(status, payload))

    assert getattr(error.value, "code", None) is NetdiskErrorCode.SHARE_INVALID


def test_quark_token_error_still_reported_as_link_expired() -> None:
    """
    41020 文件令牌校验失败仍按 LINK_EXPIRED 上报，不能被分享失效规则吃掉。
    """
    http = quark_module._QuarkHttp()

    with pytest.raises(Exception) as error:
        http._json(_Response(403, {"status": 403, "code": 41020, "message": "转存文件token校验异常"}))

    assert getattr(error.value, "code", None) is NetdiskErrorCode.LINK_EXPIRED


def test_quark_login_required_still_reported_as_auth_expired() -> None:
    """
    31001 需要登录仍按 AUTH_EXPIRED 上报，不能被分享失效规则吃掉。
    """
    http = quark_module._QuarkHttp()

    with pytest.raises(Exception) as error:
        http._json(_Response(403, {"status": 403, "code": 31001, "message": "请先登录"}))

    assert getattr(error.value, "code", None) is NetdiskErrorCode.AUTH_EXPIRED


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (41008, "需要提取码"),
        (41010, "文件涉及违规内容"),
        (0, "该分享已失效，不可访问"),
    ],
)
def test_quark_business_payload_maps_to_share_invalid(code: int, message: str) -> None:
    """
    HTTP 200 里带分享级业务码或文案时，分类同样是 SHARE_INVALID。
    """
    assert quark_module._classify(code, 200, message) is NetdiskErrorCode.SHARE_INVALID


def test_quark_cancel_download_message_is_not_share_invalid() -> None:
    """
    只出现「取消下载」这类文案不能误判成分享失效。
    """
    assert quark_module._classify(0, 200, "取消下载") is not NetdiskErrorCode.SHARE_INVALID


# ---- UC 分类 ----


def test_uc_share_not_found_on_403_is_share_invalid() -> None:
    """
    UC 分享不存在（41027）带 403 返回时按分享失效处理，不误报登录态失效。
    """
    http = uc_module.UCHttp(platform=NetdiskPlatform.UC, user_agent="ua", referer="ref")

    with pytest.raises(Exception) as error:
        http._json(_Response(403, {"status": 403, "code": 41027, "message": "分享不存在"}))

    assert getattr(error.value, "code", None) is NetdiskErrorCode.SHARE_INVALID


def test_uc_share_not_found_code_is_mapped() -> None:
    """
    UC 错误码表包含 41027 分享不存在。
    """
    assert uc_module.UC_ERROR_CODES[41027][0] is NetdiskErrorCode.SHARE_INVALID


# ---- 候选层闸门 ----


@pytest.fixture(name="pansou_cache")
def _pansou_cache() -> Any:
    """
    每个用例前后清空 PanSou 与 CloudSaver 的有效性缓存。
    """
    from app.modules import cloudsaver as cloudsaver_module
    from app.modules import pansou as pansou_module

    pansou_module._alive_cache.clear()
    cloudsaver_module._alive_cache.clear()
    yield
    pansou_module._alive_cache.clear()
    cloudsaver_module._alive_cache.clear()


def test_pansou_filter_drops_dead_and_undecidable(
    monkeypatch: pytest.MonkeyPatch,
    pansou_cache: Any,
) -> None:
    """
    PanSou 候选层只留校验通过的：失效与无法确认都丢弃。
    """
    del pansou_cache
    torrents = [_torrent("live1", "有效资源"), _torrent("dead1", "失效资源"), _torrent("slow1", "无法确认资源")]
    details = {
        "live1": (_quark_detail(), True),
        "dead1": ({"alive": False}, False),
        "slow1": ({"alive": True, "unverified": True}, True),
    }

    def fake_inspect(self: Any, torrent: TorrentInfo, cached_detail: Any = None) -> Any:
        """
        按分享码返回预设的校验结果。
        """
        del self, cached_detail
        code = str(torrent.enclosure or "").rsplit("/", 1)[-1]
        return details[code]

    monkeypatch.setattr(PansouModule, "_inspect_share", fake_inspect)
    monkeypatch.setattr(
        "app.application.netdisk.share_blacklist.get_share_blacklist",
        lambda: _FakeBlacklist(set()),
    )

    results = PansouModule()._filter_alive_shares({"id": 7, "name": "PanSou"}, torrents)

    assert [item.title for item in results] == ["有效资源"]


def test_pansou_filter_drops_blacklisted_share(
    monkeypatch: pytest.MonkeyPatch,
    pansou_cache: Any,
) -> None:
    """
    已在分享黑名单里的候选不再进结果，也不发起校验请求。
    """
    del pansou_cache
    torrents = [_torrent("bad1", "已拉黑资源"), _torrent("live1", "有效资源")]
    called: List[str] = []

    def fake_inspect(self: Any, torrent: TorrentInfo, cached_detail: Any = None) -> Any:
        """
        记录被检查的分享码，并返回有效结果。
        """
        del self, cached_detail
        code = str(torrent.enclosure or "").rsplit("/", 1)[-1]
        called.append(code)
        return _quark_detail(), True

    monkeypatch.setattr(PansouModule, "_inspect_share", fake_inspect)
    monkeypatch.setattr(
        "app.application.netdisk.share_blacklist.get_share_blacklist",
        lambda: _FakeBlacklist({("quark", "bad1")}),
    )

    results = PansouModule()._filter_alive_shares({"id": 7, "name": "PanSou"}, torrents)

    assert [item.title for item in results] == ["有效资源"]
    assert called == ["live1"]


def test_cloudsaver_filter_drops_undecidable(
    monkeypatch: pytest.MonkeyPatch,
    pansou_cache: Any,
) -> None:
    """
    CD 站点候选层同样丢弃无法确认的分享，只留校验通过的。
    """
    del pansou_cache
    torrents = [_torrent("live1", "有效资源"), _torrent("slow1", "无法确认资源")]

    def fake_inspect(self: Any, torrent: TorrentInfo, cached_detail: Any = None) -> Any:
        """
        按分享码返回预设的校验结果。
        """
        del self, cached_detail
        code = str(torrent.enclosure or "").rsplit("/", 1)[-1]
        if code == "slow1":
            return {"alive": True, "unverified": True}, True
        return _quark_detail(), True

    monkeypatch.setattr(CloudSaverModule, "_inspect_share", fake_inspect)
    monkeypatch.setattr(
        "app.application.netdisk.share_blacklist.get_share_blacklist",
        lambda: _FakeBlacklist(set()),
    )

    results = CloudSaverModule()._filter_alive_shares({"id": 9, "name": "CloudSaver"}, torrents)

    assert [item.title for item in results] == ["有效资源"]


def test_share_file_entries_carry_size() -> None:
    """
    校验通过的候选会带上真实大小，供结果页展示；0 大小只可能来自未校验候选。
    """
    from app.application.netdisk import candidates

    torrent = _torrent("live1", "有效资源")
    detail = _quark_detail()
    assert candidates.passes_quality(torrent, detail) is True
    assert torrent.size == 2048.0
    assert ShareFile(file_id="f1", name="第01集.mp4", size=2048).size == 2048
