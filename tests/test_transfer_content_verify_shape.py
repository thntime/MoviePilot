"""
验证内容门在真实整理链路形状下能看见文件。

真实链路传给门的是「文件项 + 是否蓝光目录」二元组列表；本文件按该形状喂入，
断言证据不再为空、提示词带上真实分享标题、抽帧只取视频。
"""

from types import MethodType, SimpleNamespace
from typing import Any, List

import pytest

from app.application.netdisk import content_verify, download_verify
from app.chain.transfer.workflow import TransferWorkflowOwner
from app.domain.context import MediaInfo
from app.schemas.file import FileItem
from app.schemas.types import MediaType

_MB = 1024 * 1024
_REAL_RENDER_PROMPT = download_verify._render_prompt
_SHARE_TITLE = "仙逆 4K 国漫 年番 更新167集 S01E01-E194"
_SHARE_CODE = "b442f632b23c4"
_LINGWU_NAME = "灵武大陆.S01E179.2160p.25fps.WEB-DL.H265.10bit.DDP2.0.3Audios.mp4"
_EPISODE_SECONDS = 24 * 60


def _item(name: str, path: str, size_mb: int = 500) -> FileItem:
    """
    构造一条整理文件项。

    :param name: 文件名
    :param path: 完整路径
    :param size_mb: 大小（MB）
    :return: 文件项
    """
    return FileItem(
        path=path, name=name, type="file", storage="local", size=size_mb * _MB
    )


def _mediainfo() -> MediaInfo:
    """
    构造目标媒体信息（《仙逆》，单集 24 分钟）。

    :return: 媒体信息
    """
    return MediaInfo(title="仙逆", year="2023", type=MediaType.TV, runtime=24)


def _owner(share_title: str = _SHARE_TITLE) -> SimpleNamespace:
    """
    构造只提供下载历史仓库的内容门宿主替身。

    :param share_title: 下载历史里的分享标题
    :return: 宿主替身
    """
    owner = SimpleNamespace(
        download_history_repository=SimpleNamespace(
            get_by_hash=lambda _hash: SimpleNamespace(torrent_name=share_title)
        )
    )
    owner._share_title_for_verify = MethodType(  # noqa: SLF001 - 复刻真实绑定
        TransferWorkflowOwner._share_title_for_verify, owner
    )
    return owner


class _Repository:
    """收集写入记录的内存仓库。"""

    def __init__(self) -> None:
        """初始化空仓库。"""
        self.saved: List[Any] = []

    def save(self, record: Any) -> Any:
        """
        保存一条记录。

        :param record: 待写入记录
        :return: 保存后的记录
        """
        self.saved.append(record)
        return record

    def list(self, decision: Any = None) -> List[Any]:
        """
        列出记录。

        :param decision: 处理状态
        :return: 记录列表
        """
        return list(self.saved)

    def get(self, record_id: Any) -> Any:
        """
        按标识读取记录。

        :param record_id: 记录标识
        :return: 记录
        """
        return next((item for item in self.saved if item.id == record_id), None)

    def update_decision(self, record_id: Any, decision: str) -> Any:
        """
        更新处理状态。

        :param record_id: 记录标识
        :param decision: 新状态
        :return: 更新后的记录
        """
        record = self.get(record_id)
        if record is not None:
            record.decision = decision
        return record


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    固定时长探测、清空判定缓存并固定提示词。

    :param monkeypatch: pytest 补丁工具
    """
    download_verify.clear_cache()
    monkeypatch.setattr(content_verify, "probe_duration", lambda path: float(_EPISODE_SECONDS))
    monkeypatch.setattr(
        download_verify,
        "_render_prompt",
        lambda target, evidence, score, task_name, frames=(): "PROMPT",
    )


def test_gate_sees_files_behind_candidate_tuples(monkeypatch: pytest.MonkeyPatch) -> None:
    """按真实链路形状（二元组列表）喂入时，门能看到文件大小且得分大于 0。"""
    repository = _Repository()
    monkeypatch.setattr(content_verify, "get_content_verify_store", lambda: repository)

    async def _model(prompt: str, images: Any = ()) -> str:
        """
        回放一次「判为符合」的模型输出。

        :param prompt: 提示词
        :param images: 图片
        :return: 模型输出
        """
        return '{"match": true, "confidence": 0.9, "reason": "分享标题是目标作品"}'

    monkeypatch.setattr(download_verify, "_call_model", _model)

    share_dir = f"/media/downloads/动漫/国漫/{_SHARE_CODE}"
    item = _item(_LINGWU_NAME, f"{share_dir}/{_LINGWU_NAME}")

    error = TransferWorkflowOwner._verify_downloaded_content(
        None,
        fileitem=_item(_SHARE_CODE, f"{share_dir}/"),
        file_items=[(item, False)],
        meta=None,
        mediainfo=_mediainfo(),
        batch_mtype=MediaType.TV,
        manual=False,
        preview=False,
        download_hash="a708c397c8fde2c4",
    )

    assert error is None
    assert repository.saved, "门没有写审核记录"
    record = repository.saved[0]
    assert record.total_size > 0, "门仍然看不到文件大小"
    assert record.file_count == 1
    assert record.score > 0, "本地证据得分仍为 0"
    assert record.decision == content_verify.DECISION_PASS


def test_share_title_comes_from_download_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """门的任务名取下载历史里的分享标题，而不是整理目录的分享码。"""
    owner = _owner()

    assert owner._share_title_for_verify("a708c397c8fde2c4") == _SHARE_TITLE

    from app.agent.prompt import prompt_manager
    from app.application.netdisk.content_verify import build_evidence, build_target, score_evidence

    monkeypatch.setattr("app.application.agent.get_prompt_manager", lambda: prompt_manager)
    target = build_target(_mediainfo(), None)
    evidence = build_evidence([_item(_LINGWU_NAME, f"/downloads/share/{_LINGWU_NAME}")])
    title = owner._share_title_for_verify("a708c397c8fde2c4")
    score = score_evidence(target, evidence, task_name=title)
    prompt = _REAL_RENDER_PROMPT(target, evidence, score, title)

    assert "仙逆 4K 国漫 年番" in prompt
    assert _SHARE_CODE not in prompt


def test_extract_frames_only_takes_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """字幕排在视频前面时，抽帧仍然只对视频动手。"""
    subtitle = tmp_path / "字幕.srt"
    subtitle.write_text("1", encoding="utf-8")
    video = tmp_path / f"{_LINGWU_NAME}"
    video.write_bytes(b"0")
    grabbed: List[Any] = []

    def _grab(ffmpeg: str, path: Any, position: float, output: Any) -> bool:
        """
        记录被抽帧的文件并伪造一张图。

        :param ffmpeg: ffmpeg 路径
        :param path: 被抽帧的文件
        :param position: 截帧位置
        :param output: 输出文件
        :return: 固定返回成功
        """
        grabbed.append(path)
        output.write_bytes(b"\xff\xd8\xff\xd9")
        return True

    monkeypatch.setattr(content_verify.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(content_verify, "_frame_root", lambda: tmp_path / "frames")
    monkeypatch.setattr(content_verify, "_grab_frame", _grab)

    frames = content_verify.extract_frames([str(subtitle), str(video)])

    assert frames, "没有抽到任何帧"
    assert grabbed, "没有对任何文件抽帧"
    assert all(path == video for path in grabbed), "对非视频文件也抽了帧"


def _verify_once(monkeypatch: pytest.MonkeyPatch, probe: Any, task_name: str) -> Any:
    """
    用给定时长探测与任务名跑一次内容校验。

    :param monkeypatch: pytest 补丁工具
    :param probe: 时长探测替身
    :param task_name: 下载任务名
    :return: 校验结论
    """
    monkeypatch.setattr(content_verify, "probe_duration", probe)
    monkeypatch.setattr(content_verify, "extract_frames", lambda paths, limit=3: ())
    download_verify.clear_cache()

    async def _model(prompt: str, images: Any = ()) -> str:
        """
        回放一次低置信度的「判为符合」。

        :param prompt: 提示词
        :param images: 图片
        :return: 模型输出
        """
        return '{"match": true, "confidence": 0.2, "reason": "没有可用的文件信息或截图"}'

    monkeypatch.setattr(download_verify, "_call_model", _model)
    item = _item("第01集.mkv", "/downloads/share/第01集.mkv")
    return download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=None,
        file_items=[item],
        task_name=task_name,
    )


def test_zero_evidence_is_recorded_as_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地零证据且没抽到帧时记「未判定」，不再冒充已通过。"""
    repository = _Repository()
    monkeypatch.setattr(content_verify, "get_content_verify_store", lambda: repository)

    result = _verify_once(monkeypatch, lambda path: None, "b442f632b23c4")

    assert result.matched is True
    assert result.checked is False, "零证据仍被记成已判定"
    assert result.reason.startswith("无有效证据")

    download_verify.record_for_result(
        file_items=[_item("第01集.mkv", "/downloads/share/第01集.mkv")], result=result
    )
    assert repository.saved[0].decision == content_verify.DECISION_SKIPPED


def test_evidence_present_is_still_recorded_as_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地证据命中时仍然记「已通过」。"""
    repository = _Repository()
    monkeypatch.setattr(content_verify, "get_content_verify_store", lambda: repository)

    result = _verify_once(monkeypatch, lambda path: float(_EPISODE_SECONDS), "仙逆")

    assert result.score > 0
    assert result.checked is True

    download_verify.record_for_result(
        file_items=[_item("第01集.mkv", "/downloads/share/第01集.mkv")], result=result
    )
    assert repository.saved[0].decision == content_verify.DECISION_PASS