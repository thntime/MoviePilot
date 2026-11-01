"""
网盘账号仓储的加密落库与主账号维护测试。
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application.netdisk.credentials import FernetCredentialCipher
from app.application.netdisk.contract import NetdiskAccountWrite
from app.db.adapters.netdiskaccount import TransactionalNetdiskAccountRepository
from app.db.base import Base
from app.db.models.netdiskaccount import NetdiskAccount
from app.domain.netdisk import AccountProbe, NetdiskError, NetdiskPlatform


@pytest.fixture()
def repository():
    """
    构造基于内存 SQLite 的账号仓储。
    """
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[NetdiskAccount.__table__])
    factory = sessionmaker(bind=engine)
    cipher = FernetCredentialCipher("unit-test-secret")
    yield TransactionalNetdiskAccountRepository(sync_session=factory, cipher=cipher), engine
    engine.dispose()


def _create(repository, name="主账号", primary=True, cookie="__pus=1; __puus=2"):
    """
    创建测试账号。
    """
    return repository.create_account(
        NetdiskAccountWrite(
            platform=NetdiskPlatform.QUARK,
            name=name,
            credential={"cookie": cookie},
            is_primary=primary,
        )
    )


def test_credential_is_encrypted_at_rest(repository):
    """
    数据库列中不出现凭据明文。
    """
    repo, engine = repository
    _create(repo)
    with engine.connect() as connection:
        value = connection.execute(sa.text("SELECT credential_enc FROM netdiskaccount")).scalar_one()
    assert "__puus" not in value
    assert "Cookie" not in value


def test_list_and_get_round_trip(repository):
    """
    列表只返回脱敏信息，详情可解密回原文。
    """
    repo, _ = repository
    view = _create(repo)
    listed = repo.list_accounts(NetdiskPlatform.QUARK)
    assert listed[0].masked == {"cookie": "***"}
    record = repo.get_record(view.id)
    assert record is not None
    assert record.credential == {"cookie": "__pus=1; __puus=2"}


def test_duplicate_name_rejected(repository):
    """
    同平台账号名称唯一。
    """
    repo, _ = repository
    _create(repo, "主账号")
    with pytest.raises(NetdiskError):
        _create(repo, "主账号")


def test_primary_is_unique_per_platform(repository):
    """
    保存新主账号时旧主账号自动降级。
    """
    repo, _ = repository
    first = _create(repo, "旧主账号", True)
    second = _create(repo, "新主账号", True)
    records = {view.id: view for view in repo.list_accounts(NetdiskPlatform.QUARK)}
    assert records[first.id].is_primary is False
    assert records[second.id].is_primary is True


def test_delete_primary_promotes_remaining(repository):
    """
    删除主账号后提升同平台剩余账号。
    """
    repo, _ = repository
    first = _create(repo, "主账号", True)
    second = _create(repo, "备用", False)
    assert repo.delete_account(first.id) is True
    records = {view.id: view for view in repo.list_accounts(NetdiskPlatform.QUARK)}
    assert records[second.id].is_primary is True


def test_touch_updates_status(repository):
    """
    探测结果写回状态与时间。
    """
    repo, _ = repository
    view = _create(repo)
    repo.touch_account(view.id, AccountProbe(status="expired", message="已失效"))
    record = repo.get_record(view.id)
    assert record is not None and record.status == "expired"


def test_wrong_key_returns_empty_credential(repository):
    """
    密钥不匹配时不返回明文，只返回空凭据。
    """
    repo, engine = repository
    view = _create(repo)
    other = TransactionalNetdiskAccountRepository(
        sync_session=sessionmaker(bind=engine),
        cipher=FernetCredentialCipher("another-secret"),
    )
    record = other.get_record(view.id)
    assert record is not None
    assert record.credential == {}
