"""
验证 CloudSaver 网盘模块的登录复用、401 重登、结果归一化与站点路由边界。
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.modules.cloudsaver as netdisk_module
from app.modules.cloudsaver import CloudSaverModule
from app.modules.cloudsaver import cloudsaver as cloudsaver_module
from app.modules.cloudsaver.cloudsaver import CloudSaverClient


@pytest.fixture(autouse=True)
def _clear_alive_cache(monkeypatch: pytest.MonkeyPatch):
    """
    每个用例前清空有效性缓存并把夸克客户端替换为替身，避免访问真实网络。
    """
    from app.modules.cloudsaver import _alive_cache

    _alive_cache.clear()
    FakeQuarkClient.reset()
    monkeypatch.setattr(netdisk_module, "QuarkShareClient", FakeQuarkClient)
    yield
    _alive_cache.clear()
    FakeQuarkClient.reset()


class FakeQuarkClient:
    """
    夸克分享客户端替身：按分享码返回预设文件树，并记录调用顺序。
    """

    files_by_code: Dict[str, Optional[List[dict]]] = {}
    calls: List[str] = []
    default_files: List[dict] = [
        {"fid": "f1", "name": "第01集.mp4", "size": 800 * 1024 * 1024, "duration": 1400, "is_dir": False},
    ]

    def __init__(self, timeout: int = 10) -> None:
        """
        保存超时配置，保持与真实客户端一致的构造签名。
        """
        self.timeout = timeout

    @classmethod
    def reset(cls) -> None:
        """
        清空用例之间共享的预设与调用记录。
        """
        cls.files_by_code = {}
        cls.calls = []
        cls.default_files = [
            {"fid": "f1", "name": "第01集.mp4", "size": 800 * 1024 * 1024, "duration": 1400, "is_dir": False},
        ]

    def fetch_stoken(self, pwd_id: str, passcode: str = "") -> str:
        """
        记录换取令牌的调用并返回固定令牌。
        """
        FakeQuarkClient.calls.append(f"stoken:{pwd_id}")
        return "fake-stoken"

    def collect_files(self, pwd_id: str, stoken: str, max_pages: int = 3) -> Optional[List[dict]]:
        """
        返回该分享预设的文件树，未预设时返回默认单集文件。
        """
        FakeQuarkClient.calls.append(f"files:{pwd_id}")
        if pwd_id in FakeQuarkClient.files_by_code:
            return FakeQuarkClient.files_by_code[pwd_id]
        return list(FakeQuarkClient.default_files)


class FakeResponse:
    """
    提供 CloudSaver 客户端测试所需的最小 HTTP 响应接口。
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
        记录响应资源已释放。
        """
        self.closed = True


class FakeCloudSaverServer:
    """
    按 CloudSaver 契约响应登录与搜索请求的假服务端。
    """

    def __init__(self) -> None:
        """
        初始化调用计数与 token 状态。
        """
        self.login_count = 0
        self.search_count = 0
        self.valid_tokens: set[str] = set()
        self.login_status = 200
        self.login_payload: Optional[dict] = None
        self.search_results: List[Tuple[int, dict]] = []
        self.expire_next_search = 0
        self.unreachable = False
        self.requests: List[Tuple[str, dict]] = []
        self.share_check_count = 0
        self.share_file_names = ["资源文件.mp4"]
        # 覆盖 share-info 的根层条目；为空时按 share_file_names 构造普通文件条目。
        self.share_root_items: Optional[List[dict]] = None
        self.invalid_share_codes: set[str] = set()
        # 返回非 JSON 响应，模拟网络异常或超时导致无法判定的分享。
        self.undecidable_share_codes: set[str] = set()
        # 指定分享的详情响应延迟，模拟超出检查预算的慢请求。
        self.share_delay_seconds: Dict[str, float] = {}
        self._token_seq = 0

    def _issue_token(self) -> str:
        """
        签发并记录一个可用的假 token。
        """
        self._token_seq += 1
        token = f"token-{self._token_seq}"
        self.valid_tokens.add(token)
        return token

    def post_res(self, url: str, json: Optional[dict] = None, headers: Optional[dict] = None, **kwargs: Any) -> Optional[FakeResponse]:
        """
        模拟登录与分享目录下钻接口。
        """
        if self.unreachable:
            return None
        self.login_count += 1
        self.requests.append(("login", {"url": url, "json": json, "headers": headers or {}}))
        if self.login_status >= 400:
            return FakeResponse({"message": "login failed"}, self.login_status)
        if self.login_payload is not None:
            return FakeResponse(dict(self.login_payload), self.login_status)
        return FakeResponse({"success": True, "code": 0, "data": {"token": self._issue_token()}})

    def get_res(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None, **kwargs: Any) -> Optional[FakeResponse]:
        """
        模拟搜索与分享有效性接口，校验 Bearer token 并支持强制过期。
        """
        if self.unreachable:
            return None
        if "/api/quark/share-info" in url:
            self.share_check_count += 1
            auth = str((headers or {}).get("Authorization") or "")
            token = auth.removeprefix("Bearer ").strip()
            if token not in self.valid_tokens:
                return FakeResponse({"message": "unauthorized"}, 401)
            share_code = str((params or {}).get("shareCode") or "")
            delay = self.share_delay_seconds.get(share_code)
            if delay:
                time.sleep(delay)
            if share_code in self.invalid_share_codes:
                return FakeResponse({"success": False, "message": "分享已失效"}, 500)
            if share_code in self.undecidable_share_codes:
                # 非 JSON 响应体，客户端无法判定有效性。
                return FakeResponse(None, 200)
            if self.share_root_items is not None:
                root_items = list(self.share_root_items)
            else:
                root_items = [
                    {"fileId": str(index), "fileName": name}
                    for index, name in enumerate(self.share_file_names, start=1)
                ]
            return FakeResponse({
                "success": True,
                "data": {
                    "list": root_items,
                    "fileSize": 1024 * 1024 * 500,
                    "pwdId": "pwd-id",
                    "stoken": "stoken-value",
                },
            })
        self.search_count += 1
        self.requests.append(("search", {"url": url, "params": params, "headers": headers or {}}))
        if self.expire_next_search > 0:
            self.expire_next_search -= 1
            self.valid_tokens.clear()
            return FakeResponse({"message": "unauthorized"}, 401)
        auth = str((headers or {}).get("Authorization") or "")
        token = auth.removeprefix("Bearer ").strip()
        if token not in self.valid_tokens:
            return FakeResponse({"message": "unauthorized"}, 401)
        if self.search_results:
            status, payload = self.search_results.pop(0)
            return FakeResponse(payload, status)
        return FakeResponse({"success": True, "data": []})


def install_server(monkeypatch: pytest.MonkeyPatch, server: FakeCloudSaverServer) -> None:
    """
    把客户端使用的网络适配器替换为假服务端。
    """
    monkeypatch.setattr(cloudsaver_module, "RequestUtils", lambda **kwargs: server)


def make_site(**overrides: Any) -> Dict[str, Any]:
    """
    构造一个字段完整的 CD 站点配置。
    """
    site: Dict[str, Any] = {
        "id": 7,
        "name": "测试CD站点",
        "url": "http://cloudsaver.local",
        "kind": "cd",
        "username": "user",
        "password": "secret",
        "timeout": 90,
        "downloader": "qb",
        "pri": 1,
        "is_active": True,
    }
    site.update(overrides)
    return site


def make_payload(items: List[dict], channel: str = "频道A") -> dict:
    """
    构造搜索接口的分组响应。
    """
    return {
        "success": True,
        "data": [{"channelInfo": {"name": channel}, "list": items}],
    }


def test_search_reuses_token_after_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    登录成功后连续搜索应复用同一 token，不重复登录。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([{
        "title": "庆余年",
        "content": "第一季",
        "cloudLinks": ["https://pan.quark.cn/s/abc"],
    }]))]
    install_server(monkeypatch, server)

    client = CloudSaverClient("http://cloudsaver.local", "user", "secret")
    first = client.search("庆余年")
    second = client.search("庆余年2")

    assert server.login_count == 1
    assert server.search_count == 2
    assert first[0]["title"] == "庆余年"
    assert second == []


def test_search_relogins_and_retries_once_after_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    搜索返回 401 时应清空 token、重新登录并重试一次。
    """
    server = FakeCloudSaverServer()
    server.expire_next_search = 1
    server.search_results = [(200, make_payload([{
        "title": "重试成功",
        "cloudLinks": [{"link": "https://115.com/s/xyz", "cloudType": "pan115"}],
    }]))]
    install_server(monkeypatch, server)

    client = CloudSaverClient("http://cloudsaver.local", "user", "secret")
    items = client.search("关键词")

    assert server.login_count == 2
    assert server.search_count == 2
    assert items[0]["title"] == "重试成功"


def test_parse_string_and_object_cloud_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    cloudLinks 的字符串数组与对象数组都应解析出链接和平台。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "字符串链接",
            "content": "",
            "cloudLinks": ["https://pan.quark.cn/s/string-link"],
        },
        {
            "title": "对象链接",
            "content": "",
            "cloudLinks": [{"link": "https://115.com/s/object-link", "cloudType": "pan115"}],
        },
    ]))]
    install_server(monkeypatch, server)

    items = CloudSaverClient("http://cloudsaver.local", "user", "secret").search("关键词")

    assert items[0]["links"] == [
        {"url": "https://pan.quark.cn/s/string-link", "platform": "quark", "password": None}
    ]
    assert items[1]["links"] == [
        {"url": "https://115.com/s/object-link", "platform": "pan115", "password": None}
    ]


def test_parse_password_from_url_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    提取码应同时支持链接查询参数与内容文本两种来源。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "链接提取码",
            "content": "",
            "cloudLinks": ["https://pan.quark.cn/s/abc?password=abcd"],
        },
        {
            "title": "文本提取码",
            "content": "资源简介，提取码：efgh",
            "cloudLinks": ["https://pan.quark.cn/s/def"],
        },
    ]))]
    install_server(monkeypatch, server)

    items = CloudSaverClient("http://cloudsaver.local", "user", "secret").search("关键词")

    assert items[0]["links"][0]["password"] == "abcd"
    assert items[1]["links"][0]["password"] == "efgh"


def test_search_netdisk_torrents_builds_and_dedupes_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    同一平台与同一分享链接只生成一个候选，并写入网盘字段。
    """
    server = FakeCloudSaverServer()
    share_url = "https://pan.quark.cn/s/duplicate"
    server.search_results = [(200, make_payload([
        {
            "title": "候选一",
            "content": "简介",
            "pubDate": "2026-10-01",
            "cloudType": "quark",
            "cloudLinks": [share_url],
        },
        {
            "title": "候选二",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [share_url],
        },
    ]))]
    install_server(monkeypatch, server)

    module = CloudSaverModule()
    results = module.search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert len(results) == 1
    torrent = results[0]
    assert strip_episode(torrent.title) == "候选一"
    assert torrent.enclosure == share_url
    assert torrent.page_url == share_url
    assert torrent.download_type == "netdisk"
    assert torrent.netdisk_platform == "quark"
    assert torrent.labels == ["quark", "频道A"]
    assert torrent.site == 7
    assert torrent.site_downloader == "qb"


@pytest.mark.parametrize("mode", ["unreachable", "bad_credentials"])
def test_search_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """
    CloudSaver 不可达或凭据错误时搜索返回空列表且不抛异常。
    """
    server = FakeCloudSaverServer()
    if mode == "unreachable":
        server.unreachable = True
    else:
        server.login_payload = {"success": False, "code": 1001, "message": "用户名或密码错误"}
    install_server(monkeypatch, server)

    module = CloudSaverModule()
    assert module.search_netdisk_torrents(make_site(), "关键词") == []
    available, reason = module.test_netdisk_site(make_site())
    assert available is False
    if mode == "unreachable":
        assert "网络请求失败" in reason
    else:
        assert reason == "CloudSaver 登录失败：用户名或密码错误"


def test_missing_credentials_marks_site_unavailable() -> None:
    """
    缺少地址或凭据时搜索返回空列表，测试返回可读原因。
    """
    module = CloudSaverModule()
    site = make_site(password="")

    assert module.search_netdisk_torrents(site, "关键词") == []
    assert module.test_netdisk_site(site) == (False, "CD 站点缺少 CloudSaver 地址或凭据")
    assert module.refresh_netdisk_token(site) is False


def test_non_cd_site_returns_none() -> None:
    """
    非 cd 站点的全部网盘方法都返回 None，交由其他模块处理。
    """
    module = CloudSaverModule()
    site = make_site(kind="pt")

    assert module.search_netdisk_torrents(site, "关键词") is None
    assert asyncio.run(module.async_search_netdisk_torrents(site, "关键词")) is None
    assert module.test_netdisk_site(site) is None
    assert module.refresh_netdisk_token(site) is None


def test_refresh_forces_new_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    刷新 token 应丢弃缓存并重新登录。
    """
    server = FakeCloudSaverServer()
    install_server(monkeypatch, server)

    module = CloudSaverModule()
    site = make_site()
    assert module.test_netdisk_site(site) == (True, "")
    assert server.login_count == 1
    assert module.refresh_netdisk_token(site) is True
    assert server.login_count == 2


def test_clean_title_strips_channel_prefix_and_entities():
    """
    标题清洗应去掉「名称：」前缀、还原 HTML 实体并压缩空白。
    """
    from app.modules.cloudsaver.cloudsaver import _clean_title

    assert _clean_title("名称：仙逆 (2023) 4K") == "仙逆 (2023) 4K"
    assert _clean_title("  标题: 流浪地球2 &amp; 花絮 ") == "流浪地球2 & 花絮"
    assert _clean_title("普通标题") == "普通标题"


def test_search_reports_site_observation_for_manual_paging(monkeypatch):
    """
    网盘搜索必须上报站点观察结果，否则手动分页会误判为「本页请求未完成」。
    """
    from app.application.site.observation import capture_site_search_observation
    from app.modules.cloudsaver import CloudSaverModule

    module = CloudSaverModule()
    site = {
        "id": 1,
        "name": "网盘站点",
        "kind": "cd",
        "url": "http://cloudsaver.example:8008",
        "username": "admin",
        "password": "secret",
    }
    monkeypatch.setattr(module, "_get_client", lambda site: _FakeClient())

    with capture_site_search_observation() as observation:
        items = module.search_netdisk_torrents(site, "测试")

    assert items
    assert observation.attempted is True
    assert observation.outcome == "success"
    assert observation.raw_count == len(items)
    assert observation.has_more is False


class _FakeClient:
    """
    返回固定搜索结果的 CloudSaver 客户端替身。
    """

    def share_detail(self, share_code: str, receive_code: Optional[str] = None) -> dict:
        """
        返回固定分享详情，供观察结果上报测试使用。

        :param share_code: 分享码
        :param receive_code: 提取码
        :return: 固定为有效且带大小与文件列表
        """
        return {"alive": True, "file_size": 1024 * 1024 * 500, "file_names": ["第01集.mp4", "第12集.mp4"], "stoken": "s"}

    def search(self, keyword: str) -> list[dict]:
        """
        返回一条带分享链接的结果。

        :param keyword: 搜索关键词
        :return: 归一化后的条目列表
        """
        return [
            {
                "title": f"名称：{keyword} 2024",
                "content": "",
                "pubdate": "",
                "channel": "测试频道",
                "links": [{"url": "https://pan.quark.cn/s/abcdef", "platform": "quark", "password": None}],
            }
        ]


def test_search_keeps_only_quark_and_marks_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    只展示夸克分享，并把网盘候选标记为免费下载。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "多平台资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [
                {"link": "https://pan.quark.cn/s/quarkone", "cloudType": "quark"},
                {"link": "https://www.aliyundrive.com/s/aliyunone", "cloudType": "aliyun"},
            ],
        },
    ]))]
    install_server(monkeypatch, server)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert [item.netdisk_platform for item in results] == ["quark"]
    assert results[0].uploadvolumefactor == 1.0
    assert results[0].downloadvolumefactor == 0.0
    assert results[0].volume_factor == "免费"


def test_search_drops_invalid_shares(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    有效性检查失败的分享不进入候选列表。
    """
    server = FakeCloudSaverServer()
    server.invalid_share_codes.add("deadshare")
    server.search_results = [(200, make_payload([
        {
            "title": "失效资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/deadshare", "cloudType": "quark"}],
        },
        {
            "title": "有效资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/liveshare", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert [strip_episode(item.title) for item in results] == ["有效资源"]
    assert server.share_check_count == 2


def test_search_drops_undecidable_shares(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情无法判定有效性时按失效处理，候选不进入结果。
    """
    server = FakeCloudSaverServer()
    server.undecidable_share_codes.add("unknownshare")
    server.search_results = [(200, make_payload([
        {
            "title": "无法判定资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/unknownshare", "cloudType": "quark"}],
        },
        {
            "title": "有效资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/liveshare", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert [strip_episode(item.title) for item in results] == ["有效资源"]
    assert server.share_check_count == 2
    # 无法判定的分享不写入有效性缓存，下次搜索仍会重新检查。
    from app.modules.cloudsaver import _alive_cache

    assert not any("unknownshare" in key for key in _alive_cache)


def test_search_dedupes_same_share_code_across_url_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    同一分享码的不同链接形式只保留一条候选。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "候选一",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/samecode", "cloudType": "quark"}],
        },
        {
            "title": "候选二",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/samecode?entry=share", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert len(results) == 1
    assert server.share_check_count == 1


def test_clean_title_converts_chinese_episode_marker():
    """
    中文集数标记应转换为标准季集，前端才能展示集数。
    """
    from app.modules.cloudsaver.cloudsaver import _clean_title

    assert _clean_title("名称：仙逆 (2023) 4K [更新164集]") == "仙逆 (2023) 4K S01E164"
    assert _clean_title("仙逆 第12集") == "仙逆 S01E12"
    assert _clean_title("仙逆 S01E05 4K") == "仙逆 S01E05 4K"
    assert _clean_title("短剧 更新至08话") == "短剧 S01E08"
    assert _clean_title("2026年10月30日 短剧更新目录3") == "2026年10月30日 短剧更新目录3"


def test_search_fills_size_and_episode_from_share_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情应补齐候选大小，并把文件名中的集数写入标题。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "仙逆 (2023) 4K",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/liveshare", "cloudType": "quark"}],
        },
    ]))]
    FakeQuarkClient.files_by_code["liveshare"] = make_episode_files([1, 12], duration=1400, size_mb=900)
    install_server(monkeypatch, server)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert results[0].size == 1024 * 1024 * 500
    assert results[0].title == "仙逆 (2023) 4K S01E01-E12"
    # 追加的集数标记必须能被媒体识别解析为季集展示文本，前端才有集数可显示。
    from app.domain.metainfo import MetaInfo
    from app.schemas.types import MediaType

    meta = MetaInfo(title=results[0].title or "", subtitle=results[0].description, mtype=MediaType.TV)
    assert meta.season == "S01"
    assert meta.episode == "E01-E12"
    assert meta.season_episode == "S01 E01-E12"


def test_episode_token_prefers_range() -> None:
    """
    文件名集数应生成范围标记，单集时只写一集。
    """
    assert CloudSaverModule._episode_token(["第01集.mp4", "第12集.mp4"]) == "S01E01-E12"
    assert CloudSaverModule._episode_token(["E05.mkv"]) == "S01E05"
    assert CloudSaverModule._episode_token(["01.mp4", "02.mp4"]) == "S01E01-E02"
    assert CloudSaverModule._episode_token(["花絮.mp4"]) is None


def test_episode_token_handles_range_and_ignores_resolution() -> None:
    """
    文件夹名里的区间与「剧名.集数.mp4」都应识别，分辨率与年份不算集数。
    """
    assert CloudSaverModule._episode_token(["仙逆 153-162"]) == "S01E153-E162"
    assert CloudSaverModule._episode_token(["仙逆.153.mp4", "仙逆.155.mp4", "仙逆.162.mp4"]) == "S01E153-E162"
    assert CloudSaverModule._episode_token(["仙逆 4K 1080p 2023.mp4"]) is None
    assert CloudSaverModule._episode_token(["剧名#164.mp4"]) is None
    assert CloudSaverModule._episode_token(["花絮1.mp4", "预告2.mp4"]) is None


def test_bare_number_requires_unambiguous_episode() -> None:
    """
    裸数字只在能明确判定为集数时使用，电影名与音轨规格不被误判。
    """
    assert CloudSaverModule._episode_token(["仙逆.153.mp4"]) == "S01E153"
    assert CloudSaverModule._episode_token(["162 4K.mp4"]) == "S01E162"
    assert CloudSaverModule._episode_token(["01.mp4"]) == "S01E01"
    assert CloudSaverModule._episode_token(["流浪地球2.DDP2.0&AAC5.1.mp4"]) is None
    assert CloudSaverModule._episode_token(["流浪地球2.mp4"]) is None


def strip_episode(title: str) -> str:
    """
    去掉标题末尾追加的季集标记，便于断言原始标题。
    """
    return title.split(" S01E")[0]


def install_quark(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    用夸克客户端替身替换真实客户端，避免测试访问真实网络。
    """
    monkeypatch.setattr(netdisk_module, "QuarkShareClient", FakeQuarkClient)


def make_share_payload(title: str, share_code: str = "foldershare") -> dict:
    """
    构造一条网盘搜索结果的响应体。
    """
    return make_payload([
        {
            "title": title,
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": f"https://pan.quark.cn/s/{share_code}", "cloudType": "quark"}],
        },
    ])


def make_episode_files(episodes, duration: int = 1400, size_mb: int = 900) -> List[dict]:
    """
    构造指定集数的视频文件条目。
    """
    return [
        {
            "fid": f"f{number}",
            "name": f"仙逆.{number}.mp4",
            "size": size_mb * 1024 * 1024,
            "duration": duration,
            "is_dir": False,
        }
        for number in episodes
    ]


def test_quark_tree_replaces_title_inferred_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    夸克文件树能读到真实集数时，标题推断的集数被真实区间替换。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_share_payload("仙逆 更新至164集"))]
    install_server(monkeypatch, server)
    install_quark(monkeypatch)
    FakeQuarkClient.files_by_code["foldershare"] = make_episode_files(range(153, 163))

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results is not None
    assert len(results) == 1
    assert results[0].title == "仙逆 S01E153-E162"
    assert FakeQuarkClient.calls == ["files:foldershare"]

    from app.domain.metainfo import MetaInfo
    from app.schemas.types import MediaType

    meta = MetaInfo(title=results[0].title or "", mtype=MediaType.TV)
    assert meta.season_episode == "S01 E153-E162"
    assert meta.episode_list == list(range(153, 163))


def test_share_tree_unavailable_drops_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    拿不到文件树时按失效丢弃候选，不再回退标题推断值。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_share_payload("仙逆 更新至164集"))]
    install_server(monkeypatch, server)
    install_quark(monkeypatch)
    FakeQuarkClient.files_by_code["foldershare"] = None

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results == []


def test_obfuscated_names_drop_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    文件名被混淆导致集数解析覆盖率不足时丢弃候选。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_share_payload("周默晚 仙逆"))]
    install_server(monkeypatch, server)
    install_quark(monkeypatch)
    FakeQuarkClient.files_by_code["foldershare"] = [
        {"fid": "f1", "name": "164.mp4", "size": 1029 * 1024 * 1024, "duration": 1400, "is_dir": False},
        {"fid": "f2", "name": "1#6@#2.mp4", "size": 900 * 1024 * 1024, "duration": 1400, "is_dir": False},
        {"fid": "f3", "name": "1!6%1.mp4", "size": 900 * 1024 * 1024, "duration": 1400, "is_dir": False},
    ]

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results == []


def test_each_candidate_uses_its_own_share_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    多个候选并发检查时，每个候选都必须使用自己的分享文件树，不能串号。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "甲 更新至3集",
            "content": "简介",
            "pubDate": "2026-10-03",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/shareA", "cloudType": "quark"}],
        },
        {
            "title": "乙 更新至52集",
            "content": "简介",
            "pubDate": "2026-10-02",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/shareB", "cloudType": "quark"}],
        },
        {
            "title": "丙 更新至102集",
            "content": "简介",
            "pubDate": "2026-10-01",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/shareC", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)
    FakeQuarkClient.files_by_code["shareA"] = make_episode_files([1, 2, 3])
    FakeQuarkClient.files_by_code["shareB"] = make_episode_files([50, 51, 52])
    FakeQuarkClient.files_by_code["shareC"] = make_episode_files([100, 101, 102])

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results is not None
    by_title = {item.title.split(" ")[0]: item.title for item in results}
    assert by_title["甲"].endswith("S01E01-E03")
    assert by_title["乙"].endswith("S01E50-E52")
    assert by_title["丙"].endswith("S01E100-E102")


def test_failed_share_tree_does_not_affect_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    一条候选的直连失败不影响其他候选的集数推导。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "坏资源",
            "content": "简介",
            "pubDate": "2026-10-02",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/brokenquark", "cloudType": "quark"}],
        },
        {
            "title": "好资源",
            "content": "简介",
            "pubDate": "2026-10-01",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/goodquark", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)
    FakeQuarkClient.files_by_code["brokenquark"] = None
    FakeQuarkClient.files_by_code["goodquark"] = make_episode_files([1, 2, 3])

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results is not None
    assert len(results) == 1
    assert results[0].title.endswith("S01E01-E03")


def test_short_drama_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    平均每集时长与大小同时低于阈值时判为短剧并丢弃。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_share_payload("【短剧】仙逆（115集）"))]
    install_server(monkeypatch, server)
    install_quark(monkeypatch)
    FakeQuarkClient.files_by_code["foldershare"] = make_episode_files(range(1, 6), duration=66, size_mb=10)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results == []


def test_single_condition_keeps_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    时长与大小只有一个低于阈值时保留候选。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_share_payload("仙逆 更新至164集"))]
    install_server(monkeypatch, server)
    install_quark(monkeypatch)
    FakeQuarkClient.files_by_code["foldershare"] = make_episode_files(range(1, 6), duration=66, size_mb=900)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert results is not None
    assert len(results) == 1
    assert results[0].title.endswith("S01E01-E05")


def test_short_drama_threshold_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    调整系统设置阈值后，同一分享的短剧判定结果随之改变（缓存不能短路重新判定）。
    """
    server = FakeCloudSaverServer()
    install_server(monkeypatch, server)
    FakeQuarkClient.files_by_code["foldershare"] = make_episode_files(range(1, 6), duration=400, size_mb=100)

    class _Config:
        """提供 get 接口的最小配置替身。"""

        def __init__(self, values: dict) -> None:
            self.values = values

        def get(self, key: Any = None) -> Any:
            """返回预设值。"""
            return self.values.get(getattr(key, "value", key))

    payload = make_share_payload("仙逆 更新至164集")
    monkeypatch.setattr(netdisk_module, "get_configured_system_config", lambda: _Config({}))
    server.search_results = [(200, payload)]
    kept = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")
    assert kept is not None and len(kept) == 1

    # 第二次搜索命中有效性缓存，但短剧判定必须重新计算。
    monkeypatch.setattr(netdisk_module, "get_configured_system_config", lambda: _Config({
        "NetdiskShortDramaMaxDuration": 500,
        "NetdiskShortDramaMaxSize": 150,
    }))
    server.search_results = [(200, payload)]
    dropped = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")
    assert dropped == []


def test_cached_share_still_fills_size_and_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    命中有效性缓存时仍要补齐大小与真实集数，不能因为缓存而丢失详情。
    """
    server = FakeCloudSaverServer()
    install_server(monkeypatch, server)
    FakeQuarkClient.files_by_code["foldershare"] = make_episode_files(range(1, 61))
    payload = make_share_payload("仙逆 更新至164集")

    server.search_results = [(200, payload)]
    first = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")
    assert first is not None and len(first) == 1
    assert first[0].title.endswith("S01E01-E60")
    assert first[0].size == 1024 * 1024 * 500

    server.search_results = [(200, payload)]
    second = CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")
    assert second is not None and len(second) == 1
    assert second[0].title.endswith("S01E01-E60")
    assert second[0].size == 1024 * 1024 * 500
    # 缓存命中时不再请求分享详情，但仍要拉文件树。
    assert server.share_check_count == 1


def test_share_tree_uses_detail_stoken(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享详情已带 stoken 时直接复用，不再额外换取令牌。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_share_payload("仙逆 更新至164集"))]
    install_server(monkeypatch, server)
    install_quark(monkeypatch)
    FakeQuarkClient.files_by_code["foldershare"] = make_episode_files(range(1, 3))

    CloudSaverModule().search_netdisk_torrents(make_site(), "仙逆")

    assert FakeQuarkClient.calls == ["files:foldershare"]


def test_clean_description_strips_markup_and_prompts() -> None:
    """
    描述清洗应去掉 HTML 标记、重复标题与平台引导语，只保留正文。
    """
    from app.modules.cloudsaver.cloudsaver import _clean_description

    raw = ("名称：<mark class=\"highlight\">仙逆</mark> 4K [更新至161集] "
           "描述：乡村平凡少年王林，踏入仙门修行！夸克：")
    assert _clean_description(raw) == "乡村平凡少年王林，踏入仙门修行！"

    chained = "描述：正文内容 夸克网盘： 链接："
    assert _clean_description(chained) == "正文内容"

    plain = "一段没有结构的简介"
    assert _clean_description(plain) == plain


def test_apply_share_detail_replaces_existing_range_marker() -> None:
    """
    标题已有的季集区间标记（含 -E 形式）也要被真实集数整体替换，不残留第二段标记。
    """
    from app.domain.context import TorrentInfo

    torrent = TorrentInfo(title="仙逆 S01E01-E12", enclosure="https://pan.quark.cn/s/x")
    CloudSaverModule._apply_share_detail(torrent, {"file_size": 0}, make_episode_files([153]))

    assert torrent.title == "仙逆 S01E153"


def test_search_drops_candidates_over_check_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    分享有效性检查超出总预算时，未完成的候选按失效处理，已完成的候选照常返回。
    """
    server = FakeCloudSaverServer()
    server.share_delay_seconds["slowshare"] = 0.2
    server.search_results = [(200, make_payload([
        {
            "title": "慢响应资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/slowshare", "cloudType": "quark"}],
        },
        {
            "title": "快速响应资源",
            "content": "简介",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/fastshare", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)
    # 把 30 秒总预算压到毫秒级，避免用例等待真实超时。
    monkeypatch.setattr(netdisk_module, "_ALIVE_BUDGET_SECONDS", 0.02)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert [strip_episode(item.title) for item in results] == ["快速响应资源"]


def test_search_limits_alive_checks_to_newest_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    单次搜索只检查最新的若干条候选，超出检查上限的候选不返回也不发起请求。
    """
    server = FakeCloudSaverServer()
    server.search_results = [(200, make_payload([
        {
            "title": "最旧资源",
            "content": "简介",
            "pubDate": "2026-10-01",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/shareold", "cloudType": "quark"}],
        },
        {
            "title": "最新资源",
            "content": "简介",
            "pubDate": "2026-10-03",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/sharenew", "cloudType": "quark"}],
        },
        {
            "title": "次新资源",
            "content": "简介",
            "pubDate": "2026-10-02",
            "cloudType": "quark",
            "cloudLinks": [{"link": "https://pan.quark.cn/s/sharemiddle", "cloudType": "quark"}],
        },
    ]))]
    install_server(monkeypatch, server)
    # 把 200 条检查上限压到 2 条，验证截断按发布时间优先。
    monkeypatch.setattr(netdisk_module, "_MAX_ALIVE_CHECKS", 2)

    results = CloudSaverModule().search_netdisk_torrents(make_site(), "关键词")

    assert results is not None
    assert [strip_episode(item.title) for item in results] == ["最新资源", "次新资源"]
    assert server.share_check_count == 2
