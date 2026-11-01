# 网盘全平台融合方案（MoviePilot 自研内部下载器）

目标：把夸克之外的 UC / 139 / 123 / 115 / 光鸭 / 蓝奏 / 蓝奏优享 / 百度 / 迅雷 也接进 MoviePilot，做到「搜到就能下」。
参考实现：`tidain/YunX-Desktop` 1.3.3（`87ff5d6`，2026-10-31）。该版本新增 P2P 下载，网盘取链与下载逻辑未变。

## 1. 现状差距

| 事实 | 位置 | 影响 |
| ---- | ---- | ---- |
| 搜索已支持多平台，但只放行夸克 | `app/modules/cloudsaver/__init__.py:31`（`_ALLOWED_PLATFORMS = frozenset({"quark"})`） | 其他平台的候选在构造阶段就被丢弃 |
| 有效性检查只有夸克实现 | `app/modules/cloudsaver/cloudsaver.py:474`（`/api/quark/share-info`）、`__init__.py:499`（`QuarkShareClient`） | 没有账号/接口的平台无法判定存活 |
| 下载链没有 netdisk 分支 | `app/chain/download/submission.py:669-745`（只处理种子与显式直链） | 分享链接会被当 `.torrent` 抓取，拿到 HTML 后判「种子数据有误」 |
| 无任何网盘账号存储 | 全仓 `grep -ri quark` 只命中 CloudSaver | 取链与转存都缺凭据 |

结论：要「融合所有网盘」，缺的不是下载器，而是**每平台的取链适配层 + 账号层 + 有效性检查**。

## 2. 目标架构

```text
app/chain/search ──► 候选构造 + 有效性检查 ──┐
                                             ├─► app/application/netdisk/（用例与 Port）
app/chain/download ──► netdisk 分支取直链 ───┘        │
                                                       ▼
                                        app/adapters/external/netdisk/<platform>.py
                                        （HTTP：RequestUtils，平台私有协议）
                                                       │
app/modules/<downloader>/ ◄── 多行直链 / 内部引擎 ──────┘
```

四条边界：

1. **适配层只做协议**：解析分享、列目录、取直链、转存、清理。不碰数据库、不碰调度。
2. **用例层做编排**：选账号、决定「免转存 / 转存」、刷新令牌、错误分类、限流。
3. **下载层只收直链**：内部多线程引擎或 aria2，不知道来源是哪个网盘。
4. **模块层只做注册**：下载器模块负责被 MoviePilot 发现与调度。

## 3. 统一接口（Python 草案）

```python
class NetdiskAdapter(Protocol):
    platform: str                      # quark / uc / pan123 ...
    link_policy: LinkPolicy            # DIRECT / DIRECT_WITH_FALLBACK / TRANSFER_REQUIRED
    requires_account: bool             # 取链是否必须登录态
    guest_supported: bool              # 是否支持匿名取链

    def parse_share(self, url: str) -> ShareRef: ...
    def share_detail(self, ref: ShareRef, account: Account | None) -> ShareDetail: ...
    def list_files(self, ref: ShareRef, dir_id: str, account: Account | None) -> list[ShareFile]: ...
    def resolve(self, ref: ShareRef, file: ShareFile, account: Account | None) -> DirectLink: ...
    def cleanup(self, handle: TransferHandle, account: Account | None) -> None: ...
```

- `DirectLink` 必须带：`url`、`headers`（Cookie/UA/Referer，按平台装配）、`size`、`expires_at`、`cleanup_handle`。
- `LinkPolicy` 决定调度行为：`DIRECT` 不做空间校验；`DIRECT_WITH_FALLBACK` 失败时才转存；`TRANSFER_REQUIRED` 必做空间校验与清理。
- 错误必须分类为：`AUTH_EXPIRED` / `SHARE_INVALID` / `RATE_LIMITED` / `RISK_BLOCKED` / `LINK_EXPIRED` / `UNSUPPORTED`，上层据此决定刷新、重试或放弃。

## 4. 平台矩阵

| 平台 | 取链策略 | 登录 | 特殊依赖 | 难度 |
| ---- | -------- | ---- | -------- | ---- |
| 夸克 | 免转存优先，失败回退转存 | 必须（游客仅小文件） | `__puus` 约 3h 过期需刷新；Referer 固定 | 中 |
| UC | 直取（从不转存） | 必须（游客亦放行） | 视频走 `video_preview` 取原画 | 中 |
| 139 | 直取 | 必须 | Cookie 内含账号与 authorization | 中 |
| 123 | 直取 | 必须 | access token | 低 |
| 115 | 网页端直取，大文件 `50029` 回退转存 | 未登录可直取，回退需登录 | 下载需「登录 Cookie + 900 秒 CDN Cookie」双拼 + 客户端 UA | 中高 |
| 光鸭 | 默认直取，受限分享回退转存 | 回退时需登录 | 分享者未开「免登录下载」时才转存 | 中 |
| 蓝奏 | 直取 | 匿名 | 该平台不支持转存 | 低 |
| 蓝奏优享 | 直取 | 匿名（userId 传空） | AES 生成 downloadId | 低 |
| 百度 | **必须转存** | 必须 | 转存 → `locatedownload` 拿 appall 直链 → 立即删转存；下载需 BDUSS + 手机 UA | 高 |
| 迅雷 | **必须转存** | 必须 | 转存 → 详情取直链 → 删临时文件；需 access_token + 设备指纹 + captcha | 高 |

证据：`ShareResolveRepository.kt` 的接口注释与各 `*ResolveRepository.kt` 的 `getShareDownloadLink` 实现。

## 5. 账号与凭据

- 存储：新增 `netdisk_account` 表（Alembic 迁移），字段：`platform`、`name`、`credential_enc`（Fernet 加密后的 JSON）、`is_primary`、`status`、`expires_at`、`updated_at`。加密复用 `app/application/security/token.py` 的 Fernet 能力，密钥走项目既有配置项。
- 多账号：同一平台可存多条，主账号优先；多账号多源属于增强项，默认关闭。
- 刷新：各平台刷新逻辑放在 adapter 内（夸克剥 `__puus` 请求 `/config`；UC/115/百度按各自接口），用例层只负责「失败 → 调用刷新 → 重试一次」。
- 脱敏：日志与异常只输出平台、账号名、错误分类，不输出 Cookie/token。

## 6. 接入点（文件级）

| 位置 | 改动 |
| ---- | ---- |
| `app/modules/cloudsaver/__init__.py:31` | 放开 `_ALLOWED_PLATFORMS`，改为「平台是否已实现 adapter」动态判断 |
| `app/modules/cloudsaver/__init__.py:499` | 有效性检查从 `QuarkShareClient` 改为按 `netdisk_platform` 分派到用例层 |
| `app/modules/cloudsaver/cloudsaver.py:474` | 夸克 `share-info` 保留；其他平台走自建 adapter |
| `app/chain/search/provider.py` | 候选构造时补 `netdisk_platform` 与 `share_password`（字段已存在） |
| `app/chain/download/submission.py:669` | 增加 netdisk 分支：`download_type=="netdisk"` → 取链 → 投递下载器 |
| `app/schemas/context.py:381` | `TorrentInfo` 已有 `download_type` / `netdisk_platform` / `share_password`，无需改结构 |
| 新目录 `app/application/netdisk/` | 用例、Port、错误分类、账号服务 |
| 新目录 `app/adapters/external/netdisk/` | 每平台一个 adapter，HTTP 走 `RequestUtils` |
| 下载器 | 内部引擎作为新下载器模块（需在 `DownloaderType` 加成员），或先把直链投给 aria2 |
| 前端 | 账号管理页、平台筛选、下载器配置 |

## 7. 下载引擎

取链完成后有两条路：

- **先复用 aria2**：多行直链投递，补 UA/Referer 与 `split` / `max-connection-per-server` 选项，每个任务独立子目录（`app/modules/aria2/__init__.py:278-284`、`:610-617`）。
- **再换内部引擎**：按《夸克网盘下载加速原理说明》第 4、5 节实现分片调度、慢连接抢占、断点续传与分片校验。

两条路对上层接口一致：`submit(direct_links, headers, target_dir, label)`。

## 8. 分阶段实施

| 阶段 | 范围 | 验收 |
| ---- | ---- | ---- |
| P0 | 抽象层 + 账号表 + 夸克打样（把现有夸克逻辑迁进 adapter） | 夸克分享端到端可下；其他平台行为不变 |
| P1 | 免转存平台：UC、123、139、115、光鸭、蓝奏、蓝奏优享 | 每平台一个真实分享可下；无账号时给出明确错误而非静默丢弃 |
| P2 | 必须转存平台：百度、迅雷 | 转存 → 取链 → 下载 → 清理全链路；空间不足有明确提示 |
| P3 | 内部下载引擎 + 多账号多源 + 前端完善 | 尾部不塌速；多账号开关关闭时行为与 P2 一致 |

每阶段独立可验收，避免一次性改动过大。

## 9. 替代方案与取舍

| 方案 | 优点 | 代价 |
| ---- | ---- | ---- |
| 自研 adapter（推荐） | 唯一能拿到「免转存」的路径；可控错误分类与限流 | 10 个平台，接口随时变，维护成本高 |
| 用 OpenList/AList 统一后端 | 平台覆盖现成 | 需要用户先把分享转存进自己网盘；单流下载无加速；分享链接不能直接喂给它 |
| 只做 3~4 个主力平台 | 工作量降一个量级 | 其余平台仍然「能搜不能下」 |

建议：P0+P1 先覆盖免转存的 7 个平台（除百度/迅雷），它们不需要转存，风控面也小；百度/迅雷按实际需求再上。

## 10. 风险

- **接口漂移**：每个平台都是非公开接口，字段与签名策略会变。对策：错误分类标准化 + 每平台独立 adapter + 失败可观测。
- **账号安全**：Cookie/token 等价于登录态，且 10 平台都要存。对策：加密列、脱敏日志、最小权限、支持一键清除。
- **风控**：并发下载 + 自动转存更容易触发限流/封号。对策：并发保守、取链节流、失败退避、转存后立即清理。
- **有效性检查的判定口径**：现在「无法判定一律丢弃」；多平台且无账号时会把结果全部丢光。对策：区分 `dead` / `alive` / `unknown`，`unknown` 保留并标注。

## 11. 工作量估算

| 项 | 规模 |
| ---- | ---- |
| 抽象层 + 账号表 + 迁移 | 800–1200 行 |
| 每平台 adapter（免转存类） | 200–400 行/平台 |
| 百度 / 迅雷 adapter | 500–800 行/平台 |
| 内部下载引擎 | 800–1200 行 |
| 测试（mock 回放，不打真网） | 与实现同量级 |

单人节奏下，P0–P2 大致 3–5 周，P3 再加 2–3 周。主要不确定性来自平台接口与账号联调，而不是代码量。

## 12. 未验证项

- 未在 MoviePilot 中真实联调任何平台；本文平台结论来自 YunX 1.3.3 源码阅读。
- 各平台在 MoviePilot 部署环境（可能走代理、可能无公网）下的可达性未验证。
- 百度 appall 直链、迅雷设备指纹等机制对客户端环境敏感，实际成功率需实测。
