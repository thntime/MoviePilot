"""
验证网盘容量策略：账号级冷却、按需选号、峰值空间预算、容量回退链与临时目录清理。
"""

from types import SimpleNamespace

import pytest

from app.application.netdisk import selection
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import NetdiskAccountView
from app.domain.netdisk import (
    DirectLink,
    DriveFile,
    LinkPolicy,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    NetdiskTuning,
    ShareFile,
    ShareRef,
)


@pytest.fixture(autouse=True)
def _clear_state():
    """
    每个用例开始前清空进程内速度、冷却与告警状态。
    """
    selection.clear_selection_state()
    yield
    selection.clear_selection_state()


def test_account_cooldown_blocks_only_that_account() -> None:
    """
    账号冷却只作用于该账号，不影响同平台其他账号。
    """
    selection.cool_account(7, seconds=60)

    assert selection.is_account_cooled(7) is True
    assert selection.is_account_cooled(8) is False
    assert selection.account_cooldown_remaining(7) > 0


def test_account_cooldown_expires() -> None:
    """
    冷却到期后账号重新可用。
    """
    selection.cool_account(7, seconds=0)

    assert selection.is_account_cooled(7) is False
    assert selection.account_cooldown_remaining(7) == 0


def test_clear_selection_state_clears_account_cooldown() -> None:
    """
    测试隔离入口同时清空账号冷却。
    """
    selection.cool_account(7, seconds=60)

    selection.clear_selection_state()

    assert selection.is_account_cooled(7) is False


def _view(account_id: int, *, used: int, total: int = 100, primary: bool = False) -> NetdiskAccountView:
    """
    构造账号脱敏投影，配额单位按字节语义使用同一比例即可。
    """
    return NetdiskAccountView(
        id=account_id,
        platform="quark",
        name=f"账号{account_id}",
        is_primary=primary,
        status="active",
        quota_used=used,
        quota_total=total,
    )


def _service(views, records):
    """
    用最小仓储构造账号服务。
    """

    class Store:
        """最小账号仓储。"""

        def list_accounts(self, platform=None):
            """返回预置账号视图。"""
            return views

        def get_record(self, account_id: int):
            """返回预置账号记录。"""
            return records.get(account_id)

    return NetdiskAccountService(Store())


def test_select_required_bytes_prefers_account_that_fits() -> None:
    """
    需求大于某账号剩余空间时，跳过该账号选择放得下的账号。
    """
    views = [_view(1, used=98), _view(2, used=50)]
    records = {
        1: SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active"),
        2: SimpleNamespace(id=2, platform=NetdiskPlatform.QUARK, status="active"),
    }

    selected = _service(views, records).select(NetdiskPlatform.QUARK, required_bytes=10)

    assert selected.id == 2


def test_select_required_bytes_skips_cooled_account() -> None:
    """
    冷却中的账号不参与选择，即使它容量更大。
    """
    views = [_view(1, used=50), _view(2, used=10)]
    records = {
        1: SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active"),
        2: SimpleNamespace(id=2, platform=NetdiskPlatform.QUARK, status="active"),
    }
    selection.cool_account(2, seconds=60)

    selected = _service(views, records).select(NetdiskPlatform.QUARK, required_bytes=10)

    assert selected.id == 1


def test_select_keeps_unknown_quota_selectable() -> None:
    """
    平台未返回配额时无法判断容量，账号仍可被选中。
    """
    views = [_view(1, used=0, total=0)]
    records = {1: SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active")}

    selected = _service(views, records).select(NetdiskPlatform.QUARK, required_bytes=10)

    assert selected.id == 1


def test_select_raises_space_insufficient_when_none_fits() -> None:
    """
    所有账号都放不下时抛出 SPACE_INSUFFICIENT，并给出需要与可用容量。
    """
    views = [_view(1, used=98), _view(2, used=97)]
    records = {
        1: SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active"),
        2: SimpleNamespace(id=2, platform=NetdiskPlatform.QUARK, status="active"),
    }

    with pytest.raises(NetdiskError) as error:
        _service(views, records).select(NetdiskPlatform.QUARK, required_bytes=10)

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert "需要" in error.value.message and "可用" in error.value.message


class _QuotaAdapter:
    """
    最小配额适配器：只提供平台标识、能力声明与配额。
    """

    platform = NetdiskPlatform.QUARK
    requires_account = False
    drive_capabilities = SimpleNamespace(quota=True, browse=True, delete=True)

    def __init__(self, total: int, used: int) -> None:
        """
        保存固定配额。
        """
        self._total = total
        self._used = used
        self.quota_calls = 0

    def quota(self, account):
        """
        返回固定配额并统计调用次数。
        """
        self.quota_calls += 1
        return SimpleNamespace(total=self._total, used=self._used)


def test_ensure_space_uses_peak_concurrency() -> None:
    """
    峰值需求按单文件大小乘以并发数计算；并发过高时拒绝转存。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    adapter = _QuotaAdapter(total=100, used=95)
    service = NetdiskResolveService(settings=NetdiskTuning(auto_transfer=True))
    file = ShareFile(file_id="f", name="a.mkv", size=3)
    account = SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active")

    with pytest.raises(NetdiskError) as error:
        service._ensure_space(adapter, file, account, concurrency=3)

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert "需要" in error.value.message and "可用" in error.value.message
    assert selection.is_account_cooled(1) is True
    assert selection.is_platform_cooled(NetdiskPlatform.QUARK) is False

    service._ensure_space(adapter, file, account, concurrency=1)


class _FakeAdapter:
    """
    可控的网盘适配器替身：按账号返回配额，并记录取链与清理调用。
    """

    platform = NetdiskPlatform.QUARK
    requires_account = False
    drive_capabilities = SimpleNamespace(quota=True, browse=True, delete=True)

    def __init__(self, frees, *, total: int = 100, policy=None, dirs=None):
        """
        保存每个账号的剩余空间与平台策略。
        """
        self.frees = dict(frees)
        self.total = total
        self.link_policy = policy or LinkPolicy.DIRECT_WITH_FALLBACK
        self.dirs = dirs or {}
        self.resolve_calls = []
        self.deleted = []
        self.list_calls = 0
        self.quota_calls = 0

    def quota(self, account):
        """
        返回账号配额。
        """
        self.quota_calls += 1
        free = self.frees.get(account.id, 0)
        return SimpleNamespace(total=self.total, used=max(self.total - free, 0))

    def list_dir(self, account, parent_id=""):
        """
        返回预置的根目录条目。
        """
        self.list_calls += 1
        return list(self.dirs.get(account.id, []))

    def delete_files(self, account, files):
        """
        记录删除并模拟空间释放。
        """
        self.deleted.extend(files)
        self.frees[account.id] = self.total

    def resolve(self, ref, file, account, *, allow_transfer=True, transfer_first=False):
        """
        记录取链参数并返回直链；模拟真实适配器「要求转存但又不允许转存」的失败。
        """
        if transfer_first and not allow_transfer:
            raise NetdiskError(NetdiskErrorCode.SPACE_INSUFFICIENT, "转存失败")
        self.resolve_calls.append({
            "account": getattr(account, "id", 0),
            "allow_transfer": allow_transfer,
            "transfer_first": transfer_first,
        })
        return DirectLink(
            url="https://example.invalid/file",
            headers={},
            size=int(file.size or 0),
            account_id=getattr(account, "id", 0),
        )


def _accounts(frees, *, total: int = 100):
    """
    按剩余空间构造账号服务。
    """
    views = [
        _view(account_id, used=total - free, total=total)
        for account_id, free in frees.items()
    ]
    records = {
        account_id: SimpleNamespace(
            id=account_id, platform=NetdiskPlatform.QUARK, status="active",
        )
        for account_id in frees
    }
    return _service(views, records)


def _ref():
    """
    构造分享引用。
    """
    return ShareRef(platform=NetdiskPlatform.QUARK, share_id="share")


def _file(size: int):
    """
    构造待转存文件。
    """
    return ShareFile(file_id="f", name="a.mkv", size=size)


def test_transfer_first_switches_to_account_with_space() -> None:
    """
    A1：转存优先时，账号 A 放不下就换账号 B。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    adapter = _FakeAdapter({1: 2, 2: 50})
    accounts = _accounts({1: 2, 2: 50})
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=True),
    )
    account = accounts.store.get_record(1)

    link = service._resolve_one(
        adapter, _ref(), _file(10), account, force_refresh=False, concurrency=1,
    )

    assert link.account_id == 2
    assert link.strategy == "transfer"
    assert adapter.resolve_calls[-1]["account"] == 2
    assert adapter.resolve_calls[-1]["transfer_first"] is True


def test_capacity_fallback_to_direct() -> None:
    """
    A2：所有账号都放不下且平台支持免转存时，静默回退免转存。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    adapter = _FakeAdapter({1: 2, 2: 2})
    accounts = _accounts({1: 2, 2: 2})
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=True),
    )
    account = accounts.store.get_record(1)

    link = service._resolve_one(
        adapter, _ref(), _file(10), account, force_refresh=False, concurrency=1,
    )

    assert link.strategy == "direct_fallback"
    assert adapter.resolve_calls[-1]["allow_transfer"] is False
    assert selection.is_platform_cooled(NetdiskPlatform.QUARK) is False


def test_capacity_fallback_required_platform_fails() -> None:
    """
    A3：必须转存且所有账号都放不下时失败，只冷却账号。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    adapter = _FakeAdapter({1: 2}, policy=LinkPolicy.TRANSFER_REQUIRED)
    accounts = _accounts({1: 2})
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=True),
    )
    account = accounts.store.get_record(1)

    with pytest.raises(NetdiskError) as error:
        service._resolve_one(
            adapter, _ref(), _file(10), account, force_refresh=False, concurrency=1,
        )

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert "需要" in error.value.message and "可用" in error.value.message
    assert selection.is_account_cooled(1) is True
    assert selection.is_platform_cooled(NetdiskPlatform.QUARK) is False


def test_capacity_fallback_single_file_too_large() -> None:
    """
    A9：单文件超过所有账号总容量时快速失败，不清理、不取链。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    adapter = _FakeAdapter({1: 2}, total=10, policy=LinkPolicy.TRANSFER_REQUIRED)
    accounts = _accounts({1: 2}, total=10)
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=True),
    )
    account = accounts.store.get_record(1)

    with pytest.raises(NetdiskError) as error:
        service._resolve_one(
            adapter, _ref(), _file(29), account, force_refresh=False, concurrency=1,
        )

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert adapter.list_calls == 0
    assert adapter.resolve_calls == []


def test_cleanup_temp_dirs_then_retry_succeeds() -> None:
    """
    A6：空间不足时先删除本应用临时目录，重读配额后继续转存。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    temp = DriveFile(file_id="dir-1", name="mp_tr_123", is_dir=True)
    adapter = _FakeAdapter(
        {1: 2}, policy=LinkPolicy.TRANSFER_REQUIRED, dirs={1: [temp]},
    )
    accounts = _accounts({1: 2})
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=True),
    )
    account = accounts.store.get_record(1)

    link = service._resolve_one(
        adapter, _ref(), _file(10), account, force_refresh=False, concurrency=1,
    )

    assert [item.name for item in adapter.deleted] == ["mp_tr_123"]
    assert link.strategy == "transfer"
    assert link.account_id == 1


def test_direct_no_quota_when_direct_first() -> None:
    """
    A7：免转存优先且成功时不读配额、不产生转存。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    adapter = _FakeAdapter({1: 50})
    accounts = _accounts({1: 50})
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=False),
    )
    account = accounts.store.get_record(1)

    link = service._resolve_one(
        adapter, _ref(), _file(10), account, force_refresh=False, concurrency=1,
    )

    assert link.strategy == "direct"
    assert adapter.quota_calls == 0
    assert adapter.resolve_calls[-1]["allow_transfer"] is True


def test_concurrency_limited_by_free_space() -> None:
    """
    A8：剩余空间只够一个文件时，把并发下调到 1。
    """
    from app.modules.netdisk import _limit_workers

    assert _limit_workers(3, [3 * 1024 ** 3] * 3, 5 * 1024 ** 3) == 1
    assert _limit_workers(3, [3 * 1024 ** 3] * 3, 100 * 1024 ** 3) == 3
    assert _limit_workers(3, [0, 0, 0], 5 * 1024 ** 3) == 3


def test_cleanup_skips_active_temp_dirs() -> None:
    """
    A6 边界：正在被其他任务使用的临时目录不能被清理。
    """
    from app.application.netdisk.resolve import NetdiskResolveService

    active = DriveFile(file_id="dir-active", name="mp_tr_active", is_dir=True)
    idle = DriveFile(file_id="dir-idle", name="mp_tr_idle", is_dir=True)
    adapter = _FakeAdapter(
        {1: 2}, policy=LinkPolicy.TRANSFER_REQUIRED, dirs={1: [active, idle]},
    )
    accounts = _accounts({1: 2})
    service = NetdiskResolveService(
        accounts=accounts,
        settings=NetdiskTuning(auto_transfer=True, transfer_first=True),
    )
    account = accounts.store.get_record(1)

    service._resolve_one(
        adapter, _ref(), _file(10), account,
        force_refresh=False, concurrency=1, exclude_directories={"dir-active"},
    )

    assert [item.name for item in adapter.deleted] == ["mp_tr_idle"]
