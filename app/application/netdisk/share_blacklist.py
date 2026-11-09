"""
网盘分享黑名单存储。

按平台与分享码记录被判定为不可用（例如仅预览流）的分享，带过期时间；
数据以 JSON 文件保存在配置目录，读写均先清理过期条目，写回使用原子替换。
本模块只做本地文件存取，不访问网络或数据库。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.foundation.environment import get_config_path
from app.runtime.log import logger

# 黑名单文件结构版本；结构变化时递增并做兼容读取。
_FORMAT_VERSION = 1

# 默认封禁天数。
_DEFAULT_DAYS = 7


@dataclass(frozen=True)
class ShareBlacklistEntry:
    """
    一条分享黑名单记录。

    :param platform: 网盘平台标识
    :param share_code: 分享码
    :param reason: 加入黑名单的原因
    :param created_at: 写入时间（UTC ISO 字符串）
    :param expires_at: 过期时间（UTC ISO 字符串）
    """

    platform: str
    share_code: str
    reason: str
    created_at: str
    expires_at: str


def _now() -> datetime:
    """
    返回当前 UTC 时间。
    """
    return datetime.now(timezone.utc)


def _key(platform: str, share_code: str) -> str:
    """
    拼接条目在文件中的唯一键。
    """
    return f"{platform}:{share_code}"


def _parse_time(value: str) -> Optional[datetime]:
    """
    解析 ISO 时间字符串，失败返回 None。

    :param value: 待解析的时间字符串
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ShareBlacklistStore:
    """
    基于单个 JSON 文件的分享黑名单存储，进程内以锁保证读写一致。

    :param path: 黑名单文件路径
    """

    def __init__(self, path: Path) -> None:
        """
        保存文件路径并初始化锁。
        """
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """
        返回黑名单文件路径。
        """
        return self._path

    def is_blacklisted(self, platform: str, share_code: str) -> Optional[ShareBlacklistEntry]:
        """
        查询分享是否仍在黑名单有效期内。

        :param platform: 网盘平台标识
        :param share_code: 分享码
        :return: 命中的条目；未命中或已过期返回 None
        """
        with self._lock:
            entries = self._load_pruned()
            return entries.get(_key(platform, share_code))

    def add(
        self, platform: str, share_code: str, reason: str, days: int = _DEFAULT_DAYS
    ) -> ShareBlacklistEntry:
        """
        新增或覆盖一条黑名单记录。

        :param platform: 网盘平台标识
        :param share_code: 分享码
        :param reason: 加入黑名单的原因
        :param days: 有效天数，0 表示立即过期
        :return: 写入的条目
        """
        created = _now()
        entry = ShareBlacklistEntry(
            platform=platform,
            share_code=share_code,
            reason=reason,
            created_at=created.isoformat(),
            expires_at=(created + timedelta(days=days)).isoformat(),
        )
        with self._lock:
            entries = self._load_pruned()
            entries[_key(platform, share_code)] = entry
            self._save(entries)
        return entry

    def remove(self, platform: str, share_code: str) -> bool:
        """
        移除一条黑名单记录。

        :param platform: 网盘平台标识
        :param share_code: 分享码
        :return: 是否确实移除了条目
        """
        with self._lock:
            entries = self._load_pruned()
            removed = entries.pop(_key(platform, share_code), None)
            if removed is None:
                return False
            self._save(entries)
            return True

    def prune(self) -> int:
        """
        清理所有已过期条目并写回。

        :return: 被清理的条目数
        """
        with self._lock:
            entries, removed = self._load_and_prune()
            if removed:
                self._save(entries)
            return removed

    def list_entries(self) -> List[ShareBlacklistEntry]:
        """
        列出当前有效的黑名单条目。

        :return: 未过期条目列表
        """
        with self._lock:
            return list(self._load_pruned().values())

    def _load_pruned(self) -> Dict[str, ShareBlacklistEntry]:
        """
        读取并清理过期条目，必要时写回。

        :return: 有效条目字典
        """
        entries, removed = self._load_and_prune()
        if removed:
            self._save(entries)
        return entries

    def _load_and_prune(self) -> tuple[Dict[str, ShareBlacklistEntry], int]:
        """
        读取文件并分离出过期条目，不写回。

        :return: 有效条目字典与过期条目数
        """
        entries = self._load()
        now = _now()
        alive: Dict[str, ShareBlacklistEntry] = {}
        expired = 0
        for key, entry in entries.items():
            expires_at = _parse_time(entry.expires_at)
            if expires_at is None or expires_at <= now:
                expired += 1
                continue
            alive[key] = entry
        return alive, expired

    def _load(self) -> Dict[str, ShareBlacklistEntry]:
        """
        读取黑名单文件；不存在或损坏时返回空表并记录告警。

        :return: 文件中解析出的条目字典
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            logger.warning(f"读取网盘分享黑名单失败，按空表处理：{error}")
            return {}
        try:
            data = json.loads(raw)
        except ValueError as error:
            logger.warning(f"网盘分享黑名单文件损坏，按空表处理：{error}")
            return {}
        raw_entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(raw_entries, dict):
            logger.warning("网盘分享黑名单结构异常，按空表处理")
            return {}
        entries: Dict[str, ShareBlacklistEntry] = {}
        for key, value in raw_entries.items():
            entry = self._parse_entry(value)
            if entry is None:
                logger.warning(f"网盘分享黑名单条目 {key} 字段异常，已忽略")
                continue
            entries[key] = entry
        return entries

    @staticmethod
    def _parse_entry(value: object) -> Optional[ShareBlacklistEntry]:
        """
        把文件中的条目对象转换为 dataclass。

        :param value: 文件中的原始条目
        """
        if not isinstance(value, dict):
            return None
        fields = ("platform", "share_code", "reason", "created_at", "expires_at")
        if any(not isinstance(value.get(field), str) for field in fields):
            return None
        return ShareBlacklistEntry(**{field: value[field] for field in fields})

    def _save(self, entries: Dict[str, ShareBlacklistEntry]) -> None:
        """
        以原子替换方式写回黑名单文件。

        :param entries: 待写入的条目字典
        """
        payload = {
            "version": _FORMAT_VERSION,
            "entries": {key: asdict(entry) for key, entry in entries.items()},
        }
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(directory), prefix=f".{self._path.name}.", delete=False
        )
        try:
            with handle as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, self._path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise


_store: Optional[ShareBlacklistStore] = None
_store_lock = threading.Lock()


def get_share_blacklist() -> ShareBlacklistStore:
    """
    返回进程内共享的分享黑名单存储，文件位于配置目录。

    :return: 黑名单存储单例
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            _store = ShareBlacklistStore(get_config_path() / "netdisk_share_blacklist.json")
        return _store
