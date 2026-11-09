"""
验证非空壳文件判定的大小、时长探测与完成标记兜底规则。
"""

from pathlib import Path

from app.application.netdisk.integrity import is_complete_file


def _write(path: Path, content: bytes = b"x") -> Path:
    """写入测试文件并返回路径。"""
    path.write_bytes(content)
    return path


def test_zero_byte_is_incomplete(tmp_path: Path) -> None:
    """
    大小为 0 的文件无论是否有完成标记都视为空壳。
    """
    target = _write(tmp_path / "empty.mkv", b"")
    target.with_name(target.name + ".done").touch()

    assert is_complete_file(target) is False


def test_probe_duration_positive_is_complete(tmp_path: Path) -> None:
    """
    ffprobe 能读出正时长时视为完整，不依赖完成标记。
    """
    target = _write(tmp_path / "video.mkv")

    assert is_complete_file(target, probe=lambda _path: 120.0) is True


def test_probe_failure_falls_back_to_done_marker(tmp_path: Path) -> None:
    """
    探测抛错时退化为检查同名 .done 完成标记。
    """
    target = _write(tmp_path / "video.mkv")
    target.with_name(target.name + ".done").touch()

    def _broken(_path: Path) -> float:
        """模拟 ffprobe 不可用。"""
        raise OSError("ffprobe missing")

    assert is_complete_file(target, probe=_broken) is True


def test_probe_failure_without_marker_is_incomplete(tmp_path: Path) -> None:
    """
    探测失败且没有完成标记时按未命中处理。
    """
    target = _write(tmp_path / "video.mkv")

    assert is_complete_file(target, probe=lambda _path: None) is False
