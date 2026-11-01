"""
验证分片下载引擎的异常分支、弹性区调度、限速、看门狗与单流回退。

用假网络适配器替代真实连接，所有用例只读写临时目录。
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.modules.quark import engine


class FakeResponse:
    """
    可配置状态码、头部与响应体的流式响应替身。
    """

    def __init__(self, chunks: List[bytes], status_code: int = 206,
                 content_range: str = "bytes 0-", content_type: str = "video/mp4") -> None:
        """
        保存响应行为。
        """
        self._chunks = chunks
        self.status_code = status_code
        self.headers = {"Content-Range": content_range, "Content-Type": content_type}
        self.closed = False

    def iter_content(self, chunk_size: int = 8192) -> Iterator[bytes]:
        """
        逐块返回预设响应体。
        """
        yield from self._chunks

    def close(self) -> None:
        """
        标记响应已关闭。
        """
        self.closed = True


class FakeHttp:
    """
    按 Range 头截取完整内容的假适配器。
    """

    def __init__(self, payload: bytes) -> None:
        """
        保存完整内容。
        """
        self.payload = payload
        self.ranges: List[str] = []

    def get_stream(self, url: str, headers: Optional[Dict[str, str]] = None, **_: Any):
        """
        返回按 Range 截取的上下文管理器。
        """
        range_header = str((headers or {}).get("Range") or "")
        self.ranges.append(range_header)
        start_text, end_text = range_header.replace("bytes=", "").split("-")
        start, end = int(start_text), int(end_text)
        data = self.payload[start:end + 1]
        content_range = f"bytes {start}-{end}/{len(self.payload)}"

        class _Ctx:
            """最小上下文管理器。"""

            def __enter__(self_inner):
                """返回响应对象。"""
                return FakeResponse([data], 206, content_range)

            def __exit__(self_inner, *args):
                """不吞异常。"""
                return False

        return _Ctx()


def _open_downloader(target: Path, total: int) -> engine.ChunkedDownloader:
    """
    构造下载器并打开目标文件句柄。
    """
    downloader = engine.ChunkedDownloader(target, total, threads=1, preempt=False)
    downloader._fd = os.open(target, os.O_RDWR | os.O_CREAT)  # pylint: disable=protected-access
    return downloader


def _close_downloader(downloader: engine.ChunkedDownloader) -> None:
    """
    关闭下载器持有的文件句柄。
    """
    if downloader._fd is not None:  # pylint: disable=protected-access
        os.close(downloader._fd)  # pylint: disable=protected-access
        downloader._fd = None  # pylint: disable=protected-access


def test_chunk_size_and_done_flags() -> None:
    """分片大小与完成标记按闭区间计算。"""
    chunk = engine._Chunk(0, 10, 19)  # pylint: disable=protected-access
    assert chunk.size == 10
    assert chunk.done is False
    chunk.position = 20
    assert chunk.done is True


def test_rate_limiter_without_limit_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """不限速时不等待。"""
    slept: List[float] = []
    monkeypatch.setattr(engine.time, "sleep", slept.append)

    engine._RateLimiter(0).consume(1024 * 1024)  # pylint: disable=protected-access

    assert slept == []


def test_rate_limiter_waits_for_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """超出令牌预算时按缺口等待，单次不超过 1 秒。"""
    slept: List[float] = []
    monkeypatch.setattr(engine.time, "sleep", slept.append)
    limiter = engine._RateLimiter(1)  # pylint: disable=protected-access

    limiter.consume(4096)

    assert slept == [1.0]


def test_load_plan_rejects_corrupt_or_mismatched_plan(tmp_path: Path) -> None:
    """计划文件损坏或总大小不符时视为无计划。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"")
    downloader = engine.ChunkedDownloader(target, 10, threads=1)

    downloader.plan_path.write_text("{not json", encoding="utf-8")
    assert downloader._load_plan() == {}  # pylint: disable=protected-access

    downloader.plan_path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert downloader._load_plan() == {}  # pylint: disable=protected-access

    downloader.plan_path.write_text(json.dumps({"total": 99, "done": []}), encoding="utf-8")
    assert downloader._load_plan() == {}  # pylint: disable=protected-access


def test_save_plan_failure_is_logged(tmp_path: Path) -> None:
    """计划写入失败只记录告警，不中断下载。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"0123456789")
    downloader = engine.ChunkedDownloader(target, 10, threads=1)
    downloader._chunks = [engine._Chunk(0, 0, 9)]  # pylint: disable=protected-access
    downloader._chunks[0].position = 5  # pylint: disable=protected-access
    downloader.plan_path = tmp_path / "plan_dir"
    downloader.plan_path.mkdir()

    downloader._save_plan()  # pylint: disable=protected-access


def test_digest_range_reads_without_cached_handle(tmp_path: Path) -> None:
    """没有缓存句柄时临时打开文件计算摘要。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"abcdefghij")
    downloader = engine.ChunkedDownloader(target, 10, threads=1)

    assert downloader._digest_range(0, 3) == hashlib.sha256(b"abcd").hexdigest()  # pylint: disable=protected-access


def test_digest_range_stops_on_short_file(tmp_path: Path) -> None:
    """请求区间超出文件长度时按实际可读字节计算。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"abc")
    downloader = engine.ChunkedDownloader(target, 10, threads=1)

    assert downloader._digest_range(0, 9) == hashlib.sha256(b"abc").hexdigest()  # pylint: disable=protected-access


def test_read_range_rejects_reversed_or_closed(tmp_path: Path) -> None:
    """反向区间返回空字节，句柄关闭时抛错。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"abc")
    downloader = _open_downloader(target, 3)
    try:
        assert downloader._read_range(2, 1) == b""  # pylint: disable=protected-access
        _close_downloader(downloader)
        with pytest.raises(engine.DownloadError):
            downloader._read_range(0, 1)  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_restore_requires_open_handle(tmp_path: Path) -> None:
    """文件句柄关闭后不能恢复。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 10, threads=1)

    with pytest.raises(engine.DownloadError):
        downloader._restore()  # pylint: disable=protected-access


def test_restore_ignores_untrusted_plan_entries(tmp_path: Path) -> None:
    """越界、磁盘不足或摘要不符的计划条目都不能跳过下载。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"01234")
    downloader = _open_downloader(target, 10)
    downloader._chunks = [engine._Chunk(0, 0, 9)]  # pylint: disable=protected-access
    plan = {
        "total": 10,
        "chunks": 1,
        "done": [
            [100, 200, hashlib.sha256(b"x").hexdigest()],
            [5, 9, hashlib.sha256(b"56789").hexdigest()],
            [0, 4, hashlib.sha256(b"wrong").hexdigest()],
        ],
    }
    downloader.plan_path.write_text(json.dumps(plan), encoding="utf-8")
    try:
        downloader._restore()  # pylint: disable=protected-access
        assert downloader._chunks[0].position == 0  # pylint: disable=protected-access
        assert downloader._written == 0  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_restore_truncates_missing_tail(tmp_path: Path) -> None:
    """磁盘长度不足时先补齐到目标大小。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"01234")
    downloader = _open_downloader(target, 10)
    downloader._chunks = [engine._Chunk(0, 0, 9)]  # pylint: disable=protected-access
    try:
        downloader._restore()  # pylint: disable=protected-access
        assert target.stat().st_size == 10
    finally:
        _close_downloader(downloader)


def test_run_swallows_worker_thread_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """分片线程内的异常由 _error 记录，主流程不再重复抛出。"""
    target = tmp_path / "video.mp4"
    downloader = engine.ChunkedDownloader(target, 10, threads=1, preempt=False)

    def explode(_provider: Any) -> None:
        """模拟线程层异常逃逸。"""
        raise RuntimeError("boom")

    monkeypatch.setattr(downloader, "_run_main", explode)

    downloader.run(lambda refresh=False: "https://cdn/x")


def test_run_main_returns_when_nothing_pending(tmp_path: Path) -> None:
    """全部分片已完成时主池直接返回。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 10, threads=2)
    downloader._chunks = [engine._Chunk(0, 0, 9)]  # pylint: disable=protected-access
    downloader._chunks[0].position = 10  # pylint: disable=protected-access

    downloader._run_main(lambda: "https://cdn/x")  # pylint: disable=protected-access


def test_elastic_region_handles_empty_range(tmp_path: Path) -> None:
    """弹性区起点已到文件末尾时不做任何事。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 10, threads=1)

    downloader._run_elastic(lambda: "https://cdn/x", 10)  # pylint: disable=protected-access


def test_elastic_block_clamps_to_minimum(tmp_path: Path) -> None:
    """没有写入量时弹性块取最小值。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 10, threads=4)

    assert downloader._elastic_block() == engine.ELASTIC_MIN  # pylint: disable=protected-access


def test_download_splits_and_recurses_elastic_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """大文件走完主池后按弹性块递归补齐剩余字节。"""
    payload = bytes(range(256)) * (80 * 1024)  # 20MB
    http = FakeHttp(payload)
    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: http)
    target = tmp_path / "video.mp4"
    progress: List[float] = []

    downloader = engine.ChunkedDownloader(
        target, len(payload), threads=1, preempt=False, on_progress=progress.append,
    )
    downloader.run(lambda refresh=False: "https://cdn/x")

    assert target.read_bytes() == payload
    assert progress and progress[-1] >= 99.0
    assert len(downloader._chunks) > 5  # pylint: disable=protected-access


def test_download_chunk_retries_expired_link(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """直链 403 后刷新会话并重新取链，续传成功。"""
    monkeypatch.setattr(engine.time, "sleep", lambda _seconds: None)
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 10)
    chunk = engine._Chunk(0, 0, 9)  # pylint: disable=protected-access
    calls: List[bool] = []
    responses = [FakeResponse([], 403), FakeResponse([b"0123456789"], 206, "bytes 0-9/10")]

    def get_stream(_url: str, headers: Optional[Dict[str, str]] = None, **_kwargs: Any):
        """按序返回 403 与正常响应。"""
        response = responses.pop(0)

        class _Ctx:
            """最小上下文管理器。"""

            def __enter__(self_inner):
                """返回响应。"""
                return response

            def __exit__(self_inner, *args):
                """不吞异常。"""
                return False

        return _Ctx()

    class _Http:
        """只提供 get_stream 的假适配器。"""

        def get_stream(self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs: Any):
            """转发到按序返回响应的函数。"""
            return get_stream(url, headers, **kwargs)

    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: _Http())

    def provider(refresh: bool = False) -> str:
        """记录刷新请求。"""
        calls.append(refresh)
        return "https://cdn/x"

    try:
        downloader._download_chunk(chunk, provider)  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)

    assert chunk.done is True
    assert calls == [False, True]


def test_download_chunk_retries_transient_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """普通网络异常按次数退避重试，从断点续传。"""
    monkeypatch.setattr(engine.time, "sleep", lambda _seconds: None)
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 10)
    chunk = engine._Chunk(0, 0, 9)  # pylint: disable=protected-access
    attempts: List[int] = []

    class _Http:
        """首次抛错、之后正常返回的假适配器。"""

        def get_stream(self, _url: str, headers: Optional[Dict[str, str]] = None, **_kwargs: Any):
            """模拟一次连接中断。"""
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("reset")

            class _Ctx:
                """最小上下文管理器。"""

                def __enter__(self_inner):
                    """返回响应。"""
                    return FakeResponse([b"0123456789"], 206, "bytes 0-9/10")

                def __exit__(self_inner, *args):
                    """不吞异常。"""
                    return False

            return _Ctx()

    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: _Http())

    try:
        downloader._download_chunk(chunk, lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)

    assert chunk.done is True
    assert len(attempts) == 2


def test_download_chunk_gives_up_after_repeated_expiry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """直链反复失效超过上限时报错，避免无限刷新。"""
    monkeypatch.setattr(engine.time, "sleep", lambda _seconds: None)
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 10)
    chunk = engine._Chunk(0, 0, 9)  # pylint: disable=protected-access

    class _Http:
        """总是返回 403 的假适配器。"""

        def get_stream(self, _url: str, headers: Optional[Dict[str, str]] = None, **_kwargs: Any):
            """返回失效响应。"""

            class _Ctx:
                """最小上下文管理器。"""

                def __enter__(self_inner):
                    """返回 403。"""
                    return FakeResponse([], 403)

                def __exit__(self_inner, *args):
                    """不吞异常。"""
                    return False

            return _Ctx()

    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: _Http())

    try:
        with pytest.raises(engine.DownloadError, match="多次失效"):
            downloader._download_chunk(  # pylint: disable=protected-access
                chunk, lambda refresh=False: "https://cdn/x"
            )
    finally:
        _close_downloader(downloader)


def test_consume_rejects_bad_responses(tmp_path: Path) -> None:
    """响应缺失、HTML、鉴权失败、非 206 与区间不符都判失败。"""
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 10)
    chunk = engine._Chunk(0, 0, 9)  # pylint: disable=protected-access
    try:
        with pytest.raises(engine.DownloadError, match="网络不可达"):
            downloader._consume(chunk, None)  # pylint: disable=protected-access
        with pytest.raises(engine.LinkExpired):
            downloader._consume(chunk, FakeResponse([], 206, "bytes 0-9/10", "text/html"))  # pylint: disable=protected-access
        with pytest.raises(engine.LinkExpired):
            downloader._consume(chunk, FakeResponse([], 403))  # pylint: disable=protected-access
        with pytest.raises(engine.RangeIgnored):
            downloader._consume(chunk, FakeResponse([], 200))  # pylint: disable=protected-access
        with pytest.raises(engine.DownloadError, match="HTTP 500"):
            downloader._consume(chunk, FakeResponse([], 500))  # pylint: disable=protected-access
        with pytest.raises(engine.DownloadError, match="Content-Range 不匹配"):
            downloader._consume(chunk, FakeResponse([], 206, ""))  # pylint: disable=protected-access
        with pytest.raises(engine.DownloadError, match="Content-Range 不匹配"):
            downloader._consume(chunk, FakeResponse([], 206, "bytes 5-9/10"))  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_consume_handles_cancel_empty_and_truncation(tmp_path: Path) -> None:
    """取消立即中止，空块跳过，超长块截断到分片边界。"""
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 5)
    chunk = engine._Chunk(0, 0, 4)  # pylint: disable=protected-access
    try:
        downloader._consume(chunk, FakeResponse([b"", b"0123456789"], 206, "bytes 0-4/5"))  # pylint: disable=protected-access
        assert target.read_bytes()[:5] == b"01234"
        assert chunk.position == 5

        cancelled = engine._Chunk(1, 0, 4)  # pylint: disable=protected-access
        downloader.stop_event.set()
        with pytest.raises(engine.DownloadCancelled):
            downloader._consume(cancelled, FakeResponse([b"x"], 206, "bytes 0-4/5"))  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_consume_rejects_closed_handle_and_short_body(tmp_path: Path) -> None:
    """句柄关闭与提前结束都判失败。"""
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 10)
    chunk = engine._Chunk(0, 0, 9)  # pylint: disable=protected-access
    try:
        _close_downloader(downloader)
        with pytest.raises(engine.DownloadError, match="句柄已关闭"):
            downloader._consume(chunk, FakeResponse([b"01234"], 206, "bytes 0-9/10"))  # pylint: disable=protected-access

        downloader._fd = os.open(target, os.O_RDWR | os.O_CREAT)  # pylint: disable=protected-access
        chunk.position = 0
        with pytest.raises(engine.DownloadError, match="提前结束"):
            downloader._consume(chunk, FakeResponse([b"01234"], 206, "bytes 0-9/10"))  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def _install_stream(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    """
    让单流回退拿到指定响应。
    """

    class _Http:
        """固定返回同一响应的假适配器。"""

        def get_stream(self, _url: str, headers: Optional[Dict[str, str]] = None, **_kwargs: Any):
            """返回上下文管理器。"""

            class _Ctx:
                """最小上下文管理器。"""

                def __enter__(self_inner):
                    """返回响应。"""
                    return response

                def __exit__(self_inner, *args):
                    """不吞异常。"""
                    return False

            return _Ctx()

    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: _Http())


def test_single_stream_rejects_abnormal_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """单流回退要求 200 响应且不是 HTML。"""
    downloader = _open_downloader(tmp_path / "video.mp4", 10)
    try:
        _install_stream(monkeypatch, None)
        with pytest.raises(engine.DownloadError, match="响应异常"):
            downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access

        _install_stream(monkeypatch, FakeResponse([], 500))
        with pytest.raises(engine.DownloadError, match="响应异常"):
            downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access

        _install_stream(monkeypatch, FakeResponse([], 200, content_type="text/html"))
        with pytest.raises(engine.DownloadError, match="HTML"):
            downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_single_stream_writes_and_marks_chunks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """单流回退写满文件后把分片标记为完成并补算摘要。"""
    target = tmp_path / "video.mp4"
    downloader = _open_downloader(target, 10)
    downloader._chunks = [engine._Chunk(0, 0, 4), engine._Chunk(1, 5, 9)]  # pylint: disable=protected-access
    _install_stream(monkeypatch, FakeResponse([b"01234", b"56789"], 200, content_type="video/mp4"))

    try:
        downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access
        assert target.read_bytes() == b"0123456789"
        assert all(chunk.done for chunk in downloader._chunks)  # pylint: disable=protected-access
        assert downloader._chunks[0].expected_digest == hashlib.sha256(b"01234").hexdigest()  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_single_stream_reports_short_or_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """单流下载长度不足或中途取消都判失败。"""
    downloader = _open_downloader(tmp_path / "video.mp4", 10)
    try:
        _install_stream(monkeypatch, FakeResponse([b"01234"], 200, content_type="video/mp4"))
        with pytest.raises(engine.DownloadError, match="长度不足"):
            downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access

        downloader._written = 0  # pylint: disable=protected-access
        downloader.stop_event.set()
        _install_stream(monkeypatch, FakeResponse([b"01234"], 200, content_type="video/mp4"))
        with pytest.raises(engine.DownloadError, match="已取消"):
            downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_single_stream_wraps_unknown_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """底层异常统一包装为可展示的下载错误。"""
    downloader = _open_downloader(tmp_path / "video.mp4", 10)

    class _Http:
        """连接直接抛错的假适配器。"""

        def get_stream(self, _url: str, headers: Optional[Dict[str, str]] = None, **_kwargs: Any):
            """模拟底层连接异常。"""
            raise RuntimeError("socket closed")

    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: _Http())
    try:
        with pytest.raises(engine.DownloadError, match="socket closed"):
            downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access
    finally:
        _close_downloader(downloader)


def test_single_stream_rejects_closed_handle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """单流写入前句柄已关闭时判失败。"""
    downloader = _open_downloader(tmp_path / "video.mp4", 10)
    _close_downloader(downloader)
    _install_stream(monkeypatch, FakeResponse([b"0123456789"], 200, content_type="video/mp4"))

    with pytest.raises(engine.DownloadError, match="句柄已关闭"):
        downloader._single_stream(lambda refresh=False: "https://cdn/x")  # pylint: disable=protected-access


def test_watchdog_preempts_slow_connections(tmp_path: Path) -> None:
    """看门狗采样后关闭慢连接，并遵守每轮抢占上限。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 10, threads=4, preempt=False)

    class _OneShotEvent:
        """首次等待超时、之后立即结束的事件。"""

        def __init__(self) -> None:
            """记录等待次数。"""
            self.calls = 0

        def wait(self, _timeout: float) -> bool:
            """第一次返回 False 进入采样，第二次返回 True 退出。"""
            self.calls += 1
            return self.calls > 1

        def is_set(self) -> bool:
            """未收到停止信号。"""
            return False

    class _Closable:
        """记录关闭次数的响应替身。"""

        def __init__(self) -> None:
            """初始化关闭计数。"""
            self.closed = 0

        def close(self) -> None:
            """记录一次关闭。"""
            self.closed += 1

    responses = [_Closable() for _ in range(3)]
    chunks = [engine._Chunk(index, index, index) for index in range(3)]  # pylint: disable=protected-access
    for chunk, response in zip(chunks, responses):
        chunk.response = response
        chunk.started_at = time.monotonic() - 100
    downloader._chunks = chunks  # pylint: disable=protected-access
    downloader._done_event = _OneShotEvent()  # type: ignore[assignment]  # pylint: disable=protected-access

    downloader._watchdog()  # pylint: disable=protected-access

    # 每轮最多抢占 2 个连接，第三个保持不动。
    assert [response.closed for response in responses] == [1, 1, 0]
    assert [chunk.preempts for chunk in chunks] == [1, 1, 0]  # pylint: disable=protected-access


def test_watchdog_skips_exhausted_chunks(tmp_path: Path) -> None:
    """已达抢占上限的分片不再被关闭。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 10, threads=1, preempt=False)

    class _OneShotEvent:
        """首次等待超时、之后立即结束的事件。"""

        def __init__(self) -> None:
            """记录等待次数。"""
            self.calls = 0

        def wait(self, _timeout: float) -> bool:
            """第一次返回 False 进入采样，第二次返回 True 退出。"""
            self.calls += 1
            return self.calls > 1

        def is_set(self) -> bool:
            """未收到停止信号。"""
            return False

    chunk = engine._Chunk(0, 0, 9)  # pylint: disable=protected-access
    chunk.preempts = engine.PREEMPT_MAX_PER_CHUNK
    chunk.started_at = time.monotonic() - 100
    chunk.response = FakeResponse([])
    downloader._chunks = [chunk]  # pylint: disable=protected-access
    downloader._done_event = _OneShotEvent()  # type: ignore[assignment]  # pylint: disable=protected-access

    downloader._watchdog()  # pylint: disable=protected-access

    assert chunk.response.closed is False  # type: ignore[union-attr]


def test_verify_reports_size_mismatch_and_progress(tmp_path: Path) -> None:
    """校验先检查文件长度，再按分片回报收尾进度。"""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"01234")
    downloader = engine.ChunkedDownloader(target, 10, threads=1)
    with pytest.raises(engine.DownloadError, match="长度"):
        downloader._verify()  # pylint: disable=protected-access

    target.write_bytes(b"0123456789")
    downloader._chunks = [engine._Chunk(0, 0, 9)]  # pylint: disable=protected-access
    progress: List[float] = []
    downloader.on_progress = progress.append

    downloader._verify()  # pylint: disable=protected-access

    assert progress == [99.0]


def test_report_skips_without_callback_or_total(tmp_path: Path) -> None:
    """没有回调或总大小为 0 时不回报进度。"""
    downloader = engine.ChunkedDownloader(tmp_path / "video.mp4", 0, threads=1)
    downloader._report()  # pylint: disable=protected-access

    progress: List[float] = []
    downloader.total = 10
    downloader.on_progress = progress.append
    downloader._written = 25  # pylint: disable=protected-access
    downloader._report()  # pylint: disable=protected-access

    assert progress == [100.0]
