"""
下载后内容校验：调用后台模型判断下载内容是否属于目标影视作品。

流程：本地证据打分（content_verify）→ 得分够高直接放行 → 否则请模型判一次 →
判否时按置信度决定直接拦还是抽帧复核。模型不可用、超时或输出非法时一律放行，
门 2 不能成为整理链的硬依赖。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.application.netdisk import content_verify
from app.application.netdisk.content_verify import (
    FileEvidence,
    ScoreResult,
    TargetMedia,
    VerifyRecord,
)
from app.runtime.log import logger

# 后台系统任务类型，提示词定义在 System Tasks.yaml。
SYSTEM_TASK = "transfer_content_verify"
# 后台会话前缀，便于在会话列表里区分内容校验任务。
SESSION_PREFIX = "__agent_transfer_verify"
# 单次模型调用硬超时。
TIMEOUT_SECONDS = 30.0
# 同一批文件的判定结论缓存时长。
CACHE_TTL_SECONDS = 600.0
# 直接判否所需置信度。
STRONG_REJECT_CONFIDENCE = 0.8
# 拦下来的最低置信度。
REJECT_CONFIDENCE = 0.7
# 低于该置信度视为模型在猜，不拦。
LOW_CONFIDENCE = 0.5
# 模型可能带解释文本，只截取第一个 JSON 对象。
_JSON_OBJECT_PATTERN = re.compile(r"\{.*}", re.DOTALL)
# 判定结论缓存：键为（目录, 文件名集合），值为（过期时间, 结论）。
_VERDICT_CACHE: Dict[Tuple[str, Tuple[str, ...]], Tuple[float, "VerifyResult"]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """
    一次内容校验的结论。

    :param matched: 是否判定为目标影视内容
    :param checked: 是否真的执行了校验（未执行时按放行处理）
    :param reason: 判否或跳过原因
    :param score: 本地证据得分
    :param confidence: 模型置信度
    :param frames: 抽帧图路径
    :param hits: 命中的本地证据
    :param direct: 是否由本地证据直接否决，未调用模型
    """

    matched: bool = True
    checked: bool = False
    reason: str = ""
    score: int = 0
    confidence: float = 0.0
    frames: Tuple[str, ...] = ()
    hits: Tuple[str, ...] = ()
    direct: bool = False


def verify_downloaded_content(
    *,
    mediainfo: Any = None,
    meta: Any = None,
    file_items: Sequence[Any] = (),
    task_name: str = "",
    storage: str = "local",
    download_hash: str = "",
    use_cache: bool = True,
) -> VerifyResult:
    """
    校验一批已下载文件是否属于目标影视作品。

    :param mediainfo: 目标媒体信息
    :param meta: 目标元数据
    :param file_items: 本次整理的源文件项
    :param task_name: 下载任务名（通常是分享标题）
    :param storage: 源存储类型
    :param download_hash: 下载任务标识
    :param use_cache: 是否复用同批文件的既有结论
    :return: 校验结论；任何异常都返回放行
    """
    target = content_verify.build_target(mediainfo, meta)
    probe = content_verify.probe_duration if storage == "local" else None
    evidence = content_verify.build_evidence(file_items, storage, probe=probe)
    if not evidence:
        return VerifyResult(matched=True, checked=False, reason="没有可校验的文件")
    if not _has_target(target):
        return VerifyResult(matched=True, checked=False, reason="缺少目标媒体信息")
    cache_key = _cache_key(evidence)
    if use_cache:
        cached = _read_cache(cache_key)
        if cached is not None:
            return cached
    score = content_verify.score_evidence(target, evidence, task_name)
    if score.reject_reason:
        result = VerifyResult(
            matched=False,
            checked=True,
            reason=score.reject_reason,
            score=score.score,
            confidence=1.0,
            hits=score.hits,
            direct=True,
        )
        _write_cache(cache_key, result)
        return result
    policy = content_verify.frame_policy(score)
    if policy == "skip":
        result = VerifyResult(
            matched=True, checked=True, reason="本地证据充分", score=score.score, hits=score.hits
        )
        _write_cache(cache_key, result)
        return result
    frames: Tuple[str, ...] = ()
    if policy == "frames":
        frames = content_verify.extract_frames([item.path for item in evidence])
    verdict = _judge(target, evidence, score, task_name, frames)
    if verdict is None:
        return VerifyResult(
            matched=True, checked=False, reason="模型不可用，已放行", score=score.score, hits=score.hits
        )
    matched, confidence, reason = verdict
    if matched:
        result = VerifyResult(
            matched=True,
            checked=True,
            reason=reason,
            score=score.score,
            confidence=confidence,
            frames=frames,
            hits=score.hits,
        )
        _write_cache(cache_key, result)
        return result
    if confidence >= STRONG_REJECT_CONFIDENCE:
        rejected = _reject(
            result_score=score, confidence=confidence, reason=reason, frames=frames
        )
        _write_cache(cache_key, rejected)
        return rejected
    if confidence < LOW_CONFIDENCE:
        logger.info(f"内容校验置信度 {confidence:.2f} 过低，按证据不足放行：{reason}")
        return VerifyResult(
            matched=True,
            checked=False,
            reason=f"置信度不足已放行：{reason}",
            score=score.score,
            confidence=confidence,
            hits=score.hits,
        )
    # 中等置信度的判否先抽帧复核，复核仍判否且够把握才拦。
    if not frames:
        frames = content_verify.extract_frames([item.path for item in evidence])
    if frames:
        review = _judge(target, evidence, score, task_name, frames)
        if review is not None:
            matched, confidence, reason = review
            if matched:
                logger.info(f"抽帧复核推翻文本判否，已放行：{reason}")
                result = VerifyResult(
                    matched=True,
                    checked=True,
                    reason=f"抽帧复核放行：{reason}",
                    score=score.score,
                    confidence=confidence,
                    frames=frames,
                    hits=score.hits,
                )
                _write_cache(cache_key, result)
                return result
    if confidence >= REJECT_CONFIDENCE:
        return _reject(result_score=score, confidence=confidence, reason=reason, frames=frames)
    return VerifyResult(
        matched=True,
        checked=False,
        reason=f"置信度不足已放行：{reason}",
        score=score.score,
        confidence=confidence,
        frames=frames,
        hits=score.hits,
    )


def record_rejection(
    *,
    file_items: Sequence[Any],
    result: VerifyResult,
    target: Optional[TargetMedia] = None,
    src: str = "",
    storage: str = "local",
    title: str = "",
    media_type: str = "",
    season: str = "",
    episodes: str = "",
    platform: str = "",
    share_code: str = "",
    download_hash: str = "",
) -> Optional[VerifyRecord]:
    """
    把一次判否写成待处理记录，供「内容审核」列表展示与处理。

    :param file_items: 本次整理的源文件项
    :param result: 校验结论
    :param target: 目标媒体
    :param src: 源路径
    :param storage: 源存储
    :param title: 媒体标题
    :param media_type: 媒体类型
    :param season: 季号
    :param episodes: 集号文本
    :param platform: 网盘平台
    :param share_code: 分享码
    :param download_hash: 下载任务标识
    :return: 写入的记录；失败时返回 None
    """
    sizes = [int(getattr(item, "size", 0) or 0) for item in file_items]
    durations = [int(getattr(item, "duration", 0) or 0) for item in file_items]
    durations = [value for value in durations if value > 0]
    record = VerifyRecord(
        src=str(src or ""),
        storage=str(storage or "local"),
        title=str(title or (target.title if target else "")),
        media_type=str(media_type or ""),
        season=str(season or ""),
        episodes=str(episodes or ""),
        score=int(result.score),
        confidence=float(result.confidence),
        reason=str(result.reason or ""),
        hits=list(result.hits),
        frames=list(result.frames),
        file_count=len(list(file_items)),
        total_size=sum(sizes),
        median_duration=int(sorted(durations)[len(durations) // 2]) if durations else 0,
        platform=str(platform or ""),
        share_code=str(share_code or ""),
        download_hash=str(download_hash or ""),
    )
    try:
        return content_verify.get_content_verify_store().save(record)
    except Exception as error:  # noqa: BLE001 - 记录失败不阻断判否
        logger.warning(f"写入内容校验记录失败：{type(error).__name__}: {error}")
        return None


def _reject(
    *,
    result_score: ScoreResult,
    confidence: float,
    reason: str,
    frames: Tuple[str, ...],
) -> VerifyResult:
    """
    构造判否结论。

    :param result_score: 本地打分
    :param confidence: 置信度
    :param reason: 判否原因
    :param frames: 抽帧图
    :return: 判否结论
    """
    return VerifyResult(
        matched=False,
        checked=True,
        reason=reason or "内容与目标影视不符",
        score=result_score.score,
        confidence=confidence,
        frames=frames,
        hits=result_score.hits,
    )


def _has_target(target: TargetMedia) -> bool:
    """
    判断目标媒体是否足够用于校验。

    :param target: 目标媒体
    :return: 有标题或时长返回 True
    """
    return bool(target.title or target.original_title or target.names or target.runtime_minutes)


def _cache_key(evidence: Sequence[FileEvidence]) -> Tuple[str, Tuple[str, ...]]:
    """
    构造判定缓存的键：目录加文件名集合。

    :param evidence: 文件证据
    :return: 缓存键
    """
    directory = ""
    if evidence:
        path = str(evidence[0].path or "").replace("\\", "/")
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return directory, tuple(sorted(item.name for item in evidence))


def _read_cache(key: Tuple[str, Tuple[str, ...]]) -> Optional[VerifyResult]:
    """
    读取未过期的判定结论。

    :param key: 缓存键
    :return: 结论；无缓存或已过期时返回 None
    """
    with _CACHE_LOCK:
        item = _VERDICT_CACHE.get(key)
        if item is None:
            return None
        expire_at, result = item
        if expire_at < time.monotonic():
            _VERDICT_CACHE.pop(key, None)
            return None
        return result


def _write_cache(key: Tuple[str, Tuple[str, ...]], result: VerifyResult) -> None:
    """
    写入判定结论缓存。

    :param key: 缓存键
    :param result: 结论
    """
    with _CACHE_LOCK:
        _VERDICT_CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)


def clear_cache() -> None:
    """
    清空判定结论缓存，供测试与配置变更后使用。
    """
    with _CACHE_LOCK:
        _VERDICT_CACHE.clear()


def _judge(
    target: TargetMedia,
    evidence: Sequence[FileEvidence],
    score: ScoreResult,
    task_name: str,
    frames: Sequence[str] = (),
) -> Optional[Tuple[bool, float, str]]:
    """
    请模型判一次，返回（是否符合, 置信度, 原因）。

    :param target: 目标媒体
    :param evidence: 文件证据
    :param score: 本地打分
    :param task_name: 下载任务名
    :param frames: 抽帧图路径，非空时随提示词一起提交
    :return: 判定结果；模型不可用或输出非法时返回 None
    """
    try:
        prompt = _render_prompt(target, evidence, score, task_name, frames)
        output = _call_model_sync(prompt, frames)
        return _parse_verdict(output)
    except Exception as error:  # noqa: BLE001 - 模型失败一律放行
        logger.warning(f"内容校验模型调用失败，已放行：{type(error).__name__}: {error}")
        return None


def _render_prompt(
    target: TargetMedia,
    evidence: Sequence[FileEvidence],
    score: ScoreResult,
    task_name: str,
    frames: Sequence[str] = (),
) -> str:
    """
    渲染后台系统任务提示词。

    :param target: 目标媒体
    :param evidence: 文件证据
    :param score: 本地打分
    :param task_name: 下载任务名
    :param frames: 抽帧图路径，用于告诉模型附带了几张截图
    :return: 完整提示词
    """
    from app.application.agent import get_prompt_manager

    files = [
        {
            "name": item.name,
            "path": item.path,
            "size_mb": round(item.size / 1024 / 1024, 1) if item.size else 0,
            "duration_seconds": item.duration,
        }
        for item in list(evidence)[:40]
    ]
    context = {
        "target_media": target.display(),
        "task_name": task_name or "未知",
        "download_files": json.dumps(files, ensure_ascii=False),
        "evidence_summary": f"本地得分 {score.score}/{content_verify.SCORE_TOTAL}，命中 {','.join(score.hits) or '无'}",
        "frame_count": len(list(frames)),
    }
    return get_prompt_manager().render_system_task_message(SYSTEM_TASK, template_context=context)


def _call_model_sync(prompt: str, frames: Sequence[str] = ()) -> str:
    """
    同步调用后台模型；已有事件循环时放到独立线程执行。

    :param prompt: 提示词
    :param frames: 抽帧图路径
    :return: 模型输出文本
    """
    images = _frame_images(frames)

    def _factory() -> Any:
        """
        构造模型调用协程。

        :return: 协程
        """
        return _call_model(prompt, images)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(asyncio.wait_for(_factory(), timeout=TIMEOUT_SECONDS))
    holder: Dict[str, str] = {}

    def _runner() -> None:
        """
        在独立线程里执行一次异步调用。
        """
        holder["text"] = asyncio.run(asyncio.wait_for(_factory(), timeout=TIMEOUT_SECONDS))

    thread = threading.Thread(target=_runner, daemon=True, name="content-verify-llm")
    thread.start()
    thread.join(TIMEOUT_SECONDS + 5)
    if thread.is_alive():
        raise TimeoutError("内容校验模型调用超时")
    return holder.get("text", "")


async def _call_model(prompt: str, images: Sequence[str] = ()) -> str:
    """
    通过后台 Agent 执行一次内容校验提示词。

    :param prompt: 提示词
    :param images: 抽帧图 data URI 列表
    :return: 模型输出文本
    :raises RuntimeError: 智能助手服务未运行
    """
    from app.application.agent import get_running_agent_manager
    from app.foundation.identity import SYSTEM_INTERNAL_USER_ID
    from app.runtime.settings import get_runtime_setting
    from app.schemas.types import ReplyMode

    manager = get_running_agent_manager()
    if manager is None:
        raise RuntimeError("智能助手服务未运行")
    output = [""]

    def on_output(text: str) -> None:
        """
        保存模型最新输出。

        :param text: 输出文本
        """
        output[0] = text

    session_id = f"{SESSION_PREFIX}_{uuid.uuid4().hex[:8]}__"
    try:
        await manager.process_message(
            session_id=session_id,
            user_id=SYSTEM_INTERNAL_USER_ID,
            message=prompt,
            images=list(images) or None,
            channel=None,
            source=None,
            username=get_runtime_setting("SUPERUSER"),
            reply_mode=ReplyMode.CAPTURE_ONLY,
            output_callback=on_output,
            allow_message_tools=False,
            allow_tools=False,
            wait_for_completion=True,
        )
    finally:
        await manager.clear_session(session_id=session_id, user_id=SYSTEM_INTERNAL_USER_ID)
    return output[0].strip()


def _frame_images(frames: Sequence[str]) -> Tuple[str, ...]:
    """
    把抽帧图转成 data URI，供模型多模态输入。

    :param frames: 图片路径
    :return: data URI 列表；读取失败时为空
    """
    images: List[str] = []
    for path in list(frames)[:4]:
        try:
            raw = Path(path).read_bytes()
        except OSError:
            continue
        images.append("data:image/jpeg;base64," + _base64(raw))
    return tuple(images)


def _base64(raw: bytes) -> str:
    """
    返回字节的 base64 文本。

    :param raw: 原始字节
    :return: base64 文本
    """
    import base64

    return base64.b64encode(raw).decode("ascii")


def _parse_verdict(output: str) -> Tuple[bool, float, str]:
    """
    解析模型输出为（是否符合, 置信度, 原因）。

    :param output: 模型输出文本
    :return: 判定结果
    :raises ValueError: 输出中没有合法 JSON 对象时抛出
    """
    matched = _JSON_OBJECT_PATTERN.search(output or "")
    if not matched:
        raise ValueError(f"无法从响应中提取 JSON 对象: {output}")
    payload = json.loads(matched.group())
    if not isinstance(payload, dict) or "match" not in payload:
        raise ValueError(f"模型返回缺少 match 字段: {output}")
    raw_match = payload.get("match")
    if isinstance(raw_match, str):
        matched_flag = raw_match.strip().lower() in {"true", "yes", "1", "是"}
    else:
        matched_flag = bool(raw_match)
    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    reason = str(payload.get("reason") or "").strip()
    return matched_flag, confidence, reason
