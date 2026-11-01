"""
并发 Range 分片下载引擎。

单条 HTTP 连接会被网盘 CDN 限速，引擎把同一文件切成多段、用多条连接各取一段，
总带宽随连接数叠加。核心机制：分片规划、主池/弹性区、尾部收缩、慢连接抢占、
断点续传与四道完整性校验。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from app.adapters.network.http import RequestUtils
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

    def __init__(self, limit_kbps: int) -> None:
        """
        保存限速值。

        :param limit_kbps: 限速 KB/s，0 表示不限速
        """
        self.bytes_per_second = max(int(limit_kbps or 0), 0) * 1024
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
    进程重启后从磁盘真实长度恢复。
    """

    def __init__(self, target: Path, total: int, threads: int, rate_limit_kbps: int = 0,
                 preempt: bool = True, headers: Optional[Dict[str, str]] = None,
                 on_progress: Optional[Callable[[float], None]] = None,
                 stop_event: Optional[threading.Event] = None) -> None:
        """
        保存下载参数。

        :param target: 目标文件路径
        :param total: 文件总字节数
        :param threads: 并发连接数
        :param rate_limit_kbps: 全局限速 KB/s
        :param preempt: 是否启用慢连接抢占
        :param headers: 下载请求头（Cookie/Referer/UA）
        :param on_progress: 进度回调，参数为完成百分比
        :param stop_event: 外部停止信号
        """
        self.target = target
        self.total = total
        self.threads = max(int(threads), 1)
        self.preempt = preempt
        self.headers = dict(headers or {})
        self.on_progress = on_progress
        self.stop_event = stop_event or threading.Event()
        # 单文件结束标记：只用于收尾看门狗，不能复用外部 stop_event，
        # 否则一个文件下完后会把任务级停止信号置位，导致下一个文件立刻被判为取消。
        self._done_event = threading.Event()
        # 内部异常中止信号：只用于让本文件其余分片停下，不能传给任务级暂停判断。
        self._abort = threading.Event()
        self.plan_path = target.with_suffix(target.suffix + ".plan.json")
        self.limiter = _RateLimiter(rate_limit_kbps)
        self._lock = threading.Lock()
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
                data = os.pread(handle, min(READ_BUFFER, remaining), position)
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

    def run(self, url_provider: Callable[..., str]) -> None:
        """
        执行下载。

        :param url_provider: 返回直链的回调；传入 True 表示请求刷新会话后重新取链
        """
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

    def _run_main(self, url_provider: Callable[[], str]) -> None:
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

    def _run_elastic(self, url_provider: Callable[[], str], cursor: int) -> None:
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

    def _worker(self, chunk: _Chunk, url_provider: Callable[[], str]) -> None:
        """
        下载单个分片，处理抢占与续传。
        """
        try:
            self._download_chunk(chunk, url_provider)
        except (RangeIgnored, DownloadCancelled) as err:
            self._record_error(err)
        except Exception as err:  # noqa: BLE001
            self._record_error(err)

    def _download_chunk(self, chunk: _Chunk, url_provider: Callable[..., str]) -> None:
        """
        执行分片下载循环。

        :param chunk: 当前分片
        :param url_provider: 直链回调
        """
        retries = 0
        url = url_provider()
        while not chunk.done and not self._halted:
            headers = dict(self.headers)
            headers["Range"] = f"bytes={chunk.position}-{chunk.end}"
            try:
                with RequestUtils(timeout=30).get_stream(url, headers=headers) as response:
                    chunk.response = response
                    self._consume(chunk, response)
            except RangeIgnored:
                raise
            except LinkExpired as err:
                retries += 1
                if retries > PREEMPT_MAX_PER_CHUNK:
                    raise DownloadError(f"分片 {chunk.index} 直链多次失效：{err}")
                # 直链与会话绑定，失效后必须刷新会话并重新取链，而不是重试旧 URL。
                url = url_provider(True)
            except Exception as err:  # noqa: BLE001
                retries += 1
                if self._halted:
                    return
                if retries > PREEMPT_MAX_PER_CHUNK:
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
        for data in response.iter_content(chunk_size=READ_BUFFER):
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

    def _single_stream(self, url_provider: Callable[..., str]) -> None:
        """
        服务端忽略 Range 时回退单流下载。

        :param url_provider: 直链回调
        """
        url = url_provider()
        headers = dict(self.headers)
        headers.pop("Range", None)
        position = 0
        try:
            with RequestUtils(timeout=30).get_stream(url, headers=headers) as response:
                if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
                    raise DownloadError("单流回退失败：响应异常")
                content_type = str(getattr(response, "headers", {}).get("Content-Type") or "")
                if "text/html" in content_type.lower():
                    raise DownloadError("单流回退失败：直链返回 HTML 页面")
                for data in response.iter_content(chunk_size=READ_BUFFER):
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
        except DownloadError:
            raise
        except Exception as err:  # noqa: BLE001
            raise DownloadError(f"单流回退失败：{err}")
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
            threshold = max(WATCHDOG_MIN_SPEED, average / 2)
            tail = len(active) <= PREEMPT_TAIL_INFLIGHT
            min_elapsed = PREEMPT_TAIL_ELAPSED if tail else PREEMPT_MIN_ELAPSED
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
