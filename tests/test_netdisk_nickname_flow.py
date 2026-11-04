"""
网盘账号昵称写回流程测试。

覆盖新增账号与定时刷新两条路径都会从平台侧解析昵称，解析失败时保留已有昵称。
"""

from dataclasses import replace

from app.application.netdisk import registry
from app.application.netdisk.accounts import NetdiskAccountService
from app.application.netdisk.contract import NetdiskAccountRecord, NetdiskAccountView
from app.domain.netdisk import AccountProbe, DriveCapabilities, NetdiskPlatform


class _FakeStore:
    """
    内存账号仓储替身，记录创建与字段更新。
    """

    def __init__(self, records: list[NetdiskAccountRecord]) -> None:
        self.records = {record.id: record for record in records}
        self.created: list[object] = []
        self.updated_accounts: list[tuple[int, dict]] = []

    def list_accounts(self, platform=None):
        """
        返回全部账号投影。
        """
        return [
            NetdiskAccountView(
                id=record.id,
                platform=record.platform.value,
                name=record.name,
                is_primary=record.is_primary,
                status=record.status,
                masked={"access_token": "***"},
            )
            for record in self.records.values()
            if platform is None or record.platform == platform
        ]

    def get_record(self, account_id):
        """
        按主键返回完整记录。
        """
        return self.records.get(account_id)

    def create_account(self, write):
        """
        记录创建请求并返回投影。
        """
        self.created.append(write)
        return NetdiskAccountView(
            id=99,
            platform=write.platform.value,
            name=write.name,
            is_primary=write.is_primary,
            status="active",
            masked={"access_token": "***"},
        )

    def update_account(self, account_id, **kwargs):
        """
        记录字段更新。
        """
        self.updated_accounts.append((account_id, kwargs))
        record = self.records.get(account_id)
        if record is None:
            return None
        self.records[account_id] = replace(record, credential=kwargs.get("credential") or record.credential)
        return NetdiskAccountView(
            id=record.id,
            platform=record.platform.value,
            name=record.name,
            is_primary=record.is_primary,
            status=record.status,
            masked={"access_token": "***"},
        )

    def touch_account(self, account_id, probe):
        """
        探测结果写回；本测试只关心昵称。
        """
        del account_id, probe


class _NicknameAdapter:
    """
    迅雷适配器替身：昵称可配置，也可模拟解析异常。
    """

    platform = NetdiskPlatform.XUNLEI
    supports_refresh = False
    drive_capabilities = DriveCapabilities(quota=False)
    cookie_required_keys = ("access_token", "deviceid")

    def __init__(self, nickname: str = "雷友", error: Exception | None = None) -> None:
        self.nickname = nickname
        self.error = error

    def validate_credential(self, credential):
        """
        凭据字段完整即视为有效。
        """
        del credential
        return AccountProbe(status="active")

    def probe_account(self, account):
        """
        固定返回有效。
        """
        del account
        return AccountProbe(status="active")

    def login_account_name(self, credential):
        """
        返回预设昵称或抛出预设异常。
        """
        del credential
        if self.error is not None:
            raise self.error
        return self.nickname


def _record(account_id: int, name: str) -> NetdiskAccountRecord:
    """
    构造迅雷账号记录。
    """
    return NetdiskAccountRecord(
        id=account_id,
        platform=NetdiskPlatform.XUNLEI,
        name=name,
        credential={"access_token": "token", "deviceid": "device"},
        status="active",
    )


def test_create_resolves_nickname_from_adapter(monkeypatch):
    """
    新增账号时从适配器解析昵称并随账号写入。
    """
    store = _FakeStore([])
    monkeypatch.setattr(registry, "get_adapter", lambda _platform: _NicknameAdapter())

    NetdiskAccountService(store).create(
        NetdiskPlatform.XUNLEI,
        "迅雷账号",
        {"access_token": "token", "deviceid": "device"},
    )

    assert len(store.created) == 1
    assert store.created[0].nickname == "雷友"


def test_refresh_all_writes_nickname(monkeypatch):
    """
    定时刷新探测后补写平台昵称。
    """
    store = _FakeStore([_record(1, "迅雷账号")])
    monkeypatch.setattr(registry, "get_adapter", lambda _platform: _NicknameAdapter())

    failures = NetdiskAccountService(store).refresh_all()

    assert failures == []
    assert (1, {"nickname": "雷友"}) in store.updated_accounts


def test_nickname_failure_keeps_existing(monkeypatch):
    """
    昵称解析失败时不写空值，账号状态保持有效。
    """
    store = _FakeStore([_record(1, "迅雷账号")])
    monkeypatch.setattr(
        registry,
        "get_adapter",
        lambda _platform: _NicknameAdapter(error=RuntimeError("about 接口不可用")),
    )

    failures = NetdiskAccountService(store).refresh_all()

    assert failures == []
    assert store.updated_accounts == []
    assert store.records[1].status == "active"
