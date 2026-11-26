"""
验证 HLS 播放列表解析、相对地址、按序合并与断点续传。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.domain.netdisk import DirectLink
from app.modules.netdisk import engine as hls


class FakeHlsHttp:
    """
    按地址返回播放列表或分片字节的假网络适配器。
    """

    def __init__(self, playlist: str, segments: Dict[str, bytes]) -> None:
        """
        保存播放列表与分片内容。
        """
        self.playlist = playlist
        self.segments = segments
        self.calls: List[str] = []

    def get_stream(self, url: str, headers: Optional[Dict[str, str]] = None, **_: Any):
        """
        返回播放列表或分片响应。
        """
        self.calls.append(url)
        data = self.playlist.encode("utf-8") if url.endswith(".m3u8") else self.segments[url]

        class _Response:
            """最小响应替身。"""

            status_code = 200

            def __init__(self_inner) -> None:
                """固定响应头。"""
                self_inner.headers = {"Content-Type": "application/octet-stream"}

            def iter_content(self_inner, chunk_size: int = 8192):
                """按块返回内容。"""
                for start in range(0, len(data), chunk_size):
                    yield data[start:start + chunk_size]

        class _Ctx:
            """最小上下文管理器。"""

            def __enter__(self_inner):
                """返回响应。"""
                return _Response()

            def __exit__(self_inner, *args):
                """结束时不处理。"""
                return False

        return _Ctx()


def _link(url: str = "https://cdn.example.com/path/index.m3u8") -> DirectLink:
    """
    构造单一直链。
    """
    return DirectLink(url=url, headers={"Cookie": "c=1"})


def test_parse_relative_and_absolute_segments() -> None:
    """
    分片地址按播放列表地址解析，绝对地址保持不变。
    """
    text = (
        "#EXTM3U\n"
        "#EXTINF:1,\n"
        "seg0.ts\n"
        "#EXTINF:1,\n"
        "/sub/seg1.ts\n"
        "#EXTINF:1,\n"
        "https://other.example.com/seg2.ts\n"
    )

    playlist = hls.parse_m3u8(text, "https://cdn.example.com/path/index.m3u8")

    assert playlist.variant_url == ""
    assert playlist.segments == (
        "https://cdn.example.com/path/seg0.ts",
        "https://cdn.example.com/sub/seg1.ts",
        "https://other.example.com/seg2.ts",
    )


def test_parse_master_picks_highest_bandwidth() -> None:
    """
    主播放列表按 BANDWIDTH 选择最高码率的子列表。
    """
    text = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
        "low.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
        "high.m3u8\n"
    )

    playlist = hls.parse_m3u8(text, "https://cdn.example.com/path/index.m3u8")

    assert playlist.variant_url == "https://cdn.example.com/path/high.m3u8"


def test_parse_init_segment() -> None:
    """
    fMP4 的 EXT-X-MAP 初始化分片单独返回。
    """
    text = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:1,\n"
        "seg0.m4s\n"
    )

    playlist = hls.parse_m3u8(text, "https://cdn.example.com/path/index.m3u8")

    assert playlist.init_segments == ("https://cdn.example.com/path/init.mp4",)
    assert playlist.segments == ("https://cdn.example.com/path/seg0.m4s",)


def test_download_merges_segments_in_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    分片按播放列表顺序合并为单文件，并清理断点文件。
    """
    playlist = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXTINF:1,\nc.ts\n"
    segments = {
        "https://cdn.example.com/path/a.ts": b"AAA",
        "https://cdn.example.com/path/b.ts": b"BBB",
        "https://cdn.example.com/path/c.ts": b"CCC",
    }
    http = FakeHlsHttp(playlist, segments)
    monkeypatch.setattr(hls, "RequestUtils", lambda **kwargs: http)
    target = tmp_path / "video.mp4"

    downloader = hls.HlsDownloader(target, [_link()])
    downloader.run(lambda refresh=False: [_link()])

    assert target.read_bytes() == b"AAABBBCCC"
    assert not downloader.part_path.exists()
    assert not downloader.plan_path.exists()
    assert http.calls == [
        "https://cdn.example.com/path/index.m3u8",
        "https://cdn.example.com/path/a.ts",
        "https://cdn.example.com/path/b.ts",
        "https://cdn.example.com/path/c.ts",
    ]


def test_download_resumes_from_segment_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    断点记录已完成片数后，恢复时只请求剩余分片。
    """
    playlist = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXTINF:1,\nc.ts\n"
    segments = {
        "https://cdn.example.com/path/a.ts": b"AAA",
        "https://cdn.example.com/path/b.ts": b"BBB",
        "https://cdn.example.com/path/c.ts": b"CCC",
    }
    http = FakeHlsHttp(playlist, segments)
    monkeypatch.setattr(hls, "RequestUtils", lambda **kwargs: http)
    target = tmp_path / "video.mp4"
    downloader = hls.HlsDownloader(target, [_link()])
    downloader.part_path.write_bytes(b"AAA")
    downloader.plan_path.write_text(
        '{"total": 3, "done": 1, "sizes": [3]}', encoding="utf-8"
    )

    downloader.run(lambda refresh=False: [_link()])

    assert target.read_bytes() == b"AAABBBCCC"
    assert "https://cdn.example.com/path/a.ts" not in http.calls
    assert "https://cdn.example.com/path/b.ts" in http.calls
    assert "https://cdn.example.com/path/c.ts" in http.calls


def test_master_playlist_is_resolved_before_segments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    主播放列表先解析出子列表，再按子列表下载分片。
    """
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=100\n"
        "low.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=900\n"
        "high.m3u8\n"
    )
    media = "#EXTM3U\n#EXTINF:1,\nseg.ts\n"
    segments = {"https://cdn.example.com/path/seg.ts": b"DATA"}
    http = FakeHlsHttp(master, segments)
    original = http.get_stream

    def get_stream(url: str, headers=None, **kwargs):
        """主列表与媒体列表返回不同文本。"""
        if url.endswith("high.m3u8"):
            http.calls.append(url)
            return _playlist_ctx(media)
        return original(url, headers, **kwargs)

    monkeypatch.setattr(hls, "RequestUtils", lambda **kwargs: http)
    monkeypatch.setattr(http, "get_stream", get_stream)
    target = tmp_path / "video.mp4"

    hls.HlsDownloader(target, [_link()]).run(lambda refresh=False: [_link()])

    assert target.read_bytes() == b"DATA"
    assert "https://cdn.example.com/path/high.m3u8" in http.calls


def _playlist_ctx(text: str):
    """
    构造返回播放列表文本的响应上下文。
    """
    data = text.encode("utf-8")

    class _Response:
        """最小响应替身。"""

        status_code = 200

        def __init__(self_inner) -> None:
            """固定响应头。"""
            self_inner.headers = {"Content-Type": "application/vnd.apple.mpegurl"}

        def iter_content(self_inner, chunk_size: int = 8192):
            """按块返回内容。"""
            for start in range(0, len(data), chunk_size):
                yield data[start:start + chunk_size]

    class _Ctx:
        """最小上下文管理器。"""

        def __enter__(self_inner):
            """返回响应。"""
            return _Response()

        def __exit__(self_inner, *args):
            """结束时不处理。"""
            return False

    return _Ctx()
