"""
整理链辅助函数与降级分支的行为契约。

这些分支都是「拿不到外部信息就放行或跳过」的兜底路径：它们不出现在正常流程里，
但一旦写错就会把整批整理卡死或误拦，因此按函数逐个盯住。
"""

from types import SimpleNamespace
from unittest.mock import Mock

from app.chain.transfer.workflow import (
    TransferWorkflowOwner,
    _candidate_fileitems,
    _episode_range_text,
)
from app.schemas.file import FileItem


def _fileitem(name: str) -> FileItem:
    """
    构造一个最小可用的本地文件项。
    """
    return FileItem(path=f"/downloads/{name}", storage="local", type="file", name=name)


def test_candidate_fileitems_accepts_plain_and_paired_candidates() -> None:
    """
    候选既可能是裸文件项，也可能是「文件项 + 是否蓝光目录」二元组，空值要丢掉。
    """
    first = _fileitem("a.mkv")
    second = _fileitem("b.mkv")

    assert _candidate_fileitems([first, (second, True)]) == [first, second]
    assert _candidate_fileitems([None, (None, False)]) == []
    assert _candidate_fileitems(None) == []


def test_episode_range_text_covers_single_and_inverted_ranges() -> None:
    """
    集数范围文案：起始集无效时为空串，结束集小于起始集时按单集处理。
    """
    assert _episode_range_text(SimpleNamespace(begin_episode=0, end_episode=3)) == ""
    assert _episode_range_text(SimpleNamespace(begin_episode=5, end_episode=5)) == "E05"
    assert _episode_range_text(SimpleNamespace(begin_episode=5, end_episode=2)) == "E05"
    assert _episode_range_text(SimpleNamespace(begin_episode=5, end_episode=8)) == "E05-E08"


def test_covered_by_downloader_polling_fails_closed_when_config_unreadable(monkeypatch) -> None:
    """
    读不到下载目录配置时按「在下载器监控范围内」处理，宁可少动一次也不要漏整理。
    """
    def _raise() -> list:
        """
        模拟读取下载目录配置失败。
        """
        raise RuntimeError("配置读取失败")

    monkeypatch.setattr("app.chain.transfer.workflow.DirectoryHelper", Mock(return_value=Mock(get_download_dirs=_raise)))

    assert TransferWorkflowOwner._covered_by_downloader_polling(SimpleNamespace(path="/downloads/a")) is True
    assert TransferWorkflowOwner._covered_by_downloader_polling(SimpleNamespace(path=None)) is False


def test_share_title_for_verify_falls_back_to_empty_string() -> None:
    """
    下载历史读取异常时返回空串，让调用方回退到文件项名称，不让校验硬依赖历史库。
    """
    owner = object.__new__(TransferWorkflowOwner)
    owner.download_history_repository = Mock()
    owner.download_history_repository.get_by_hash.side_effect = RuntimeError("历史库不可用")

    assert owner._share_title_for_verify("hash-1") == ""
    assert owner._share_title_for_verify(None) == ""

    owner.download_history_repository.get_by_hash.side_effect = None
    owner.download_history_repository.get_by_hash.return_value = SimpleNamespace(torrent_name="分享标题")
    assert owner._share_title_for_verify("hash-1") == "分享标题"


def test_verify_downloaded_content_releases_on_exception(monkeypatch) -> None:
    """
    内容校验自身抛异常时一律放行，这道门不能成为整理链的硬依赖。
    """
    def _boom(**_: object) -> None:
        """
        模拟校验实现内部异常。
        """
        raise RuntimeError("校验崩了")

    monkeypatch.setattr("app.chain.transfer.workflow.verify_downloaded_content", _boom)

    owner = object.__new__(TransferWorkflowOwner)
    owner.download_history_repository = Mock()

    result = owner._verify_downloaded_content(
        fileitem=_fileitem("a.mkv"),
        file_items=[_fileitem("a.mkv")],
        meta=SimpleNamespace(begin_episode=1, end_episode=1),
        mediainfo=SimpleNamespace(title="片名"),
        batch_mtype=None,
        manual=False,
        preview=False,
        download_hash=None,
    )

    assert result is None
