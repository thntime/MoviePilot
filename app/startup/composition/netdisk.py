"""
网盘取链、账号与登录的组合根装配。

具体 Adapter 只在这里实例化；应用层通过注册表消费 Port。
"""

from __future__ import annotations

from app.application.netdisk.registry import (
    configure_netdisk_adapters,
    configure_netdisk_services,
    reset_netdisk_services,
)
from app.application.netdisk.credentials import FernetCredentialCipher
from app.application.netdisk.settings import SystemNetdiskSettings
from app.runtime.log import logger


def configure_netdisk_composition() -> None:
    """
    装配网盘适配器、账号仓储、参数、Cookie 导入与扫码登录端口。
    """
    from app.adapters.external.netdisk.baidu import BaiduAdapter
    from app.adapters.external.netdisk.c139 import C139Adapter
    from app.adapters.external.netdisk.guangya import GuangYaAdapter
    from app.adapters.external.netdisk.login.browser import BrowserQrLoginAdapter
    from app.adapters.external.netdisk.login.cookiecloud import CookieCloudImportAdapter
    from app.adapters.external.netdisk.pan115 import Pan115Adapter
    from app.adapters.external.netdisk.pan123 import Pan123Adapter
    from app.adapters.external.netdisk.quark import QuarkAdapter
    from app.adapters.external.netdisk.uc import UCAdapter
    from app.adapters.external.netdisk.xunlei import XunleiAdapter
    from app.application.configuration import get_token_runtime_config
    from app.db.adapters.netdiskaccount import TransactionalNetdiskAccountRepository
    from app.db.session import SessionFactory

    from typing import Mapping, Optional, cast

    from app.application.netdisk.contract import NetdiskAdapter
    from app.domain.netdisk import NetdiskAccountRecord, NetdiskPlatform

    adapters = {
        adapter.platform: adapter
        for adapter in (
            QuarkAdapter(),
            UCAdapter(),
            C139Adapter(),
            Pan123Adapter(),
            Pan115Adapter(),
            GuangYaAdapter(),
            BaiduAdapter(),
            XunleiAdapter(),
        )
    }
    cipher = FernetCredentialCipher(get_token_runtime_config().secret_key)
    store = TransactionalNetdiskAccountRepository(sync_session=SessionFactory, cipher=cipher)

    def write_back_credential(record: NetdiskAccountRecord, credential: Mapping[str, str]) -> None:
        """
        把响应 Set-Cookie 合并后的凭据写回账号。

        只记录异常类型，不打印凭据内容，也不向请求线程抛出。

        :param record: 请求使用的账号记录
        :param credential: 合并后的完整凭据
        """
        try:
            store.update_account(record.id, credential=dict(credential))
        except Exception as error:  # noqa: BLE001 - 回写失败不能影响请求线程
            logger.warning(
                f"网盘账号凭据回写失败：platform={record.platform.value} "
                f"account={record.name} error={type(error).__name__}"
            )

    def refresh_credential(record: NetdiskAccountRecord) -> Optional[Mapping[str, str]]:
        """
        鉴权失败时用平台能力换取新凭据并写回账号。

        不支持续期的平台直接返回 None；续期失败保留旧凭据。

        :param record: 本次请求使用的账号记录
        :return: 新凭据；无法续期时返回 None
        """
        adapter = adapters.get(record.platform)
        if adapter is None or not getattr(adapter, "supports_refresh", False):
            return None
        credential = adapter.refresh_account(record)
        if not credential:
            return None
        write_back_credential(record, credential)
        return credential

    for adapter in adapters.values():
        http = getattr(adapter, "http", None)
        if http is not None:
            http.credential_sink = write_back_credential
            http.credential_refresher = refresh_credential

    configure_netdisk_adapters(cast(dict[NetdiskPlatform, NetdiskAdapter], adapters))
    configure_netdisk_services(
        account_store=store,
        settings=SystemNetdiskSettings(),
        cookie_import=CookieCloudImportAdapter(),
        login_adapter=BrowserQrLoginAdapter(),
    )
    logger.info(f"网盘取链端口已装配：platforms={','.join(adapters)}")


def reset_netdisk_composition() -> None:
    """
    释放网盘端口，支持重复 lifespan 与失败回滚。
    """
    reset_netdisk_services()
