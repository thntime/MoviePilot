"""
网盘批次下载的 AI 决策服务。

把一次下载批次的摘要（目标媒体、有效文件、失败文件、缺失集数）交给后台模型，
由模型给出是否立即整理、是否补下缺失集或只做说明的建议。决策仅作参考，下载链
仍按自身规则执行；模型不可用、超时或返回非法内容时统一降级为“先整理有效文件”。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, List

from app.runtime.log import logger

# 允许的决策值；其他取值一律按解析失败处理。
ALLOWED_DECISIONS = frozenset({"organize_now", "retry_missing", "explain_only"})
# 降级时使用的固定原因，便于调用方与日志识别“模型未参与决策”。
FALLBACK_REASON = "AI 不可用，默认先整理有效文件"
# 系统指令固定要求模型只输出 JSON，避免自然语言解释混入解析。
SYSTEM_PROMPT = "你是网盘下载批次决策助手，只返回 JSON，不要输出解释或 Markdown 代码块。"
# 模型可能带解释文本或代码块，只截取第一个 JSON 对象。
_JSON_OBJECT_PATTERN = re.compile(r"\{.*}", re.DOTALL)


@dataclass(frozen=True)
class BatchDecision:
    """
    一次批次的 AI 决策结果。

    :param decision: 决策值，取值为 `organize_now` / `retry_missing` / `explain_only`
    :param reason: 模型给出的中文原因；降级时为固定说明
    :param missing_episodes: 模型认为缺失的集数列表，未提供时为空列表
    """

    decision: str
    reason: str
    missing_episodes: List[int]


def judge_batch(
    summary: Mapping[str, Any], timeout: float = 60.0
) -> BatchDecision:
    """
    同步请求模型判断批次处理方式。

    本函数在下载线程中调用，因此内部自行创建事件循环执行异步模型调用；
    任何异常、超时或非法输出都降级为默认决策，不向调用方抛出。

    :param summary: 批次摘要，会以 JSON 原文交给模型
    :param timeout: 模型调用硬超时秒数
    :return: 决策结果；失败时返回默认的 `organize_now`
    """
    try:
        prompt = _build_prompt(summary)
        output = asyncio.run(asyncio.wait_for(_call_llm(prompt), timeout=timeout))
        payload = json.loads(_extract_json(output))
        decision = str(payload.get("decision") or "")
        if decision not in ALLOWED_DECISIONS:
            raise ValueError(f"未知决策值: {decision}")
        missing_episodes = [
            int(item) for item in payload.get("missing_episodes") or []
        ]
        return BatchDecision(
            decision=decision,
            reason=str(payload.get("reason") or ""),
            missing_episodes=missing_episodes,
        )
    except Exception as error:  # noqa: BLE001 - AI 失败只降级，不影响批次执行
        logger.warning(
            f"网盘批次 AI 决策失败，降级为默认整理：{type(error).__name__}: {error}"
        )
        return BatchDecision("organize_now", FALLBACK_REASON, [])


async def _call_llm(prompt: str) -> str:
    """
    调用统一后台模型并返回文本输出。

    独立成函数便于测试替换，避免测试触达真实模型。

    :param prompt: 已拼装好的完整提示词
    :return: 模型输出文本
    """
    from app.agent.llm.helper import LLMHelper

    llm = await LLMHelper.get_llm(streaming=False)
    try:
        response = await llm.ainvoke(prompt)
    finally:
        # 模型实例持有独占 HTTP 客户端，须在创建它的同一事件循环内释放。
        await LLMHelper.close_llm(llm)
    return getattr(response, "content", None) or str(response)


def _build_prompt(summary: Mapping[str, Any]) -> str:
    """
    拼装系统指令与批次摘要 JSON。

    :param summary: 批次摘要
    :return: 完整提示词文本
    """
    return f"{SYSTEM_PROMPT}\n\n{json.dumps(summary, ensure_ascii=False)}"


def _extract_json(output: str) -> str:
    """
    从模型输出中截取第一个 JSON 对象文本。

    :param output: 模型输出文本
    :return: JSON 对象文本
    :raises ValueError: 输出中没有 JSON 对象时抛出
    """
    matched = _JSON_OBJECT_PATTERN.search(output or "")
    if not matched:
        raise ValueError(f"无法从响应中提取JSON对象: {output}")
    return matched.group()
