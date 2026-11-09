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


def test_budget_exceeded_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    目录数超过预算时立即按未命中处理并记录超预算日志。
    """
    root = [_dir(f"Example Show {index:02d}", f"d{index}") for index in range(51)]
    trees = {1: {"": root}}
    for index in range(51):
        trees[1][f"d{index}"] = []
    _install(monkeypatch, trees)

    hit = existing.find_complete_media(
        [(1, "主账号")], "Example Show", 1, {1},
    )

    assert hit is None
    assert "超出预算" in capsys.readouterr().err


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
