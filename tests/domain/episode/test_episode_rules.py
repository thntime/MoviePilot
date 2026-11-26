"""
集数解析规则测试：文件名提取与下载记录集数文本解析。
"""

from app.domain.episode import (
    episode_numbers,
    episode_token,
    is_special_name,
    parse_episode_selection,
    parse_season_number,
)


def test_bare_number_prefix_is_episode() -> None:
    """裸集数前缀能识别，画质标记不会被当成区间端点。"""
    assert episode_numbers("164 4K.mkv") == [164]
    assert episode_numbers("162 4K.mp4") == [162]
    assert episode_numbers("01~4K.mp4") == [1]
    assert episode_numbers("05~4K.mp4") == [5]


def test_range_keeps_both_endpoints() -> None:
    """真正的集数区间仍取两端。"""
    assert episode_numbers("01~04.mp4") == [1, 4]
    assert episode_numbers("153-162.mp4") == [153, 162]


def test_structured_episode_wins() -> None:
    """结构化写法优先于裸数字。"""
    assert episode_numbers("仙逆 S01E164 4K.mkv") == [164]
    assert episode_numbers("第12集.mp4") == [12]
    assert episode_numbers("E07.mkv") == [7]


def test_year_and_resolution_are_not_episodes() -> None:
    """年份与分辨率不作为集数，多候选时宁可不识别。"""
    assert episode_numbers("1923 4K.mkv") == []
    assert episode_numbers("1917 2019 1080p.mkv") == []
    assert episode_numbers("花絮.mp4") == []


def test_episode_token_range() -> None:
    """多个文件的季集标记取最小与最大集数。"""
    assert episode_token(["01~4K.mp4", "02~4K.mp4"]) == "S01E01-E02"
    assert episode_token(["05~4K.mp4"]) == "S01E05"
    assert episode_token([]) is None


def test_parse_episode_selection_single_and_range() -> None:
    """下载记录的集数文本支持单集与闭区间，裸数字同样有效。"""
    assert parse_episode_selection("E164") == [164]
    assert parse_episode_selection("160") == [160]
    assert parse_episode_selection("153-162") == list(range(153, 163))
    assert parse_episode_selection("160,161") == [160, 161]
    assert parse_episode_selection("E01-E24") == list(range(1, 25))
    assert parse_episode_selection("E153-E162") == list(range(153, 163))


def test_parse_episode_selection_skips_gaps() -> None:
    """多段写法不填充未列出的空洞。"""
    assert parse_episode_selection("E01-E05、E08") == [1, 2, 3, 4, 5, 8]
    assert parse_episode_selection("") == []


def test_parse_season_number() -> None:
    """季号支持 S01 与纯数字。"""
    assert parse_season_number("S01") == 1
    assert parse_season_number("2") == 2
    assert parse_season_number("") is None


def test_special_name_detection() -> None:
    """特典与附加视频不参与集数映射。"""
    assert is_special_name("NCOP.mp4") is True
    assert is_special_name("花絮 01.mp4") is True
    assert is_special_name("164 4K.mkv") is False
