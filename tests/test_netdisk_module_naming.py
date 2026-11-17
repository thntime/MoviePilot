"""
网盘下载器的落盘命名、集数过滤与文件类型判断测试。
"""

from app.domain.netdisk import ShareFile
from app.modules.netdisk import NetdiskModule


def _file(name: str) -> ShareFile:
    """
    构造一个分享文件条目。
    """
    return ShareFile(file_id=name, name=name, size=1)


def test_episode_name_prefixes_bare_episode():
    """
    有目标季时裸集数文件名补 SxxExx 前缀，整理阶段才能识别；无季信息保持原样。
    """
    assert NetdiskModule._episode_name("164 4K.mkv", season=1) == "S01E164 164 4K.mkv"
    assert NetdiskModule._episode_name("01~4K.mp4", season=1) == "S01E001 01~4K.mp4"
    assert NetdiskModule._episode_name("164 4K.mkv") == "164 4K.mkv"


def test_episode_name_keeps_existing_marker_and_ambiguous_name():
    """
    已带季集标记或集数不唯一的文件名保持原样。
    """
    assert NetdiskModule._episode_name("仙逆 S01E164 4K.mkv") == "仙逆 S01E164 4K.mkv"
    assert NetdiskModule._episode_name("1917 2019 1080p.mkv") == "1917 2019 1080p.mkv"
    assert NetdiskModule._episode_name("花絮.mp4") == "花絮.mp4"


def test_filter_episodes_keeps_only_target_names():
    """
    有精确命中时只保留目标集数文件，无法解析集数的文件不再兜底。
    """
    files = [_file("剧名.S01E27.mkv"), _file("剧名.S01E28.mkv"), _file("剧名.S01E01.mkv"), _file("剧名.mkv")]
    kept = NetdiskModule._filter_episodes(files, {27, 28})
    names = {item.name for item in kept}
    assert names == {"剧名.S01E27.mkv", "剧名.S01E28.mkv"}


def test_filter_episodes_keeps_ambiguous_when_no_exact_hit():
    """
    目标季没有任何精确命中时，无法解析集数的文件作为兜底保留，避免漏集。
    """
    files = [_file("剧名.S01E01.mkv"), _file("剧名.mkv")]
    kept = NetdiskModule._filter_episodes(files, {27})
    names = {item.name for item in kept}
    assert names == {"剧名.mkv"}


def test_filter_episodes_keeps_all_without_target():
    """
    没有目标集数时保留全部文件。
    """
    files = [_file("a.mkv"), _file("b.mp4")]
    assert NetdiskModule._filter_episodes(files, None) == files


def test_filter_episodes_logs_target_and_selected_count(monkeypatch):
    """
    过滤时输出目标集数、分享文件数与选中数，便于订阅链路排查。
    """
    from app.modules import netdisk as netdisk_module

    messages = []
    monkeypatch.setattr(netdisk_module.logger, "info", lambda message: messages.append(message))
    files = [_file("剧名.S01E27.mkv"), _file("剧名.S01E01.mkv")]
    NetdiskModule._filter_episodes(files, {27})
    assert any("目标集数" in message and "选中 1 个" in message for message in messages)


def test_wanted_accepts_video_only():
    """
    只下载视频文件，压缩包与图片都不下载。
    """
    assert NetdiskModule._wanted("a.mkv") is True
    assert NetdiskModule._wanted("a.zip") is False
    assert NetdiskModule._wanted("a.jpg") is False


def test_instance_tuning_clamps_threads_to_platform_cap():
    """
    实例线程数不能突破平台硬上限（迅雷 8），非法限速回落为 0。
    """
    from types import SimpleNamespace

    from app.domain.netdisk import NetdiskPlatform

    config = SimpleNamespace(config={"threads": 9999, "rate_limit": 1024, "preempt": False, "multi_source": True})
    tuning = NetdiskModule._instance_tuning(config, NetdiskPlatform.XUNLEI)
    assert tuning.threads == 8
    assert tuning.rate_limit == 1024 * 1024
    assert tuning.preempt_enabled is False
    assert tuning.multi_source is True


def test_instance_tuning_keeps_defaults_without_overrides():
    """
    实例没有覆盖项时沿用系统默认值。
    """
    from types import SimpleNamespace

    from app.application.netdisk.resolve import NetdiskResolveService
    from app.domain.netdisk import NetdiskPlatform

    base = NetdiskResolveService().tuning
    tuning = NetdiskModule._instance_tuning(SimpleNamespace(config={}), NetdiskPlatform.QUARK)
    assert tuning.threads == base.threads
    assert tuning.rate_limit == base.rate_limit
