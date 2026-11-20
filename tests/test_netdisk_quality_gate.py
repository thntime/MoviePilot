"""
验证网盘搜索质量门：文件分类、类型占比、标题过滤、集数四形态、时长与命名一致性。

用例全部直接调用 `candidates.passes_quality`，覆盖 2026-11-20 质量门改造的每一条判据。
"""

from typing import Any, Dict, List

import pytest

from app.application.netdisk import candidates
from app.domain.context import TorrentInfo
from app.schemas.types import MediaType

_MB = 1024 * 1024
_URL = "https://pan.quark.cn/s/probe"


def _files(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    原样返回文件条目列表，仅用于强调入参形态。

    :param entries: 文件条目
    :return: 文件条目
    """
    return entries


def _entry(
    name: str,
    size_mb: float = 800,
    duration: int = 1300,
    path: str = "",
) -> Dict[str, Any]:
    """
    构造一条分享文件树条目。

    :param name: 文件名
    :param size_mb: 大小（MB）
    :param duration: 时长（秒），0 表示平台未提供
    :param path: 相对路径，空表示只有文件名
    :return: 文件条目
    """
    entry: Dict[str, Any] = {"name": name, "size": int(size_mb * _MB), "duration": duration}
    if path:
        entry["path"] = path
    return entry


def _run(title: str, entries: List[Dict[str, Any]], media_type: Any = None) -> TorrentInfo:
    """
    用给定标题与文件树跑一次质量门。

    :param title: 候选标题
    :param entries: 文件条目
    :param media_type: 目标媒体类型
    :return: 判定后的候选对象
    """
    torrent = TorrentInfo(title=title, enclosure=_URL)
    candidates.passes_quality(
        torrent,
        {"alive": True, "_files": entries},
        config_getter=lambda: {},
        media_type=media_type,
    )
    return torrent


def _passed(title: str, entries: List[Dict[str, Any]], media_type: Any = None) -> bool:
    """
    返回质量门判定结果。

    :param title: 候选标题
    :param entries: 文件条目
    :param media_type: 目标媒体类型
    :return: 通过返回 True
    """
    torrent = TorrentInfo(title=title, enclosure=_URL)
    return candidates.passes_quality(
        torrent,
        {"alive": True, "_files": entries},
        config_getter=lambda: {},
        media_type=media_type,
    )


def test_audio_only_share_is_dropped() -> None:
    """纯 mp3 有声小说分享不再通过影视质量门。"""
    entries = [_entry(f"斗破苍穹 有声小说 第{index:03d}集.mp3", size_mb=20, duration=900)
               for index in range(1, 121)]

    assert _passed("斗破苍穹 年番 更新至120集", entries) is False


def test_numeric_mp3_share_is_dropped() -> None:
    """只有数字命名的音频分享同样按类型不符丢弃。"""
    entries = [_entry(f"{index:02d}.mp3", size_mb=20, duration=900) for index in range(1, 25)]

    assert _passed("斗破苍穹 S05E01-E24", entries) is False


def test_mixed_share_with_feature_video_is_kept() -> None:
    """正片占绝对多数的混包保留，音频只算附加内容。"""
    entries = [_entry(f"斗破苍穹 S05E{index:02d}.mkv") for index in range(1, 41)]
    entries += [_entry(f"第{index:03d}集.mp3", size_mb=20, duration=900) for index in range(1, 41)]

    torrent = _run("斗破苍穹 年番", entries)

    assert torrent.title.endswith("S05E01-E40")


def test_single_video_movie_is_kept() -> None:
    """单个正片且无集号时按单片电影保留。"""
    torrent = _run("斗破苍穹 电影版 2024", [_entry("斗破苍穹 电影版 2024 1080p.mkv", size_mb=4000, duration=5400)])

    assert torrent.episode_kind == "feature"
    assert torrent.episodes == []


def test_audio_dominant_mixed_share_is_dropped() -> None:
    """一个视频混大量音频时按类型占比丢弃，不能靠「有视频」蒙混过关。"""
    entries = [_entry("斗破苍穹 正片.mkv", size_mb=30, duration=1300)]
    entries += [_entry(f"第{index:03d}集.mp3", size_mb=20, duration=900) for index in range(1, 41)]

    assert _passed("斗破苍穹 年番", entries) is False


def test_music_share_keeps_audio_only() -> None:
    """目标为音乐时纯音频分享保留，不受影视门约束。"""
    entries = [_entry(f"专辑 {index:02d} - 曲目.mp3", size_mb=10, duration=240) for index in range(1, 13)]

    assert _passed("某专辑 2024", entries, media_type=MediaType.MUSIC) is True


def test_feature_share_with_extra_clips_is_kept() -> None:
    """正片带花絮时花絮不计入分母，14 集正片不再被误杀。"""
    entries = [_entry(f"斗破苍穹 S05E{index:02d}.mkv") for index in range(1, 15)]
    entries += [_entry(f"花絮{index:02d}.mkv", size_mb=100, duration=300) for index in range(1, 7)]

    assert _passed("斗破苍穹 年番", entries) is True


def test_single_trailer_is_dropped() -> None:
    """只有一个预告片时既不算正片也不算单片电影。"""
    assert _passed("斗破苍穹 年番", [_entry("预告.mp4", size_mb=200, duration=120)]) is False


def test_bluray_disc_is_kept() -> None:
    """蓝光原盘的 m2ts 片段是合法正片，不能按集数不清晰丢弃。"""
    entries = [
        _entry("00001.m2ts", size_mb=20000, duration=1300, path="BDMV/STREAM/00001.m2ts"),
        _entry("00002.m2ts", size_mb=20000, duration=1300, path="BDMV/STREAM/00002.m2ts"),
    ]

    torrent = _run("斗破苍穹 年番 原盘", entries)

    assert torrent.episode_source == "path"


def test_movie_collection_is_kept() -> None:
    """一个分享里多部电影时按合集保留，交给下载阶段裁剪。"""
    entries = [
        _entry("斗破苍穹 第一部 2023 1080p.mkv", size_mb=4000, duration=5400),
        _entry("斗破苍穹 第二部 2024 1080p.mkv", size_mb=4200, duration=5500),
        _entry("斗破苍穹 第三部 2025 1080p.mkv", size_mb=4300, duration=5600),
    ]

    torrent = _run("斗破苍穹 电影合集", entries)

    assert torrent.episode_kind == "collection"
    assert "合集" in torrent.quality_flags


def test_chinese_numeral_episodes_are_recognized() -> None:
    """中文数字集号（第十二集）先归一成阿拉伯数字再判定。"""
    entries = [_entry(f"斗破苍穹 第{name}集.mp4") for name in ("十二", "十三", "十四")]

    torrent = _run("斗破苍穹 年番", entries)

    assert torrent.episodes == [12, 13, 14]
    assert torrent.title.endswith("S01E12-E14")


def test_title_name_conflict_is_dropped() -> None:
    """标题写斗破苍穹、文件名全是启运丹田时按命名冲突丢弃。"""
    entries = [_entry(f"启运丹田 {index:02d}.mp4", size_mb=800, duration=1300) for index in range(1, 41)]

    assert _passed("斗破苍穹 年番", entries) is False


def test_title_episode_is_written_back_for_single_file() -> None:
    """单文件分享从标题取集号，写回标准季集标记并保留原始标题。"""
    torrent = _run(
        "🎬 凡人修仙传：慕兰之战  第197集",
        [_entry("凡人修仙传：慕兰之战 4K.mp4", size_mb=1500, duration=1400)],
    )

    assert torrent.episodes == [197]
    assert torrent.episode_source == "title"
    assert torrent.title.endswith("S01E197")
    assert torrent.raw_title == "🎬 凡人修仙传：慕兰之战  第197集"


def test_short_drama_with_large_size_is_dropped() -> None:
    """每集 4 分钟、200MB 的短剧按单集中位时长丢弃，不再要求大小同时超标。"""
    entries = [_entry(f"某短剧 第{index:03d}集.mp4", size_mb=200, duration=240) for index in range(1, 81)]

    assert _passed("某短剧 全80集", entries) is False


def test_audio_book_title_is_dropped() -> None:
    """标题声明是有声内容时即使文件是视频也丢弃。"""
    entries = [_entry(f"斗破苍穹 第{index:03d}集.mp4") for index in range(1, 21)]

    assert _passed("斗破苍穹 有声小说 更新至20集", entries) is False


def test_small_episodes_without_duration_are_dropped() -> None:
    """拿不到时长时，平均单集偏小且集数偏多按疑似有声书丢弃。"""
    entries = [_entry(f"斗破苍穹 第{index:03d}集.mp4", size_mb=100, duration=0) for index in range(1, 41)]

    assert _passed("斗破苍穹 年番", entries) is False


def test_season_is_read_from_relative_path() -> None:
    """目录名里的季号要写回候选，不能一律当第一季。"""
    entries = [
        _entry(f"斗破苍穹 S02E{index:02d}.mkv", path=f"斗破苍穹/第2季/斗破苍穹 S02E{index:02d}.mkv")
        for index in range(1, 4)
    ]

    torrent = _run("斗破苍穹 年番", entries)

    assert torrent.season == 2
    assert torrent.title.endswith("S02E01-E03")


def test_archive_size_dominant_share_is_dropped() -> None:
    """压缩包按体积占绝对多数时按类型占比丢弃，哪怕正片集数能解析。"""
    entries = [_entry(f"斗破苍穹 S01E{index:02d}.mkv") for index in range(1, 3)]
    entries += [_entry(f"斗破苍穹 素材{index:02d}.zip", size_mb=5000, duration=0) for index in range(1, 6)]

    assert _passed("斗破苍穹 年番", entries) is False


def test_declared_episode_count_rescues_unnumbered_files() -> None:
    """标题声明「更200集」时，文件名里落在范围内的数字算作可解析，整包不再被误杀。"""
    entries = [_entry(f"凡人修仙传 年番4 S04E{index:02d}.mkv") for index in range(1, 11)]
    entries += [_entry(f"凡人修仙传 年番4 {index}.4K.mkv") for index in range(11, 21)]

    torrent = _run("凡人修仙传 年番4 (2026)【更200集】【4K.高码率】", entries)

    assert torrent.episode_kind == "series"
    assert torrent.episodes == list(range(1, 11))


def test_unnumbered_files_without_declared_count_still_dropped() -> None:
    """标题没声明集数时，无法解析的文件仍按集数不清晰丢弃。"""
    entries = [_entry(f"凡人修仙传 S04E{index:02d}.mkv") for index in range(1, 3)]
    entries += [_entry(f"凡人修仙传 慕兰之战 4K.mkv") for _ in range(8)]

    assert _passed("凡人修仙传 年番4", entries) is False


def test_archive_dominant_share_is_dropped() -> None:
    """主要集数在压缩包里时丢弃，避免误报整季可用。"""
    entries = [_entry(f"斗破苍穹 S01E{index:02d}.mkv") for index in range(1, 3)]
    entries += [_entry(f"斗破苍穹 {index:03d}.zip", size_mb=900, duration=0) for index in range(3, 25)]

    assert _passed("斗破苍穹 年番", entries) is False


def test_unreadable_tree_is_dropped() -> None:
    """拿不到完整文件树时直接丢弃。"""
    torrent = TorrentInfo(title="斗破苍穹 年番", enclosure=_URL)

    assert candidates.passes_quality(torrent, {"alive": True}, config_getter=lambda: {}) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("第十二集", [12]),
        ("第两百集", [200]),
        ("第一百九十七集", [197]),
        ("第197集", [197]),
        ("凡人修仙传 2", []),
    ],
)
def test_title_episode_numbers(text: str, expected: List[int]) -> None:
    """标题集号只认「集/话/期」前的数字，片名里的数字不当集号。"""
    from app.domain.episode import title_episode_numbers

    assert title_episode_numbers(text) == expected


def test_files_helper_returns_entries() -> None:
    """_files 辅助函数保持入参不变，避免测试夹具被意外改写。"""
    entries = [_entry("a.mkv")]

    assert _files(entries) is entries
