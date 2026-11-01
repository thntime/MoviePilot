"""
夸克下载器的落盘命名与分享列表翻页测试。
"""

from typing import Any, Dict, List

from app.modules.quark import QuarkModule
from app.modules.quark.quark import QuarkClient


def test_episode_name_prefixes_bare_episode() -> None:
    """裸集数文件名补 S01Exx 前缀，整理阶段才能识别。"""
    assert QuarkModule._episode_name("164 4K.mkv") == "S01E164 164 4K.mkv"
    assert QuarkModule._episode_name("01~4K.mp4") == "S01E001 01~4K.mp4"


def test_episode_name_keeps_existing_marker() -> None:
    """已带季集标记的文件名不再加前缀。"""
    assert QuarkModule._episode_name("仙逆 S01E164 4K.mkv") == "仙逆 S01E164 4K.mkv"


def test_episode_name_keeps_ambiguous_name() -> None:
    """候选不唯一或特典文件保持原名，绝不猜测。"""
    assert QuarkModule._episode_name("1917 2019 1080p.mkv") == "1917 2019 1080p.mkv"
    assert QuarkModule._episode_name("花絮.mp4") == "花絮.mp4"


def test_share_files_pages_until_no_new_entry() -> None:
    """分享列表按 fid 去重翻页，整页无新条目即结束。"""
    pages: Dict[str, List[Dict[str, Any]]] = {
        "1": [
            {"fid": "a", "file_name": "01~4K.mp4", "size": 10, "dir": False},
            {"fid": "b", "file_name": "02~4K.mp4", "size": 11, "dir": False},
        ],
        "2": [
            {"fid": "c", "file_name": "03~4K.mp4", "size": 12, "dir": False},
        ],
        "3": [
            {"fid": "c", "file_name": "03~4K.mp4", "size": 12, "dir": False},
        ],
    }
    client = QuarkClient(cookie="c=1")
    calls: List[str] = []

    def fake_get(url: str, params: Any, action: str) -> Dict[str, Any]:
        page = str((params or {}).get("_page"))
        calls.append(page)
        return {"status": 200, "code": 0, "data": {"list": pages.get(page, [])}}

    client._get = fake_get  # type: ignore[assignment]
    entries = client.share_files("code", "stoken")

    assert [item["fid"] for item in entries] == ["a", "b", "c"]
    assert calls == ["1", "2", "3"]


def test_share_files_keeps_duration_and_size() -> None:
    """分享条目保留文件大小与时长，供候选展示与校验使用。"""
    client = QuarkClient(cookie="c=1")

    def fake_get(url: str, params: Any, action: str) -> Dict[str, Any]:
        return {
            "status": 200,
            "code": 0,
            "data": {
                "list": [
                    {
                        "fid": "a",
                        "file_name": "01~4K.mp4",
                        "size": 1024,
                        "duration": 1500,
                        "dir": False,
                    }
                ]
            },
        }

    client._get = fake_get  # type: ignore[assignment]
    entries = client.share_files("code", "stoken")

    assert entries[0]["size"] == 1024
    assert entries[0]["duration"] == 1500


def test_transfer_fallback_cleans_temp_dir(monkeypatch, tmp_path) -> None:
    """走转存回退时，下载结束后必须删除网盘上的临时转存目录。"""
    import threading
    from pathlib import Path
    from unittest.mock import Mock

    from app.modules.quark.engine import ChunkedDownloader
    from app.modules.quark.quark import QuarkError

    module = QuarkModule.__new__(QuarkModule)
    client = Mock()
    client.cookie = "c=1"
    client.threads = 4
    client.rate_limit = 0
    client.preempt = False
    client.share_token = Mock(return_value="st")
    client.share_link = Mock(side_effect=QuarkError("免转存失败"))
    client.delete_files = Mock()

    monkeypatch.setattr(
        QuarkModule, "_transfer_link",
        staticmethod(lambda *args, **kwargs: (
            {"url": "http://example.invalid/a.mp4", "size": 10}, "tmp_dir_fid"
        )),
    )
    monkeypatch.setattr(ChunkedDownloader, "run", lambda self, provider: None)

    module._download_one(
        client, "code", "", {"name": "a.mp4", "size": 10, "fid": "f", "fid_token": "t"},
        "st", Path(tmp_path), "task", threading.Event(), "", 10, 0.0,
    )

    client.delete_files.assert_called_once_with(["tmp_dir_fid"])


def _files(*names: str) -> list[dict]:
    """构造分享文件条目。"""
    return [{"name": name, "size": 1, "fid": name, "fid_token": "t", "is_dir": False} for name in names]


def test_filter_episodes_keeps_only_target() -> None:
    """指定目标集数时只保留对应文件。"""
    kept = QuarkModule._filter_episodes(
        _files("162 4K.mp4", "163 4K.mp4", "164 4K.mp4"), {164}
    )

    assert [item["name"] for item in kept] == ["164 4K.mp4"]


def test_filter_episodes_without_target_keeps_all() -> None:
    """没有目标集数时整包下载，行为与旧版一致。"""
    files = _files("162 4K.mp4", "163 4K.mp4")

    assert QuarkModule._filter_episodes(files, None) == files
    assert QuarkModule._filter_episodes(files, set()) == files


def test_filter_episodes_keeps_ambiguous_names() -> None:
    """文件名解析不出唯一集数时保留，宁可多下也不漏集；特典文件跳过。"""
    kept = QuarkModule._filter_episodes(
        _files("1917 2019 1080p.mkv", "花絮.mp4", "164 4K.mkv"), {164}
    )

    assert [item["name"] for item in kept] == ["1917 2019 1080p.mkv", "164 4K.mkv"]


def test_filter_episodes_skips_when_multiple_directories() -> None:
    """根层多个子目录时无法确认目标集位置，放弃过滤整包下载。"""
    files = _files("164 4K.mkv")

    assert QuarkModule._filter_episodes(files, {999}, skip_filter=True) == files


def test_filter_episodes_runs_before_rename() -> None:
    """过滤必须发生在补 S01Exx 前缀之前，否则原本解析不出的文件会被误判。"""
    kept = QuarkModule._filter_episodes(_files("164 4K.mkv"), {153})

    assert kept == []
