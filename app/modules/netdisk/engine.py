"""
并发 Range 分片下载引擎。

单条 HTTP 连接会被网盘 CDN 限速，引擎把同一文件切成多段、用多条连接各取一段，
总带宽随连接数叠加。核心机制：分片规划、主池/弹性区、尾部收缩、慢连接抢占、
断点续传与四道完整性校验。

引擎只认识直链集合，不关心来源平台：分片按序号轮换直链源，每片使用所属源的
请求头；直链过期时通过 ``url_provider(refresh=True)`` 重新取链后继续未完成分片。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast
from urllib.parse import urljoin

from app.adapters.network.http import RequestUtils
from app.domain.netdisk import DirectLink
from app.runtime.log import logger

# 单片下限与片数上限。
MIN_CHUNK_SIZE = 256 * 1024
MAX_CHUNKS = 512
# 主池占比，其余为弹性区。
MAIN_POOL_RATIO = 0.7
# 弹性块大小范围与目标单 worker 耗时。
ELASTIC_MIN = 256 * 1024
ELASTIC_MAX = 4 * 1024 * 1024
ELASTIC_TARGET_SECONDS = 5.0
# 尾部收缩下限。
TAIL_MIN = 64 * 1024
# 看门狗采样周期、绝对阈值与相对阈值。
WATCHDOG_INTERVAL = 5.0
WATCHDOG_MIN_SPEED = 12 * 1024
# 抢占门槛与上限。
PREEMPT_MIN_ELAPSED = 15.0
PREEMPT_TAIL_ELAPSED = 3.0
PREEMPT_TAIL_INFLIGHT = 3
PREEMPT_MAX_PER_CHUNK = 3
PREEMPT_COOLDOWN = 10.0
PREEMPT_MAX_PER_ROUND = 2
# Range 被忽略的容忍次数，超过后整任务回退单流。
RANGE_IGNORED_TOLERANCE = 3
# 读缓冲。
READ_BUFFER = 64 * 1024
# 小于该大小不分片。
MIN_SPLIT_SIZE = 5 * 1024 * 1024


class DownloadError(Exception):
    """
    下载失败，异常消息为可直接展示的中文原因。
    """


class RangeIgnored(Exception):
    """
    服务端忽略了 Range，需要回退单流。
    """


class DownloadCancelled(Exception):
    """
    下载被外部取消。
    """


class LinkExpired(Exception):
    """
    直链失效（403/412 或返回 HTML），需要刷新会话后重新取链。
    """


def _size_based_chunks(total: int) -> int:
    """
    按文件大小给出基础片数档位。

    :param total: 文件总字节数
    :return: 基础片数
    """
    if total < 50 * 1024 * 1024:
        return 8
    if total < 500 * 1024 * 1024:
        return 32
    return 64


def plan_chunk_count(total: int, threads: int) -> int:
    """
    计算分片数量。

    公式：min(max(按大小档位, 线程数×8), 总大小/256KB, 512)；小于 5MB 不分片。

    :param total: 文件总字节数
    :param threads: 并发连接数
    :return: 分片数量
    """
    if total <= MIN_SPLIT_SIZE:
        return 1
    by_size = _size_based_chunks(total)
    by_threads = max(int(threads), 1) * 8
    by_bytes = max(total // MIN_CHUNK_SIZE, 1)
    return max(min(max(by_size, by_threads), by_bytes, MAX_CHUNKS), 1)


def plan_chunks(total: int, threads: int) -> List[Tuple[int, int]]:
    """
    生成主池分片区间（闭区间）与弹性区起点。

    主池覆盖前 70% 字节并按固定块顺序下发，剩余 30% 由弹性区按需分配。

    :param total: 文件总字节数
    :param threads: 并发连接数
    :return: 主池区间列表，最后一项的结束位置即弹性区起点
    """
    if total <= MIN_SPLIT_SIZE:
        return [(0, total - 1)]
    count = plan_chunk_count(total, threads)
    main_count = max(int(count * MAIN_POOL_RATIO), 1)
    main_bytes = max(int(total * MAIN_POOL_RATIO), MIN_CHUNK_SIZE)
    chunk_size = max(main_bytes // main_count, MIN_CHUNK_SIZE)
    chunks: List[Tuple[int, int]] = []
    start = 0
    for _ in range(main_count):
        if start >= main_bytes:
            break
        end = min(start + chunk_size - 1, total - 1)
        chunks.append((start, end))
        start = end + 1
    if not chunks:
        chunks.append((0, total - 1))
    return chunks


class _Chunk:
    """
    单个分片的运行状态。
    """

    def __init__(self, index: int, start: int, end: int) -> None:
        """
        初始化分片状态。
        """
        self.index = index
        self.start = start
        self.end = end
        self.position = start
        self.response = None
        self.preempts = 0
        self.last_preempt = 0.0
        self.started_at = time.monotonic()
        self.last_bytes = start
        self.last_at = self.started_at
        self.speed = 0.0
        self.digest = hashlib.sha256()
        self.expected_digest = ""

    @property
    def size(self) -> int:
        """
        分片总字节数。
        """
        return self.end - self.start + 1

    @property
    def done(self) -> bool:
        """
        分片是否已完成。
        """
        return self.position > self.end


class _RateLimiter:
    """
    全局限速令牌桶，作用于单个任务的所有连接。
    """

    def __init__(self, limit_bytes: int) -> None:
        """
        保存限速值。

        :param limit_bytes: 限速字节每秒，0 表示不限速
        """
        self.bytes_per_second = max(int(limit_bytes or 0), 0)
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._sent = 0

    def consume(self, size: int) -> None:
        """
        按累计字节数与限速计算需要等待的时间。
        """
        if not self.bytes_per_second:
            return
        with self._lock:
            self._sent += size
            expected = self._sent / self.bytes_per_second
            actual = time.monotonic() - self._started
        if expected > actual:
            time.sleep(min(expected - actual, 1.0))


class ChunkedDownloader:
    """
    单文件分片下载器。

    使用绝对偏移写入目标文件，配合 ``<target>.plan.json`` 记录已完成区间，
    进程重启后从磁盘真实长度恢复。分片按序号轮换 ``links`` 中的直链源。
    """

    def __init__(self, target: Path, total: int, links: Sequence[DirectLink],
                 threads: int = 8, rate_limit_bytes: int = 0,
                 preempt: bool = True, buffer_size: int = READ_BUFFER,
                 retries: int = 3, preempt_bps: int = WATCHDOG_MIN_SPEED,
                 preempt_age_seconds: float = PREEMPT_MIN_ELAPSED,
                 refresh_seconds: int = 30, max_concurrency: int = 0,
                 on_progress: Optional[Callable[[float], None]] = None,
                 stop_event: Optional[threading.Event] = None) -> None:
        """
        保存下载参数。

        :param target: 目标文件路径
        :param total: 文件总字节数
        :param links: 直链源集合，分片按序号轮换
        :param threads: 并发连接数
        :param rate_limit_bytes: 全局限速字节每秒
        :param preempt: 是否启用慢连接抢占
        :param buffer_size: 网络读缓冲大小
        :param retries: 分片重试次数
        :param preempt_bps: 慢连接判定阈值，单位字节每秒
        :param preempt_age_seconds: 慢连接判定时长
        :param refresh_seconds: 直链刷新节流，单位秒
        :param max_concurrency: 平台并发上限，0 表示不限制
        :param on_progress: 进度回调，参数为完成百分比
        :param stop_event: 外部停止信号
        """
        self.target = target
        self.total = total
        self.links: Tuple[DirectLink, ...] = tuple(links)
        self.threads = max(int(threads), 1)
        if max_concurrency > 0:
            self.threads = min(self.threads, int(max_concurrency))
        self.preempt = preempt
        self.buffer_size = max(int(buffer_size), 1024)
        self.retries = max(int(retries), 1)
        self.preempt_bps = max(int(preempt_bps), 0)
        self.preempt_age = max(float(preempt_age_seconds), 0.0)
        self.refresh_seconds = max(int(refresh_seconds), 0)
        self.on_progress = on_progress
        self.stop_event = stop_event or threading.Event()
        # 单文件结束标记：只用于收尾看门狗，不能复用外部 stop_event，
        # 否则一个文件下完后会把任务级停止信号置位，导致下一个文件立刻被判为取消。
        self._done_event = threading.Event()
        # 内部异常中止信号：只用于让本文件其余分片停下，不能传给任务级暂停判断。
        self._abort = threading.Event()
        self.plan_path = target.with_suffix(target.suffix + ".plan.json")
        self.limiter = _RateLimiter(rate_limit_bytes)
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshed_at = 0.0
        self._written = 0
        self._chunks: List[_Chunk] = []
        self._error: Optional[Exception] = None
        self._started_at = time.monotonic()
        self._fd: Optional[int] = None

    # ---- 计划与恢复 ----

    def _load_plan(self) -> Dict[str, object]:
        """
        读取分片计划；文件大小不符时视为无效。
        """
        if not self.plan_path.exists():
            return {}
        try:
            plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(plan, dict) or int(plan.get("total") or 0) != self.total:
            return {}
        return plan

    def _save_plan(self) -> None:
        """
        写回分片计划与已完成区间（含分片摘要，供恢复时校验）。
        """
        done = [
            [chunk.start, chunk.position - 1, chunk.digest.hexdigest()]
            for chunk in self._chunks
            if chunk.position > chunk.start and chunk.digest.digest()
        ]
        payload = {"total": self.total, "chunks": len(self._chunks), "done": done}
        try:
            self.plan_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"写入下载计划失败：{err}")

    def _digest_range(self, start: int, end: int) -> str:
        """
        读取磁盘指定区间并计算 SHA-256。

        :param start: 起始偏移
        :param end: 结束偏移（含）
        :return: 十六进制摘要
        """
        handle = self._fd
        opened = None
        if handle is None:
            opened = os.open(self.target, os.O_RDONLY)
            handle = opened
        try:
            digest = hashlib.sha256()
            remaining = end - start + 1
            position = start
            while remaining > 0:
                data = os.pread(handle, min(self.buffer_size, remaining), position)
                if not data:
                    break
                digest.update(data)
                position += len(data)
                remaining -= len(data)
            return digest.hexdigest()
        finally:
            if opened is not None:
                os.close(opened)

    def _read_range(self, start: int, end: int) -> bytes:
        """
        读取磁盘指定区间的字节。

        :param start: 起始偏移
        :param end: 结束偏移（含）
        :return: 区间字节
        """
        if end < start:
            return b""
        fd = self._fd
        if fd is None:
            raise DownloadError("下载文件句柄已关闭")
        return os.pread(fd, end - start + 1, start)

    def _restore(self) -> None:
        """
        按计划与磁盘真实长度恢复已完成区间。

        只有磁盘上确实存在该区间的数据时才跳过；文件长度不足时先补齐到目标大小。
        """
        fd = self._fd
        if fd is None:
            raise DownloadError("下载文件句柄已关闭")
        disk_size = os.fstat(fd).st_size
        plan = self._load_plan()
        if not plan and disk_size:
            # 没有可用计划时，旧数据不可信，直接重置文件。
            os.ftruncate(fd, 0)
            disk_size = 0
        done = cast(List[Any], plan.get("done") or [])
        for chunk in self._chunks:
            for entry in done:
                start, end = int(entry[0]), int(entry[1])
                expected = str(entry[2]) if len(entry) > 2 else ""
                end = min(end, chunk.end)
                if start > chunk.end or end < chunk.start:
                    continue
                # 磁盘上没有这段数据、或摘要对不上时不能相信计划，必须重下。
                if disk_size < end + 1 or not expected:
                    continue
                digest = self._digest_range(start, end)
                if digest != expected:
                    continue
                chunk.position = max(chunk.position, end + 1)
                chunk.expected_digest = expected
                # 片内部分完成时，把磁盘上已有区间喂回增量摘要，续传后整体摘要才正确。
                if chunk.position <= chunk.end:
                    chunk.digest = hashlib.sha256()
                    chunk.digest.update(self._read_range(chunk.start, chunk.position - 1))
        if disk_size != self.total:
            os.ftruncate(fd, self.total)
        self._written = sum(chunk.position - chunk.start for chunk in self._chunks)

    # ---- 下载 ----

    def run(self, url_provider: Callable[..., Sequence[DirectLink]]) -> None:
        """
        执行下载。

        :param url_provider: 返回直链集合的回调；传入 True 表示刷新会话后重新取链
        """
        if not self.links:
            self.links = tuple(url_provider())
        if not self.links:
            raise DownloadError("没有可用的下载直链")
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self._chunks = [
            _Chunk(index, start, end)
            for index, (start, end) in enumerate(plan_chunks(self.total, self.threads))
        ]
        elastic_start = self._chunks[-1].end + 1 if self._chunks else self.total
        self._fd = os.open(self.target, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._restore()
            self._report()
            watchdog = threading.Thread(target=self._watchdog, daemon=True)
            if self.preempt:
                watchdog.start()
            try:
                self._run_main(url_provider)
                self._run_elastic(url_provider, elastic_start)
            except Exception:  # noqa: BLE001
                pass
            if isinstance(self._error, RangeIgnored):
                # 服务端忽略 Range：整任务回退单流，绝不为单个分片下载整文件。
                self._error = None
                self._abort.clear()
                attempts = 0
                while attempts < RANGE_IGNORED_TOLERANCE:
                    attempts += 1
                    try:
                        self._single_stream(url_provider)
                        self._error = None
                        break
                    except Exception as err:  # noqa: BLE001
                        self._error = err
                        self._abort.clear()
            if self._error is not None:
                raise self._error
            if self.stop_event.is_set() and any(not chunk.done for chunk in self._chunks):
                raise DownloadCancelled("下载已取消")
            try:
                self._verify()
            finally:
                self._done_event.set()
            self._save_plan()
            self.plan_path.unlink(missing_ok=True)
        finally:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def _run_main(self, url_provider: Callable[..., Sequence[DirectLink]]) -> None:
        """
        下载主池分片。

        所有 worker 共享同一个待下载队列，谁先下完谁继续领下一个分片；
        不能先等首批全部结束再串行处理剩余分片，否则并发会掉到 1 路。
        """
        pending = [chunk for chunk in self._chunks if not chunk.done]
        if not pending:
            return
        workers = min(self.threads, len(pending))
        queue = list(pending)
        queue_lock = threading.Lock()

        def drain() -> None:
            """持续领取分片，直到队列取空或任务停止。"""
            while not self._halted:
                with queue_lock:
                    if not queue:
                        return
                    chunk = queue.pop(0)
                self._worker(chunk, url_provider)

        threads = [
            threading.Thread(target=drain, daemon=True)
            for _ in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _run_elastic(self, url_provider: Callable[..., Sequence[DirectLink]], cursor: int) -> None:
        """
        下载弹性区：按剩余量动态切块，尾部收缩到 workers 均分。

        :param url_provider: 直链回调
        :param cursor: 弹性区起点
        """
        if cursor >= self.total or self._halted:
            return
        remaining = self.total - cursor
        block = self._elastic_block()
        workers = min(self.threads, max(remaining // max(block, 1), 1))
        threads: List[threading.Thread] = []
        position = cursor
        for _ in range(workers):
            left = self.total - position
            if left <= 0:
                break
            size = min(block, left)
            if left < block * workers:
                size = max(left // workers, TAIL_MIN)
            chunk = _Chunk(len(self._chunks), position, min(position + size - 1, self.total - 1))
            self._chunks.append(chunk)
            position = chunk.end + 1
            thread = threading.Thread(target=self._worker, args=(chunk, url_provider), daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        if position < self.total and not self._halted:
            self._run_elastic(url_provider, position)

    def _elastic_block(self) -> int:
        """
        按当前实测吞吐估算弹性块大小：单 worker 约 5 秒的下载量。

        :return: 夹在 256KB–4MB 之间的块大小
        """
        elapsed = max(time.monotonic() - self._started_at, 1e-6)
        speed = self._written / elapsed if self._written else 0.0
        workers = max(self.threads, 1)
        block = int(speed / workers * ELASTIC_TARGET_SECONDS)
        return max(min(block, ELASTIC_MAX), ELASTIC_MIN)

    def _record_error(self, error: Exception) -> None:
        """
        记录首个分片异常并停止其余分片。

        :param error: 分片异常
        """
        with self._lock:
            if self._error is None:
                self._error = error
        # 内部中止只影响本文件的其余分片，不能置位任务级 stop_event，
        # 否则上层会把它当成用户暂停。
        self._abort.set()

    @property
    def _halted(self) -> bool:
        """
        外部暂停或内部异常中止，任一命中都需要停止分片循环。
        """
        return self.stop_event.is_set() or self._abort.is_set()

    def _worker(self, chunk: _Chunk, url_provider: Callable[..., Sequence[DirectLink]]) -> None:
        """
        下载单个分片，处理抢占与续传。
        """
        try:
            self._download_chunk(chunk, url_provider)
        except (RangeIgnored, DownloadCancelled) as err:
            self._record_error(err)
        except Exception as err:  # noqa: BLE001
            self._record_error(err)

    def _link_for(self, chunk: _Chunk) -> DirectLink:
        """
        按分片序号轮换直链源。

        :param chunk: 当前分片
        :return: 该分片使用的直链
        """
        if not self.links:
            raise DownloadError("没有可用的下载直链")
        return self.links[chunk.index % len(self.links)]

    def _refresh_links(self, url_provider: Callable[..., Sequence[DirectLink]]) -> None:
        """
        节流刷新直链集合，同一任务在节流窗口内最多刷新一次。

        :param url_provider: 直链回调
        """
        with self._refresh_lock:
            now = time.monotonic()
            if now - self._refreshed_at < self.refresh_seconds:
                return
            self._refreshed_at = now
            fresh = tuple(url_provider(True))
            if fresh:
                self.links = fresh

    def _download_chunk(self, chunk: _Chunk, url_provider: Callable[..., Sequence[DirectLink]]) -> None:
        """
        执行分片下载循环。

        :param chunk: 当前分片
        :param url_provider: 直链回调
        """
        retries = 0
        while not chunk.done and not self._halted:
            link = self._link_for(chunk)
            headers = dict(link.headers)
            headers["Range"] = f"bytes={chunk.position}-{chunk.end}"
            try:
                with RequestUtils(timeout=30).get_stream(link.url, headers=headers) as response:
                    chunk.response = response
                    self._consume(chunk, response)
            except RangeIgnored:
                raise
            except LinkExpired as err:
                retries += 1
                if retries > self.retries:
                    raise DownloadError(f"分片 {chunk.index} 直链多次失效：{err}")
                # 直链与会话绑定，失效后必须刷新会话并重新取链，而不是重试旧 URL。
                self._refresh_links(url_provider)
            except Exception as err:  # noqa: BLE001
                retries += 1
                if self._halted:
                    return
                if retries > self.retries:
                    raise DownloadError(f"分片 {chunk.index} 重试次数超限：{err}")
                logger.debug(f"分片 {chunk.index} 中断，从 {chunk.position} 续传：{err}")
                time.sleep(0.5 * retries)
            finally:
                chunk.response = None

    def _consume(self, chunk: _Chunk, response: Any) -> None:
        """
        读取响应并写入目标文件。

        :param chunk: 当前分片
        :param response: 流式响应
        """
        if response is None:
            raise DownloadError("下载请求失败：网络不可达")
        status = int(getattr(response, "status_code", 0) or 0)
        content_type = str(getattr(response, "headers", {}).get("Content-Type") or "")
        if "text/html" in content_type.lower():
            raise LinkExpired("下载直链返回 HTML 页面")
        if status in (401, 403, 412):
            raise LinkExpired(f"直链返回 HTTP {status}")
        if status == 200:
            raise RangeIgnored()
        if status != 206:
            raise DownloadError(f"分片请求失败：HTTP {status}")
        headers = cast(Dict[str, Any], getattr(response, "headers", {}) or {})
        content_range = str(headers.get("Content-Range") or "")
        if not content_range or f"bytes {chunk.position}-" not in content_range:
            raise DownloadError(f"分片 {chunk.index} 的 Content-Range 不匹配：{content_range or '缺失'}")
        for data in response.iter_content(chunk_size=self.buffer_size):
            if self._halted:
                raise DownloadCancelled("下载已取消")
            if not data:
                continue
            if chunk.position + len(data) - 1 > chunk.end:
                data = data[: chunk.end - chunk.position + 1]
            fd = self._fd
            if fd is None:
                raise DownloadError("下载文件句柄已关闭")
            os.pwrite(fd, data, chunk.position)
            chunk.position += len(data)
            chunk.digest.update(data)
            with self._lock:
                self._written += len(data)
            self.limiter.consume(len(data))
            self._report()
            if chunk.done:
                chunk.expected_digest = chunk.digest.hexdigest()
                self._save_plan()
                return
        if not chunk.done:
            raise DownloadError(f"分片 {chunk.index} 提前结束，剩余 {chunk.end - chunk.position + 1} 字节")

    def _single_stream(self, url_provider: Callable[..., Sequence[DirectLink]]) -> None:
        """
        服务端忽略 Range 时回退单流下载。

        :param url_provider: 直链回调
        """
        retries = 0
        while True:
            link = self.links[0] if self.links else None
            if link is None:
                raise DownloadError("单流回退失败：没有可用直链")
            headers = dict(link.headers)
            headers.pop("Range", None)
            position = 0
            try:
                with RequestUtils(timeout=30).get_stream(link.url, headers=headers) as response:
                    if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
                        raise DownloadError("单流回退失败：响应异常")
                    content_type = str(getattr(response, "headers", {}).get("Content-Type") or "")
                    if "text/html" in content_type.lower():
                        raise DownloadError("单流回退失败：直链返回 HTML 页面")
                    for data in response.iter_content(chunk_size=self.buffer_size):
                        if self._halted:
                            raise DownloadError("下载已取消")
                        if not data:
                            continue
                        fd = self._fd
                        if fd is None:
                            raise DownloadError("下载文件句柄已关闭")
                        os.pwrite(fd, data, position)
                        position += len(data)
                        with self._lock:
                            self._written += len(data)
                        self.limiter.consume(len(data))
                        self._report()
                break
            except DownloadError:
                raise
            except Exception as err:  # noqa: BLE001
                retries += 1
                if retries > self.retries:
                    raise DownloadError(f"单流回退失败：{err}")
                self._refresh_links(url_provider)
        if position < self.total:
            raise DownloadError("单流回退下载长度不足")
        for chunk in self._chunks:
            chunk.position = chunk.end + 1
            chunk.expected_digest = self._digest_range(chunk.start, chunk.end)

    # ---- 监控与校验 ----

    def _report(self) -> None:
        """
        回调下载进度。
        """
        if not self.on_progress or not self.total:
            return
        self.on_progress(min(self._written / self.total * 100, 100.0))

    def _watchdog(self) -> None:
        """
        周期性采样分片速度并抢占慢连接。
        """
        while not self._done_event.wait(WATCHDOG_INTERVAL):
            if self._halted:
                return
            now = time.monotonic()
            active = [chunk for chunk in self._chunks if chunk.response is not None and not chunk.done]
            speeds = []
            for chunk in active:
                delta = chunk.position - chunk.last_bytes
                elapsed = max(now - chunk.last_at, 1e-6)
                chunk.speed = delta / elapsed
                chunk.last_bytes = chunk.position
                chunk.last_at = now
                speeds.append(chunk.speed)
            if not speeds:
                continue
            average = sum(speeds) / len(speeds)
            threshold = max(self.preempt_bps, average / 2)
            tail = len(active) <= PREEMPT_TAIL_INFLIGHT
            min_elapsed = min(PREEMPT_TAIL_ELAPSED, self.preempt_age) if tail else self.preempt_age
            preempted = 0
            for chunk in active:
                if preempted >= PREEMPT_MAX_PER_ROUND:
                    break
                if chunk.preempts >= PREEMPT_MAX_PER_CHUNK:
                    continue
                if now - chunk.started_at < min_elapsed:
                    continue
                if now - chunk.last_preempt < PREEMPT_COOLDOWN:
                    continue
                if chunk.speed >= threshold:
                    continue
                chunk.preempts += 1
                chunk.last_preempt = now
                preempted += 1
                # 只置标志位对挂住的连接无效，必须主动关闭在飞响应。
                response = chunk.response
                if response is not None:
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _verify(self) -> None:
        """
        校验最终文件：长度、分片摘要与写入完整性。
        """
        if os.path.getsize(self.target) != self.total:
            raise DownloadError("下载文件长度与预期不一致")
        count = len(self._chunks) or 1
        for index, chunk in enumerate(self._chunks):
            actual = self._digest_range(chunk.start, chunk.end)
            if chunk.expected_digest and actual != chunk.expected_digest:
                raise DownloadError(f"分片 {chunk.index} 校验失败")
            # 大文件校验可能持续数十秒，按分片回报进度，避免任务卡在 99% 无反馈。
            if self.on_progress and index % 32 == 0:
                self.on_progress(99.0 + 1.0 * index / count)


# ---- HLS（m3u8）下载 ----
# 只做「解析播放列表 → 按序下载分片 → 按序合并为单文件」，不转码。
# 断点以分片序号记录：``<target>.hls.json`` 保存已完成片数与每片字节长度，
# ``<target>.hls.part`` 保存已合并的数据，恢复时按记录长度截断后继续。

_BANDWIDTH_PATTERN = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)
_MAP_PATTERN = re.compile(r'#EXT-X-MAP:.*?URI="([^"]+)"', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class HlsPlaylist:
    """
    m3u8 播放列表的解析结果。

    :param segments: 媒体分片地址，已解析为绝对地址
    :param init_segments: 初始化分片地址（``EXT-X-MAP``），fMP4 流需要
    :param variant_url: 主播放列表选中的子播放列表地址；本身就是媒体列表时为空
    """

    segments: Tuple[str, ...] = ()
    init_segments: Tuple[str, ...] = ()
    variant_url: str = ""


def parse_m3u8(text: str, base_url: str) -> HlsPlaylist:
    """
    解析 m3u8 文本，支持相对地址与主播放列表。

    主播放列表存在多个 ``EXT-X-STREAM-INF`` 时按 BANDWIDTH 选最高码率子列表。

    :param text: m3u8 文本
    :param base_url: 播放列表地址，用于解析相对地址
    :return: 解析结果
    """
    lines = [line.strip() for line in str(text or "").splitlines()]
    variant = _pick_variant(lines, base_url)
    if variant:
        return HlsPlaylist(variant_url=variant)

    init_segments: List[str] = []
    segments: List[str] = []
    for line in lines:
        if not line:
            continue
        map_match = _MAP_PATTERN.match(line)
        if map_match:
            init_segments.append(urljoin(base_url, map_match.group(1)))
            continue
        if line.startswith("#"):
            continue
        segments.append(urljoin(base_url, line))
    return HlsPlaylist(
        segments=tuple(segments),
        init_segments=tuple(init_segments),
    )


def _pick_variant(lines: Sequence[str], base_url: str) -> str:
    """
    从主播放列表中选择码率最高的子列表。

    :param lines: 去除首尾空白后的文本行
    :param base_url: 播放列表地址
    :return: 子播放列表绝对地址；不是主播放列表时为空
    """
    best_url = ""
    best_bandwidth = -1
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        bandwidth_match = _BANDWIDTH_PATTERN.search(line)
        bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
        target = next(
            (item for item in lines[index + 1:] if item and not item.startswith("#")),
            "",
        )
        if not target:
            continue
        if bandwidth > best_bandwidth:
            best_bandwidth = bandwidth
            best_url = urljoin(base_url, target)
    return best_url


class HlsDownloader:
    """
    顺序下载并合并 HLS 分片。

    分片按序号轮换直链源，每片使用所属源的请求头；直链过期时通过
    ``url_provider(refresh=True)`` 重新取链。
    """

    def __init__(self, target: Path, links: Sequence[DirectLink],
                 retries: int = 3, buffer_size: int = READ_BUFFER,
                 on_progress: Optional[Callable[[float], None]] = None,
                 stop_event: Optional[threading.Event] = None) -> None:
        """
        保存下载参数。

        :param target: 目标文件路径
        :param links: 直链源集合
        :param retries: 单个分片的重试次数
        :param buffer_size: 网络读缓冲大小
        :param on_progress: 进度回调，参数为完成百分比
        :param stop_event: 外部停止信号
        """
        self.target = target
        self.links: Tuple[DirectLink, ...] = tuple(links)
        self.retries = max(int(retries), 1)
        self.buffer_size = max(int(buffer_size), 1024)
        self.on_progress = on_progress
        self.stop_event = stop_event or threading.Event()
        self.plan_path = target.with_suffix(target.suffix + ".hls.json")
        self.part_path = target.with_suffix(target.suffix + ".hls.part")

    def run(self, url_provider: Optional[Callable[..., Sequence[DirectLink]]] = None) -> None:
        """
        执行 HLS 下载并合并为单文件。

        :param url_provider: 返回直链集合的回调；传入 True 表示刷新后重新取链
        """
        if not self.links:
            raise DownloadError("没有可用的 HLS 直链")
        playlist = self._load_playlist(url_provider)
        segments = playlist.init_segments + playlist.segments
        if not segments:
            raise DownloadError("m3u8 中没有可下载的分片")
        done, sizes = self._load_plan(len(segments))
        offset = sum(sizes[:done])
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if done and self.part_path.exists() and self.part_path.stat().st_size >= offset:
            mode = "r+b"
        else:
            done, sizes, offset = 0, [], 0
            mode = "wb"
        with open(self.part_path, mode) as handle:
            handle.truncate(offset)
            handle.seek(offset)
            for index in range(done, len(segments)):
                self._check_stop()
                data = self._fetch_segment(index, segments[index], url_provider)
                handle.write(data)
                handle.flush()
                sizes.append(len(data))
                done = index + 1
                self._save_plan(len(segments), done, sizes)
                self._report(done, len(segments))
        os.replace(self.part_path, self.target)
        self.plan_path.unlink(missing_ok=True)

    def _load_playlist(self, url_provider: Optional[Callable[..., Sequence[DirectLink]]]) -> HlsPlaylist:
        """
        下载并解析播放列表；主列表会继续下载选中的子列表。

        :param url_provider: 直链回调
        :return: 媒体播放列表
        """
        link = self.links[0]
        text = self._fetch_text(link, link.url, url_provider)
        playlist = parse_m3u8(text, link.url)
        if playlist.variant_url:
            text = self._fetch_text(link, playlist.variant_url, url_provider)
            playlist = parse_m3u8(text, playlist.variant_url)
        return playlist

    def _link_for(self, index: int) -> DirectLink:
        """
        按分片序号轮换直链源。

        :param index: 分片序号
        :return: 该分片使用的直链
        """
        if not self.links:
            raise DownloadError("没有可用的 HLS 直链")
        return self.links[index % len(self.links)]

    def _fetch_text(self, link: DirectLink, url: str,
                    url_provider: Optional[Callable[..., Sequence[DirectLink]]]) -> str:
        """
        下载播放列表文本，直链失效时刷新一次。

        :param link: 当前直链
        :param url: 播放列表地址
        :param url_provider: 直链回调
        :return: 播放列表文本
        """
        data = self._fetch(link, url, url_provider)
        return data.decode("utf-8", errors="replace")

    def _fetch_segment(self, index: int, url: str,
                       url_provider: Optional[Callable[..., Sequence[DirectLink]]]) -> bytes:
        """
        下载单个分片，失败时按配置重试。

        :param index: 分片序号
        :param url: 分片地址
        :param url_provider: 直链回调
        :return: 分片字节
        """
        retries = 0
        while True:
            self._check_stop()
            link = self._link_for(index)
            try:
                return self._fetch(link, url, url_provider)
            except LinkExpired as err:
                retries += 1
                if retries > self.retries:
                    raise DownloadError(f"HLS 分片 {index} 直链多次失效：{err}") from err
                if url_provider is not None:
                    fresh = tuple(url_provider(True))
                    if fresh:
                        self.links = fresh
                time.sleep(0.2 * retries)
            except DownloadError:
                retries += 1
                if retries > self.retries:
                    raise
                time.sleep(0.5 * retries)

    def _fetch(self, link: DirectLink, url: str,
               url_provider: Optional[Callable[..., Sequence[DirectLink]]]) -> bytes:
        """
        发起一次 GET 并读取完整响应体。

        :param link: 当前直链
        :param url: 请求地址
        :param url_provider: 直链回调，未使用时保留统一签名
        :return: 响应体
        """
        del url_provider
        headers = dict(link.headers)
        with RequestUtils(timeout=30).get_stream(url, headers=headers) as response:
            if response is None:
                raise DownloadError("HLS 请求失败：网络不可达")
            status = int(getattr(response, "status_code", 0) or 0)
            content_type = str(getattr(response, "headers", {}).get("Content-Type") or "")
            if "text/html" in content_type.lower():
                raise LinkExpired("HLS 直链返回 HTML 页面")
            if status in (401, 403, 412):
                raise LinkExpired(f"HLS 直链返回 HTTP {status}")
            if status != 200:
                raise DownloadError(f"HLS 请求失败：HTTP {status}")
            return b"".join(response.iter_content(chunk_size=self.buffer_size))

    def _check_stop(self) -> None:
        """
        外部取消时抛出取消异常。
        """
        if self.stop_event.is_set():
            raise DownloadCancelled("下载已取消")

    def _load_plan(self, total: int) -> Tuple[int, List[int]]:
        """
        读取断点计划；分片总数或数据长度不匹配时重新开始。

        :param total: 本次分片总数
        :return: 已完成片数与每片字节长度
        """
        if not self.plan_path.exists() or not self.part_path.exists():
            return 0, []
        try:
            plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0, []
        if not isinstance(plan, dict) or int(plan.get("total") or 0) != total:
            return 0, []
        done = max(int(plan.get("done") or 0), 0)
        sizes = [max(int(value), 0) for value in plan.get("sizes") or []]
        if done > total or len(sizes) < done:
            return 0, []
        return done, sizes[:done]

    def _save_plan(self, total: int, done: int, sizes: Sequence[int]) -> None:
        """
        原子写回断点计划。

        :param total: 分片总数
        :param done: 已完成片数
        :param sizes: 每片字节长度
        """
        payload = {"total": total, "done": done, "sizes": list(sizes)}
        try:
            temp = self.plan_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            temp.replace(self.plan_path)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"写入 HLS 断点计划失败：{err}")

    def _report(self, done: int, total: int) -> None:
        """
        回报按片数计算的进度。

        :param done: 已完成片数
        :param total: 分片总数
        """
        if self.on_progress and total > 0:
            self.on_progress(min(done / total * 100.0, 100.0))
