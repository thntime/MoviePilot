# 网盘下载链路与 YunX-Desktop 一致性结论文档

> 用途：作为新建 Comet change 的需求与设计输入，并记录实施后的最终状态。
> 日期：2026-11-07。方法：实施前为只读静态比对；实施后状态已按落地代码同步，标注「改造前」的表格仅作历史对照。

## 0. 结论摘要

1. **分片引擎已与 YunX 同源一致**：片数公式、主池/弹性区、慢连接抢占、断点续传、逐片 SHA-256 全部相同，不需要重做。
2. **真正的差异是并发口径**：改造前 MoviePilot 的 `threads` 是「整任务总连接数」并按文件数均分，YunX 的 `threads` 是「每文件连接数」，默认值 8 vs 32、3 文件时实际 6 条 vs 96 条。本次 change 已统一为每文件连接数、默认 32。
3. **2 处正确性缺陷**（会漏文件）本次 change 已修复：分享目录递归深度提到 12（与 YunX 一致）、夸克分享翻页改为 50/页 × 200 页（与 YunX 一致，同仓 UC 也是 50）。
4. **游客取链已补齐**：无账号时用 `__pugs`，仅约 50MB 以内；有账号完全不进入游客分支，`23018` 归 `UNSUPPORTED`，`inspect` 无账号仍抛 `AUTH_EXPIRED`。
5. **3 处 MoviePilot 优于 YunX**，改造时不要回退：直链过期换链、`pwrite` 直写无合并、0 字节文件拒绝。
6. **下载链路本身没有「文件总数」上限**；用户能调的「并发文件数」默认 3、实例上限 64，由系统键 `NetdiskMaxConcurrentFiles`（默认 3，夹 1–64）决定；`NetdiskMaxConcurrentDownloads` 只决定全局任务槽（默认 3，夹 1–16）。

## 1. 对比基准

| 项 | 值 |
| -- | -- |
| MoviePilot | `/home/zxl/workspace/zxl/MoviePilot/MoviePilot`，分支 `v3`，工作树含未提交改动 |
| YunX-Desktop | `/tmp/opencode/YunX-Desktop`，`main` @ `3700c1c` |
| 范围 | 夸克为主，兼顾 UC/迅雷的平台差异；只覆盖解析→转存→取链→下载→完成判定 |
| 证据 | 文中 `文件:行号` 均已按当前代码核对 |

## 2. 六项逐项结论

| 环节 | 一致性 | 判定 |
| ---- | ------ | ---- |
| 解析分享地址 | 已对齐 | 递归深度 12、翻页 50/页 × 200 页，不再漏文件 |
| 转存 | 一致 | 接口/轮询/唯一临时目录相同；MP 清理更完整 |
| 下载文件 | 基本一致 | MP 多直连与过期换链（更好）；游客取链已补齐 |
| 分片 | 高度一致（同源移植） | 参数逐项相同；MP 写入与断点更优 |
| 多个同时下载 | 已对齐 | 统一为每文件连接（默认 32）；任务单位仍不同 |
| 下载完成 | 一致，MP 更严 | MP 拒绝 0 字节、有 `.done` 标记 |

### 2.1 解析分享地址

**一致部分**

- 夸克链接正则 `pan\.quark\.cn/s/([A-Za-z0-9]+)`；`pwd_id` 即分享短码；`passcode` 优先入参，其次 URL 查询参数，最后文案「提取码：xxxx」（`app/domain/netdisk.py:375-439`）。
- `stoken` 走 `POST /1/clouddrive/share/sharepage/token`，请求体含 `pwd_id` / `passcode` / `support_visit_limit_private_share`（`app/adapters/external/netdisk/quark.py:367-376`）。
- 列目录走 `sharepage/detail`，参数 `ver=2`、`_page`、`_size`、`_fetch_total=1`（`quark.py:413-433`）。

**改造前的不一致（前两行已由本次 change 修复）**

| 项 | MoviePilot（改造前） | YunX | 判定 |
| -- | ---------- | ---- | ---- |
| 单页条数 / 页数 | 100 × 20（`quark.py:61-62`） | 50 × 200 | 当时有跳页风险 |
| 递归深度 | 3（`app/modules/netdisk/__init__.py:66`） | 12 | 当时会漏深层目录 |
| 失败重试 | 401/403 刷新凭据重试一次 + 应用层 3 次循环（`base.py:400-441`、`resolve.py:347-381`） | 仅连接级重试 | MP 更优 |
| stoken 缓存 | 进程内 6h，任务开始强制换新（`quark.py:71`） | 不缓存 | MP 更优 |

当前实现已为单页 50、最多 200 页、递归深度 12，与 YunX 一致。

**为什么翻页 100 会漏文件**：`_page=2&_size=100` 的 offset 由服务端按 `(page-1)*size` 计算。若服务端把单页截断为 50，第 2 页从第 101 条开始，第 51–100 条被跳过；MP 的停止条件是「整页无新条目」（`quark.py:457-458`），去重逻辑救不了 offset 跳页。YunX 注释明确「实测单页最多 50」，同仓 UC 适配器也用 50（`uc.py:67-68`）。

### 2.2 转存

**一致**：`sharepage/save` 参数（`pwd_id`/`stoken`/`pdir_fid`/`to_pdir_fid`/`fid_list`/`fid_token_list`/`scene=link`）、轮询 `10×1s`、完成条件 `finished_at>0 || status==2 || task_status==2`、新 fid 取 `save_as.save_as_top_fids[0]`，两边完全相同（`quark.py:647-711` vs `QuarkApi.kt:333-373`）。

**一致**：都先建唯一临时子目录再转存，避免服务端按 `to_pdir_fid` 去重返回旧 fid。

**差异**

| 项 | MoviePilot | YunX | 判定 |
| -- | ---------- | ---- | ---- |
| 临时目录名 | `mp_tr_<毫秒>`（`quark.py:757`） | `tr_<nanoTime>_<随机>` | MP 同毫秒并发可能撞名，低概率 |
| 清理 | 下载完成后清理 + 孤儿补偿（`resolve.py:273-300`、`__init__.py:1419-1450`） | 成功回调删，失败残留 | MP 更完整 |
| 空间预检 | 有，批量预算 + 不足时清理旧临时目录 | 有 | 一致 |

### 2.3 下载文件

**一致**：直链接口 `POST /1/clouddrive/file/download`；请求头 Cookie + `quark-cloud-drive/2.5.20` UA + `Referer: https://pan.quark.cn/`，不带 Origin；`206` + `Content-Range` 校验；`text/html` 判失效。

**差异**

| 项 | MoviePilot | YunX | 判定 |
| -- | ---------- | ---- | ---- |
| 代理 | 强制直连，清空 `proxies`（`engine.py:58`） | 走系统默认 | MP 更稳 |
| 直链过期 | 401/403/412 立即换链，30s 节流，多分片同时失效只刷一次（`engine.py:669-693`） | 重试复用原 URL | MP 更优，YunX 是缺陷 |
| 游客取链 | 已支持：无账号时用 `__pugs`，仅约 50MB 以内；`23018` 归 `UNSUPPORTED` | 支持 `__pugs`，约 50MB | 已补齐；有账号不进入游客分支 |

### 2.4 分片

**逐项一致**（`engine.py:31-56` vs `DownloadManager.kt:269-277`、`:3750-3769`）：

- 单片下限 256KB、片数上限 512、<5MB 不分片
- 片数公式 `min(max(bySize, threads×8), total/256KB, 512)`；大小档 8/32/64
- 主池 70% + 弹性区 30%，弹性块 256KB–4MB、目标 5 秒
- 尾部块下限 64KB、Range 忽略容忍 3 次
- 慢连接：5s 采样、`max(12KB/s, 均速/2)`、15s（收尾 3s）、每片 3 次、每轮 2 路、冷却 10s
- 读缓冲 64KB、全局限速令牌桶

**MoviePilot 更优的部分**

| 项 | MoviePilot | YunX |
| -- | ---------- | ---- |
| 落盘 | 预分配目标文件 + `os.pwrite` 绝对偏移写，无合并（`engine.py:476-478`） | 分片 part 文件 + 合并 |
| 断点记录 | `.plan.json` 存区间 + 每片 SHA-256（`engine.py:345-395`） | `plan.txt` + part 长度 |
| 完成标记 | `.done` 区分预分配空壳（`__init__.py:142-163`） | 依赖合并完成 |

**差异**：分片重试次数 MP 默认 3（1–10 可配），YunX `3+4=7`；MP 偏少。

### 2.5 多个同时下载

| 层级 | MoviePilot | YunX |
| ---- | ---------- | ---- |
| 任务单位 | 一个分享 = 一个任务（`__init__.py:706`） | 一个文件 = 一个任务 |
| 全局任务并发 | 默认 3，上限 16（`__init__.py:96-104`） | 默认 3，上限 10 |
| 单任务并发文件 | 默认 3，上限 64（`__init__.py:699`） | 无此层（每个文件独立任务） |
| 每文件连接 | `threads` 直接生效（`__init__.py:830`） | `threads` 直接生效 |
| threads 默认 / 上限 | 32 / 512 | 32 / 512 |
| 全局在飞 | `clamp(可用内存 × 25% ÷ 读缓冲, 512, 4096)`（`engine.py:60`） | 512（按堆预算推导） |

**默认总连接**：MP 3 文件 × 32 = 96 条；YunX 3 文件 × 32 = 96 条。

### 2.6 下载完成

**一致**：总长度校验 + 每片 SHA-256，都不校验服务端 MD5。

**差异**：MP 额外要求「无 `.plan.json` + 存在 `.done`」，并在模块层直接拒绝 `size<=0`（`__init__.py:1336`）；YunX 对 0 字节文件会当成功保存。MP 更严。

## 3. 并发口径专项（核心差异）

### 3.1 改造前事实（已由本次 change 修正）

| 事实 | MoviePilot（改造前） | YunX（基准） |
| ---- | --------------- | ------------ |
| 任务单位 | 一个分享 = 一个任务 | 一个文件 = 一个任务 |
| 同时下载文件数 | 默认 3，上限 8 | 默认 3，上限 10 |
| threads 口径 | 整个任务的总连接数，按文件数均分 | 每个文件的连接数 |
| threads 默认 / 上限 | 8 / 512 | 32 / 512 |
| 默认总连接 | 3 文件 × `8//3`=2 = **6 条** | 3 文件 × 32 = **96 条** |
| 分片公式 | 与 YunX 完全相同（移植） | 基准 |
| 745MB 文件默认片数 | **64 片**（只有 2 条连接在拉） | **256 片**（32 条连接） |
| 弹性区 / 慢连接抢占 / 断点 / SHA-256 | 与 YunX 相同 | 基准 |

一句话：分片、抢占、续传、校验都已和 YunX 一样；改造前真正不同的是 `threads` 是「每文件」还是「整任务」，以及默认值差 4 倍 —— 本次 change 已统一为每文件、默认 32。

**改造前的两个连带效应**

- `workers = min(max_tasks, 待下文件数)`（`__init__.py:827`）：单文件时反而拿到全部 8 条连接，3 个文件时每个只剩 2 条 —— 文件加得越多，单文件越慢。
- `per_file_threads` 在 `_limit_workers_for_task` 调整之后才计算；账号空间不足被下调并发文件数时，单文件连接数会随之变大。

### 3.2 实施后参数

| 项目 | 改造前 | 实施后 |
| ---- | ------ | ------ |
| threads 语义 | 整任务总连接 | 每文件连接数 |
| threads 默认值 | 8 | 32（与 YunX 一致） |
| threads 上限 | 512 | 512（不变） |
| 同时下载文件数默认 | 3 | 3（不变） |
| 同时下载文件数上限 | 8 | 64（YunX 自己是 10） |
| 默认总连接 | 6 | 96（3 × 32） |
| 分片公式 / 弹性区 / 抢占 / 断点 / 校验 | — | 不动（已经一致） |
| 前端文案 | 「并发连接数」含糊 | 「每文件连接数」，并说明总连接 = 文件数 × 该值 |

## 4. 最大下载文件数量：三种口径

1. **单任务文件总数：没有上限。** `_collect_files` 不截断（`__init__.py:940-1040`），`pending` 全量提交线程池。会截断的是递归深度和单目录页数。
2. **并发文件数：默认 3，实例上限 64，可由系统键配置。**

   | 位置 | 现值 | 说明 |
   | ---- | ---- | ---- |
   | `app/modules/netdisk/__init__.py:699` `int_value("max_tasks", base.max_tasks, 1, 64)` | 上限 64 | 实例级硬夹 |
   | `app/application/netdisk/settings.py:84-91` `NetdiskMaxConcurrentFiles` | 默认 3，夹 1–64 | 单任务并发文件数的系统默认值 |
   | `app/modules/netdisk/__init__.py:100` `NetdiskMaxConcurrentDownloads` | 默认 3，夹 1–16 | 只决定全局任务槽，不再影响 `max_tasks` |
   | `MoviePilot-Frontend/src/components/dialog/DownloaderInfoDialog.vue:549` `max="64"` | 上限 64 | 前端输入框 |
   | `zh-CN.ts:5849-5850` 等三语文案「范围 1-64」 | — | 文案 |

3. **搜索候选检查数：200 硬编码。** `app/modules/cloudsaver/__init__.py:40` `_MAX_ALIVE_CHECKS = 200`；系统设置 `NetdiskProbeLimit` 虽存在但**无使用点**（`settings.py:108` 只读入 tuning），属未接线配置。

**只调 `max_tasks` 不提速（单文件维度）**：每文件连接数由 `threads` 决定（`per_file_threads = threads`），调大 `max_tasks` 只是让更多文件同时下载，总连接 = 并发文件数 × `threads`；单个文件的快慢仍取决于 `threads`。

## 5. 改造边界与风险（已定案）

1. **全局在飞上限已改为动态推导。** 原固定 `_CHUNK_SLOTS=512`（`engine.py:60`）跨任务共享，64 文件 × 32 连接 = 理论 2048 会被 512 夹住；现按内存预算 `clamp(可用内存 × 25% ÷ 读缓冲, 512, 4096)` 计算，2048 可正常兑现。
2. **平台上限仍要生效。** `_PLATFORM_CONCURRENCY_CAP`（`__init__.py:65`）与适配器 `max_concurrency`（夸克 512、迅雷 8）继续夹住每文件连接数，迅雷不能被放到 32。
3. **系统设置键已拆分。** `NetdiskMaxConcurrentFiles` 只决定「单任务并发文件数」（默认 3，夹 1–64），`NetdiskMaxConcurrentDownloads` 只决定「全局任务槽」（默认 3，夹 1–16）；两者互不影响，实例上限 64 不再被系统键的 1–16 截断。
4. **账号空间会下调 workers。** `_limit_workers` 按「剩余空间 ÷ 最大单文件 × 1.2」压缩并发文件数（`__init__.py:166-185`），小号上 64 并发会被自动压回。
5. **内存预算已随在飞上限一起重算。** 512 在飞 × 64KB 读缓冲 ≈ 32MB；2048 在飞 ≈ 128MB，4096 在飞 ≈ 256MB。动态公式按可用内存收敛，避免小内存机器 OOM。
6. **最终在飞上限公式（已落地）。** 全局在飞 = `clamp(可用内存 × 25% ÷ 读缓冲, 512, 4096)`，读缓冲默认 64KB，首次使用时计算。64 文件 × 32 连接 = 2048 落在该区间内，不再被固定 512 夹住。

## 6. 非并发差异（已纳入本次 change）

| 项 | MoviePilot | YunX | 判定 |
| -- | ---------- | ---- | ---- |
| 分享目录递归深度 | 12（`__init__.py:66`） | 12（`ResolveViewModel.kt:713`） | 已修复，与 YunX 一致，不再漏深层目录 |
| 夸克分享翻页 | 50/页 × 200 页（`quark.py:62-63`） | 50/页 × 200 页 | 已修复，与 YunX 一致（旧 100/20 实测 300 条目录只读到 150 条） |
| 游客取链 | 已支持：无账号时用 `__pugs`，仅约 50MB 以内；`23018` 归 `UNSUPPORTED`；`inspect` 无账号仍抛 `AUTH_EXPIRED` | 支持 `__pugs`，约 50MB | 已补齐；有账号不进入游客分支 |
| 直链过期 | 立即换链并节流（`engine.py:669-693`） | 复用原 URL 重试 | MP 更优 |
| 0 字节文件 | 模块层直接拒绝（`__init__.py:1336`） | 会当成功保存 | MP 更优 |
| 转存临时目录名 | `mp_tr_<毫秒>`（`quark.py:757`） | `tr_<nanoTime>_<随机>` | MP 同毫秒并发可能撞名 |

## 7. 验收标准（给新 change 用）

**行为断言**

1. 3 文件并发、`threads=32` 时，每文件 32 条连接，任务总连接 96。
2. 745MB 文件在 `threads=32` 下片数为 256，不再出现「64 片只有 2 条连接」。
3. 单文件下载时连接数为 32（不是 8）。
4. `max_tasks` 可配到 64（后端实例配置 + 前端输入框 + 系统设置路径一致）。
5. 迅雷每文件连接数仍被夹到 8。
6. 全局在飞上限与内存预算匹配，且不成为新的隐性瓶颈。
7. 前端文案统一为「每文件连接数」，三语同步，明确「总连接 = 文件数 × 该值」。
8. 递归深度与翻页参数调整后，深层目录与 >100 条目录不再漏文件。

**建议测试**

- `tests/test_netdisk_engine.py`：`plan_chunk_count(total, threads=32)` 断言；`threads` 语义变更后的片数/连接数。
- `tests/test_netdisk_module.py`：`per_file_threads` 计算、`max_tasks` 夹取、平台上限夹取。
- `tests/test_netdisk_adapters_quark.py`：翻页参数与「整页无新条目」停止条件。
- 前端：`DownloaderInfoDialog` 输入上限与三语文案。

## 8. 待定决策（已定案）

1. 全局在飞上限：已改为按内存预算动态推导，`clamp(可用内存 × 25% ÷ 读缓冲, 512, 4096)`。
2. 已拆成两个键：`NetdiskMaxConcurrentFiles`（单任务并发文件数，默认 3、夹 1–64）与 `NetdiskMaxConcurrentDownloads`（全局任务槽，默认 3、夹 1–16）。
3. `max_tasks=64` 全平台放开，平台差异由适配器 `max_concurrency` 继续夹住（迅雷每文件 8）。
4. 游客取链已纳入本次范围并实现（仅无账号、约 50MB 以内）。
5. 递归深度与翻页修复与并发改造同一 change 完成。

## 9. 未验证项

- 未用真实夸克账号实测完整取链与下载；服务端单页 50 条上限已由「旧 100/20 实测 300 条目录只读到 150 条」间接确认。
- 「单连接 40~80KB/s、少数 3~7KB/s」「超过 8 路 Range 被降级为 200」来自 YunX 代码注释与文档记录，未独立复现。
- 并发上限、抢占阈值在不同网络与账号档位下的最优值未测量；目标值只能作为起点。
- 64 并发文件对夸克风控的实际影响未知。

## 10. 证据索引

| 主题 | MoviePilot | YunX |
| ---- | ---------- | ---- |
| 分享解析 | `app/domain/netdisk.py:375-439` | `ShareLinkParser.kt:21-66` |
| stoken | `quark.py:358-390` | `QuarkApi.kt:110-124` |
| 列目录/翻页 | `quark.py:395-458` | `QuarkApi.kt:139-162`、`QuarkResolveRepository.kt:34-52` |
| 转存 | `quark.py:647-711` | `QuarkApi.kt:333-373` |
| 取直链 | `quark.py:510-568` | `QuarkApi.kt:416-443` |
| 请求头 | `quark.py:586-588`、`base.py:258-287` | `ResolveViewModel.kt:1972-1981` |
| 分片参数 | `app/modules/netdisk/engine.py:31-60` | `DownloadManager.kt:269-277`、`:3750-3769` |
| 抢占 | `engine.py:880-924` | `DownloadManager.kt:154-176`、`:457-530` |
| 断点/校验 | `engine.py:345-395`、`:926-936` | `DownloadManager.kt:2000-2029`、`:3610-3643` |
| 并发 | `__init__.py:96-104,166-185,699,827-830` | `SettingsRepository.kt:19-24,59-63` |
| 完成判定 | `__init__.py:142-163,862-873` | `DownloadManager.kt:3610-3637` |
| 递归深度 | `__init__.py:66` | `ResolveViewModel.kt:713` |
