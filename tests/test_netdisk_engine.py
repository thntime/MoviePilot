"""
验证通用分片下载引擎的规划、续传、校验、多源轮换与直链刷新。
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.domain.netdisk import DirectLink
from app.modules.netdisk import engine


class FakeStreamResponse:
    """
    模拟 206 流式响应。
    """

    def __init__(self, data: bytes, status_code: int = 206, content_range: str = "",
                 content_type: str = "video/mp4") -> None:
        """
        保存响应体与头部。
        """
        self._data = data
        self.status_code = status_code
        self.headers = {
            "Content-Range": content_range,
            "Content-Type": content_type,
        }
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        """
        按块返回响应体。
        """
        for start in range(0, len(self._data), chunk_size):
            yield self._data[start:start + chunk_size]

    def close(self) -> None:
        """
        标记响应已关闭。
        """
        self.closed = True


class FakeHttp:
    """
    按 Range 头返回对应字节的假网络适配器。

    ``status_by_url`` 可为特定地址指定状态码，用于模拟直链过期。
    """

    def __init__(self, payload: bytes, status_code: int = 206,
                 content_type: str = "video/mp4",
                 status_by_url: Optional[Dict[str, int]] = None,
                 truncate_first_full: int = 0,
                 fail_status: int = 0, fail_times: int = 0) -> None:
        """
        保存完整内容与响应行为。

        :param truncate_first_full: 首次 200 整文件响应只返回前 N 字节，模拟 CDN 提前断开；
                                    之后的 Range 请求按 206 正常返回。
        """
        self.payload = payload
        self.status_code = status_code
        self.content_type = content_type
        self.status_by_url = dict(status_by_url or {})
        self.calls: List[Dict[str, Any]] = []
        self._truncate = truncate_first_full
        self._fail_status = fail_status
        self._fail_times = fail_times

    def get_stream(self, url: str, headers: Optional[Dict[str, str]] = None, **_: Any):
        """
        返回按 Range 截取的响应上下文。
        """
        headers = dict(headers or {})
        range_header = str(headers.get("Range") or "")
        self.calls.append({"url": url, "headers": headers, "range": range_header})
        if self._fail_times > 0:
            self._fail_times -= 1
            status = self._fail_status
            data = b""
            content_range = ""

            class _FailCtx:
                """返回失败状态的上下文。"""

                def __enter__(self_inner):
                    """返回响应对象。"""
                    return FakeStreamResponse(data, status, content_range, "video/mp4")

                def __exit__(self_inner, *args):
                    """结束时不做额外处理。"""
                    return False

            return _FailCtx()
        status = self.status_by_url.get(url, self.status_code)
        if status == 200 and self._truncate:
            data = self.payload[:self._truncate]
            self._truncate = 0
            content_range = ""
        elif status == 200 and range_header:
            start_text, end_text = range_header.replace("bytes=", "").split("-")
            start = int(start_text)
            end = int(end_text) if end_text else len(self.payload) - 1
            data = self.payload[start:end + 1]
            status = 206
            content_range = f"bytes {start}-{end}/{len(self.payload)}"
        elif status == 200:
            data = self.payload
            content_range = ""
        elif status in (401, 403, 412):
            data = b"<html>expired</html>"
            content_range = ""
        else:
            start_text, end_text = range_header.replace("bytes=", "").split("-")
            start, end = int(start_text), int(end_text)
            data = self.payload[start:end + 1]
            content_range = f"bytes {start}-{end}/{len(self.payload)}"

        class _Ctx:
            """最小上下文管理器。"""

            def __enter__(self_inner):
                """返回响应对象。"""
                return FakeStreamResponse(data, status, content_range, self.content_type)

            def __exit__(self_inner, *args):
                """结束时不做额外处理。"""
                return False

        return _Ctx()

    @property
    def urls(self) -> List[str]:
        """
        按调用顺序返回请求地址。
        """
        return [call["url"] for call in self.calls]


def install_http(monkeypatch: pytest.MonkeyPatch, http: FakeHttp) -> None:
    """
    用假网络适配器替换引擎内的 RequestUtils。
    """
    monkeypatch.setattr(engine, "RequestUtils", lambda **kwargs: http)


def _links(*urls: str) -> List[DirectLink]:
    """
    构造带独立请求头的直链集合。
    """
    return [
        DirectLink(url=url, headers={"Cookie": f"cookie-{index}"})
        for index, url in enumerate(urls)
    ]


def test_plan_chunk_count_follows_formula() -> None:
    """
    片数按大小档位、线程数与单片下限共同决定，小文件不分片。
    """
    assert engine.plan_chunk_count(4 * 1024 * 1024, 16) == 1
    assert engine.plan_chunk_count(40 * 1024 * 1024, 2) == 16
    assert engine.plan_chunk_count(40 * 1024 * 1024, 16) == 128
    assert engine.plan_chunk_count(600 * 1024 * 1024, 16) == 128
    assert engine.plan_chunk_count(200 * 1024 * 1024 * 1024, 512) == 512


def test_plan_chunks_covers_main_pool_contiguously() -> None:
    """
    主池区间必须连续且覆盖约 70% 字节。
    """
    total = 100 * 1024 * 1024
    chunks = engine.plan_chunks(total, 16)
    assert chunks[0][0] == 0
    for previous, current in zip(chunks, chunks[1:]):
        assert current[0] == previous[1] + 1
    assert chunks[-1][1] < total
    assert chunks[-1][1] >= int(total * engine.MAIN_POOL_RATIO) - engine.MIN_CHUNK_SIZE


def test_download_writes_full_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    分片下载应写出与源一致的完整文件，并清理计划文件。
    """
    payload = bytes(range(256)) * 4096  # 1MB
    http = FakeHttp(payload)
    install_http(monkeypatch, http)
    target = tmp_path / "video.mp4"

    downloader = engine.ChunkedDownloader(target, len(payload), _links("https://cdn/file"), threads=4)
    downloader.run(lambda refresh=False: _links("https://cdn/file"))

    assert target.read_bytes() == payload
    assert not downloader.plan_path.exists()
    assert all(call["range"].startswith("bytes=") for call in http.calls)


def test_download_requests_bypass_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    网盘 CDN 直链必须显式直连，不能被宿主的 HTTP_PROXY/HTTPS_PROXY 接管。
    """
    payload = bytes(range(256)) * 4096  # 1MB
    http = FakeHttp(payload)
    seen: List[Dict[str, Any]] = []

    def fake_request_utils(**kwargs: Any) -> FakeHttp:
        """记录构造参数并返回假网络适配器。"""
        seen.append(kwargs)
        return http

    monkeypatch.setattr(engine, "RequestUtils", fake_request_utils)
    target = tmp_path / "video.mp4"

    downloader = engine.ChunkedDownloader(target, len(payload), _links("https://cdn/file"), threads=2)
    downloader.run(lambda refresh=False: _links("https://cdn/file"))

    assert seen
    assert all(call.get("proxies") == {"http": "", "https": ""} for call in seen)


def test_multi_source_rotates_links_and_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    多源模式下分片按序号轮换直链，每片使用所属源的请求头。
    """
    payload = b"m" * (6 * 1024 * 1024)
    http = FakeHttp(payload)
    install_http(monkeypatch, http)
    links = _links("https://cdn/a", "https://cdn/b")

    engine.ChunkedDownloader(tmp_path / "video.mp4", len(payload), links, threads=4).run(
        lambda refresh=False: links
    )

    assert set(http.urls) == {"https://cdn/a", "https://cdn/b"}
    for call in http.calls:
        expected = "cookie-0" if call["url"].endswith("/a") else "cookie-1"
        assert call["headers"]["Cookie"] == expected


def test_link_expired_refreshes_and_continues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    直链过期时刷新取链，并用新直链继续未完成分片。
    """
    payload = b"r" * (2 * 1024 * 1024)
    old_url, new_url = "https://cdn/old", "https://cdn/new"
    http = FakeHttp(payload, status_by_url={old_url: 403})
    install_http(monkeypatch, http)
    refreshes: List[bool] = []

    def provider(refresh: bool = False):
        """首次返回旧直链，刷新后返回新直链。"""
        refreshes.append(bool(refresh))
        return _links(new_url) if refresh else _links(old_url)

    downloader = engine.ChunkedDownloader(
        tmp_path / "video.mp4", len(payload), _links(old_url), threads=1,
        refresh_seconds=0,
    )
    downloader.run(provider)

    assert target_bytes(tmp_path / "video.mp4") == payload
    assert True in refreshes
    assert new_url in http.urls


def target_bytes(path: Path) -> bytes:
    """
    读取目标文件内容。
    """
    return path.read_bytes()


def test_download_resumes_from_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    已有计划与部分数据时只补缺失区间。
    """
    payload = b"abcdefghij" * 1000
    http = FakeHttp(payload)
    install_http(monkeypatch, http)
    target = tmp_path / "video.mp4"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    plan = {"total": len(payload), "chunks": 1, "done": [[0, len(payload) - 1, digest]]}
    target.with_suffix(target.suffix + ".plan.json").write_text(json.dumps(plan), encoding="utf-8")

    downloader = engine.ChunkedDownloader(target, len(payload), _links("https://cdn/file"), threads=2)
    downloader.run(lambda refresh=False: _links("https://cdn/file"))

    # 计划标记为全部完成时不再发起任何 Range 请求。
    assert http.calls == []


def test_range_ignored_falls_back_to_single_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    服务端忽略 Range 返回 200 时整任务回退单流，文件内容仍正确。
    """
    payload = b"x" * (6 * 1024 * 1024)
    install_http(monkeypatch, FakeHttp(payload, status_code=200))
    target = tmp_path / "video.mp4"

    engine.ChunkedDownloader(target, len(payload), _links("https://cdn/file"), threads=2).run(
        lambda refresh=False: _links("https://cdn/file")
    )

    assert target.read_bytes() == payload


def test_html_response_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    直链返回 HTML 页面时判失败，不落盘。
    """
    payload = b"<html>forbidden</html>"
    install_http(monkeypatch, FakeHttp(payload, content_type="text/html; charset=utf-8"))
    target = tmp_path / "video.mp4"

    with pytest.raises(engine.DownloadError):
        engine.ChunkedDownloader(
            target, len(payload), _links("https://cdn/file"), threads=2, retries=1,
            refresh_seconds=0,
        ).run(lambda refresh=False: _links("https://cdn/file"))


def test_partial_chunk_resume_seeds_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    片内部分完成时，续传前必须把磁盘已有字节喂回摘要，否则整体校验必失败。
    """
    payload = bytes(range(256)) * 8192  # 2MB
    http = FakeHttp(payload)
    install_http(monkeypatch, http)
    target = tmp_path / "video.mp4"
    half = len(payload) // 2
    target.write_bytes(payload[:half])
    plan = {
        "total": len(payload),
        "chunks": 1,
        "done": [[0, half - 1, hashlib.sha256(payload[:half]).hexdigest()]],
    }
    target.with_suffix(target.suffix + ".plan.json").write_text(json.dumps(plan), encoding="utf-8")

    engine.ChunkedDownloader(target, len(payload), _links("https://cdn/file"), threads=1).run(
        lambda refresh=False: _links("https://cdn/file")
    )

    assert target.read_bytes() == payload
    assert [call["range"] for call in http.calls] == [f"bytes={half}-{len(payload) - 1}"]


def test_stale_plan_size_resets_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    计划与文件大小不符时重置旧数据，按新大小重新下载。
    """
    payload = b"z" * (2 * 1024 * 1024)
    install_http(monkeypatch, FakeHttp(payload))
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x" * (3 * 1024 * 1024))
    target.with_suffix(target.suffix + ".plan.json").write_text(
        json.dumps({"total": 3 * 1024 * 1024, "chunks": 1, "done": []}), encoding="utf-8"
    )

    engine.ChunkedDownloader(target, len(payload), _links("https://cdn/file"), threads=1).run(
        lambda refresh=False: _links("https://cdn/file")
    )

    assert target.read_bytes() == payload


def test_cancel_does_not_report_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    外部取消时必须抛取消异常，不能静默完成。
    """
    payload = b"q" * (6 * 1024 * 1024)
    install_http(monkeypatch, FakeHttp(payload))
    target = tmp_path / "video.mp4"
    stop = __import__("threading").Event()
    downloader = engine.ChunkedDownloader(
        target, len(payload), _links("https://cdn/file"), threads=1, stop_event=stop
    )

    def on_progress(percent: float) -> None:
        """收到进度后立即取消。"""
        stop.set()

    downloader.on_progress = on_progress
    with pytest.raises(engine.DownloadCancelled):
        downloader.run(lambda refresh=False: _links("https://cdn/file"))


def test_max_concurrency_caps_threads() -> None:
    """
    平台并发上限不可被线程配置突破。
    """
    downloader = engine.ChunkedDownloader(
        Path("/tmp/video.mp4"), 100, _links("https://cdn/file"),
        threads=32, max_concurrency=4,
    )
    assert downloader.threads == 4


def test_verify_detects_corruption(tmp_path: Path) -> None:
    """
    分片摘要不一致时校验失败。
    """
    target = tmp_path / "video.mp4"
    target.write_bytes(b"a" * 100)
    downloader = engine.ChunkedDownloader(target, 100, _links("https://cdn/file"), threads=1)
    downloader._chunks = [engine._Chunk(0, 0, 99)]  # pylint: disable=protected-access
    downloader._chunks[0].position = 100  # pylint: disable=protected-access
    downloader._chunks[0].expected_digest = hashlib.sha256(b"b" * 100).hexdigest()  # pylint: disable=protected-access

    with pytest.raises(engine.DownloadError):
        downloader._verify()  # pylint: disable=protected-access


def test_single_stream_short_read_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    单流回退提前断开时应按已写字节续传，而不是从头重下或直接判失败。
    """
    import os

    payload = b"x" * (6 * 1024 * 1024)
    http = FakeHttp(payload, status_code=200, truncate_first_full=2 * 1024 * 1024)
    install_http(monkeypatch, http)
    target = tmp_path / "video.mp4"
    downloader = engine.ChunkedDownloader(
        target, len(payload), _links("https://cdn/file"), threads=1, retries=3,
        refresh_seconds=0,
    )
    downloader.links = tuple(_links("https://cdn/file"))
    downloader._fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)  # pylint: disable=protected-access
    try:
        downloader._single_stream(  # pylint: disable=protected-access
            lambda refresh=False: _links("https://cdn/file")
        )
    finally:
        os.close(downloader._fd)  # pylint: disable=protected-access

    assert target.read_bytes() == payload
    assert any(call["range"] == "bytes=2097152-" for call in http.calls)


def test_preallocated_file_without_marker_is_redownloaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    中断后文件已被预分配到目标大小但没有完成标记时，必须重新下载。
    """
    payload = b"y" * (1024 * 1024)
    target = tmp_path / "video.mp4"
    target.write_bytes(b"\0" * len(payload))
    http = FakeHttp(payload)
    install_http(monkeypatch, http)

    engine.ChunkedDownloader(
        target, len(payload), _links("https://cdn/file"), threads=1, retries=2,
    ).run(lambda refresh=False: _links("https://cdn/file"))

    assert http.calls
    assert target.read_bytes() == payload
    assert target.with_name(target.name + ".done").exists()


def test_completed_file_with_marker_skips_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    完成标记存在且大小匹配时不发起任何请求。
    """
    payload = b"z" * 1024
    target = tmp_path / "video.mp4"
    target.write_bytes(payload)
    target.with_name(target.name + ".done").write_text(str(len(payload)), encoding="utf-8")
    http = FakeHttp(payload)
    install_http(monkeypatch, http)

    engine.ChunkedDownloader(
        target, len(payload), _links("https://cdn/file"), threads=1,
    ).run(lambda refresh=False: _links("https://cdn/file"))

    assert http.calls == []


def test_chunk_retries_transient_5xx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    CDN 返回 503 时退避重试，恢复后继续下载而不是整任务失败。
    """
    payload = b"q" * (1024 * 1024)
    http = FakeHttp(payload, fail_status=503, fail_times=2)
    install_http(monkeypatch, http)
    target = tmp_path / "video.mp4"

    engine.ChunkedDownloader(
        target, len(payload), _links("https://cdn/file"), threads=1, retries=1,
        refresh_seconds=0,
    ).run(lambda refresh=False: _links("https://cdn/file"))

    assert target.read_bytes() == payload
    assert len(http.calls) >= 3


def test_parse_m3u8_rejects_encrypted_playlist() -> None:
    """
    加密 HLS 无法保证产出可播放文件，必须明确失败。
    """
    text = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        "#EXTINF:10,\nseg0.ts\n"
    )

    with pytest.raises(engine.DownloadError):
        engine.parse_m3u8(text, "https://cdn/index.m3u8")
