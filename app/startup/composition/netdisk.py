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

    from typing import cast

    from app.application.netdisk.contract import NetdiskAdapter
    from app.domain.netdisk import NetdiskPlatform

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
