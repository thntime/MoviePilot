"""
验证搜索门按目标名判剧。

标题写《仙逆》、分享里却是《灵武大陆》的集号文件时，这条候选必须在搜索阶段
就被丢弃，而不是下载完再靠内容门兜。没给目标名时保持原有「只标记不丢弃」。
"""

from typing import Any, Dict, List

from app.application.netdisk import candidates
from app.domain.context import TorrentInfo

_MB = 1024 * 1024
_URL = "https://pan.quark.cn/s/probe"
_SHARE_TITLE = "仙逆 4K 国漫 年番 更新167集 S01E01-E194"
_TARGET = ("仙逆",)
_EPISODES = [170, 172, 173, 174, 176, 177, 179, 180, 184, 185, 186, 187, 188, 189, 190, 193, 194]


def _entry(name: str, size_mb: float = 500) -> Dict[str, Any]:
    """
    构造一条分享文件树条目。

    :param name: 文件名
    :param size_mb: 大小（MB）
    :return: 文件条目
    """
    return {"name": name, "size": int(size_mb * _MB), "duration": 0}


def _lingwu_files() -> List[Dict[str, Any]]:
    """
    造出生产里那 17 个《灵武大陆》文件。

    :return: 文件条目
    """
    return [
        _entry(f"灵武大陆.S01E{episode}.2160p.25fps.WEB-DL.H265.10bit.DDP2.0.mp4")
        for episode in _EPISODES
    ]


def _xianni_files() -> List[Dict[str, Any]]:
    """
    造出同集号的《仙逆》文件。

    :return: 文件条目
    """
    return [_entry(f"仙逆.S01E{episode}.2160p.WEB-DL.H265.mp4") for episode in _EPISODES]


def _passed(entries: List[Dict[str, Any]], target_names: Any = ()) -> bool:
    """
    跑一次质量门。

    :param entries: 文件条目
    :param target_names: 目标作品名
    :return: 通过返回 True
    """
    torrent = TorrentInfo(title=_SHARE_TITLE, enclosure=_URL)
    return candidates.passes_quality(
        torrent,
        {"alive": True, "_files": entries},
        config_getter=lambda: {},
        target_names=target_names,
    )


def test_title_claims_target_but_files_are_other_work() -> None:
    """标题自称《仙逆》、文件却是《灵武大陆》时丢弃这条候选。"""
    assert _passed(_lingwu_files(), _TARGET) is False


def test_same_title_with_target_files_passes() -> None:
    """同样的标题、文件确实是《仙逆》时照常通过。"""
    assert _passed(_xianni_files(), _TARGET) is True


def test_without_target_names_keeps_old_behaviour() -> None:
    """没给目标名时保持原有行为，不按目标名丢弃。"""
    assert _passed(_lingwu_files()) is True


def test_target_mismatch_helper_is_opt_in() -> None:
    """目标名判定是可选能力：空目标名一律放行。"""
    torrent = TorrentInfo(title=_SHARE_TITLE, enclosure=_URL)
    groups = candidates.classify_files(_lingwu_files())

    assert candidates._target_mismatch(_SHARE_TITLE, groups, ()) is False
    assert candidates._target_mismatch(_SHARE_TITLE, groups, _TARGET) is True
    assert torrent.title == _SHARE_TITLE
