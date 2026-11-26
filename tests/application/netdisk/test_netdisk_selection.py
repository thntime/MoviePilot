"""
验证网盘账号排序、容量预检与平台冷却。
"""

from types import SimpleNamespace

import pytest

from app.application.netdisk import selection
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import NetdiskAccountView
from app.application.netdisk.resolve import NetdiskResolveService
from app.domain.netdisk import (
    DriveQuota,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareFile,
)


@pytest.fixture(autouse=True)
def _clear_state():
    """
    每个用例开始前清空进程内速度、冷却与告警状态。
    """
    selection.clear_selection_state()
    yield
    selection.clear_selection_state()


def _view(account_id: int, *, primary: bool, used: int, total: int = 100) -> NetdiskAccountView:
    """
    构造账号脱敏投影。
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


def test_select_prefers_account_with_more_free_capacity() -> None:
    """
    主账号容量接近写满时，优先选择剩余容量更大的账号。
    """
    views = [_view(1, primary=True, used=95), _view(2, primary=False, used=10)]
    records = {
        1: SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active"),
        2: SimpleNamespace(id=2, platform=NetdiskPlatform.QUARK, status="active"),
    }

    class Store:
        """最小账号仓储。"""

        def list_accounts(self, platform=None):
            """返回预置账号视图。"""
            return views

        def get_record(self, account_id: int):
            """返回预置账号记录。"""
            return records.get(account_id)

    selected = NetdiskAccountService(Store()).select(NetdiskPlatform.QUARK)
    assert selected.id == 2


def test_select_uses_speed_when_capacity_equal() -> None:
    """
    容量相同时，近期速度更高的账号优先。
    """
    views = [_view(1, primary=True, used=50), _view(2, primary=False, used=50)]
    records = {
        1: SimpleNamespace(id=1, platform=NetdiskPlatform.QUARK, status="active"),
        2: SimpleNamespace(id=2, platform=NetdiskPlatform.QUARK, status="active"),
    }

    class Store:
        """最小账号仓储。"""

        def list_accounts(self, platform=None):
            """返回预置账号视图。"""
            return views

        def get_record(self, account_id: int):
            """返回预置账号记录。"""
            return records.get(account_id)

    selection.record_account_speed(1, 1024, 10)
    selection.record_account_speed(2, 4096, 10)
    selected = NetdiskAccountService(Store()).select(NetdiskPlatform.QUARK)
    assert selected.id == 2


def test_ensure_space_raises_when_quota_insufficient() -> None:
    """
    转存前发现剩余容量不足时抛出 SPACE_INSUFFICIENT，并只冷却该账号。
    """
    adapter = SimpleNamespace(
        platform=NetdiskPlatform.QUARK,
        drive_capabilities=SimpleNamespace(quota=True),
        quota=lambda account: DriveQuota(used=95, total=100),
    )
    service = NetdiskResolveService()
    with pytest.raises(NetdiskError) as error:
        service._ensure_space(adapter, ShareFile(file_id="f", name="a.mkv", size=10), SimpleNamespace(id=1))
    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT
    assert selection.is_account_cooled(1)
    assert not selection.is_platform_cooled(NetdiskPlatform.QUARK)


def test_alert_is_deduplicated_within_window() -> None:
    """
    同一平台在告警窗口内只告警一次。
    """
    assert selection.should_alert("quark") is True
    assert selection.should_alert("quark") is False


def test_speed_survives_restart(tmp_path) -> None:
    """速度写入持久化端口后，重新装配仍可读回。"""
    from app.adapters.system.netdisk_speed import JsonNetdiskSpeedStore

    path = tmp_path / "speeds.json"
    selection.configure_speed_store(JsonNetdiskSpeedStore(path))
    selection.record_account_speed(7, 10 * 1024 * 1024, 1)
    selection.clear_selection_state()
    selection.configure_speed_store(JsonNetdiskSpeedStore(path))
    assert selection.account_speed(7) == pytest.approx(10 * 1024 * 1024)


def test_record_speed_uses_ewma_alpha_on_second_sample() -> None:
    """第二次采样按 0.7 旧值 + 0.3 新值混合，而不是直接覆盖。"""
    selection.record_account_speed(1, 10 * 1024 * 1024, 1)
    selection.record_account_speed(1, 20 * 1024 * 1024, 1)
    assert selection.account_speed(1) == pytest.approx(
        10 * 1024 * 1024 * 0.7 + 20 * 1024 * 1024 * 0.3
    )


def test_save_failure_does_not_raise() -> None:
    """持久化失败只降级为告警，内存速度仍然生效。"""

    class BrokenStore:
        """写入必定失败的存储替身。"""

        def load(self):
            """返回空映射。"""
            return {}

        def save(self, speeds):
            """模拟磁盘写入失败。"""
            raise OSError("disk full")

    selection.configure_speed_store(BrokenStore())
    selection.record_account_speed(3, 1024, 1)
    assert selection.account_speed(3) == pytest.approx(1024)


def test_load_failure_returns_zero() -> None:
    """加载失败时按无样本处理，不向上抛出。"""

    class BrokenStore:
        """读取必定失败的存储替身。"""

        def load(self):
            """模拟损坏文件。"""
            raise ValueError("bad file")

        def save(self, speeds):
            """忽略写入。"""

    selection.configure_speed_store(BrokenStore())
    assert selection.account_speed(5) == 0.0
