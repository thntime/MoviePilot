"""
验证下载后内容门：本地打分、直接否决、置信度复核、降级放行与缓存。

模型调用一律替换为替身，测试不触达真实模型与网络。
"""

from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from app.application.netdisk import content_verify, download_verify
from app.chain.transfer.workflow import TransferWorkflowOwner
from app.domain.context import MediaInfo
from app.schemas.file import FileItem
from app.schemas.types import MediaType

_MB = 1024 * 1024
_RUNTIME_MINUTES = 45
# 夹具会把模块属性替换成固定串，这里先留一份真实实现供提示词用例直接调用。
_REAL_RENDER_PROMPT = download_verify._render_prompt


def _item(name: str, size_mb: int = 800, path: str = "") -> FileItem:
    """
    构造一条整理文件项。

    :param name: 文件名
    :param size_mb: 大小（MB）
    :param path: 完整路径，空时按文件名拼下载目录
    :return: 文件项
    """
    full_path = path or f"/downloads/share/{name}"
    return FileItem(path=full_path, name=name, type="file", storage="local", size=size_mb * _MB)


def _mediainfo(title: str = "斗破苍穹", runtime: int = _RUNTIME_MINUTES) -> MediaInfo:
    """
    构造目标媒体信息。

    :param title: 标题
    :param runtime: 时长（分钟）
    :return: 媒体信息
    """
    return MediaInfo(title=title, year="2020", type=MediaType.TV, runtime=runtime)


def _meta(episode: int = 0) -> SimpleNamespace:
    """
    构造元数据替身。

    :param episode: 目标集号，0 表示电影或未知
    :return: 元数据对象
    """
    return SimpleNamespace(
        name="斗破苍穹",
        year="2020",
        type=MediaType.TV,
        begin_season=1,
        begin_episode=episode,
        end_episode=episode,
    )


class _FakeModel:
    """
    记录调用次数并回放固定输出的模型替身。
    """

    def __init__(self, outputs: List[str]) -> None:
        """保存回放输出与调用记录。

        :param outputs: 依次回放的输出文本
        """
        self.outputs = list(outputs)
        self.calls = 0

    async def __call__(self, prompt: str, images: Tuple[str, ...] = ()) -> str:
        """回放一条输出。

        :param prompt: 提示词
        :param images: 图片
        :return: 输出文本
        """
        self.calls += 1
        index = min(self.calls - 1, len(self.outputs) - 1)
        return self.outputs[index]


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    固定时长探测并清空判定缓存，避免用例之间互相污染。

    :param monkeypatch: pytest 补丁工具
    """
    download_verify.clear_cache()
    monkeypatch.setattr(content_verify, "probe_duration", lambda path: float(_RUNTIME_MINUTES * 60))
    monkeypatch.setattr(
        download_verify,
        "_render_prompt",
        lambda target, evidence, score, task_name, frames=(): "PROMPT",
    )


def test_evidence_sufficient_skips_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """名字、目录、时长、集号都对上时不再调用模型。"""
    model = _FakeModel(['{"match": false, "confidence": 0.9}'])
    monkeypatch.setattr(download_verify, "_call_model", model)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(episode=1),
        file_items=[_item("斗破苍穹 第01集.mkv", path="/downloads/斗破苍穹/斗破苍穹 第01集.mkv")],
        task_name="斗破苍穹",
    )

    assert result.matched is True
    assert result.checked is True
    assert model.calls == 0


def test_model_rejection_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型明确判否且置信度足够时拦下。"""
    model = _FakeModel(['{"match": false, "confidence": 0.92, "reason": "文件名是另一部作品"}'])
    monkeypatch.setattr(download_verify, "_call_model", model)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is False
    assert result.checked is True
    assert result.confidence == pytest.approx(0.92)
    assert "另一部作品" in result.reason


def test_model_approval_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型判为符合时放行。"""
    model = _FakeModel(['{"match": true, "confidence": 0.8, "reason": "名字一致"}'])
    monkeypatch.setattr(download_verify, "_call_model", model)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is True
    assert result.checked is True


def test_invalid_json_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型输出不是合法 JSON 时放行。"""
    monkeypatch.setattr(download_verify, "_call_model", _FakeModel(["我觉得不太像"]))

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is True
    assert result.checked is False


def test_agent_unavailable_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """智能助手未运行时放行。"""

    def _raise(prompt: str, images: Tuple[str, ...] = ()) -> str:
        """模拟助手未运行。

        :param prompt: 提示词
        :param images: 图片
        :return: 不返回
        :raises RuntimeError: 始终抛出
        """
        raise RuntimeError("智能助手服务未运行")

    monkeypatch.setattr(download_verify, "_call_model", _raise)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is True
    assert result.checked is False
    assert "模型不可用" in result.reason


def test_timeout_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型超时时放行。"""

    def _timeout(prompt: str, images: Tuple[str, ...] = ()) -> str:
        """模拟模型超时。

        :param prompt: 提示词
        :param images: 图片
        :return: 不返回
        :raises TimeoutError: 始终抛出
        """
        raise TimeoutError("模型超时")

    monkeypatch.setattr(download_verify, "_call_model", _timeout)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is True
    assert result.checked is False


def test_duration_mismatch_rejects_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """时长差超过一半时直接否决，不浪费一次模型调用。"""
    model = _FakeModel(['{"match": true, "confidence": 0.9}'])
    monkeypatch.setattr(download_verify, "_call_model", model)
    monkeypatch.setattr(content_verify, "probe_duration", lambda path: 590.0)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田 01.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is False
    assert result.direct is True
    assert model.calls == 0


def test_low_confidence_rejection_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """置信度低于 0.5 时不拦，按证据不足放行。"""
    model = _FakeModel(['{"match": false, "confidence": 0.3, "reason": "看不出来"}'])
    monkeypatch.setattr(download_verify, "_call_model", model)

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is True
    assert result.checked is False


def test_medium_confidence_reviewed_by_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """中等置信度的判否先抽帧复核，复核判为符合时放行。"""
    model = _FakeModel([
        '{"match": false, "confidence": 0.6, "reason": "名字不像"}',
        '{"match": true, "confidence": 0.9, "reason": "画面是正片"}',
    ])
    monkeypatch.setattr(download_verify, "_call_model", model)
    monkeypatch.setattr(content_verify, "extract_frames", lambda paths: ("/tmp/frame.jpg",))

    result = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert result.matched is True
    assert model.calls == 2


def test_verdict_is_cached_per_file_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一批文件的结论在缓存有效期内只问一次模型。"""
    model = _FakeModel(['{"match": false, "confidence": 0.95, "reason": "另一部作品"}'])
    monkeypatch.setattr(download_verify, "_call_model", model)
    items = [_item("启运丹田.mkv")]

    first = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(), meta=_meta(), file_items=items, task_name="启运丹田"
    )
    second = download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(), meta=_meta(), file_items=items, task_name="启运丹田"
    )

    assert first.matched is False
    assert second.matched is False
    assert model.calls == 1


def test_manual_transfer_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """手动整理不调模型也不拦。"""

    def _boom(**kwargs: Any) -> Any:
        """被调用即失败。

        :param kwargs: 任意参数
        :return: 不返回
        :raises AssertionError: 始终抛出
        """
        raise AssertionError("手动整理不应触发内容校验")

    monkeypatch.setattr(
        "app.chain.transfer.workflow.verify_downloaded_content", _boom
    )

    error = TransferWorkflowOwner._verify_downloaded_content(
        None,
        fileitem=_item("启运丹田.mkv"),
        file_items=[_item("启运丹田.mkv")],
        meta=_meta(),
        mediainfo=_mediainfo(),
        batch_mtype=MediaType.TV,
        manual=True,
        preview=False,
        download_hash="h1",
    )

    assert error is None


def test_preview_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """预览模式不触发内容校验。"""

    def _boom(**kwargs: Any) -> Any:
        """被调用即失败。

        :param kwargs: 任意参数
        :return: 不返回
        :raises AssertionError: 始终抛出
        """
        raise AssertionError("预览不应触发内容校验")

    monkeypatch.setattr(
        "app.chain.transfer.workflow.verify_downloaded_content", _boom
    )

    error = TransferWorkflowOwner._verify_downloaded_content(
        None,
        fileitem=_item("启运丹田.mkv"),
        file_items=[_item("启运丹田.mkv")],
        meta=_meta(),
        mediainfo=_mediainfo(),
        batch_mtype=MediaType.TV,
        manual=False,
        preview=True,
        download_hash="h1",
    )

    assert error is None


def test_rejection_writes_pending_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """判否时写一条待处理记录，供内容审核列表处理。"""
    saved: List[Any] = []

    class _Repository:
        """记录判否写入的内存仓库。"""

        def save(self, record: Any) -> Any:
            """保存一条记录。

            :param record: 待处理记录
            :return: 保存后的记录
            """
            saved.append(record)
            return record

        def list(self, decision: Any = None) -> List[Any]:
            """列出记录。

            :param decision: 处理状态
            :return: 记录列表
            """
            return list(saved)

        def get(self, record_id: str) -> Any:
            """按标识读取记录。

            :param record_id: 记录标识
            :return: 记录
            """
            return next((item for item in saved if item.id == record_id), None)

        def update_decision(self, record_id: str, decision: str) -> Any:
            """更新处理状态。

            :param record_id: 记录标识
            :param decision: 新状态
            :return: 更新后的记录
            """
            record = self.get(record_id)
            if record is not None:
                record.decision = decision
            return record

    repository = _Repository()
    monkeypatch.setattr(content_verify, "get_content_verify_store", lambda: repository)
    model = _FakeModel(['{"match": false, "confidence": 0.95, "reason": "文件名是另一部作品"}'])
    monkeypatch.setattr(download_verify, "_call_model", model)

    error = TransferWorkflowOwner._verify_downloaded_content(
        None,
        fileitem=_item("启运丹田.mkv"),
        file_items=[_item("启运丹田.mkv")],
        meta=_meta(),
        mediainfo=_mediainfo(),
        batch_mtype=MediaType.TV,
        manual=False,
        preview=False,
        download_hash="h1",
    )

    assert error is not None and "内容校验不通过" in error
    records = repository.list()
    assert len(records) == 1
    assert records[0].reason == "文件名是另一部作品"
    assert records[0].decision == content_verify.DECISION_REJECT
    assert records[0].title == "斗破苍穹"


def test_transfer_workflow_stops_on_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实整理编排在门 2 判否时返回失败，不进入规划。"""
    from app.chain.transfer.facade import TransferChain

    chain = TransferChain()
    item = _item("启运丹田.mkv")
    monkeypatch.setattr(chain, "_collect_transfer_candidates", lambda **kwargs: ([item], False))
    monkeypatch.setattr(chain, "_filter_manual_transfer_history", lambda items, skip, record: items)
    monkeypatch.setattr(chain, "_filter_automatic_pending_transfers", lambda items, flag, record: items)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.verify_downloaded_content",
        lambda **kwargs: SimpleNamespace(
            matched=False,
            checked=True,
            reason="文件名是另一部作品",
            score=3,
            confidence=0.95,
            frames=(),
            hits=("时长",),
            direct=False,
        ),
    )
    monkeypatch.setattr("app.chain.transfer.workflow.record_rejection", lambda **kwargs: None)

    success, message = chain._run_transfer_workflow(
        fileitem=item,
        meta=_meta(),
        mediainfo=_mediainfo(),
        mtype=MediaType.TV,
        manual=False,
        preview=False,
        download_hash="h1",
        background=False,
    )

    assert success is False
    assert "内容校验不通过" in str(message)


def test_frames_are_sent_to_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """抽帧图要作为图片随提示词一起提交给模型，不能只落盘存证。"""
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xd9")
    captured: List[Tuple[str, ...]] = []

    async def _model(prompt: str, images: Tuple[str, ...] = ()) -> str:
        """记录收到的图片并回放判定。

        :param prompt: 提示词
        :param images: 图片
        :return: 模型输出
        """
        captured.append(tuple(images))
        return '{"match": true, "confidence": 0.9, "reason": "画面是正片"}'

    monkeypatch.setattr(download_verify, "_call_model", _model)
    monkeypatch.setattr(content_verify, "extract_frames", lambda paths: (str(frame),))

    download_verify.verify_downloaded_content(
        mediainfo=_mediainfo(),
        meta=_meta(),
        file_items=[_item("启运丹田.mkv")],
        task_name="启运丹田",
    )

    assert captured, "模型没有被调用"
    assert captured[0], "模型没有收到图片"
    assert captured[0][0].startswith("data:image/jpeg;base64,")


def test_prompt_reports_attached_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """提示词要告诉模型附带了几张截图。"""
    from app.agent.prompt import prompt_manager
    from app.application.netdisk.content_verify import build_evidence, build_target, score_evidence

    monkeypatch.setattr("app.application.agent.get_prompt_manager", lambda: prompt_manager)
    target = build_target(_mediainfo(), _meta())
    evidence = build_evidence([_item("启运丹田.mkv")])
    score = score_evidence(target, evidence, task_name="启运丹田")
    prompt = _REAL_RENDER_PROMPT(target, evidence, score, "启运丹田", ("a.jpg", "b.jpg"))

    assert "2 still frames" in prompt
