"""
验证 AI 分享预选的作用域：仅预览端点可调用，自动订阅仍走规则过滤。
"""

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from app.domain.netdisk import ShareFile
from app.modules.netdisk import NetdiskModule

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _share_file(file_id: str, name: str) -> ShareFile:
    """
    构造分享内单个视频文件条目。

    :param file_id: 平台文件标识
    :param name: 文件名
    :return: 分享文件条目
    """
    return ShareFile(file_id=file_id, name=name, size=1024, path=name)


def test_collect_files_keeps_rule_based_selection_without_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    自动订阅的 `_collect_files` 只按目标集数过滤，不触发 AI 预选。
    """
    ai_mock = MagicMock()
    monkeypatch.setattr(
        "app.application.netdisk.ai_selection.select_share_files", ai_mock,
    )
    files: List[ShareFile] = [
        _share_file("f1", "S01E01.mkv"),
        _share_file("f2", "S01E02.mkv"),
        _share_file("f3", "S01E03.mkv"),
    ]
    module = NetdiskModule.__new__(NetdiskModule)

    selected = module._collect_files(files, episodes={1, 3}, season=1)

    assert ai_mock.called is False
    assert [item.file_id for item in selected] == ["f1", "f3"]


@pytest.mark.parametrize(
    "relative_path",
    [
        "app/modules/netdisk/__init__.py",
        "app/chain/download/batch.py",
    ],
)
def test_auto_subscribe_path_has_no_ai_selection_reference(relative_path: str) -> None:
    """
    自动订阅相关模块不得引用 AI 预选服务。
    """
    source = (_PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    assert "select_share_files" not in source
    assert "app.application.netdisk.ai_selection" not in source


def test_preview_endpoint_is_single_ai_selection_caller() -> None:
    """
    预览端点内部只调用一次 AI 预选，作为唯一调用方。
    """
    source = (_PROJECT_ROOT / "app/api/endpoints/netdisk.py").read_text(encoding="utf-8")

    assert source.count("select_share_files(") == 1
    assert "from app.application.netdisk.ai_selection import select_share_files" in source
