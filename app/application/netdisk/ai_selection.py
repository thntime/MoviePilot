"""
网盘分享文件的 AI 预选服务。

只负责把分享内的文件名、大小、相对路径与季集线索交给后台模型，并把模型返回的
`file_id` 校验为分享内真实存在的文件；最终下载范围仍由下载链的规则解析决定。
模型不可用、超时或返回非法内容时一律降级为空预选，不阻塞用户手动选择。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Optional

from app.domain.episode import (
    compact_numbers,
    episode_numbers,
    is_special_name,
    parse_episode_selection,
    parse_season_number,
)
from app.domain.netdisk import ShareFile
from app.runtime.log import logger

# 后台系统任务类型，提示词定义由 Task 3.2 提供。
SYSTEM_TASK = "netdisk_share_select"
# 后台会话前缀，便于在会话列表中区分预选任务。
SESSION_PREFIX = "__agent_netdisk_share_select"
# 单次分享参与 AI 预选的文件数量上限。
DEFAULT_MAX_FILES = 200
# 单次预选的后台调用硬超时；模型挂起时按失败降级，避免阻塞手动下载。
AI_SELECTION_TIMEOUT_SECONDS = 30
# 显式季号标记：S01 / 第1季 / Season 1。裸数字不参与季号判断。
_EXPLICIT_SEASON_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[Ss]\d{1,3}|第\s*\d{1,3}\s*季|[Ss]eason\s*\d{1,3})", re.IGNORECASE
)
# 模型可能返回解释文本，只截取第一个 JSON 数组。
_JSON_ARRAY_PATTERN = re.compile(r"\[.*?]", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ShareSelectionResult:
    """
    AI 预选结果。

    :param selected_file_ids: 模型选中且在候选集合内的文件标识，保持模型顺序
    :param suggested_episode_selection: 由选中文件集数压缩出的预选集数文本
    """

    selected_file_ids: tuple[str, ...] = ()
    suggested_episode_selection: str = ""


async def select_share_files(
    files: Sequence[ShareFile],
    *,
    season: object = None,
    episodes: object = None,
    user_intent: str = "",
    max_files: int = DEFAULT_MAX_FILES,
) -> ShareSelectionResult:
    """
    让后台模型从分享文件里预选目标季集对应的文件。

    :param files: 分享解析后的文件树条目
    :param season: 目标季号线索，支持 int、`S01` 文本或季号序列
    :param episodes: 目标集数线索，支持 int、`E01-E03` 文本或集数序列
    :param user_intent: 用户的自然语言意图，作为模型判断的补充
    :param max_files: 参与预选的文件数量上限
    :return: 校验后的预选结果；任何失败都返回空结果
    """
    candidates = _build_candidates(files, max_files)
    if not candidates:
        return ShareSelectionResult()

    template_context = {
        "share_files": json.dumps(candidates, ensure_ascii=False),
        "target": _target_text(season, episodes, user_intent),
    }
    try:
        output = await _invoke_selection_llm(template_context)
    except Exception as error:  # noqa: BLE001 - AI 失败只降级，不影响手动选择
        logger.warning(
            f"网盘分享 AI 预选调用失败，降级为空预选：{type(error).__name__}: {error}"
        )
        return ShareSelectionResult()

    try:
        selected_ids = _parse_selected_ids(output)
    except (TypeError, ValueError) as error:
        logger.warning(f"网盘分享 AI 预选结果无法解析，降级为空预选：{error}")
        return ShareSelectionResult()

    candidates_by_id = {candidate["file_id"]: candidate for candidate in candidates}
    names = {file_id: candidate["name"] for file_id, candidate in candidates_by_id.items()}
    selected: list[str] = []
    seen: set[str] = set()
    for file_id in selected_ids:
        if file_id in seen or file_id not in names:
            continue
        seen.add(file_id)
        selected.append(file_id)
    if not selected:
        return ShareSelectionResult()

    target_seasons = _normalize_seasons(season)
    numbers: list[int] = []
    for file_id in selected:
        candidate = candidates_by_id[file_id]
        if target_seasons and candidate.get("season") not in (None, *target_seasons):
            continue
        numbers.extend(episode_numbers(names[file_id]))
    suggestion = compact_numbers(sorted(set(numbers))) if numbers else ""
    return ShareSelectionResult(
        selected_file_ids=tuple(selected),
        suggested_episode_selection=suggestion,
    )


async def _invoke_selection_llm(template_context: dict[str, str]) -> str:
    """
    通过统一后台提示词机制执行分享文件预选。

    :param template_context: 提示词模板上下文
    :return: 模型最终输出文本
    :raises RuntimeError: 智能助手服务未运行时抛出
    :raises asyncio.TimeoutError: 后台调用超过硬超时时抛出
    """
    from app.application.agent import get_prompt_manager, get_running_agent_manager
    from app.schemas.types import ReplyMode

    prompt = get_prompt_manager().render_system_task_message(
        SYSTEM_TASK,
        template_context=template_context,
    )
    manager = get_running_agent_manager()
    if manager is None:
        raise RuntimeError("智能助手服务未运行")

    full_output = [""]

    def on_output(text: str) -> None:
        """
        保存预选 Agent 最新输出，任务结束后作为最终结果解析。

        :param text: Agent 当前输出文本
        """
        full_output[0] = text

    await asyncio.wait_for(
        manager.run_background_prompt(
            message=prompt,
            session_prefix=SESSION_PREFIX,
            output_callback=on_output,
            reply_mode=ReplyMode.CAPTURE_ONLY,
            allow_message_tools=False,
            allow_tools=False,
        ),
        timeout=AI_SELECTION_TIMEOUT_SECONDS,
    )
    return full_output[0].strip()


def _build_candidates(
    files: Sequence[ShareFile], max_files: int
) -> list[dict[str, Any]]:
    """
    把分享文件投影为模型可读的候选条目。

    目录与特典/附加视频不参与预选；候选字段不包含 token、分享密码或 Cookie。

    :param files: 分享解析后的文件树条目
    :param max_files: 候选数量上限
    :return: 候选条目列表
    """
    limit = max(0, int(max_files))
    candidates: list[dict[str, Any]] = []
    for item in files or []:
        if len(candidates) >= limit:
            break
        if item.is_dir or is_special_name(item.name):
            continue
        path = str(item.path or item.name or "")
        candidates.append(
            {
                "file_id": str(item.file_id),
                "name": item.name,
                "size": int(item.size or 0),
                "path": path,
                "season": _season_of(item, path),
                "episodes": episode_numbers(item.name),
            }
        )
    return candidates


def _season_of(item: ShareFile, path: str) -> Optional[int]:
    """
    从相对路径解析季号线索，文件名中的显式季号优先于目录名。

    文件名只认显式季号标记（`S01`/`第1季`/`Season 1`），裸数字不当作季号；目录段
    反向查找，先认显式标记，再接受 1-99 的纯数字目录。与下载链的季号判定保持一致。

    :param item: 分享文件条目
    :param path: 已规范化的相对路径
    :return: 季号；无法识别时返回 None
    """
    relative = PurePosixPath(path or item.name or "")
    matched = _EXPLICIT_SEASON_PATTERN.search(relative.name)
    if matched:
        return parse_season_number(matched.group(0))
    for part in reversed(relative.parent.parts):
        matched = _EXPLICIT_SEASON_PATTERN.search(part)
        if matched:
            return parse_season_number(matched.group(0))
        if part.isdigit() and 0 < int(part) <= 99:
            return int(part)
    return None


def _target_text(season: object, episodes: object, user_intent: str) -> str:
    """
    拼装目标季集与用户意图的描述文本。

    :param season: 目标季号线索
    :param episodes: 目标集数线索
    :param user_intent: 用户的自然语言意图
    :return: 供提示词使用的目标描述
    """
    parts: list[str] = []
    seasons = _normalize_seasons(season)
    if seasons:
        parts.append(f"季 {compact_numbers(seasons)}")
    numbers = _normalize_episodes(episodes)
    if numbers:
        parts.append(f"集 {compact_numbers(numbers)}")
    intent = str(user_intent or "").strip()
    if intent:
        parts.append(f"用户意图 {intent}")
    return "；".join(parts)


def _normalize_seasons(value: object) -> list[int]:
    """
    把调用方传入的季号线索规范为升序去重列表。

    :param value: 单个季号、`S01` 文本或季号序列
    :return: 季号列表
    """
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        season = parse_season_number(value)
        return [season] if season is not None else []
    if isinstance(value, (list, tuple, set, frozenset)):
        numbers: set[int] = set()
        for item in value:
            numbers.update(_normalize_seasons(item))
        return sorted(numbers)
    return []


def _normalize_episodes(value: object) -> list[int]:
    """
    把调用方传入的集数线索规范为升序去重列表。

    :param value: 单个集号、`E01-E03` 文本或集号序列
    :return: 集号列表
    """
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return sorted(set(parse_episode_selection(value)))
    if isinstance(value, (list, tuple, set, frozenset)):
        numbers: set[int] = set()
        for item in value:
            numbers.update(_normalize_episodes(item))
        return sorted(numbers)
    return []


def _parse_selected_ids(output: str) -> list[str]:
    """
    从模型文本中提取并过滤 `file_id` 字符串数组。

    :param output: 模型输出文本
    :return: 字符串标识列表
    :raises ValueError: 未找到数组、数组不是合法 JSON 或不是数组类型时抛出
    """
    json_match = _JSON_ARRAY_PATTERN.search(output or "")
    if not json_match:
        raise ValueError(f"无法从响应中提取JSON数组: {output}")
    payload = json.loads(json_match.group())
    if not isinstance(payload, list):
        raise ValueError(f"AI 返回格式错误: {output}")
    return [item for item in payload if isinstance(item, str)]
