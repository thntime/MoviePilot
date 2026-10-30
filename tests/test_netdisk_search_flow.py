"""
CD 站点网盘搜索的路由、投影与过滤中性化测试。
"""

from types import SimpleNamespace

from app.chain import site as site_module
from app.chain.search import provider as provider_module
from app.domain.context import TorrentInfo
from app.modules.filter import FilterModule


class _RuleHelper:
    """
    过滤模块测试用的轻量规则仓库，避免依赖真实系统配置。
    """

    def __init__(self, groups) -> None:
        """
        保存测试规则组。

        :param groups: 规则组替身列表
        """
        self._groups = groups

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        """
        按名称返回测试规则组。

        :param media: 未使用的媒体信息
        :param group_names: 需要保留的规则组名称
        :return: 规则组列表
        """
        if not group_names:
            return self._groups
        return [group for group in self._groups if group.name in group_names]


def _snapshot(site_id: int, kind: str, name: str) -> SimpleNamespace:
    """
    构造站点配置快照替身。

    :param site_id: 站点主键
    :param kind: 站点类型
    :param name: 站点名称
    :return: 具备搜索链所需字段的快照替身
    """
    return SimpleNamespace(
        id=site_id,
        name=name,
        domain=f"site{site_id}.example.com",
        url=f"http://site{site_id}.example.com/",
        pri=1,
        kind=kind,
        auth_type="auth" if kind == "cd" else "cookie",
        username="user" if kind == "cd" else None,
        password="secret" if kind == "cd" else None,
        auth_refresh_interval=300 if kind == "cd" else None,
        timeout=90 if kind == "cd" else 15,
        downloader=None,
        proxy=0,
        limit_interval=0,
        limit_count=0,
        limit_seconds=0,
        is_active=True,
    )


def test_netdisk_indexers_only_include_cd_sites() -> None:
    """
    CD 站点投影只保留类型为 cd 的启用站点。
    """
    snapshots = [
        _snapshot(1, "pt", "PT 站点"),
        _snapshot(2, "cd", "网盘站点"),
    ]

    projected = provider_module._netdisk_indexers(snapshots, [], set())

    assert [site["id"] for site in projected] == [2]
    assert projected[0]["kind"] == "cd"
    assert projected[0]["username"] == "user"
    assert projected[0]["timeout"] == 90


def test_netdisk_indexers_respect_selected_sites() -> None:
    """
    调用方指定站点 ID 时，未选中的 CD 站点不参与本次搜索。
    """
    snapshots = [
        _snapshot(2, "cd", "网盘站点一"),
        _snapshot(3, "cd", "网盘站点二"),
    ]

    projected = provider_module._netdisk_indexers(snapshots, [3], set())

    assert [site["id"] for site in projected] == [3]


def test_search_site_page_routes_netdisk_site_to_netdisk_search(monkeypatch) -> None:
    """
    CD 站点的一页搜索必须走网盘搜索入口，而不是 PT 索引器搜索。
    """
    calls = []

    class _Owner:
        """
        记录搜索入口调用的站点所有者替身。
        """

        def search_netdisk_torrents(self, **kwargs):
            """
            记录网盘搜索调用。

            :param kwargs: 搜索参数
            :return: 固定候选列表
            """
            calls.append(("netdisk", kwargs))
            return [TorrentInfo(title="网盘候选", download_type="netdisk")]

        def search_site_torrents(self, **kwargs):
            """
            记录 PT 站点搜索调用。

            :param kwargs: 搜索参数
            :return: 固定候选列表
            """
            calls.append(("torrent", kwargs))
            return [TorrentInfo(title="种子候选")]

    monkeypatch.setattr(provider_module, "_wait_for_site_request", lambda site: None)

    result = provider_module._search_site_page(
        _Owner(),
        site={"id": 2, "name": "网盘站点", "kind": "cd"},
        keyword="测试",
        media_type=None,
        page=0,
    )

    assert calls and calls[0][0] == "netdisk"
    assert result and result[0].download_type == "netdisk"


def test_search_site_page_keeps_pt_site_on_torrent_search(monkeypatch) -> None:
    """
    PT 站点的一页搜索保持原有索引器搜索入口。
    """
    calls = []

    class _Owner:
        """
        记录搜索入口调用的站点所有者替身。
        """

        def search_netdisk_torrents(self, **kwargs):  # noqa: ARG002
            """
            记录不应发生的网盘搜索调用。

            :param kwargs: 搜索参数
            :return: 空列表
            """
            calls.append("netdisk")
            return []

        def search_site_torrents(self, **kwargs):  # noqa: ARG002
            """
            记录 PT 站点搜索调用。

            :param kwargs: 搜索参数
            :return: 固定候选列表
            """
            calls.append("torrent")
            return [TorrentInfo(title="种子候选")]

    monkeypatch.setattr(provider_module, "_wait_for_site_request", lambda site: None)

    provider_module._search_site_page(
        _Owner(),
        site={"id": 1, "name": "PT 站点", "kind": "pt"},
        keyword="测试",
        media_type=None,
        page=0,
    )

    assert calls == ["torrent"]


def test_filter_skips_pt_only_conditions_for_netdisk_candidates() -> None:
    """
    网盘候选没有做种与促销语义，不应被 PT 专属规则淘汰。
    """
    module = FilterModule()
    module.rulehelper = _RuleHelper([SimpleNamespace(name="test", rule_string="SEED")])
    module.rule_set = {"SEED": {"seeders": 5}}

    torrent_candidate = TorrentInfo(title="Movie 2024 1080p", seeders=0)
    netdisk_candidate = TorrentInfo(
        title="Movie 2024 1080p",
        seeders=0,
        download_type="netdisk",
        netdisk_platform="quark",
    )

    filtered = module.filter_torrents(
        rule_groups=["test"],
        torrent_list=[torrent_candidate, netdisk_candidate],
    )

    assert torrent_candidate not in filtered
    assert netdisk_candidate in filtered


def test_refresh_netdisk_tokens_respects_interval_and_isolates_failures(monkeypatch) -> None:
    """
    CD 站点令牌刷新按各自间隔执行，单站点失败不影响其他站点，PT 站点不参与。
    """
    from app.chain.site import SiteChain

    chain = object.__new__(SiteChain)
    snapshots = [
        _snapshot(1, "pt", "PT 站点"),
        _snapshot(2, "cd", "网盘站点一"),
        _snapshot(3, "cd", "网盘站点二"),
    ]
    chain.site_repository = SimpleNamespace(list_active=lambda: snapshots)
    calls: list[int] = []

    def fake_refresh(site):
        """
        记录刷新调用，并让站点 3 失败。

        :param site: CD 站点配置
        :return: 站点 2 成功，站点 3 失败
        """
        calls.append(site["id"])
        return site["id"] != 3

    monkeypatch.setattr(SiteChain, "refresh_netdisk_token", lambda self, site: fake_refresh(site))
    site_module._netdisk_token_refreshed_at.clear()

    chain.refresh_netdisk_tokens()

    assert calls == [2, 3]
    assert set(site_module._netdisk_token_refreshed_at) == {2}

    chain.refresh_netdisk_tokens()

    # 站点 2 已到间隔不再登录；站点 3 上轮失败，下个节拍继续重试。
    assert calls == [2, 3, 3]


def test_apply_site_kinds_tags_indexer_from_snapshot() -> None:
    """
    资源包未携带类型时，用站点快照把 CD 站点标注为网盘站点，避免走 PT 索引器路径。
    """
    indexers = [{"id": 1, "name": "th"}]
    snapshots = [_snapshot(1, "cd", "th")]

    tagged = provider_module._apply_site_kinds(indexers, snapshots)

    assert tagged[0]["kind"] == "cd"
    assert provider_module._is_netdisk_site(tagged[0]) is True


def test_apply_site_kinds_defaults_unknown_site_to_pt() -> None:
    """
    快照中不存在的站点保持 PT 语义，避免误走网盘搜索。
    """
    indexers = [{"id": 9, "name": "未知站点"}]

    tagged = provider_module._apply_site_kinds(indexers, [])

    assert tagged[0]["kind"] == "pt"
