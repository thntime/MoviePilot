"""网盘平台启用集合读取行为测试。"""

from types import SimpleNamespace
from typing import Any, Optional

from app.application.netdisk import settings as netdisk_settings
from app.domain.netdisk import NetdiskPlatform
from app.schemas.types import SystemConfigKey


def _patch_platform_config(monkeypatch: Any, value: Optional[Any]) -> None:
    """
    把系统配置读取替换为固定返回值。

    :param monkeypatch: pytest 补丁工具
    :param value: `NetdiskEnabledPlatforms` 的配置值
    """

    def _get(key: SystemConfigKey) -> Optional[Any]:
        """
        按 key 返回配置值。

        :param key: 配置键
        :return: 仅对平台启用键返回值
        """
        return value if key == SystemConfigKey.NetdiskEnabledPlatforms else None

    monkeypatch.setattr(
        netdisk_settings,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=_get),
    )


def test_missing_key_enables_all_platforms(monkeypatch: Any) -> None:
    """
    配置键缺失时全部平台启用。
    """
    _patch_platform_config(monkeypatch, None)

    assert netdisk_settings.enabled_platforms() == frozenset(NetdiskPlatform)


def test_empty_list_disables_all_platforms(monkeypatch: Any) -> None:
    """
    空列表表示全部平台禁用。
    """
    _patch_platform_config(monkeypatch, [])

    assert netdisk_settings.enabled_platforms() == frozenset()


def test_subset_keeps_configured_platforms(monkeypatch: Any) -> None:
    """
    只保留配置中列出的平台。
    """
    _patch_platform_config(monkeypatch, ["quark", "pan115"])

    assert netdisk_settings.enabled_platforms() == frozenset(
        {NetdiskPlatform.QUARK, NetdiskPlatform.PAN115}
    )


def test_platform_aliases_are_normalized(monkeypatch: Any) -> None:
    """
    别名归一为平台枚举。
    """
    _patch_platform_config(monkeypatch, ["115", "189"])

    assert netdisk_settings.enabled_platforms() == frozenset(
        {NetdiskPlatform.PAN115, NetdiskPlatform.TIANYI}
    )


def test_unknown_platforms_are_ignored(monkeypatch: Any) -> None:
    """
    无法识别的平台被忽略。
    """
    _patch_platform_config(monkeypatch, ["quark", "foo"])

    assert netdisk_settings.enabled_platforms() == frozenset({NetdiskPlatform.QUARK})


def test_duplicate_platforms_are_collapsed(monkeypatch: Any) -> None:
    """
    重复平台只保留一份。
    """
    _patch_platform_config(monkeypatch, ["quark", "QUARK", "quark"])

    assert netdisk_settings.enabled_platforms() == frozenset({NetdiskPlatform.QUARK})
