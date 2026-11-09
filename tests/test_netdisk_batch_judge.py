"""
验证网盘批次 AI 决策服务：输出解析、提示词构造与失败降级行为。

测试不调用真实模型，统一用异步假实现替换 `_call_llm`。
"""

import asyncio
import json

from app.application.netdisk import batch_judge


def test_judge_batch_parses_json(monkeypatch):
    """
    模型返回合法 JSON 时按原值给出决策与缺失集数。
    """

    async def fake_call(_prompt: str) -> str:
        return '{"decision":"retry_missing","reason":"缺 2 集","missing_episodes":[2]}'

    monkeypatch.setattr(batch_judge, "_call_llm", fake_call)
    result = batch_judge.judge_batch({"media": {"title": "x"}})
    assert result.decision == "retry_missing"
    assert result.reason == "缺 2 集"
    assert result.missing_episodes == [2]


def test_judge_batch_falls_back_on_error(monkeypatch):
    """
    模型调用抛异常时降级为默认整理，且原因包含降级说明。
    """

    async def fake_call(_prompt: str) -> str:
        raise RuntimeError("no llm")

    monkeypatch.setattr(batch_judge, "_call_llm", fake_call)
    result = batch_judge.judge_batch({"media": {"title": "x"}})
    assert result.decision == "organize_now"
    assert "AI 不可用" in result.reason


def test_judge_batch_falls_back_on_unknown_decision(monkeypatch):
    """
    决策值不在允许集合内时同样降级为默认整理。
    """

    async def fake_call(_prompt: str) -> str:
        return '{"decision":"delete_everything","reason":"?","missing_episodes":[]}'

    monkeypatch.setattr(batch_judge, "_call_llm", fake_call)
    result = batch_judge.judge_batch({"media": {"title": "x"}})
    assert result.decision == "organize_now"
    assert "AI 不可用" in result.reason


def test_judge_batch_falls_back_on_timeout(monkeypatch):
    """
    模型调用超过超时时间时降级，不向调用方抛出异常。
    """

    async def slow_call(_prompt: str) -> str:
        await asyncio.sleep(5)
        return "{}"

    monkeypatch.setattr(batch_judge, "_call_llm", slow_call)
    result = batch_judge.judge_batch({"media": {"title": "x"}}, timeout=0.01)
    assert result.decision == "organize_now"
    assert "AI 不可用" in result.reason


def test_judge_batch_prompt_keeps_chinese(monkeypatch):
    """
    提示词内嵌摘要 JSON，中文按原文传递而不是转义。
    """
    captured: dict[str, str] = {}

    async def fake_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"decision":"explain_only","reason":"ok","missing_episodes":[]}'

    monkeypatch.setattr(batch_judge, "_call_llm", fake_call)
    summary = {"media": {"title": "庆余年"}}
    result = batch_judge.judge_batch(summary)

    assert result.decision == "explain_only"
    assert json.dumps(summary, ensure_ascii=False) in captured["prompt"]
    assert "只返回 JSON" in captured["prompt"]


def test_judge_batch_extracts_json_from_text(monkeypatch):
    """
    模型在 JSON 前后附带解释文本时仍能提取出决策对象。
    """

    async def fake_call(_prompt: str) -> str:
        return '好的：\n```json\n{"decision":"organize_now","reason":"完整","missing_episodes":[]}\n```'

    monkeypatch.setattr(batch_judge, "_call_llm", fake_call)
    result = batch_judge.judge_batch({"media": {"title": "x"}})
    assert result.decision == "organize_now"
    assert result.reason == "完整"
