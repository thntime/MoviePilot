"""
网盘取链编排的单元测试：策略分支、缓存、多源、刷新与清理。
"""

from datetime import datetime, timedelta

import pytest

from app.adapters.external.netdisk.base import BaseNetdiskAdapter
from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService, mask_credential
from app.application.netdisk.contract import (
    NetdiskAccountRecord,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskTuning,
)
from app.application.netdisk.resolve import NetdiskResolveService
from app.domain.netdisk import (
    AccountProbe,
    DirectLink,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    ShareRef,
    TransferHandle,
)


class _Adapter(BaseNetdiskAdapter):
    """
    可编排行为的取链适配器替身。
    """

    platform = NetdiskPlatform.QUARK
    link_policy = LinkPolicy.DIRECT_WITH_FALLBACK
    requires_account = True
    max_concurrency = 512
    cookie_required_keys = ("__pus", "__puus")
    cookie_domains = (".quark.cn",)
    login_url = "https://pan.quark.cn/"

    def __init__(self) -> None:
        super().__init__()
        self.resolve_calls: list[tuple[int, bool]] = []
        self.cleanup_calls: list[TransferHandle] = []
        self.fail_times = 0
        self.fail_code = NetdiskErrorCode.AUTH_EXPIRED
        self.last_allow_transfer = True

    def share_detail(self, ref, account):
        """
        返回固定的分享详情。
        """
        return ShareDetail(
            title="测试分享",
            size=10,
            files=(ShareFile(file_id="f1", name="a.mp4", size=10),),
            stoken="st-1",
        )

    def list_files(self, ref, parent_id, account):
        """
        返回固定目录。
        """
        return [ShareFile(file_id="f2", name="b.mp4", size=5)]

    def resolve(self, ref, file, account, *, allow_transfer=True, transfer_first=False):
        """
        记录调用参数并按需失败。
        """
        self.resolve_calls.append((account.id if account else 0, allow_transfer))
        self.last_transfer_first = transfer_first
        self.last_allow_transfer = allow_transfer
        if self.fail_times > 0:
            self.fail_times -= 1
            raise NetdiskError(self.fail_code, "直链已过期", platform=self.platform)
        return DirectLink(
            url=f"https://cdn.example.com/{file.file_id}",
            headers={"Cookie": "x=1"},
            size=file.size,
            expires_at=datetime.now() + timedelta(hours=1),
            cleanup_handle=TransferHandle(platform=self.platform, account_id=account.id if account else 0),
            account_id=account.id if account else 0,
        )

    def cleanup(self, handle, account):
        """
        记录清理调用。
        """
        self.cleanup_calls.append(handle)

    def refresh_account(self, account):
        """
        返回刷新后的 Cookie。
        """
        return {"cookie": "__pus=1; __puus=2"}

    def validate_credential(self, credential):
        """
        固定返回有效。
        """
        return AccountProbe(status="active")


class _UcAdapter(_Adapter):
    """
    UC 适配器替身：声明支持游客，但按规格无账号时仍必须登录。
    """

    platform = NetdiskPlatform.UC
    guest_supported = True


class _Store:
    """
    内存账号仓储替身。
    """

    def __init__(self) -> None:
        self.views: list[NetdiskAccountView] = []
        self.records: dict[int, NetdiskAccountRecord] = {}
        self._next = 1

    def list_accounts(self, platform=None):
        return [view for view in self.views if platform is None or view.platform == platform.value]

    def get_record(self, account_id):
        return self.records.get(account_id)

    def create_account(self, payload: NetdiskAccountWrite):
        account_id = self._next
        self._next += 1
        view = NetdiskAccountView(
            id=account_id,
            platform=payload.platform.value,
            name=payload.name,
            is_primary=payload.is_primary,
            status="active",
            masked=mask_credential(payload.credential),
        )
        self.views.append(view)
        self.records[account_id] = NetdiskAccountRecord(
            id=account_id,
            platform=payload.platform,
            name=payload.name,
            credential=dict(payload.credential),
            is_primary=payload.is_primary,
        )
        return view

    def update_account(self, account_id, **kwargs):
        record = self.records[account_id]
        self.records[account_id] = NetdiskAccountRecord(
            id=record.id,
            platform=record.platform,
            name=kwargs.get("name") or record.name,
            credential=dict(kwargs.get("credential") or record.credential),
            is_primary=bool(kwargs.get("is_primary", record.is_primary)),
            status=kwargs.get("status") or record.status,
        )
        return self.views[0]

    def delete_account(self, account_id):
        return True

    def touch_account(self, account_id, probe):
        return None


class _Settings:
    """
    固定参数快照。
    """

    def __init__(self, **kwargs) -> None:
        self.tuning = NetdiskTuning(**kwargs)

    def load(self):
        return self.tuning


@pytest.fixture()
def env(monkeypatch):
    """
    装配注册表、账号与取链服务。
    """
    from app.application.netdisk import resolve as resolve_module

    monkeypatch.setattr(resolve_module, "_PLATFORM_MIN_INTERVAL", 0)
    store = _Store()
    adapter = _Adapter()
    settings = _Settings()
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: adapter})
    registry.configure_netdisk_services(
        account_store=store,
        settings=settings,
        cookie_import=None,
        login_adapter=None,
    )
    accounts = NetdiskAccountService(store)
    service = NetdiskResolveService(accounts=accounts, settings=settings)
    yield service, adapter, store, settings
    registry.reset_netdisk_services()


def _account(accounts, name="主账号", primary=True):
    """
    创建一个可用账号。
    """
    return accounts.create(
        "quark",
        name,
        {"cookie": "__pus=1; __puus=2"},
        is_primary=primary,
    )


def test_direct_policy_passes_allow_transfer(env):
    """
    免转存策略下按开关决定是否允许回退转存。
    """
    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)
    links = service.resolve_file("https://pan.quark.cn/s/abc123", None, file)
    assert len(links) == 1
    assert adapter.last_allow_transfer is True
    assert links[0].cleanup_handle is not None


def test_auto_transfer_disabled(env):
    """
    关闭自动转存时不允许适配器转存。
    """
    service, adapter, store, settings = env
    settings.tuning = NetdiskTuning(auto_transfer=False)
    accounts = NetdiskAccountService(store)
    _account(accounts)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)
    service.resolve_file("https://pan.quark.cn/s/abc123", None, file)
    assert adapter.last_allow_transfer is False


def test_multi_source_uses_each_account(env):
    """
    多源模式下每个账号各取一条直链。
    """
    service, adapter, store, settings = env
    settings.tuning = NetdiskTuning(multi_source=True)
    accounts = NetdiskAccountService(store)
    first = _account(accounts, "主账号", True)
    second = _account(accounts, "备用", False)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)
    links = service.resolve_file("https://pan.quark.cn/s/abc123", None, file, multi_source=True)
    assert {link.account_id for link in links} == {first.id, second.id}


def test_link_cache_avoids_second_resolve(env):
    """
    同一文件第二次取链复用缓存。
    """
    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)
    service.resolve_file("https://pan.quark.cn/s/abc123", None, file)
    service.resolve_file("https://pan.quark.cn/s/abc123", None, file)
    assert len(adapter.resolve_calls) == 1


def test_force_refresh_retries_after_link_expired(env):
    """
    直链过期时刷新账号并重试一次。
    """
    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)
    adapter.fail_times = 1
    links = service.resolve_file("https://pan.quark.cn/s/abc123", None, file)
    assert len(links) == 1
    assert len(adapter.resolve_calls) == 2


def test_cleanup_calls_adapter(env):
    """
    下载完成后清理转存内容。
    """
    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)
    link = service.resolve_file("https://pan.quark.cn/s/abc123", None, file)[0]
    service.cleanup(link)
    assert len(adapter.cleanup_calls) == 1


def test_walk_recurses_directories(env):
    """
    递归展开分享目录。
    """
    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    adapter.share_detail = lambda ref, account: ShareDetail(
        title="t",
        files=(ShareFile(file_id="d1", name="dir", is_dir=True),),
    )
    adapter.list_files = lambda ref, parent_id, account: [
        ShareFile(file_id="f1", name="a.mp4", size=10)
    ]
    result = service.walk("https://pan.quark.cn/s/abc123")
    assert [item.file_id for item in result.files] == ["f1"]


def _depth_chain(depth):
    """
    构造最深视频位于第 depth 层的目录链。

    第 1 至 depth-1 层是目录，第 depth 层是视频文件；根目录只含第一层目录。

    :param depth: 最深文件的层号，根目录条目为第 1 层
    :return: 根目录条目与「目录 ID -> 子项」映射
    """
    dirs = [
        ShareFile(file_id=f"d{level}", name=f"d{level}", is_dir=True)
        for level in range(1, depth)
    ]
    children = {}
    for index, item in enumerate(dirs):
        if index + 1 < len(dirs):
            children[item.file_id] = [dirs[index + 1]]
        else:
            children[item.file_id] = [
                ShareFile(file_id=f"f{depth}", name=f"e{depth}.mkv", size=1)
            ]
    return tuple(dirs[:1]), children


def _walk_depth_names(service, adapter, depth, max_depth):
    """
    按给定最大深度展开目录链，返回展开到的文件名。

    :param service: 取链服务
    :param adapter: 适配器替身
    :param depth: 最深文件的层号
    :param max_depth: 传给 walk 的最大递归深度
    :return: 展开到的文件名列表
    """
    root, children = _depth_chain(depth)
    adapter.share_detail = lambda ref, account: ShareDetail(title="t", files=root)
    adapter.list_files = lambda ref, parent_id, account: children[parent_id]
    result = service.walk("https://pan.quark.cn/s/abc123", max_depth=max_depth)
    return [item.name for item in result.files]


def test_walk_reaches_twelfth_level(env):
    """
    分享目录递归到第 12 层文件，第 13 层被截断。
    """
    from app.modules.netdisk import _MAX_WALK_DEPTH

    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    assert "e12.mkv" in _walk_depth_names(service, adapter, 12, _MAX_WALK_DEPTH)
    assert "e13.mkv" not in _walk_depth_names(service, adapter, 13, _MAX_WALK_DEPTH)


def test_share_token_is_passed_to_adapter(env):
    """
    候选透传的分享令牌直接进入 ShareRef，避免重复换取。
    """
    service, adapter, store, settings = env
    accounts = NetdiskAccountService(store)
    _account(accounts)
    seen = {}

    def fake_detail(ref, account):
        seen["token"] = ref.share_token
        return ShareDetail(title="t", files=(ShareFile(file_id="f1", name="a.mp4"),))

    adapter.share_detail = fake_detail
    service.inspect("https://pan.quark.cn/s/abc123", share_token="st-cached")
    assert seen["token"] == "st-cached"


def test_inspect_without_account_raises(env):
    """
    必须登录的平台在无账号时返回 AUTH_EXPIRED。
    """
    service, adapter, store, settings = env
    with pytest.raises(NetdiskError) as error:
        service.inspect("https://pan.quark.cn/s/abc123")
    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_resolve_file_without_account_uses_guest(env):
    """
    支持游客的平台在无账号时直接返回游客直链，不抛 AUTH_EXPIRED。
    """
    service, adapter, store, settings = env
    adapter.guest_supported = True
    file = ShareFile(file_id="f1", name="a.mp4", size=10)

    links = service.resolve_file("https://pan.quark.cn/s/abc123", None, file)

    assert len(links) == 1
    assert links[0].account_id == 0
    assert adapter.resolve_calls == [(0, True)]


def test_resolve_file_with_account_skips_guest(env):
    """
    有可用账号时 resolve_file 只走账号链路，不回落游客。
    """
    service, adapter, store, settings = env
    adapter.guest_supported = True
    accounts = NetdiskAccountService(store)
    account = _account(accounts)
    file = ShareFile(file_id="f1", name="a.mp4", size=10)

    links = service.resolve_file("https://pan.quark.cn/s/abc123", None, file)

    assert len(links) == 1
    assert links[0].account_id == account.id
    assert adapter.resolve_calls == [(account.id, True)]


def test_account_space_error_is_not_treated_as_guest(env, monkeypatch):
    """
    账号选择报空间不足时必须原样抛出，不能回落成游客取链。
    """
    service, adapter, store, settings = env
    adapter.guest_supported = True
    file = ShareFile(file_id="f1", name="a.mp4", size=10)

    def fail_select(platform, exclude_ids=(), required_bytes=0):
        """
        模拟账号选择阶段判定空间不足。
        """
        raise NetdiskError(
            NetdiskErrorCode.SPACE_INSUFFICIENT,
            "空间不足",
            platform=NetdiskPlatform.QUARK,
        )

    monkeypatch.setattr(service._accounts, "select", fail_select)

    with pytest.raises(NetdiskError) as error:
        service.resolve_file("https://pan.quark.cn/s/abc123", None, file)

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert adapter.resolve_calls == []


def test_non_quark_guest_supported_platform_still_requires_account(env):
    """
    只有夸克放开游客取链；UC 等声明 guest_supported 的平台无账号仍抛 AUTH_EXPIRED。
    """
    service, adapter, store, settings = env
    uc_adapter = _UcAdapter()
    registry.configure_netdisk_adapters({
        NetdiskPlatform.QUARK: adapter,
        NetdiskPlatform.UC: uc_adapter,
    })
    file = ShareFile(file_id="f1", name="a.mp4", size=10)

    with pytest.raises(NetdiskError) as error:
        service.resolve_file("https://drive.uc.cn/s/xyz789", None, file)

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert uc_adapter.resolve_calls == []
