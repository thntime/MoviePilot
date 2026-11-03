"""
个人网盘账号批量刷新与异常提醒的单元测试。
"""

from dataclasses import replace

from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import NetdiskAccountRecord, NetdiskAccountView
from app.domain.netdisk import AccountProbe, NetdiskPlatform
from app.scheduler import chain as chain_module
from app.scheduler.chain import SchedulerChain


class _FakeStore:
    """
    内存账号仓储替身，记录刷新写回与探测结果。
    """

    def __init__(self, records: list[NetdiskAccountRecord]) -> None:
        self.records = {record.id: record for record in records}
        self.touched: dict[int, AccountProbe] = {}

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

    def update_account(self, account_id, **kwargs):
        """
        写回凭据与状态，返回更新后的脱敏投影。
        """
        record = self.records.get(account_id)
        if record is None:
            return None
        updated = replace(
            record,
            credential=kwargs.get("credential") or record.credential,
            status=kwargs.get("status") or record.status,
        )
        self.records[account_id] = updated
        return NetdiskAccountView(
            id=updated.id,
            platform=updated.platform.value,
            name=updated.name,
            is_primary=updated.is_primary,
            status=updated.status,
            masked={"cookie": "***"},
        )

    def touch_account(self, account_id, probe):
        """
        记录探测结果。
        """
        self.touched[account_id] = probe


class _FakeAdapter:
    """
    账号刷新与探测适配器替身。
    """

    def __init__(
        self,
        *,
        supports_refresh: bool = False,
        refresh_result=None,
        refresh_error: Exception | None = None,
        probe_result: AccountProbe | None = None,
    ) -> None:
        self.supports_refresh = supports_refresh
        self.refresh_result = refresh_result
        self.refresh_error = refresh_error
        self.probe_result = probe_result or AccountProbe(status="active")
        self.refreshed: list[int] = []

    def refresh_account(self, account):
        """
        按预设结果返回新凭据或抛出异常。
        """
        if self.refresh_error is not None:
            raise self.refresh_error
        self.refreshed.append(account.id)
        return self.refresh_result

    def probe_account(self, account):
        """
        按预设结果返回探测状态。
        """
        return self.probe_result


def _record(account_id: int, platform: NetdiskPlatform, name: str) -> NetdiskAccountRecord:
    """
    构造测试账号记录。
    """
    return NetdiskAccountRecord(
        id=account_id,
        platform=platform,
        name=name,
        credential={"cookie": "__pus=secret-token; __puus=secret-token"},
    )


def test_refresh_all_updates_credential_and_probes(monkeypatch):
    """
    支持刷新的账号写回新凭据并记录有效探测结果。
    """
    store = _FakeStore([_record(1, NetdiskPlatform.QUARK, "主账号")])
    adapter = _FakeAdapter(
        supports_refresh=True,
        refresh_result={"cookie": "__pus=new-token; __puus=new-token"},
    )
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapter)

    failures = NetdiskAccountService(store).refresh_all()

    assert failures == []
    assert store.records[1].credential == {"cookie": "__pus=new-token; __puus=new-token"}
    assert store.records[1].status == "active"
    assert store.touched[1].status == "active"
    assert adapter.refreshed == [1]


def test_refresh_all_collects_expired_account(monkeypatch):
    """
    探测为失效的账号进入失败列表并保留平台与账号名。
    """
    store = _FakeStore([_record(1, NetdiskPlatform.UC, "备用账号")])
    adapter = _FakeAdapter(probe_result=AccountProbe(status="expired", message="登录已失效"))
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapter)

    failures = NetdiskAccountService(store).refresh_all()

    assert len(failures) == 1
    assert failures[0].account_id == 1
    assert failures[0].platform == "uc"
    assert failures[0].name == "备用账号"
    assert failures[0].message == "登录已失效"
    assert store.touched[1].status == "expired"


def test_refresh_all_isolates_account_exception(monkeypatch):
    """
    单个账号刷新抛错时仍处理其他账号，并只把异常账号计为失败。
    """
    store = _FakeStore(
        [
            _record(1, NetdiskPlatform.QUARK, "异常账号"),
            _record(2, NetdiskPlatform.UC, "正常账号"),
        ]
    )
    broken = _FakeAdapter(supports_refresh=True, refresh_error=RuntimeError("刷新接口不可用"))
    healthy = _FakeAdapter(supports_refresh=True, refresh_result={"cookie": "__pus=ok; __puus=ok"})
    adapters = {NetdiskPlatform.QUARK: broken, NetdiskPlatform.UC: healthy}
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapters[platform])

    failures = NetdiskAccountService(store).refresh_all()

    assert len(failures) == 1
    assert failures[0].account_id == 1
    assert "刷新接口不可用" in failures[0].message
    assert store.touched[1].status == "active"
    assert store.touched[2].status == "active"
    assert store.records[2].credential == {"cookie": "__pus=ok; __puus=ok"}


def test_refresh_netdisk_accounts_posts_message_without_credential(monkeypatch):
    """
    失败项通过系统消息提醒，正文只包含平台与账号名。
    """
    store = _FakeStore([_record(1, NetdiskPlatform.QUARK, "主账号")])
    adapter = _FakeAdapter(probe_result=AccountProbe(status="expired", message="登录已失效"))
    monkeypatch.setattr(registry, "get_adapter", lambda platform: adapter)
    monkeypatch.setattr(chain_module, "NetdiskAccountService", lambda: NetdiskAccountService(store))
    messages = []
    chain = SchedulerChain.__new__(SchedulerChain)
    chain.post_message = messages.append

    result = chain.refresh_netdisk_accounts()

    assert result == {"checked": 1, "failed": 1}
    assert len(messages) == 1
    assert messages[0].title == "网盘账号登录态异常"
    assert "quark" in messages[0].text
    assert "主账号" in messages[0].text
    assert "secret-token" not in messages[0].text
    assert "__pus" not in messages[0].text
