"""
网盘取链应用层的端口注册表。

组合根在启动时注入具体实现；用例只通过本模块查找端口，不直接依赖 Adapter。
"""

from __future__ import annotations

from typing import Mapping, Optional

from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform
from app.application.netdisk.contract import (
    CookieImportPort,
    NetdiskAccountStore,
    NetdiskAdapter,
    NetdiskLoginAdapter,
    NetdiskSettingsPort,
    NetdiskTuning,
)

_adapters: dict[NetdiskPlatform, NetdiskAdapter] = {}
_account_store: Optional[NetdiskAccountStore] = None
_settings: Optional[NetdiskSettingsPort] = None
_cookie_import: Optional[CookieImportPort] = None
_login_adapter: Optional[NetdiskLoginAdapter] = None


def configure_netdisk_adapters(adapters: Mapping[NetdiskPlatform, NetdiskAdapter]) -> None:
    """
    注册平台适配器。

    :param adapters: 平台到适配器的映射
    """
    _adapters.clear()
    _adapters.update(adapters)


def configure_netdisk_services(
    *,
    account_store: NetdiskAccountStore,
    settings: NetdiskSettingsPort,
    cookie_import: CookieImportPort,
    login_adapter: NetdiskLoginAdapter,
) -> None:
    """
    注册账号仓储、参数、Cookie 导入与登录适配器。

    :param account_store: 账号持久化端口
    :param settings: 参数读取端口
    :param cookie_import: Cookie 导入端口
    :param login_adapter: 扫码登录端口
    """
    global _account_store, _settings, _cookie_import, _login_adapter
    _account_store = account_store
    _settings = settings
    _cookie_import = cookie_import
    _login_adapter = login_adapter


def reset_netdisk_services() -> None:
    """
    释放全部已注册端口，支持重复 lifespan 与失败回滚。
    """
    global _account_store, _settings, _cookie_import, _login_adapter
    _adapters.clear()
    _account_store = None
    _settings = None
    _cookie_import = None
    _login_adapter = None


def get_adapter(platform: NetdiskPlatform | str) -> NetdiskAdapter:
    """
    按平台取适配器。

    :param platform: 平台枚举或标识
    :return: 已注册的适配器
    :raises NetdiskError: 平台未实现时抛出 UNSUPPORTED
    """
    resolved = platform if isinstance(platform, NetdiskPlatform) else NetdiskPlatform(str(platform))
    adapter = _adapters.get(resolved)
    if adapter is None:
        raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该网盘平台暂不支持", platform=resolved)
    return adapter


def find_adapter(platform: object) -> Optional[NetdiskAdapter]:
    """
    按平台标识查找适配器，无法识别或未实现时返回 None。

    :param platform: 平台标识
    :return: 适配器或 None
    """
    from app.domain.netdisk import normalize_platform

    resolved = platform if isinstance(platform, NetdiskPlatform) else normalize_platform(platform)
    return _adapters.get(resolved) if resolved else None


def registered_platforms() -> tuple[NetdiskPlatform, ...]:
    """
    返回已注册的平台集合。

    :return: 平台枚举元组
    """
    return tuple(_adapters)


def get_account_store() -> NetdiskAccountStore:
    """
    返回账号仓储。

    :raises RuntimeError: 组合根尚未装配时抛出
    """
    if _account_store is None:
        raise RuntimeError("网盘账号仓储尚未装配")
    return _account_store


def get_settings() -> NetdiskSettingsPort:
    """
    返回参数读取端口。

    :raises RuntimeError: 组合根尚未装配时抛出
    """
    if _settings is None:
        raise RuntimeError("网盘参数端口尚未装配")
    return _settings


def get_cookie_import() -> CookieImportPort:
    """
    返回 Cookie 导入端口。

    :raises RuntimeError: 组合根尚未装配时抛出
    """
    if _cookie_import is None:
        raise RuntimeError("Cookie 导入端口尚未装配")
    return _cookie_import


def get_login_adapter() -> NetdiskLoginAdapter:
    """
    返回扫码登录端口。

    :raises RuntimeError: 组合根尚未装配时抛出
    """
    if _login_adapter is None:
        raise RuntimeError("网盘登录端口尚未装配")
    return _login_adapter


def current_tuning() -> NetdiskTuning:
    """
    读取当前参数快照；端口缺失时使用默认值。

    :return: 参数快照
    """
    if _settings is None:
        return NetdiskTuning()
    return _settings.load()
