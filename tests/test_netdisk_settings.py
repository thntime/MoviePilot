"""
验证网盘下载系统设置的默认值与边界夹紧。
"""

from types import SimpleNamespace

from app.application.netdisk.settings import SystemNetdiskSettings
from app.domain.netdisk import NetdiskTuning
from app.schemas.types import SystemConfigKey


def _patch_config(monkeypatch, values):
    """
    用内存字典替换系统设置读取，隔离真实配置。

    :param monkeypatch: pytest 补丁工具
    :param values: 配置项到取值的映射
    """
    monkeypatch.setattr(
        "app.application.netdisk.settings.get_configured_system_config",
        lambda: SimpleNamespace(get=values.get),
    )


def test_netdisk_tuning_default_threads_is_per_file_connections():
    """
    参数快照的线程数默认值为每文件 32 个连接。
    """
    assert NetdiskTuning().threads == 32


def test_netdisk_settings_default_threads(monkeypatch):
    """
    未配置线程数时，系统设置使用 32 作为默认值。
    """
    _patch_config(monkeypatch, {})
    assert SystemNetdiskSettings().load().threads == 32


def test_netdisk_settings_threads_clamped_to_max(monkeypatch):
    """
    线程数超过上限时夹紧到 512。
    """
    _patch_config(monkeypatch, {SystemConfigKey.NetdiskDownloadThreads: 9999})
    assert SystemNetdiskSettings().load().threads == 512


def test_netdisk_settings_default_max_tasks(monkeypatch):
    """
    未配置单任务并发文件数时，默认同时下载 3 个文件。
    """
    _patch_config(monkeypatch, {})
    assert SystemNetdiskSettings().load().max_tasks == 3


def test_netdisk_settings_max_tasks_reads_concurrent_files_key(monkeypatch):
    """
    单任务并发文件数来自 NetdiskMaxConcurrentFiles。
    """
    _patch_config(monkeypatch, {SystemConfigKey.NetdiskMaxConcurrentFiles: 64})
    assert SystemNetdiskSettings().load().max_tasks == 64


def test_netdisk_settings_max_tasks_ignores_max_concurrent_downloads(monkeypatch):
    """
    全局任务槽配置不再影响单任务并发文件数。
    """
    _patch_config(monkeypatch, {SystemConfigKey.NetdiskMaxConcurrentDownloads: 64})
    assert SystemNetdiskSettings().load().max_tasks == 3


def test_netdisk_settings_max_tasks_clamped_to_range(monkeypatch):
    """
    单任务并发文件数超出范围时夹紧到 1–64。
    """
    _patch_config(monkeypatch, {SystemConfigKey.NetdiskMaxConcurrentFiles: 9999})
    assert SystemNetdiskSettings().load().max_tasks == 64

    _patch_config(monkeypatch, {SystemConfigKey.NetdiskMaxConcurrentFiles: 0})
    assert SystemNetdiskSettings().load().max_tasks == 1
