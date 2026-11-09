"""
验证网盘分享黑名单的写入、命中、过期与清理。
"""

from pathlib import Path

from app.application.netdisk.share_blacklist import ShareBlacklistStore
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, is_share_level_error


def test_blacklist_add_and_hit(tmp_path: Path) -> None:
    """
    新增条目后按平台与分享码命中，其他分享码不命中。
    """
    store = ShareBlacklistStore(tmp_path / "blacklist.json")
    entry = store.add("uc", "abc", "直链不完整")
    assert entry.platform == "uc"
    assert store.is_blacklisted("uc", "abc") is not None
    assert store.is_blacklisted("uc", "other") is None


def test_blacklist_expires_and_prunes(tmp_path: Path) -> None:
    """
    days=0 的条目立即过期，读取与列表都不再返回。
    """
    store = ShareBlacklistStore(tmp_path / "blacklist.json")
    store.add("uc", "abc", "x", days=0)
    assert store.is_blacklisted("uc", "abc") is None
    assert store.list_entries() == []


def test_share_level_error_classification() -> None:
    """
    只有分享失效与分享内容不可用算分享级错误，其余错误不拉黑分享。
    """
    assert is_share_level_error(NetdiskError(NetdiskErrorCode.SHARE_INVALID, "分享已失效"))
    assert is_share_level_error(NetdiskError(NetdiskErrorCode.UNSUPPORTED, "UC 直链不完整（1/2）"))
    # 迅雷/百度把「分享无直链」归为 UNKNOWN，仍需按消息片段识别为分享级失败。
    assert is_share_level_error(NetdiskError(NetdiskErrorCode.UNKNOWN, "迅雷云盘未返回可用下载链接"))
    assert not is_share_level_error(NetdiskError(NetdiskErrorCode.UNKNOWN, "网络不可达"))
    assert not is_share_level_error(NetdiskError(NetdiskErrorCode.SPACE_INSUFFICIENT, "空间不足"))


def test_share_level_error_unwraps_cause() -> None:
    """
    被 DownloadError 等外层异常包装时，仍应沿异常链识别分享级错误。
    """
    inner = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "仅预览流")
    try:
        try:
            raise inner
        except NetdiskError as cause:
            raise RuntimeError("取链失败") from cause
    except RuntimeError as wrapped:
        assert is_share_level_error(wrapped)
