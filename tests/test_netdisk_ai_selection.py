"""
验证网盘分享的 AI 预选服务：候选构造、模型输出校验与降级行为。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.domain.netdisk import ShareFile

from app.application.netdisk.ai_selection import (
    ShareSelectionResult,
    select_share_files,
)


class _FakePromptManager:
    """
    记录后台系统任务提示词的渲染请求，并可按需抛出渲染异常。
    """

    def __init__(self) -> None:
        """初始化空的调用记录与异常开关。"""
        self.calls = []
        self.error = None

    def render_system_task_message(
        self, task_type, *, template_context=None, extra_rules=None
    ) -> str:
        """
        记录任务类型与模板上下文，返回固定提示词。

        :param task_type: 后台系统任务类型
        :param template_context: 模板上下文
        :param extra_rules: 附加规则
        :return: 固定提示词文本
        """
        self.calls.append((task_type, template_context))
        if self.error is not None:
            raise self.error
        return "PROMPT"


class _FakeAgentManager:
    """
    模拟后台 Agent 管理器，回放固定输出或抛出指定异常。
    """

    def __init__(self, output: str = "[]") -> None:
        """初始化回放输出、模拟延迟与空的调用记录。"""
        self.output = output
        self.delay = 0.0
        self.error = None
        self.calls = []

    async def run_background_prompt(self, **kwargs) -> None:
        """
        记录调用参数，把回放输出交给回调或抛出指定异常。

        :param kwargs: 后台任务调用参数
        """
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        callback = kwargs.get("output_callback")
        if callback is not None:
            callback(self.output)


@pytest.fixture()
def agent_env(monkeypatch):
    """
    装配可观测的提示词管理器与后台 Agent 管理器替身。
    """
    prompt_manager = _FakePromptManager()
    agent_manager = _FakeAgentManager()
    monkeypatch.setattr(
        "app.application.agent.get_prompt_manager", lambda: prompt_manager
    )
    monkeypatch.setattr(
        "app.application.agent.get_running_agent_manager", lambda: agent_manager
    )
    return SimpleNamespace(prompt=prompt_manager, manager=agent_manager)


def _share_file(
    file_id: str,
    name: str,
    *,
    path: str = "",
    is_dir: bool = False,
    size: int = 1024,
) -> ShareFile:
    """
    构造测试用分享文件条目。

    :param file_id: 平台文件标识
    :param name: 文件名称
    :param path: 相对分享根路径
    :param is_dir: 是否为目录
    :param size: 文件大小
    :return: 分享文件条目
    """
    return ShareFile(
        file_id=file_id,
        name=name,
        size=size,
        is_dir=is_dir,
        path=path or name,
    )


@pytest.mark.asyncio
async def test_select_share_files_maps_selected_ids_and_episodes(agent_env) -> None:
    """
    正常路径：候选字段齐全、提示词上下文正确，选中文件映射为集数区间。
    """
    agent_env.manager.output = '["f1", "f2"]'
    files = [
        _share_file("f1", "S01E01.mkv", path="S01/S01E01.mkv"),
        _share_file("f2", "S01E02.mkv", path="S01/S01E02.mkv"),
        _share_file("f3", "S01E03.mkv", path="S01/S01E03.mkv"),
    ]

    result = await select_share_files(
        files, season=1, episodes=[1, 2, 3], user_intent="只要前三集"
    )

    assert ShareSelectionResult(
        selected_file_ids=("f1", "f2"), suggested_episode_selection="1-2"
    ) == result
    assert 1 == len(agent_env.prompt.calls)
    task_type, template_context = agent_env.prompt.calls[0]
    assert "netdisk_share_select" == task_type
    assert {"share_files", "target"} == set(template_context)
    candidates = json.loads(template_context["share_files"])
    assert 3 == len(candidates)
    assert {
        "file_id",
        "name",
        "size",
        "path",
        "season",
        "episodes",
    } == set(candidates[0])
    assert 1 == candidates[0]["season"]
    assert [1] == candidates[0]["episodes"]
    assert "S01/S01E01.mkv" == candidates[0]["path"]
    assert "token" not in candidates[0]
    target = template_context["target"]
    assert "季 1" in target
    assert "集 1-3" in target
    assert "用户意图 只要前三集" in target
    assert 1 == len(agent_env.manager.calls)
    call = agent_env.manager.calls[0]
    assert "__agent_netdisk_share_select" == call["session_prefix"]
    assert not call["allow_message_tools"]
    from app.schemas.types import ReplyMode

    assert ReplyMode.CAPTURE_ONLY == call["reply_mode"]


@pytest.mark.asyncio
async def test_select_share_files_skips_ai_without_candidates(agent_env) -> None:
    """
    空文件树（仅目录与特典）时不调用 AI，直接返回空预选。
    """
    files = [
        _share_file("d1", "S01", is_dir=True),
        _share_file("s1", "花絮.mp4"),
        _share_file("s2", "NCOP.mkv"),
    ]

    result = await select_share_files(files)

    assert ShareSelectionResult() == result
    assert [] == agent_env.prompt.calls
    assert [] == agent_env.manager.calls


@pytest.mark.asyncio
async def test_select_share_files_drops_invalid_and_duplicate_ids(agent_env) -> None:
    """
    非法、重复与未知 file_id 被丢弃，合法标识保持模型顺序。
    """
    agent_env.manager.output = '["f2", "unknown", "f2", 3, "f1"]'
    files = [
        _share_file("f1", "S01E01.mkv"),
        _share_file("f2", "S01E02.mkv"),
    ]

    result = await select_share_files(files)

    assert ("f2", "f1") == result.selected_file_ids
    assert "1-2" == result.suggested_episode_selection


@pytest.mark.asyncio
async def test_select_share_files_caps_candidates_at_max_files(agent_env) -> None:
    """
    超过 200 个候选时只提交前 200 个，第 201 个标识无法被选中。
    """
    files = [
        _share_file(f"f{index}", f"S01E{index + 1:03d}.mkv") for index in range(201)
    ]
    agent_env.manager.output = '["f200"]'

    result = await select_share_files(files)

    assert ShareSelectionResult() == result
    candidates = json.loads(agent_env.prompt.calls[0][1]["share_files"])
    assert 200 == len(candidates)
    assert "f199" == candidates[-1]["file_id"]


@pytest.mark.asyncio
async def test_select_share_files_season_ignores_bare_numbers(agent_env) -> None:
    """
    扁平文件与年份目录里的裸数字不当季号，显式季号目录仍可识别。
    """
    files = [
        _share_file("f1", "05.mp4"),
        _share_file("f2", "05.mp4", path="S01/05.mp4"),
        _share_file("f3", "01.mp4", path="电影2024/01.mp4"),
    ]

    await select_share_files(files)

    candidates = json.loads(agent_env.prompt.calls[0][1]["share_files"])
    assert [None, 1, None] == [item["season"] for item in candidates]
    assert [[5], [5], [1]] == [item["episodes"] for item in candidates]


@pytest.mark.asyncio
async def test_select_share_files_selected_file_without_episodes(agent_env) -> None:
    """
    选中文件解析不出集数时，该文件不贡献集数，预选集数为空。
    """
    agent_env.manager.output = '["f1"]'
    files = [_share_file("f1", "movie.mkv")]

    result = await select_share_files(files)

    assert ("f1",) == result.selected_file_ids
    assert "" == result.suggested_episode_selection


@pytest.mark.asyncio
async def test_select_share_files_returns_empty_when_agent_unavailable(
    monkeypatch,
) -> None:
    """
    智能助手服务未运行时降级为空预选，不抛致命异常。
    """
    monkeypatch.setattr(
        "app.application.agent.get_running_agent_manager", lambda: None
    )

    result = await select_share_files([_share_file("f1", "S01E01.mkv")])

    assert ShareSelectionResult() == result


@pytest.mark.asyncio
async def test_select_share_files_returns_empty_on_render_failure(agent_env) -> None:
    """
    提示词渲染失败时降级为空预选。
    """
    agent_env.prompt.error = ValueError("missing task")
    agent_env.manager.output = '["f1"]'

    result = await select_share_files([_share_file("f1", "S01E01.mkv")])

    assert ShareSelectionResult() == result


@pytest.mark.asyncio
async def test_select_share_files_returns_empty_on_empty_array(agent_env) -> None:
    """
    模型返回空数组时返回空预选。
    """
    agent_env.manager.output = "[]"

    result = await select_share_files([_share_file("f1", "S01E01.mkv")])

    assert ShareSelectionResult() == result


@pytest.mark.asyncio
async def test_select_share_files_returns_empty_on_invalid_json(agent_env) -> None:
    """
    模型输出不是 JSON 数组时返回空预选。
    """
    agent_env.manager.output = "没有可用文件"

    result = await select_share_files([_share_file("f1", "S01E01.mkv")])

    assert ShareSelectionResult() == result


@pytest.mark.asyncio
async def test_select_share_files_returns_empty_on_exception(agent_env) -> None:
    """
    后台调用抛异常（含超时）时返回空预选。
    """
    agent_env.manager.error = TimeoutError("timeout")

    result = await select_share_files([_share_file("f1", "S01E01.mkv")])

    assert ShareSelectionResult() == result


@pytest.mark.asyncio
async def test_select_share_files_returns_empty_on_hard_timeout(
    agent_env, monkeypatch
) -> None:
    """
    后台调用超过硬超时时降级为空预选，不抛异常。
    """
    monkeypatch.setattr(
        "app.application.netdisk.ai_selection.AI_SELECTION_TIMEOUT_SECONDS", 0.01
    )
    agent_env.manager.delay = 0.2

    result = await select_share_files([_share_file("f1", "S01E01.mkv")])

    assert ShareSelectionResult() == result
