"""
验证内容审核四档判定：按组合矩阵定档，未知证据不得判成不可信。

四档含义：reject 完全不可信、manual 人工确认、ai AI 确认、system 系统确认。
"""

from typing import Any, List, Tuple

import pytest

from app.application.netdisk import content_verify, download_verify
from app.domain.context import MediaInfo
from app.schemas.file import FileItem
from app.schemas.types import MediaType

_MB = 1024 * 1024
_EPISODE_SECONDS = 24 * 60
_OK_REPLY = '{"match": true, "confidence": 0.9, "reason": "符合目标作品"}'


def _item(name: str, size_mb: int = 500) -> FileItem:
    """
    构造一个源文件项。

    :param name: 文件名
    :param size_mb: 大小（MB）
    :return: 文件项
    """
    return FileItem(
        path=f"/media/downloads/x/{name}",
        name=name,
        type="file",
        storage="local",
        size=size_mb * _MB,
    )


def _mediainfo() -> MediaInfo:
    """
    构造目标媒体信息《仙逆》，单集 24 分钟。

    :return: 媒体信息
    """
    return MediaInfo(title="仙逆", year="2023", type=MediaType.TV, runtime=24)


@pytest.fixture(autouse=True)
def _clean() -> None:
    """每个用例前清空判定缓存。"""
    download_verify.clear_cache()


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    files: List[FileItem],
    model_reply: str = _OK_REPLY,
    duration: Any = _EPISODE_SECONDS,
    task_name: str = "x",
) -> Tuple[Any, dict]:
    """
    按给定证据跑一次内容门。

    :param monkeypatch: pytest 补丁工具
    :param files: 源文件列表
    :param model_reply: 模型回放内容
    :param duration: 时长探测返回值；None 表示探测失败
    :param task_name: 任务名
    :return: 判定结果与模型调用计数
    """
    calls = {"model": 0}
    monkeypatch.setattr(content_verify, "probe_duration", lambda path: duration)
    monkeypatch.setattr(content_verify, "extract_frames", lambda paths, limit=3: ())
    monkeypatch.setattr(
        download_verify,
        "_render_prompt",
        lambda target, evidence, score, task_name, frames=(): "PROMPT",
    )

    async def _model(prompt: str, images: Any = ()) -> str:
        """
        回放一次模型输出并计数。

        :param prompt: 提示词
        :param images: 图片
        :return: 模型输出
        """
        calls["model"] += 1
        return model_reply

    monkeypatch.setattr(download_verify, "_call_model", _model)
    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=None,
        file_items=files,
        task_name=task_name,
        storage="local",
        download_hash="tier-probe",
        use_cache=False,
    )
    return result, calls


def test_hard_negative_lands_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    """时长与目标严重不符（121 秒假集）时落完全不可信。"""
    result, _ = _run(monkeypatch, files=[_item("仙逆.S01E179.2160p.mkv")], duration=121)
    assert result.matched is False
    assert result.tier == content_verify.TIER_REJECT


def test_system_tier_when_duration_and_name_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """文件名与时长都命中时落系统确认，且不调模型。"""
    result, calls = _run(monkeypatch, files=[_item("仙逆.S01E179.2160p.mkv")])
    assert result.matched is True
    assert result.tier == content_verify.TIER_SYSTEM
    assert calls["model"] == 0


def test_ai_tier_when_duration_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """有名字证据但时长探测失败时，交给 AI 判断并落 AI 确认。"""
    result, calls = _run(monkeypatch, files=[_item("仙逆.S01E179.2160p.mkv")], duration=None)
    assert result.matched is True
    assert result.tier == content_verify.TIER_AI
    assert calls["model"] >= 1


def test_manual_tier_for_zero_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """零证据时落人工确认，不能自动放行也不能判不可信。"""
    result, _ = _run(monkeypatch, files=[_item("第01集.mkv")], duration=None)
    assert result.tier == content_verify.TIER_MANUAL


def test_unknown_duration_never_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向验证：时长探测失败时，模型拿不准也不能判成完全不可信。"""
    result, _ = _run(
        monkeypatch,
        files=[_item("第01集.mkv")],
        duration=None,
        model_reply='{"match": false, "confidence": 0.6, "reason": "拿不准"}',
    )
    assert result.tier != content_verify.TIER_REJECT
    assert result.tier == content_verify.TIER_MANUAL
