"""
验证网盘下载兜底拒绝已被禁用的网盘平台，且不进入重试或换源路径。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import app.chain.download.submission as download_submission
import app.modules.netdisk as netdisk_module
from app.chain.download import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.schemas.context import MetaInfo
from app.domain.netdisk import DriveFile, NetdiskPlatform
from app.modules.netdisk import NetdiskModule, store
from app.schemas.types import MediaType


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    把任务存储指向临时目录，避免污染真实配置。
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield


def _netdisk_context(platform: str) -> Context:
    """
    构造一个网盘候选的下载上下文。

    :param platform: 候选声明的网盘平台标识
    :return: 含媒体、识别元数据与网盘种子信息的上下文
    """
    media = MediaInfo(title="测试剧", year="2026", type=MediaType.TV)
    return Context(
        media_info=media,
        meta_info=MetaInfo(title="测试剧 S01E01"),
        torrent_info=TorrentInfo(
            title="测试剧 S01E01 1080p",
            enclosure="https://pan.quark.cn/s/abc123",
            download_type="netdisk",
            netdisk_platform=platform,
        ),
    )


def test_chain_rejects_disabled_platform_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    被禁用平台的下载请求在准备阶段直接拒绝，不调用下载器也不登记失败冷却。
    """
    monkeypatch.setattr(
        download_submission,
        "enabled_platforms",
        lambda: frozenset({NetdiskPlatform.UC}),
    )
    chain = DownloadChain()
    dispatched = Mock()
    recorded_failure = Mock()
    monkeypatch.setattr(chain, "download", dispatched)
    monkeypatch.setattr(chain, "_record_download_failure", recorded_failure)

    download_hash, reason = chain.download_single(
        context=_netdisk_context("quark"),
        episodes={1},
        return_detail=True,
    )

    assert download_hash is None
    assert "quark" in reason
    # 只尝试一次：禁用平台不进入下载器分发，也不写失败冷却（没有冷却就不会重试或换源重投）。
    dispatched.assert_not_called()
    recorded_failure.assert_not_called()


def test_enabled_platform_still_prepares_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    启用中的平台不受兜底拦截影响，仍能正常准备提交事实。
    """
    monkeypatch.setattr(
        download_submission,
        "enabled_platforms",
        lambda: frozenset(NetdiskPlatform),
    )
    chain = DownloadChain()
    context = _netdisk_context("quark")
    chain._resolve_media_download_dir = Mock(  # pylint: disable=protected-access
        return_value=("local", Path("/downloads"), None)
    )
    media_chain = Mock()
    media_chain.supplement_tmdb_info.return_value = context.media_info

    with patch.object(download_submission, "MediaChain", return_value=media_chain), \
            patch.object(download_submission.eventmanager, "send_event", return_value=None):
        prepared, error = chain._prepare_download_single(  # pylint: disable=protected-access
            context=context,
            torrent_content=None,
            torrent_file=None,
            episodes={1},
            channel=None,
            source=None,
            downloader=None,
            save_path=None,
            userid=None,
            username=None,
        )

    assert prepared is not None
    assert error is None


def test_module_rejects_disabled_platform_without_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    模块入口同样拒绝禁用平台，且不创建任务目录与任务记录。
    """
    monkeypatch.setattr(
        netdisk_module,
        "enabled_platforms",
        lambda: frozenset({NetdiskPlatform.UC}),
    )
    download_dir = tmp_path / "downloads"

    result = NetdiskModule().download(
        "https://pan.quark.cn/s/abc123",
        download_dir,
        "",
        download_type="netdisk",
        netdisk_platform="quark",
    )

    assert result is not None
    assert result[1] is None
    assert "quark" in result[3]
    assert store.find_by_share_code("abc123") is None
    assert not download_dir.exists()


def test_download_existing_rejects_disabled_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    个人盘已有文件入口同样拒绝禁用平台，且不创建任务目录与任务记录。
    """
    monkeypatch.setattr(
        netdisk_module,
        "enabled_platforms",
        lambda: frozenset({NetdiskPlatform.UC}),
    )
    module = NetdiskModule()
    config = SimpleNamespace(name="网盘", default=True, config={})
    monkeypatch.setattr(module, "get_configs", lambda: {"网盘": config})
    started = Mock()
    monkeypatch.setattr(module, "_run_existing_task", started)

    class FakeFilesService:
        """只提供账号平台信息的个人盘服务替身。"""

        def account(self, account_id: int) -> SimpleNamespace:
            """
            返回任务账号的平台。

            :param account_id: 账号主键
            :return: 含平台信息的账号记录
            """
            return SimpleNamespace(platform="quark")

    monkeypatch.setattr(netdisk_module, "NetdiskFilesService", FakeFilesService)
    download_dir = tmp_path / "downloads"

    result = module.download_existing(
        account_id=7,
        files=[DriveFile(file_id="f1", name="Show.S01E01.mkv", size=10)],
        download_dir=download_dir,
        episodes={1},
    )

    assert result is not None
    assert result[1] is None
    assert "quark" in result[3]
    started.assert_not_called()
    assert not download_dir.exists()


def test_start_torrents_refuses_disabled_platform_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    被禁用平台的历史任务重试被拒绝，任务保持暂停并记录禁用原因。
    """
    monkeypatch.setattr(
        netdisk_module,
        "enabled_platforms",
        lambda: frozenset({NetdiskPlatform.UC}),
    )
    module = NetdiskModule()
    started = []
    monkeypatch.setattr(module, "_run_task", lambda *args, **kwargs: started.append(args))
    store.upsert_task("t-quark", {
        "hash": "t-quark",
        "state": "paused",
        "netdisk_platform": "quark",
        "share_url": "https://pan.quark.cn/s/abc123",
        "share_code": "abc123",
        "save_path": str(tmp_path / "abc123"),
        "path": str(tmp_path / "abc123"),
        "passcode": "",
        "episodes": [1],
        "downloader": "网盘",
        "category": None,
    })

    assert module.start_torrents("t-quark") is None

    assert started == []
    record = store.get_task("t-quark")
    assert record is not None
    assert record["state"] == "paused"
    assert record["paused_reason"] == "platform_disabled"
    assert "quark" in record["error"]
