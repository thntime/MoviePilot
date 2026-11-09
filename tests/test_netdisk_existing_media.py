"""
验证网盘个人盘已有完整文件查询的命中、缺集与预算行为。
"""

import pytest

from app.application.netdisk import existing
from app.domain.netdisk import DriveFile


@pytest.fixture(autouse=True)
def _clear_cache():
    """
    每个用例前清空模块级短 TTL 缓存，避免用例间串扰。
    """
    existing._CACHE.clear()
    yield
    existing._CACHE.clear()


class _FakeFilesService:
    """
    用内存目录树替代真实网盘浏览端口。
    """

    def __init__(self, trees):
        """保存账号到目录树的映射。"""
        self.trees = trees
        self.calls = []

    def list_dir(self, account_id: int, parent_id: str = "") -> list[DriveFile]:
        """按账号与父目录返回预置条目。"""
        self.calls.append((account_id, parent_id))
        return list(self.trees.get(account_id, {}).get(parent_id, []))


def _file(name: str, file_id: str = "", size: int = 100) -> DriveFile:
    """构造普通文件条目。"""
    return DriveFile(file_id=file_id or name, name=name, size=size)


def _dir(name: str, file_id: str) -> DriveFile:
    """构造目录条目。"""
    return DriveFile(file_id=file_id, name=name, is_dir=True)


def _install(monkeypatch: pytest.MonkeyPatch, trees) -> _FakeFilesService:
    """把假浏览端口注入被测模块。"""
    service = _FakeFilesService(trees)
    monkeypatch.setattr(existing, "NetdiskFilesService", lambda: service)
    return service


def test_find_hit_in_first_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    第一个账号存在覆盖全部目标集数的文件时直接命中，不再查询后续账号。
    """
    trees = {
        1: {
            "": [_dir("Example Show", "d1")],
            "d1": [
                _file("Example.Show.S01E01.mkv"),
                _file("Example.Show.S01E02.mkv"),
                _file("Example.Show.S01E03.mkv"),
            ],
        },
        2: {
            "": [_dir("Example Show", "d2")],
            "d2": [_file("Example.Show.S01E01.mkv")],
        },
    }
    service = _install(monkeypatch, trees)

    hit = existing.find_complete_media(
        [(1, "主账号"), (2, "备用")], "Example Show", 1, {1, 2, 3},
    )

    assert hit is not None
    assert hit.account_id == 1
    assert {item.name for item in hit.files} == {
        "Example.Show.S01E01.mkv",
        "Example.Show.S01E02.mkv",
        "Example.Show.S01E03.mkv",
    }
    assert all(account_id == 1 for account_id, _ in service.calls)


def test_find_miss_when_episode_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    目标集数未全部出现时按未命中处理。
    """
    trees = {
        1: {
            "": [_dir("Example Show", "d1")],
            "d1": [
                _file("Example.Show.S01E01.mkv"),
                _file("Example.Show.S01E02.mkv"),
            ],
        },
    }
    _install(monkeypatch, trees)

    assert existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1, 2, 3},
    ) is None


def test_non_video_and_empty_files_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    非视频文件与零字节文件不能制造「已有完整文件」命中。
    """
    trees = {
        1: {
            "": [_dir("Example Show", "d1")],
            "d1": [
                _file("Example.Show.S01E01.txt"),
                _file("Example.Show.S01E02.mkv", size=0),
            ],
        },
    }
    _install(monkeypatch, trees)

    assert existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1, 2},
    ) is None


def test_unknown_size_is_allowed_with_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    平台未返回大小时仍允许命中，但记录 debug 便于排查。
    """
    trees = {
        1: {
            "": [_dir("Example Show", "d1")],
            "d1": [
                DriveFile(
                    file_id="f1", name="Example.Show.S01E01.mkv", size=None,
                ),
            ],
        },
    }
    _install(monkeypatch, trees)
    debug_messages = []
    monkeypatch.setattr(
        existing.logger,
        "debug",
        lambda message, *args, **kwargs: debug_messages.append(str(message)),
    )

    hit = existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1},
    )

    assert hit is not None
    assert any("缺少大小" in message for message in debug_messages)


def test_directory_with_conflicting_season_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    目录名带其他季号时即使标题匹配也不下钻，避免复用错季文件。
    """
    trees = {
        1: {
            "": [_dir("Example Show S02", "d2")],
            "d2": [_file("Example.Show.S02E01.mkv")],
        },
    }
    _install(monkeypatch, trees)

    assert existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1},
    ) is None


def test_budget_exceeded_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    目录数超过预算时立即按未命中处理并记录超预算日志。
    """
    root = [_dir(f"Example Show {index:02d}", f"d{index}") for index in range(51)]
    trees = {1: {"": root}}
    for index in range(51):
        trees[1][f"d{index}"] = []
    _install(monkeypatch, trees)
    warnings = []
    # 项目 logger 使用独立 console logger（propagate=False），caplog 抓不到，
    # 这里直接记录告警调用，避免依赖 stderr 初始化状态。
    monkeypatch.setattr(
        existing.logger,
        "warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )

    hit = existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1},
    )

    assert hit is None
    assert any("超出预算" in message for message in warnings)


def test_same_title_season_different_episodes_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    同标题同季不同目标集数不能复用上一次的命中结果。
    """
    trees = {
        1: {
            "": [_dir("Example Show", "d1")],
            "d1": [
                _file("Example.Show.S01E01.mkv"),
                _file("Example.Show.S01E02.mkv"),
            ],
        },
    }
    _install(monkeypatch, trees)

    first = existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1},
    )
    second = existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {2},
    )

    assert first is not None
    assert [item.name for item in first.files] == ["Example.Show.S01E01.mkv"]
    assert second is not None
    assert [item.name for item in second.files] == ["Example.Show.S01E02.mkv"]


def test_negative_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    未命中结果也进入短 TTL 缓存，避免连续对账重复扫盘。
    """
    trees = {
        1: {
            "": [_dir("Example Show", "d1")],
            "d1": [_file("Example.Show.S01E01.mkv")],
        },
    }
    service = _install(monkeypatch, trees)

    assert existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {2},
    ) is None
    calls = len(service.calls)
    assert existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {2},
    ) is None
    assert len(service.calls) == calls
