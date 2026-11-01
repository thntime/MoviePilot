"""
剧集编号的解析与显示规则。

解析入口供搜索结果、网盘下载器与整理兜底共用，避免各模块各写一套正则。
"""

import re
from typing import Iterable, List, Optional

# 结构化集数写法：第12集 / S01E12 / E12。
STRUCTURED_PATTERNS = (
    re.compile(r"第\s*(\d{1,4})\s*[集话話期]"),
    re.compile(r"[Ss]\d{1,2}\s*[Ee][Pp]?\s*(\d{1,4})"),
    re.compile(r"(?<![0-9A-Za-z])[Ee][Pp]?\s*(\d{1,4})(?![0-9A-Za-z])"),
)
# 区间写法：153-162 / 153~162 / 153至162。
# 两端都必须是非字母数字边界，否则 `01~4K` 里的 `4` 会被当成右端点。
RANGE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(\d{1,4})\s*[-~—至到]\s*(\d{1,4})(?![0-9A-Za-z])"
)
# 裸数字集数（如「仙逆.153.mp4」），两侧不能紧邻字母或数字。
BARE_PATTERN = re.compile(r"(?<![0-9A-Za-z])(\d{1,4})(?![0-9A-Za-z])")
# 版本号形态（2.0 / 5.1）出现在文件名里时，数字属于音轨或编码规格而不是集数。
VERSION_PATTERN = re.compile(r"\d+\.\d+")
# 裸数字必须位于名称开头或紧跟这些分隔符，才算候选集数。
EPISODE_SEPARATORS = " \t._-()[]（）【】"
# 分辨率与年份不是集数。
RESOLUTION_TOKENS = frozenset({360, 480, 540, 576, 720, 1080, 1440, 2160, 4320})
YEAR_MIN, YEAR_MAX = 1900, 2099
# 文件名混淆特征：出现这些符号（如 1#6@#2），或数字之间夹 &，不参与集数解析。
OBFUSCATED_PATTERN = re.compile(r"[#@!%^*]|\d\s*&\s*\d")
# 花絮、预告一类文件不是正片集数。
NON_EPISODE_PATTERN = re.compile(r"花絮|预告|特典|番外|OST|主题曲|片头|片尾", re.IGNORECASE)
# 整理侧识别特典/附加视频用：比搜索侧更宽，覆盖常见英文标记。
SPECIAL_NAME_PATTERN = re.compile(
    r"花絮|预告|特典|番外|OST|主题曲|片头|片尾"
    r"|\b(?:NCOP|NCED|OVA|OAD|MENU|TRAILER|FEATURETTE|SP|PV|CM)\b",
    re.IGNORECASE,
)
# 下载记录里的集数文本按这些分隔符切分。
SELECTION_SPLITTER = re.compile(r"[、,，;；/\s]+")


def compact_numbers(numbers: List[int]) -> str:
    """
    把连续剧集编号压缩为逗号分隔的数字区间。
    """
    numbers.sort()
    result = []
    start = numbers[0]
    end = numbers[0]
    for number in numbers[1:]:
        if number == end + 1:
            end = number
            continue
        result.append(str(start) if start == end else f"{start}-{end}")
        start = end = number
    result.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(result)


def format_ranges(numbers: List[int]) -> str:
    """
    把剧集编号格式化为带 E 前缀和中文顿号的连续区间。
    """
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"E{numbers[0]:02d}"
    numbers.sort()
    ranges = []
    start = numbers[0]
    end = numbers[0]
    for number in numbers[1:]:
        if number == end + 1:
            end = number
            continue
        ranges.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
        start = end = number
    ranges.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
    return "、".join(ranges)


def is_plausible_episode(value: int) -> bool:
    """
    判断一个数字是否可能是集数，排除分辨率与年份。
    """
    return 0 < value <= 9999 and value not in RESOLUTION_TOKENS and not YEAR_MIN <= value <= YEAR_MAX


def bare_episode(file_name: str) -> Optional[int]:
    """
    从文件名中提取裸数字集数，只在能明确判定时返回。

    去掉扩展名后不含 `\\d+\\.\\d+` 版本号形态，且「位于开头或紧跟分隔符的独立数字」
    恰好只有一个。
    """
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    if VERSION_PATTERN.search(stem):
        return None
    candidates: List[int] = []
    for matched in BARE_PATTERN.finditer(stem):
        value = int(matched.group(1))
        if matched.start() > 0 and stem[matched.start() - 1] not in EPISODE_SEPARATORS:
            continue
        if is_plausible_episode(value):
            candidates.append(value)
    if len(candidates) != 1:
        return None
    return candidates[0]


def episode_numbers(file_name: str) -> List[int]:
    """
    从一个文件名中提取集数，结构化写法优先，其次区间与裸数字。

    :param file_name: 文件名或目录名
    :return: 该名称包含的集数列表；无法识别时为空
    """
    text = str(file_name or "")
    if OBFUSCATED_PATTERN.search(text) or NON_EPISODE_PATTERN.search(text):
        return []
    numbers: List[int] = []
    for pattern in STRUCTURED_PATTERNS:
        matched = pattern.search(text)
        if matched:
            numbers.append(int(matched.group(1)))
            break
    else:
        ranged = RANGE_PATTERN.search(text)
        if ranged and is_plausible_episode(int(ranged.group(1))) \
                and is_plausible_episode(int(ranged.group(2))):
            numbers.extend([int(ranged.group(1)), int(ranged.group(2))])
        else:
            episode = bare_episode(text)
            if episode is not None:
                numbers.append(episode)
    return [value for value in numbers if is_plausible_episode(value)]


def episode_token(file_names: Iterable[str]) -> Optional[str]:
    """
    从一组文件名推导季集标记，使用最小与最大集数构成连续区间。
    """
    episodes: List[int] = []
    for name in file_names or []:
        episodes.extend(episode_numbers(name))
    if not episodes:
        return None
    begin, end = min(episodes), max(episodes)
    if begin == end:
        return f"S01E{begin:02d}"
    return f"S01E{begin:02d}-E{end:02d}"


def parse_episode_selection(text: str) -> List[int]:
    """
    解析下载记录里的集数文本，展开成具体集号。

    支持 `E164`、`E01-E24`、`E01-E05、E08`、`E01,E02` 等写法；区间按闭区间展开，
    不填充未列出的空洞。

    :param text: 下载记录的 episodes 字段
    :return: 升序去重的集号列表
    """
    value = str(text or "").strip()
    if not value:
        return []
    numbers: List[int] = []
    for token in SELECTION_SPLITTER.split(value):
        if not token:
            continue
        matched = re.fullmatch(r"[Ee][Pp]?(\d{1,4})", token) or re.fullmatch(r"(\d{1,4})", token)
        if matched:
            numbers.append(int(matched.group(1)))
            continue
        matched = re.fullmatch(
            r"[Ee][Pp]?(\d{1,4})\s*[-~—至到]\s*[Ee]?[Pp]?(\d{1,4})", token
        )
        if matched:
            start, end = int(matched.group(1)), int(matched.group(2))
            if start <= end:
                numbers.extend(range(start, end + 1))
            continue
        matched = re.fullmatch(r"(\d{1,4})\s*[-~—至到]\s*(\d{1,4})", token)
        if matched:
            start, end = int(matched.group(1)), int(matched.group(2))
            if start <= end:
                numbers.extend(range(start, end + 1))
    return sorted(set(numbers))


def parse_season_number(text: str) -> Optional[int]:
    """
    解析季号文本，支持 `S01`、`1`、`第1季`。
    """
    value = str(text or "").strip()
    if not value:
        return None
    matched = re.search(r"[Ss](\d{1,3})", value) or re.search(r"(\d{1,3})", value)
    if not matched:
        return None
    return int(matched.group(1))


def is_special_name(file_name: str) -> bool:
    """
    判断文件名是否为特典/附加视频，这类文件不参与集数映射。
    """
    return bool(SPECIAL_NAME_PATTERN.search(str(file_name or "")))
