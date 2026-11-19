"""
验证 PanSou 站点的搜索解析、认证头、平台过滤、三态校验与连接测试。

全部用例使用本地替身，不访问真实网络。
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.application.netdisk.resolve import NetdiskResolveService, ResolvedShare
from app.domain.netdisk import (
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
)
from app.modules.pansou import PansouModule
from app.modules.pansou import client as pansou_client_module
from app.modules.pansou.client import PansouClient, PansouError


class FakeResponse:
    """
    提供 PanSou 客户端测试所需的最小 HTTP 响应接口。

    响应体按 UTF-8 字节给出，用来覆盖服务端缺少 charset 的真实情况。
    """

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        """
        保存响应体与状态码。

        :param payload: 需要序列化的响应对象；bytes 原样使用
        :param status_code: HTTP 状态码
        """
        if isinstance(payload, bytes):
            self.content = payload
        else:
            self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status_code = status_code
        self.closed = False

    def close(self) -> None:
        """
        记录响应资源已释放。
        """
        self.closed = True


class FakeServer:
    """
    按 PanSou 契约响应的假服务端，记录每次请求的参数与请求头。
    """

    def __init__(self) -> None:
        """
        初始化空的请求记录与默认响应。
        """
        self.responses: Dict[str, FakeResponse] = {}
        self.requests: List[Dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        把 RequestUtils.get_res 替换为按路径返回预设响应的替身。
        """
        def fake_get_res(
            self_http: Any,
            url: str,
            params: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
        ) -> Optional[FakeResponse]:
            """
            记录请求并返回按路径匹配的预设响应。
            """
            del self_http
            self.requests.append({"url": url, "params": params, "headers": kwargs.get("headers") or {}})
            for path, response in self.responses.items():
                if url.endswith(path):
                    return response
            return FakeResponse({"code": 0, "message": "success", "data": {}})

        monkeypatch.setattr(pansou_client_module.RequestUtils, "get_res", fake_get_res)

    def last_headers(self) -> Dict[str, Any]:
        """
        返回最近一次请求的请求头。
        """
        return self.requests[-1]["headers"] if self.requests else {}


@pytest.fixture(name="server")
def _server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """
    每个用例安装独立的假服务端并清空有效性缓存。
    """
    from app.modules.pansou import _alive_cache

    _alive_cache.clear()
    fake = FakeServer()
    fake.install(monkeypatch)
    yield fake
    _alive_cache.clear()


def _merge_payload(merged: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    构造 pansou 的 merge 响应体。
    """
    total = sum(len(items) for items in merged.values())
    return {"code": 0, "message": "success", "data": {"total": total, "merged_by_type": merged}}


def _ps_site(**overrides: Any) -> Dict[str, Any]:
    """
    构造一个 PS 站点配置。
    """
    site: Dict[str, Any] = {
        "id": 7,
        "name": "PanSou",
        "url": "http://pansou.example:3303",
        "kind": "ps",
        "timeout": 30,
        "token": None,
    }
    site.update(overrides)
    return site


def _install_walk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    errors: Optional[Dict[str, Exception]] = None,
    files: Optional[Dict[str, List[ShareFile]]] = None,
) -> None:
    """
    把本机统一取链服务替换为按分享码返回文件树或抛异常的替身。
    """
    from app.domain.netdisk import parse_share_link

    def fake_walk(
        self: Any,
        url: str,
        password: Any = None,
        platform: Any = None,
        **kwargs: Any,
    ) -> ResolvedShare:
        """
        按分享码返回预设文件树或抛出预设异常。
        """
        del self, password, platform, kwargs
        ref = parse_share_link(url)
        code = ref.share_id if ref is not None and ref.share_id else url
        error = (errors or {}).get(code)
        if error is not None:
            raise error
        entries = (files or {}).get(code, [ShareFile(file_id="f1", name="第01集.mp4", size=1024)])
        return ResolvedShare(
            ref=ShareRef(platform=NetdiskPlatform.QUARK, share_id=code),
            detail=ShareDetail(title="资源", size=0),
            files=tuple(entries),
        )

    monkeypatch.setattr(NetdiskResolveService, "walk", fake_walk)


def test_search_parses_merge_response_into_candidates(server: FakeServer) -> None:
    """
    merge 响应按平台分组展开成候选，标题、提取码与来源标签都保留。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{
            "url": "https://pan.quark.cn/s/abc123",
            "password": "9d8s",
            "note": "火影忍者 全集 1080p",
            "datetime": "2026-11-19T10:15:57+08:00",
            "source": "plugin:xiaokupan",
        }],
    }))

    results = PansouModule().search_netdisk_torrents(_ps_site(), "火影")

    assert results is not None
    assert len(results) == 1
    candidate = results[0]
    assert candidate.title == "火影忍者 全集 1080p"
    assert candidate.enclosure == "https://pan.quark.cn/s/abc123"
    assert candidate.share_password == "9d8s"
    assert candidate.netdisk_platform == "quark"
    assert candidate.download_type == "netdisk"
    assert candidate.labels == ["plugin:xiaokupan"]
    assert candidate.site == 7


def test_search_decodes_utf8_title_without_charset_header(server: FakeServer) -> None:
    """
    响应头没有 charset 时仍按 UTF-8 解码，中文标题不能变成乱码。
    """
    body = json.dumps(_merge_payload({
        "quark": [{"url": "https://pan.quark.cn/s/cn123", "note": "流浪地球 2 4K"}],
    }), ensure_ascii=False).encode("utf-8")
    server.responses["/api/search"] = FakeResponse(body)

    results = PansouModule().search_netdisk_torrents(_ps_site(), "流浪地球")

    assert results is not None
    assert [item.title for item in results] == ["流浪地球 2 4K"]


@pytest.mark.parametrize("kind", ["cd", "pt", "tg", "unknown"])
def test_non_ps_site_returns_none(server: FakeServer, kind: str) -> None:
    """
    非 ps 站点不参与 PanSou 搜索，返回 None 交由其他模块处理。
    """
    del server

    assert PansouModule().search_netdisk_torrents(_ps_site(kind=kind), "火影") is None
    assert PansouModule().test_netdisk_site(_ps_site(kind=kind)) is None


def test_search_sends_bearer_token_when_configured(server: FakeServer) -> None:
    """
    站点配置了 token 时请求头带 Bearer 认证。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({}))

    PansouModule().search_netdisk_torrents(_ps_site(token="secret-token"), "火影")

    assert server.last_headers().get("Authorization") == "Bearer secret-token"


def test_search_without_token_sends_no_authorization(server: FakeServer) -> None:
    """
    站点没有 token 时请求头不带 Authorization。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({}))

    PansouModule().search_netdisk_torrents(_ps_site(), "火影")

    assert "Authorization" not in server.last_headers()


def test_search_requests_merge_result(server: FakeServer) -> None:
    """
    搜索固定请求 merge 结果与全量来源。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({}))

    PansouModule().search_netdisk_torrents(_ps_site(), "火影")

    assert server.requests[-1]["params"] == {"kw": "火影", "res": "merge", "src": "all"}


def test_invalid_share_is_dropped(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    明确失效的分享（SHARE_INVALID）从候选中丢弃。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{"url": "https://pan.quark.cn/s/dead1", "note": "失效资源"}],
    }))
    _install_walk(monkeypatch, errors={
        "dead1": NetdiskError(NetdiskErrorCode.SHARE_INVALID, "分享不存在"),
    })

    assert PansouModule().search_netdisk_torrents(_ps_site(), "火影") == []


def test_undecidable_share_kept_as_unverified(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    鉴权或网络异常无法判定时保留候选并标注未验证。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{"url": "https://pan.quark.cn/s/unknown1", "note": "待验证资源"}],
    }))
    _install_walk(monkeypatch, errors={"unknown1": RuntimeError("网络超时")})

    results = PansouModule().search_netdisk_torrents(_ps_site(), "火影")

    assert results is not None
    assert len(results) == 1
    assert "（未验证）" in (results[0].description or "")


def test_unsupported_platform_is_filtered(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    没有取链适配器且不在展示白名单的平台不进入候选。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{"url": "https://pan.quark.cn/s/keep1", "note": "夸克资源"}],
        "aliyun": [{"url": "https://www.aliyundrive.com/s/drop1", "note": "阿里资源"}],
    }))
    _install_walk(monkeypatch)

    results = PansouModule().search_netdisk_torrents(_ps_site(), "火影")

    assert results is not None
    assert [item.netdisk_platform for item in results] == ["quark"]


def test_disabled_platform_is_filtered(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    系统设置里关掉的平台即使有适配器也不进入候选。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{"url": "https://pan.quark.cn/s/off1", "note": "夸克资源"}],
    }))
    _install_walk(monkeypatch)
    monkeypatch.setattr(
        "app.application.netdisk.settings.enabled_platforms",
        lambda: frozenset(),
    )

    assert PansouModule().search_netdisk_torrents(_ps_site(), "火影") == []


def test_health_ok_reports_site_usable(server: FakeServer) -> None:
    """
    /api/health 返回 ok 时站点连接测试通过。
    """
    server.responses["/api/health"] = FakeResponse({"status": "ok", "plugin_count": 74})

    assert PansouModule().test_netdisk_site(_ps_site()) == (True, "")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse({"status": "degraded"}, 200), "PanSou 状态异常：degraded"),
        (FakeResponse({"message": "boom"}, 500), "PanSou 连接失败：HTTP 500"),
        (FakeResponse(b"not-json", 200), "PanSou 连接失败：响应不是有效 JSON"),
    ],
)
def test_health_failure_reports_reason(
    server: FakeServer,
    response: FakeResponse,
    expected: str,
) -> None:
    """
    健康检查异常时返回可展示的中文原因。
    """
    server.responses["/api/health"] = response

    ok, message = PansouModule().test_netdisk_site(_ps_site())

    assert ok is False
    assert message == expected


def test_missing_address_reports_unavailable(server: FakeServer) -> None:
    """
    站点缺少 PanSou 地址时搜索返回空并报告站点不可用。
    """
    del server

    assert PansouModule().search_netdisk_torrents(_ps_site(url=""), "火影") == []
    assert PansouModule().test_netdisk_site(_ps_site(url="")) == (False, "PS 站点缺少 PanSou 地址")


def test_search_failure_reports_empty(server: FakeServer) -> None:
    """
    搜索接口返回错误时返回空列表而不是抛异常。
    """
    server.responses["/api/search"] = FakeResponse({"code": 1, "message": "参数错误"}, 200)

    assert PansouModule().search_netdisk_torrents(_ps_site(), "火影") == []


def test_auth_failure_reports_token_hint(server: FakeServer) -> None:
    """
    服务端开启认证但站点没填 token 时给出可操作的原因。
    """
    server.responses["/api/search"] = FakeResponse({"message": "unauthorized"}, 401)

    with pytest.raises(PansouError, match="Bearer token"):
        PansouClient(base_url="http://pansou.example:3303").search("火影")


def test_async_search_delegates_to_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    异步入口把阻塞搜索放到线程池，结果与同步一致。
    """
    module = PansouModule()
    monkeypatch.setattr(
        PansouModule,
        "search_netdisk_torrents",
        lambda self, site, keyword, mtype=None, page=0: [],
    )

    assert asyncio_run(module.async_search_netdisk_torrents(_ps_site(), "火影")) == []


def asyncio_run(awaitable: Any) -> Any:
    """
    运行一个协程并返回结果，避免在模块顶部引入事件循环工具。
    """
    import asyncio

    return asyncio.run(awaitable)


def test_module_manifest_is_registered() -> None:
    """
    capability 清单必须与模块目录一一对应，且能按声明物化。
    """
    from app.runtime.extensions.module.adapter import build_host_module_registry

    registry = build_host_module_registry()
    spec = next(item for item in registry.list_specs() if item.id == "PansouModule")

    assert spec.metadata["type"] == "indexer"
    assert spec.metadata["subtype"] == "Pansou"
    assert PansouModule.get_subtype().name == "Pansou"
    assert PansouModule.get_priority() == 2


def test_site_timeout_falls_back_to_thirty_seconds() -> None:
    """
    非法超时配置回退为 30 秒。
    """
    assert PansouModule._site_timeout({}) == 30
    assert PansouModule._site_timeout({"timeout": 0}) == 30
    assert PansouModule._site_timeout({"timeout": "45"}) == 45


def _numbered_files(count: int, start: int = 1) -> List[ShareFile]:
    """
    构造「纯编号命名」的分享文件树，例如 01 4K.mkv / 02 4K.mkv。
    """
    return [
        ShareFile(file_id=f"f{number}", name=f"{number:02d} 4K.mkv", size=350 * 1024 * 1024)
        for number in range(start, start + count)
    ]


def test_mislabeled_share_is_dropped(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    标题声明 218 集、分享里只有 01-40 集的错标包必须被丢弃。

    真实案例：频道把《启运丹田》的分享标成《斗破苍穹 年番 4K 更新218集》，
    文件名只有编号，元数据里没有别的判据。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{
            "url": "https://pan.quark.cn/s/33354a7c3298",
            "note": "斗破苍穹 年番 4K 更新218集",
        }],
    }))
    _install_walk(monkeypatch, files={"33354a7c3298": _numbered_files(40)})

    assert PansouModule().search_netdisk_torrents(_ps_site(), "斗破苍穹") == []


def test_recent_episode_pack_is_kept(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    只放最近若干集的正常分享（180-219 集）不触发内容一致性检查。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [{
            "url": "https://pan.quark.cn/s/recent219",
            "note": "斗破苍穹 年番 4K 更新219集",
        }],
    }))
    _install_walk(monkeypatch, files={"recent219": _numbered_files(40, start=180)})

    results = PansouModule().search_netdisk_torrents(_ps_site(), "斗破苍穹")

    assert results is not None
    assert len(results) == 1


def test_small_claim_does_not_trigger_conflict() -> None:
    """
    声明集数低于下限时不判矛盾，避免短片、综艺被误伤。
    """
    files = [{"name": f"{number:02d}.mp4"} for number in range(1, 13)]

    assert PansouModule._claim_conflicts("某某短剧 全12集", files) is False


def test_claim_conflict_ignores_share_without_episode_numbers() -> None:
    """
    文件名解析不出集号时不做判断，交给通用质量门处理。
    """
    files = [{"name": "斗破苍穹 正片.mkv"}, {"name": "花絮.mkv"}]

    assert PansouModule._claim_conflicts("斗破苍穹 更新218集", files) is False


def test_unrecognized_link_is_dropped(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    错域名或伪造域名的链接（pan.qoark.cn）在进入取链前就被丢弃。
    """
    server.responses["/api/search"] = FakeResponse(_merge_payload({
        "quark": [
            {"url": "https://pan.qoark.cn/s/dpcq", "note": "🎞️ 斗破苍穹"},
            {"url": "https://pan.quark.cn/s/good123", "note": "斗破苍穹 年番"},
        ],
    }))
    _install_walk(monkeypatch)

    results = PansouModule().search_netdisk_torrents(_ps_site(), "斗破苍穹")

    assert results is not None
    assert [item.enclosure for item in results] == ["https://pan.quark.cn/s/good123"]

def test_platform_host_match_keeps_real_forms_and_rejects_lookalikes() -> None:
    """
    域名一致性只拦「错域名」，不能误伤 123/139/UC 等真实链接写法。
    """
    assert PansouModule._platform_matches_host("quark", "https://pan.qoark.cn/s/dpcq") is False
    assert PansouModule._platform_matches_host("quark", "https://pan.quark.cn/s/abc123") is True
    assert PansouModule._platform_matches_host("pan123", "https://123pan.cn/123pan/u9izjv-jwlWv") is True
    assert PansouModule._platform_matches_host("pan139", "https://caiyun.139.com/m/i?105CqFasD8oLm") is True
    assert PansouModule._platform_matches_host("uc", "https://fast.uc.cn/s/431c8fbe24474") is True
    assert PansouModule._platform_matches_host("tianyi", "https://cloud.189.cn/t/abcd") is True
    assert PansouModule._platform_matches_host("magnet", "magnet:?xt=urn:btih:abc") is True