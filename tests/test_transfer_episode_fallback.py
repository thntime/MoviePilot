"""
整理侧集数兜底测试：文件名解析不出集数时用下载记录回填。

兜底必须严格受限：同任务、TV、唯一候选且落在本次下载的集数集合内，否则一律不动。
"""

from types import SimpleNamespace
from typing import Any, Optional

from app.chain.transfer.request import _TransferCandidatePlanner
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType

_HASH = "4686113e382be608"


def _planner(download_hash: Optional[str] = _HASH, explicit_meta: Any = None) -> Any:
    """构造只带兜底所需属性的规划器实例。"""
    planner = _TransferCandidatePlanner.__new__(_TransferCandidatePlanner)
    planner._download_hash = download_hash
    planner._meta = explicit_meta
    return planner


def _file(name: str) -> Any:
    """构造整理文件条目。"""
    return SimpleNamespace(path=f"/media/downloads/电视剧/{name}")


def _history(
        *,
        download_hash: str = _HASH,
        media_type: str = MediaType.TV.value,
        episodes: str = "E164",
        seasons: str = "S01",
) -> Any:
    """构造下载记录快照。"""
    return SimpleNamespace(
        download_hash=download_hash,
        type=media_type,
        episodes=episodes,
        seasons=seasons,
    )


def test_fills_single_episode_from_history() -> None:
    """单文件单集时回填季集。"""
    meta = MetaInfo("164 4K.mkv")
    _planner()._fill_episode_from_history(meta, _file("164 4K.mkv"), _history())

    assert meta.type == MediaType.TV
    assert meta.begin_season == 1
    assert meta.begin_episode == 164


def test_fills_when_candidate_in_selection() -> None:
    """候选集数落在下载集数集合内才回填。"""
    meta = MetaInfo("01~4K.mp4")
    _planner()._fill_episode_from_history(
        meta, _file("01~4K.mp4"), _history(episodes="E01-E24")
    )

    assert meta.begin_episode == 1


def test_rejects_candidate_outside_selection() -> None:
    """候选集数不在本次下载集合内时不回填。"""
    meta = MetaInfo("05~4K.mp4")
    _planner()._fill_episode_from_history(
        meta, _file("05~4K.mp4"), _history(episodes="E01-E02")
    )

    assert meta.begin_episode is None


def test_rejects_hash_mismatch() -> None:
    """下载记录与当前任务不一致时不回填，避免手动整理串任务。"""
    meta = MetaInfo("164 4K.mkv")
    _planner()._fill_episode_from_history(
        meta, _file("164 4K.mkv"), _history(download_hash="other")
    )

    assert meta.begin_episode is None


def test_rejects_missing_download_hash() -> None:
    """没有任务标识时不回填。"""
    meta = MetaInfo("164 4K.mkv")
    _planner(download_hash=None)._fill_episode_from_history(
        meta, _file("164 4K.mkv"), _history()
    )

    assert meta.begin_episode is None


def test_rejects_non_tv_history() -> None:
    """电影等非剧集记录不参与回填。"""
    meta = MetaInfo("164 4K.mkv")
    _planner()._fill_episode_from_history(
        meta, _file("164 4K.mkv"), _history(media_type="电影")
    )

    assert meta.begin_episode is None


def test_keeps_existing_episode() -> None:
    """文件名已解析出集数时保持原值。"""
    meta = MetaInfo("仙逆 S01E164 4K.mkv")
    _planner()._fill_episode_from_history(
        meta, _file("仙逆 S01E164 4K.mkv"), _history(episodes="E01-E24")
    )

    assert meta.begin_episode == 164


def test_keeps_explicit_meta() -> None:
    """调用方显式传入元数据时不覆盖。"""
    explicit = MetaInfo("仙逆")
    meta = MetaInfo("164 4K.mkv")
    _planner(explicit_meta=explicit)._fill_episode_from_history(
        meta, _file("164 4K.mkv"), _history()
    )

    assert meta.begin_episode is None


def test_rejects_special_extra() -> None:
    """特典与附加视频不回填集数。"""
    meta = MetaInfo("花絮.mp4")
    _planner()._fill_episode_from_history(meta, _file("花絮.mp4"), _history())

    assert meta.begin_episode is None


def test_rejects_ambiguous_candidates() -> None:
    """文件名含多个候选集数时不回填。"""
    meta = MetaInfo("1917 2019 1080p.mkv")
    _planner()._fill_episode_from_history(
        meta, _file("1917 2019 1080p.mkv"), _history(episodes="E01-E99")
    )

    assert meta.begin_episode is None


def test_rejects_empty_selection() -> None:
    """下载记录没有集数信息时不回填。"""
    meta = MetaInfo("164 4K.mkv")
    _planner()._fill_episode_from_history(
        meta, _file("164 4K.mkv"), _history(episodes="")
    )

    assert meta.begin_episode is None
