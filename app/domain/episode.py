"""
剧集编号的解析与显示规则。

解析入口供搜索结果、网盘下载器与整理兜底共用，避免各模块各写一套正则。
"""

import re
from typing import Dict, Iterable, List, Optional, Set

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
# 标题声明的标准季集标记：S01E140 / S01E140-E179。
DECLARED_WINDOW_PATTERN = re.compile(
    r"[Ss](\d{1,3})\s*[Ee][Pp]?\s*(\d{1,4})(?:\s*[-~—至到]\s*[Ee]?\s*(\d{1,4}))?"
)
# 标题声明窗口的集数上限，异常标题不展开成超大窗口。
DECLARED_WINDOW_LIMIT = 2000
# 中文数字与单位：网盘分享常用「第十二集」「两百集」这类写法。
_CN_DIGIT_VALUES = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT_VALUES = {"十": 10, "百": 100, "千": 1000}
# 只在「集/话/期」前的中文数字才归一，避免把片名里的数字词改坏。
_CN_EPISODE_PATTERN = re.compile(r"([零〇一二两三四五六七八九十百千]{1,8})(\s*[集话話期])")
# 标题里声明集号的写法：第197集 / 更新至218集 / 全80集 / 共24话 / 197集。
TITLE_EPISODE_PATTERN = re.compile(r"(?:第|更新至?|全集?|共)?\s*(\d{1,4})\s*[集话話期]")
# 标题里声明集号区间的写法：140-179集。
TITLE_EPISODE_RANGE_PATTERN = re.compile(r"(\d{1,4})\s*[-~—至到]\s*(\d{1,4})\s*[集话話期]")
# 窗口识别只看独立数字：前后不能紧邻字母，避免把 4K 的 K 前数字算进来。
WINDOW_NUMBER_PATTERN = re.compile(r"(?<![0-9A-Za-z])(\d{1,4})(?![0-9A-Za-z])")


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


def _chinese_number_to_int(text: str) -> Optional[int]:
    """
    把一段中文数字转成整数。

    支持「十二」「二十」「一百九十七」「两百」「一千零一」这类常见写法；
    无法解析时返回 None。

    :param text: 只含中文数字与单位的文本
    :return: 整数值；无法解析时返回 None
    """
    if not text:
        return None
    total = 0
    number = 0
    for char in text:
        digit = _CN_DIGIT_VALUES.get(char)
        if digit is not None:
            number = digit
            continue
        unit = _CN_UNIT_VALUES.get(char)
        if unit is None:
            return None
        if number == 0:
            number = 1
        total += number * unit
        number = 0
    return total + number


def chinese_numerals_to_arabic(text: str) -> str:
    """
    把「第X集」里的中文数字换成阿拉伯数字，其它文本原样保留。

    只处理紧跟「集/话/期」的中文数字，片名里的数字词（如「一人之下」）
    不会被改写；转换失败或数值异常时保留原文。

    :param text: 文件名或标题
    :return: 归一化后的文本
    """
    value = str(text or "")
    if not value:
        return value

    def _replace(matched: "re.Match[str]") -> str:
        """
        替换单个中文集号。

        :param matched: 正则匹配结果
        :return: 替换文本
        """
        number = _chinese_number_to_int(matched.group(1))
        if number is None or number <= 0 or number > DECLARED_WINDOW_LIMIT:
            return matched.group(0)
        return f"{number}{matched.group(2)}"

    return _CN_EPISODE_PATTERN.sub(_replace, value)


def title_episode_numbers(title: str) -> List[int]:
    """
    从候选标题里取声明的集号。

    只认「集/话/期」前的数字（第197集、更新至218集、全80集、140-179集），
    避免把片名里的数字当成集号；年份与分辨率由集数可信度规则排除。

    :param title: 候选标题
    :return: 升序去重的集号列表
    """
    text = chinese_numerals_to_arabic(str(title or ""))
    if not text:
        return []
    numbers: Set[int] = set()
    for matched in TITLE_EPISODE_RANGE_PATTERN.finditer(text):
        begin, end = int(matched.group(1)), int(matched.group(2))
        if 0 < begin <= end <= DECLARED_WINDOW_LIMIT:
            numbers.update(range(begin, end + 1))
    for matched in TITLE_EPISODE_PATTERN.finditer(text):
        value = int(matched.group(1))
        if is_plausible_episode(value):
            numbers.add(value)
    return sorted(numbers)


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
    text = chinese_numerals_to_arabic(str(file_name or ""))
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


def windowed_episode_numbers(file_name: str, window: Iterable[int]) -> List[int]:
    """
    在给定集数窗口内提取文件名里的集数。

    站点为规避网盘审核，常把分享内文件写成 `140_179.mp4`、`140@xx.mp4`、
    `140.4K.mp4` 这类只有集数、没有片名季号的形态。这类名字按通用规则会被
    版本号、混淆符号或多候选数字否决；窗口模式改用「落在目标范围内的数字」
    当白名单，范围外的年份、帧率、清晰度自然被排除。

    :param file_name: 文件名或目录名
    :param window: 目标集数窗口，为空时不识别
    :return: 落在窗口内的集号，升序去重；特典文件始终为空
    """
    allowed = {int(value) for value in window or ()}
    text = chinese_numerals_to_arabic(str(file_name or ""))
    if not allowed or is_special_name(text):
        return []
    numbers: Set[int] = set()
    for matched in WINDOW_NUMBER_PATTERN.finditer(text):
        value = int(matched.group(1))
        if value in allowed:
            numbers.add(value)
    return sorted(numbers)


def declared_episode_window(title: str) -> Dict[int, Set[int]]:
    """
    从候选标题解析声明的季集窗口。

    `S01E140-E179` 展开成第 1 季 140-179 集；单集写法 `S01E140` 只含 140。

    :param title: 候选标题
    :return: 季号到集号集合的映射；标题没有标准季集标记时为空字典
    """
    windows: Dict[int, Set[int]] = {}
    for matched in DECLARED_WINDOW_PATTERN.finditer(str(title or "")):
        season = int(matched.group(1))
        begin = int(matched.group(2))
        end = int(matched.group(3) or begin)
        if begin <= 0 or end < begin or end - begin + 1 > DECLARED_WINDOW_LIMIT:
            continue
        windows.setdefault(season, set()).update(range(begin, end + 1))
    return windows


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
