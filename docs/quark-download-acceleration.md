# 夸克网盘下载加速原理说明（面向自研下载器）

本文只讲原理与实现要点：夸克的「高速下载」为什么能快、快在哪一层、要复刻需要哪些机制和参数。
分析对象：[`tidain/YunX-Desktop`](https://github.com/tidain/YunX-Desktop)（`main`，Kotlin/Compose 桌面端）。涉及 YunX 的结论均给出源码位置；仅靠阅读代码、未实测的部分见第 8 节。

## 1. 一句话结论

**加速来自并发 Range：** 夸克 CDN 对单条 HTTP 连接限速，把服务端下发的直链当普通 HTTP 资源、用多条连接各取一段字节，总带宽就随连接数叠加；真正决定体验的是「分片怎么切、慢连接怎么换、断了怎么续、错了怎么判」。

它不是两件事：不是改写 CDN 节点（`QuarkCdn.fastest()` 只有一行 `return original`，注释说明改写 host 或预探测会消耗直链额度并可能命中节点签名导致 412，`QuarkCdn.kt:1-13`），也不是破解会员限速（取链请求里 `speedup_session` 传空串，`QuarkApi.kt:468`、`:527`）。

## 2. 为什么能加速

### 2.1 被绕过的是「按连接限速」

YunX 源码里记录得很具体：

- 「网盘 CDN 是**按连接**限速的，且个别连接会落在慢节点上：实测多数连接 40~80KB/s，少数只有 3~7KB/s」（`DownloadManager.kt:158-159`）。
- 「低单连接速率（如夸克 10KB/s）下每个 1MB 重片耗时约 100s，尾部极易空转」（`DownloadManager.kt:3597`）。

单连接被压到几十 KB/s 时，唯一不需要平台配合的提速手段就是**同一直链开多条连接、各自请求不同字节区间**（HTTP `Range`）。这与 IDM / aria2 `--split` 同一类做法，不涉及协议层破解。

### 2.2 边界：平台侧限制仍然生效

- 并发有阈值：迅雷类 CDN 超过约 8 路并发 Range 会把多余请求降级成 200 整文件（`DownloadManager.kt:124-127`），所以迅雷被固定 8 路（`SettingsRepository.kt:521`）；夸克没有固定上限，走用户设置（代码默认 32、上限 512，`SettingsRepository.kt:19-33`、`:519-521`）。
- 频控、账号额度、直链时效都不受影响；并发越高越容易触发风控。
- 提速是「同一账号、同一文件」的速率叠加，不改变账号本身的限速档位。

## 3. 完整链路：先拿到一条可用直链

### 3.1 分享解析与两条取链路径

| 路径 | 做法 | 适用与回退 |
| ---- | ---- | ---------- |
| 免转存 | 把分享凭证（`fids` / `fids_token` / `pwd_id` / `stoken`）直接交给 `file/download` 换直链，不写用户网盘 | 默认优先；个别分享类型失败时回退转存（`QuarkApi.kt:502-560`） |
| 转存后取链 | 每次转存到**唯一子目录** `tr_<nanoTime>_<随机>`，再对新 `fid` 取直链，下载完成后再删子目录 | 兜底路径；唯一子目录用于绕开服务端按 `to_pdir_fid` 去重，否则二次转存会返回已删除 fid 导致 `404 / 21001`（`QuarkResolveRepository.kt:96-140`） |

分享链接与提取码由 `ShareLinkParser.parse()` 识别，`getShareToken()` 携带提取码换取 `stoken`（`QuarkApi.kt:116-160`）。

### 3.2 直链下载的四项必需条件

任一条不满足，表现为 412、403 或拿到 HTML 页：

1. **账号 Cookie，且含未过期的 `__puus`。** `__puus` 约 3 小时过期，是直链签名校验的关键字段；刷新方式是剥离 `__puus` 后请求 `/config`，由响应 `Set-Cookie` 重新下发（`QuarkApi.kt:21`、`:391-413`）。
2. **客户端 UA。** 用 `QuarkConstants.API_USER_AGENT`，浏览器 UA 会被判为非客户端（`ResolveViewModel.kt:1972-1977`）。
3. **固定 Referer。** `Referer: https://pan.quark.cn/`（`QuarkConstants.kt:84`、`ResolveViewModel.kt:1969-1977`）。
4. **直链自身有时效，且签名绑定取链时刻的 Cookie。** 用 A 账号的 Cookie 去下 B 账号的直链会直接 412/403（`ResolveViewModel.kt:1890-1905` 注释）。

游客链路（YunX 的 `getGuestShareDownloadLink()`）用响应下发的 `__pugs`，实测只对约 50MB 以内小文件可用，缺 `__pugs` 时 CDN 直接 412（`QuarkApi.kt:74-78`、`:446-500`），不建议作为主路径。

MoviePilot 的本机实现（`app/adapters/external/netdisk/quark.py`）：

- **触发条件**：仅当没有可用账号时，`resolve()` 才走 `_guest_link()`；有账号时完全不进入游客分支，仍走 `__puus` 的正常取链。
- **`__pugs` 来源**：从游客取链请求响应的 `Set-Cookie` 中取 `__pugs`，写入直链下载头 `Cookie: __pugs=...`；客户端 UA、`Referer: https://pan.quark.cn/` 等其余请求头与账号取链一致。
- **大小限制**：`_GUEST_MAX_BYTES = 50MB`，超过即抛 `UNSUPPORTED`（「游客取链仅支持 50MB 以内文件」）。
- **错误分类**：服务端 `23018` 统一归为 `UNSUPPORTED`，不再当作未知错误。
- **探测不变**：无账号时 `inspect` 仍抛 `AUTH_EXPIRED`，不会因为支持游客取链而放开目录探测。

### 3.3 下载请求本身

- 每个分片发一条 `GET` + `Range: bytes=<from>-<to>`，必须拿到 `206` 且 `Content-Range` 与请求区间一致。
- 服务器忽略 Range 返回 `200` 时**不能**按分片写入，要交给上层回退单流（`ChunkDownloader.kt:283-303`）。
- 响应 `Content-Type: text/html` 视为防盗链/过期页，判失败、不落盘（`:276-279`、`:559-563`）。

## 4. 引擎机制（要复刻的就是这六项）

### 4.1 分片规划：片数、主池与弹性区

- 片数 `chunkCountFor(total, threads)` = `min(max(bySize, threads*8), total/256KB, 512)`；`bySize` 按大小分档：<5MB 不分片、<50MB 取 8、<500MB 取 32、其余 64（`DownloadManager.kt:3594-3610`）。
- 单片下限 256KB：再小会产生海量 Range 请求，再大则尾部空转。
- 主池占 70% 片数、按固定块顺序下发；剩余 30% 是弹性区（`DownloadManager.kt:1866`、`:1926-1931`）。
- 主池保连接复用与稳定吞吐，弹性区负责吃长尾。

### 4.2 弹性分片与尾部收缩

`ElasticAllocator`（`DownloadManager.kt:191-246`）：

- 按字节顺序发块，分片物理相邻，保持连接复用（不做「中点劈分」）。
- 块大小 ≈ 单 worker 下载 5 秒的量，夹在 256KB–4MB：快则大块省 Range 开销，慢则小块充分并行。
- **尾部收缩**：剩余不足 `workers × 块` 时把块压到 `remaining / workers`，下限 64KB，让所有连接一起冲完最后一段，消除「其他片都下完、只剩一路慢慢爬」。

### 4.3 慢连接抢占：把慢连接换掉，而不是等它

- 看门狗每 5 秒采样每路瞬时速度（`DownloadManager.kt:154-155`、`:457-466`）。
- 判定阈值 `max(12KB/s, 本任务平均单连接速度 / 2)`；常规要求已跑满 15 秒且剩余 ≥128KB；收尾（在飞 ≤3 路）放宽到 3 秒；每片最多 3 次，冷却 10 秒，每轮最多 2 路（`:154-176`、`:473-530`）。
- 生效方式是**主动 cancel 该分片的 HTTP Call**：只置标志位对「连接被 CDN 挂住、read 阻塞」无效（`:517-524` 注释）。
- 断连后从已写字节零退避续传，不丢数据、不计失败（`ChunkDownloader.kt:204-241`、`:79`）。

这是最值得移植的一项：它解决的是「平均值被拖低、越到后面越慢」，而不是峰值速度。

### 4.4 断点续传

- 分片缓存模式：每个分片一个 part 文件，外加 `plan.txt`（`chunks=N total=N main=N`）记录分片计划；计划与当前文件大小不符时清空重下（`DownloadManager.kt:119-122`、`:1840-1860`）。
- 多线程直写模式：预分配目标 `.part` 文件，`FileChannel.write(buf, position)` 按偏移并行写（`DownloadManager.kt:3144-3185`、`ChunkDownloader.kt:367-433`），用 `.plan` / `.done` 记录分片计划与已完成分片摘要（`DirectPartManifest.kt:10-18`）。
- 恢复时以「磁盘真实长度」为准收集缺失区间，不能只信 worker 返回值（`DownloadManager.kt:2182-2201`）。

### 4.5 完整性校验

四道闸（`ChunkDownloader.kt:283-303`、`:320-352`、`:623-659`）：

1. `206` 的 `Content-Range` 必须与请求区间一致。
2. 写入字节数必须严格等于预期，防空洞文件。
3. 分片级 SHA-256：写入时增量计算，合并时逐片重算比对。
4. Range 被忽略（`200`）立即返回 `RANGE_IGNORED`，由上层回退单流，绝不为单片下载整文件；容忍次数为 3（`DownloadManager.kt:176`）。

### 4.6 资源上限与限速

- 全进程在飞上限 `maxHeap/8/读缓冲`，夹在 8–512，跨任务共享一个信号量（`DownloadManager.kt:3625-3647`）。
- 专用分片线程池：核心数 = 在飞上限、空闲 30 秒回收、daemon，避免通用 IO 池把 512 线程压成 64 路（`:3655-3665`）。
- 读缓冲默认 64KB（`DownloadTuning.kt`、`ChunkDownloader.kt:42`）。
- 全局限速用令牌桶，所有任务合计（`DownloadManager.kt:356-357`）。

### 4.7 多账号多源（可选，实验性）

同一文件用不同账号各取一条直链，分片按序号轮换源，每源带自己的请求头；分片规划与断点计划完全不变，开关该功能不会让已有断点失效（`ResolveViewModel.kt:1861-1865`、`:1985-2010`、`DownloadManager.kt:1908-1912`）。前提是各账号有独立额度，代价是风控暴露面变大。

## 5. 自研实现要点（映射到 Python / asyncio）

### 5.1 分层

| 层 | 职责 | 关键约束 |
| -- | ---- | -------- |
| 取链层 | 分享解析、免转存/转存、`__puus` 刷新、直链缓存 | 直链与「取链时的 Cookie」绑定，缓存条目必须带 Cookie 指纹；失败分类要能区分 412 / 21001 / HTML |
| 调度层 | 分片规划、弹性分配、看门狗、重试与抢占 | 只依赖抽象传输接口，便于测试替身 |
| 传输层 | 单分片 Range 请求、写入、校验 | 必须能主动取消在飞请求；区分 206/200/HTML/异常 |
| 落盘层 | part 文件或预分配直写、计划与摘要持久化 | 写入按绝对偏移，进程重启后可恢复 |

在 MoviePilot 中对应「下载器模块」：模块发现只需目录 + `capability.toml`，但有硬校验——模块包与 manifest 一一对应、`entrypoint` 必须是 `app.modules.<目录>:<capability id>`、`metadata.subtype` 必须在 `DownloaderType` 枚举内（`app/runtime/extensions/module/adapter.py:125-190`、`app/schemas/types.py:684-696`）。下载器方法契约见 `app/runtime/extensions/module/contracts.py:1344-1465`，其中 `download` 的聚合规则是 `FIRST_NON_EMPTY`（`:1392-1400`）。出站 HTTP 走 `RequestUtils`（支持自定义 header 与 `get_stream()`，`app/adapters/network/http.py:776-810`、`:927-943`），模块层可直接使用。

### 5.2 并发模型

- 用 `asyncio` 管分片协程、用线程池跑阻塞式 `requests` 读取；不要在事件循环里做同步 IO。
- 两个层级要分开限流：**任务级 worker 数**（用户可调）与**全进程在飞请求数**（按内存预算推导），后者是硬上限。
- 取消必须是「真的取消」：持有每个在飞请求的句柄，抢占/暂停时主动 close，否则阻塞读会一直挂到超时。

### 5.3 分片状态机

`pending → inflight → done`，失败回到 `pending`（带重试计数），抢占是 `inflight → pending` 但**保留已写字节**。状态落盘只记「哪些片已完成 + 摘要」，进度以磁盘实际长度为准。

### 5.4 直链刷新

直链过期或返回 412/403 时，不是重试原 URL，而是：刷新 `__puus` → 重新取链 → 用新直链继续未完成分片。刷新要有节流，避免频繁取链触发频控。

### 5.5 礼貌性与资源

- 并发默认保守（网盘 8~10 路起步），可配置上限。
- 失败退避用指数退避 + 抖动，不做重试风暴。
- 单任务读缓冲 64KB 起步，按在飞路数反推内存占用。

### 5.6 可观测性

至少暴露：每片瞬时速度与已收字节、抢占次数、`RANGE_IGNORED` 次数、重试原因分布、在飞请求数与线程池队列长度。没有这些，尾部塌速和 200 降级都只能靠猜。

## 6. 参数速查

| 参数 | YunX 取值 | 说明 |
| ---- | --------- | ---- |
| 每文件连接数（`threads`） | 默认 32，上限 512，迅雷固定 8 | 按平台设置（`SettingsRepository.kt:519-521`） |
| 单片下限 | 256KB | 片数公式的分母 |
| 片数上限 | 512 | 同时是用户可选的最高线程档位 |
| 主池 / 弹性区 | 70% / 30% | `DownloadManager.kt:1866` |
| 弹性块 | 256KB–4MB，目标 5 秒 | 按单 worker 实测速度自适应 |
| 尾部块下限 | 64KB | 尾部收缩下限 |
| 抢占采样 | 5 秒 | 看门狗周期 |
| 抢占阈值 | `max(12KB/s, 均速/2)` | 绝对下限兜住收尾退化 |
| 抢占门槛 | 15 秒；收尾 3 秒 | 收尾 = 在飞 ≤3 路 |
| 抢占次数/冷却 | 每片 3 次 / 10 秒 | 每轮最多 2 路 |
| Range 忽略容忍 | 3 次 | 超过才整任务回退单流 |
| 读缓冲 | 64KB | 从 256KB 下调以控内存 |
| 在飞上限 | `maxHeap/8/64KB`，夹 8–512 | 跨任务共享 |

## 7. 关键坑清单

1. **改写 CDN host 会 412**：原样使用服务端下发的直链，不做节点优选（`QuarkCdn.kt:1-13`）。
2. **`__puus` 会过期**：取链与下载之间隔久了必须刷新，否则 412。
3. **Cookie 与直链必须同源**：换账号 Cookie 下另一账号直链直接 412/403。
4. **缺 Referer 拿到 HTML**：`Content-Type: text/html` 一律判失败，不落盘。
5. **转存去重**：二次转存可能返回已删除 fid（404/21001），每次用唯一子目录。
6. **200 不是成功**：Range 被忽略时必须回退单流，不能把整文件当分片写。
7. **只置标志位无法抢占**：连接被挂住时读循环不跑，必须主动 cancel。
8. **进度不能只信 worker**：以磁盘真实长度收集缺失区间。
9. **线程数 ≠ 并行度**：还要受全进程在飞上限与线程池大小约束。
10. **尾部才是体验**：没有尾部收缩与抢占，前面再快最后一段也会塌。

## 8. 证据索引与未验证项

### 8.1 证据索引（YunX-Desktop，`main`）

| 主题 | 文件与行号 |
| ---- | ---------- |
| 不做节点改写 | `desktop/src/main/kotlin/com/yunx/app/data/network/QuarkCdn.kt:1-13` |
| `__puus` 与刷新 | `.../data/network/QuarkApi.kt:21`、`:391-413` |
| 取链与免转存 | `.../data/network/QuarkApi.kt:416-443`、`:446-500`、`:502-560` |
| 转存唯一子目录 | `.../data/repository/QuarkResolveRepository.kt:96-140` |
| 下载请求头 | `.../ui/viewmodel/ResolveViewModel.kt:1870-2010` |
| 分片与校验 | `.../data/download/ChunkDownloader.kt:169-243`、`:283-303`、`:320-352`、`:623-659` |
| 抢占与弹性 | `.../data/download/DownloadManager.kt:154-176`、`:191-246`、`:457-530` |
| 分片规划 | `.../data/download/DownloadManager.kt:3594-3610` |
| 在飞上限与线程池 | `.../data/download/DownloadManager.kt:3625-3665` |
| 默认参数 | `.../data/download/DownloadTuning.kt`、`.../data/prefs/SettingsRepository.kt:19-33`、`:519-521` |
| 项目自述 | `YunX-Desktop/docs/FEATURES.md:12-19`、`YunX-Desktop/docs/UPSTREAM-DIFF.md:113-118` |

### 8.2 未验证项

- 未使用真实夸克账号跑通取链与下载；夸克侧结论来自源码阅读与该项目文档，不是实测。
- 「单连接 40~80KB/s、少数 3~7KB/s」「超过 8 路 Range 被降级为 200」来自代码注释与文档记录，未独立复现。
- 并发上限、抢占阈值在不同网络与账号档位下的最优值未测量；表中数值只能作为起点。
- 多账号多源在 YunX 中是实验性功能，对平台风控的实际影响未知。
