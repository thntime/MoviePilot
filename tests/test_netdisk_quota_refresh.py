"""
网盘账号空间定时刷新的单元测试。

覆盖全账号遍历、单账号失败隔离，以及失败时不改状态与凭据。
"""

from dataclasses import replace

from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import NetdiskAccountRecord, NetdiskAccountView
from app.domain.netdisk import DriveCapabilities, DriveQuota, NetdiskPlatform


class _FakeStore:
    """
    内存账号仓储替身，只记录空间写回与账号字段变化。
    """

    def __init__(self, records: list[NetdiskAccountRecord]) -> None:
        self.records = {record.id: record for record in records}
        self.quotas: dict[int, tuple[int, int]] = {}
        self.updated_accounts: list[tuple[int, dict]] = []
        self.touched: list[tuple[int, object]] = []

    def list_accounts(self, platform=None):
        """
        返回全部账号投影，可按平台过滤。
        """
        return [
            NetdiskAccountView(
                id=record.id,
                platform=record.platform.value,
                name=record.name,
                is_primary=record.is_primary,
                status=record.status,
                masked={"cookie": "***"},
            )
            for record in self.records.values()
            if platform is None or record.platform == platform
        ]

    def get_record(self, account_id):
        """
        按主键返回完整账号记录。
        """
        return self.records.get(account_id)

    def update_quota(self, account_id, used, total):
        """
        记录空间写回。
        """
        self.quotas[account_id] = (used, total)

    def update_account(self, account_id, **kwargs):
        """
        记录非空间字段写回，供断言空间刷新不触碰状态与凭据。
        """
        self.updated_accounts.append((account_id, kwargs))
        record = self.records.get(account_id)
        if record is None:
            return None
        self.records[account_id] = replace(
            record,
            credential=kwargs.get("credential") or record.credential,
            status=kwargs.get("status") or record.status,
        )
        return NetdiskAccountView(
            id=record.id,
            platform=record.platform.value,
            name=record.name,
            is_primary=record.is_primary,
            status=self.records[account_id].status,
            masked={"cookie": "***"},
        )

    def touch_account(self, account_id, probe):
        """
        记录探测写回。
        """
        self.touched.append((account_id, probe))


class _QuotaAdapter:
    """
    空间查询适配器替身。
    """

    supports_refresh = False
    drive_capabilities = DriveCapabilities(quota=True)

    def __init__(self, quota: DriveQuota | None = None, error: Exception | None = None) -> None:
        self._quota = quota or DriveQuota()
        self.error = error

    def quota(self, account):
        """
        返回预设空间或抛出预设异常。
        """
        del account
        if self.error is not None:
            raise self.error
        return self._quota


def _record(account_id: int, platform: NetdiskPlatform, name: str) -> NetdiskAccountRecord:
    """
    构造测试账号记录。
    """
    return NetdiskAccountRecord(
        id=account_id,
        platform=platform,
        name=name,
        credential={"cookie": "__pus=secret-token; __puus=secret-token"},
        status="active",
    )


def test_refresh_quotas_updates_every_account(monkeypatch):
    """
    遍历全部账号并写回各自的空间占用。
    """
    store = _FakeStore(
        [
            _record(1, NetdiskPlatform.QUARK, "主账号"),
            _record(2, NetdiskPlatform.UC, "备用账号"),
        ]
    )
    adapters = {
        NetdiskPlatform.QUARK: _QuotaAdapter(DriveQuota(used=100, total=1000)),
        NetdiskPlatform.UC: _QuotaAdapter(DriveQuota(used=200, total=2000)),
    }
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapters[platform])

    result = NetdiskAccountService(store).refresh_quotas()

    assert result == {"checked": 2, "updated": 2, "failed": 0}
    assert store.quotas == {1: (100, 1000), 2: (200, 2000)}
    assert store.updated_accounts == []
    assert store.touched == []


def test_refresh_quotas_isolates_single_failure(monkeypatch):
    """
    单个账号失败时继续处理其他账号，且失败账号状态与凭据保持不变。
    """
    store = _FakeStore(
        [
            _record(1, NetdiskPlatform.QUARK, "异常账号"),
            _record(2, NetdiskPlatform.UC, "正常账号"),
        ]
    )
    adapters = {
        NetdiskPlatform.QUARK: _QuotaAdapter(error=RuntimeError("空间接口不可用")),
        NetdiskPlatform.UC: _QuotaAdapter(DriveQuota(used=300, total=3000)),
    }
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapters[platform])
    before = store.records[1]

    result = NetdiskAccountService(store).refresh_quotas()

    assert result == {"checked": 2, "updated": 1, "failed": 1}
    assert store.quotas == {2: (300, 3000)}
    assert store.records[1] == before
    assert store.records[1].status == "active"
    assert store.records[1].credential == {"cookie": "__pus=secret-token; __puus=secret-token"}
    assert store.updated_accounts == []
    assert store.touched == []
