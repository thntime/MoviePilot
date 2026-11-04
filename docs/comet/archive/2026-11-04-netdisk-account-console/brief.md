# 目标

修复网盘账号「登录一会儿就掉线」的会话保活缺陷，并重做网盘账号控制台的布局与账号管理交互：平台作为带图标、默认折叠的一级菜单，账号详情改为「账号信息 70% / 存储空间 30%」左右布局并在下方内嵌完整文件浏览；新增每 30 分钟的空间信息刷新任务；新增与编辑账号字段对齐（Cookie、CookieCloud、账号密码、短信、网页登录）；平台名称统一中文；修复迅雷用户名识别与扫码登录成功文案，消除网页登录弹窗的上下空白。

# 范围

## Source coverage

覆盖边界：用户本次消息中的 7 条需求条目与 2 张截图；YunX-Desktop 链接仅覆盖「凭据存储 / 续期 / 失效处理」相关实现，作为保活机制的参考，不覆盖其 UI、下载或其他功能。

| 来源条目与位置 | 读取状态 | 需要保留的内容 | Spec 位置 | 验收 ID | 覆盖状态 | 理由或替代关系 |
| --- | --- | --- | --- | --- | --- | --- |
| S1：需求第 1 条 | complete | 参照 yunx 刷新账号登录 Cookie 保持登录；排查本项目与 yunx 差异；解决登录一会就掉线 | specs/netdisk-account-console/spec.md 的「凭据保活」 | A1、A2、A3 | covered | 当前有效需求 |
| S2：需求第 2 条 | complete | 网盘类型为一级菜单、默认全部合上、左侧带图标、展开后二级为账号；账号信息与存储空间左右 70/30；展示账号名称与最近检测时间、去掉过期时间；Cookie 默认展示；去掉浏览文件按钮，在账号信息/存储空间下方展示文件信息；去掉网盘文件菜单；检测连通性/刷新令牌/设置主账号与编辑账号同级；删除账号在编辑账号右侧；新增每 30 分钟刷新空间信息的定时任务 | specs/netdisk-account-console/spec.md 的「控制台导航」「账号详情」「文件信息」「空间刷新」 | A4、A6、A7、A8、A9 | covered | 当前有效需求 |
| S3：需求第 3 条 | complete | 编辑账号名称对应不上；新增时有的登录方式（CookieCloud、网页登录、手机登录等）编辑时也必须有 | specs/netdisk-account-console/spec.md 的「账号新增与编辑」 | A10 | covered | 当前有效需求 |
| S4：需求第 4 条 | complete | 新增账号时网盘平台需要显示中文 | specs/netdisk-account-console/spec.md 的「平台显示名」 | A11 | covered | 当前有效需求 |
| S5：需求第 5 条 + 图 2 | complete | 网页登录弹窗上下空白过大，需要贴合内容 | specs/netdisk-account-console/spec.md 的「网页登录弹窗」 | A12 | covered | 当前有效需求 |
| S6：需求第 6 条 | complete | 迅雷网盘识别不到用户名 | specs/netdisk-account-console/spec.md 的「迅雷用户名」 | A5 | covered | 当前有效需求 |
| S7：需求第 7 条 | complete | 登录成功下方显示「当前登录用户:xxxx」而不是状态/用户名文案 | specs/netdisk-account-console/spec.md 的「网页登录弹窗」 | A13 | covered | 当前有效需求 |
| S8：图 1（当前账号控制台截图） | complete | 现状布局参照：左侧平台分组+账号条目、右侧详情含账号信息/存储空间/平台能力、浏览文件与编辑账号在头部、检测/刷新/主账号按钮一组、删除账号独立在底部 | specs/netdisk-account-console/spec.md 的「控制台导航」「账号详情」 | A6、A7、A8 | covered | 作为现状与目标布局的对照 |
| S9：图 2（当前网页登录弹窗截图） | complete | 现状问题参照：二维码整页截图上下留有大块空白，倒计时与等待提示被推到底部 | specs/netdisk-account-console/spec.md 的「网页登录弹窗」 | A12 | covered | 作为现状问题证据 |
| S10：https://github.com/tidain/YunX-Desktop | complete | 参考实现的保活设计：凭证持久化、用前惰性续期、响应 Set-Cookie 回写、鉴权失败就地刷新重试一次、刷新失败保留旧凭证 | specs/netdisk-account-console/spec.md 的「凭据保活」 | A1、A2、A3 | covered | 实现参考，不是逐条需求来源 |

## 目录结构

### 新建

- `app/application/netdisk/keepalive.py` — 统一凭据续期策略：按平台 TTL 半衰期惰性刷新、响应 Set-Cookie 合并回写、鉴权失败刷新后重试一次、刷新失败回退旧凭据。
- `tests/test_netdisk_keepalive.py` — 惰性续期节流、Set-Cookie 回写按请求凭据定位账号、401 重试一次、失败保留旧凭据。
- `tests/test_netdisk_quota_refresh.py` — 30 分钟空间刷新任务：全账号遍历、单账号失败隔离、不写状态不清凭据。
- `tests/test_netdisk_account_edit.py` — 编辑账号：按平台解析凭据文本、状态恢复 active、名称与昵称同步、CookieCloud/密码/短信/扫码重新授权、明文凭据接口权限。
- `MoviePilot-Frontend/src/components/setting/NetdiskPlatformGroup.vue` — 左侧平台一级菜单：平台图标、折叠状态、账号子项。
- `MoviePilot-Frontend/src/components/setting/NetdiskAccountFiles.vue` — 账号详情内嵌文件浏览面板（列表、目录导航、分享/移动/重命名/删除）。
- `MoviePilot-Frontend/src/composables/useNetdiskFileBrowser.ts` — 从 `NetdiskBrowser.vue` 抽出的文件浏览状态与操作，供内嵌面板复用。

### 修改

- `app/adapters/external/netdisk/base.py` — 扩展续期契约（`supports_refresh`、本地过期时间解析）、统一响应 Set-Cookie 回写钩子与鉴权失败重试入口。
- `app/adapters/external/netdisk/quark.py`、`app/adapters/external/netdisk/uc.py` — 接入 90 分钟惰性续期与 Set-Cookie 回写，保持现有 `refresh_account` 行为兼容。
- `app/adapters/external/netdisk/guangya.py` — 使用登录时已保存的 `refresh_token` 在临期前续期，写回新 token 与过期时间。
- `app/adapters/external/netdisk/xunlei.py` — 保存并使用 `refresh_token`，按 JWT `exp` 预刷新与 401 重试；`login_account_name` 兼容 `data.user` 等嵌套结构。
- `app/adapters/external/netdisk/login/browser.py` — 迅雷网页登录读取并保存 `refresh_token`、`expires_in`，与光鸭保持一致。
- `app/application/netdisk/accounts.py` — 状态恢复（重新登录/改凭据后置 active）、`unknown` 不覆盖 `active`、启动后首跑、`refresh_quotas()` 与昵称写回。
- `app/application/netdisk/login.py` — 编辑已有账号时同步名称/昵称/状态；复用 `_credential_from_text` 解析裸 JWT。
- `app/application/netdisk/contract.py` — 账号存储与适配器端口扩展（quota 写入、状态重置、凭据方式、明文凭据读取）。
- `app/db/adapters/netdiskaccount.py` — `update_account` 支持状态与昵称更新，新增 quota 写入，探测 `unknown` 不降级 `active`。
- `app/api/endpoints/netdisk.py` — 新增单账号详情接口（返回明文凭据，仅超管）；编辑接口按平台解析凭据；新增 CookieCloud/密码/短信/扫码重新授权路径。
- `app/schemas/netdisk.py` — 更新请求模型扩展（凭据文本、登录方式、昵称）与账号详情模型。
- `app/scheduler/catalog.py`、`app/scheduler/services.py`、`app/scheduler/chain.py`、`app/startup/initializers/scheduler.py` — 注册并装配每 30 分钟的空间刷新任务，启动后首次执行。
- `MoviePilot-Frontend/src/views/setting/AccountSettingNetdisk.vue` — 主从布局、平台分组折叠、移动端分组中文名。
- `MoviePilot-Frontend/src/components/setting/NetdiskAccountList.vue` — 平台分组折叠与图标、账号条目字段调整。
- `MoviePilot-Frontend/src/components/setting/NetdiskAccountDetail.vue` — 70/30 左右布局、字段与按钮调整、内嵌文件浏览。
- `MoviePilot-Frontend/src/components/dialog/NetdiskAccountDialog.vue` — 平台中文名、编辑模式字段对齐新增、扫码弹窗高度、登录成功文案。
- `MoviePilot-Frontend/src/components/cards/NetdiskAccountCard.vue` — 移动端字段与菜单对齐。
- `MoviePilot-Frontend/src/utils/netdisk.ts` — 平台中文名、图标与排序映射。
- `MoviePilot-Frontend/src/api/netdisk.ts` — 单账号详情（明文凭据）、编辑/重新授权接口封装。
- `MoviePilot-Frontend/src/router/i18n-menu.ts`、`MoviePilot-Frontend/src/pages/setting.vue` — 移除「网盘文件」设置 tab 及其分支。
- `MoviePilot-Frontend/src/locales/zh-CN.ts`、`zh-TW.ts`、`en-US.ts` — 新增/调整 `netdisk.*` 文案，三份同步。

### 删除

- `MoviePilot-Frontend/src/views/netdisk/NetdiskBrowser.vue`、`MoviePilot-Frontend/src/pages/netdisk.vue` 及 `src/router/index.ts` 中的 `/netdisk` 路由 — 文件浏览并入账号详情后不再保留独立页面与入口。

### 明确不建

- 不新增数据库迁移或列 — 复用现有 `quota_used`、`quota_total`、`last_checked_at`、`nickname`、`status`。
- 不为 115/123/139/百度伪造刷新接口 — 平台无 refresh 能力，只在鉴权失效时给出「请重新登录」状态。
- 不新增空间刷新周期配置项 — 按用户要求固定 30 分钟。
- 不引入新的外部图标库或 CDN — 复用现有离线图标资源与平台配色。
- 不拆分 Supervisor — 前后端围绕同一账号控制台与账号生命周期，拆分会重复修改同一批验收项。

# 非目标

- 不改变网盘分享搜索、转存、下载、整理与失败对账链路。
- 不改变站点 Cookie（CookieCloud 同步）逻辑与下载器账号管理。
- 不实现账号启用/停用开关、账号分组标签或批量操作。
- 不重做主侧边栏导航与移动端整体布局，只调整网盘账号页面内部结构。
- 不新增账号详情页独立路由。

# 验收示例

- A1 凭据保活：夸克/UC 在每次取凭证前按 90 分钟窗口惰性续期；任意 API 响应带回新 Cookie 时合并并回写数据库；鉴权失效时刷新后重试一次；刷新失败保留旧凭据，不清空账号。
- A2 令牌续期：光鸭使用已保存的 `refresh_token` 在临期前自动续期并写回；迅雷网页登录保存 `refresh_token`，在 access_token 临期（JWT `exp` 前 60 秒）或 401 时续期并写回；续期失败保留旧凭据并提示重新登录。
- A3 状态恢复与启动首跑：探测返回 `unknown` 不再覆盖 `active`；重新登录或编辑凭据成功后账号状态恢复 `active` 并重新进入可选账号池；进程启动后立即执行一次账号刷新/探测，而不是等待 6 小时。
- A4 空间定时刷新：新增每 30 分钟执行的定时任务，遍历全部账号调用平台空间接口并写回已用/总空间；单账号失败只记录告警，不中断其他账号，不修改账号状态、不清空凭据。
- A5 迅雷用户名：迅雷账号在新增、探测与刷新时都能从平台用户信息（含 `data.user` 等嵌套结构）或已保存凭据解析出昵称并写入；解析失败时保留已有昵称而不是清空。
- A6 控制台导航：网盘账号控制台左侧以网盘平台为一级菜单，带平台图标，默认全部折叠；点击展开后显示该平台下的账号；移动端同样按平台分组并可折叠。
- A7 账号详情布局：选中账号后，详情顶部为账号信息（左，约 70%）与存储空间（右，约 30%）并排；账号信息展示账号名称、平台昵称与最近检测时间，不展示过期时间；Cookie 完整明文默认展示并提供复制；账号名称取用户命名。
- A8 操作按钮：检测连通性、刷新令牌、设置主账号与编辑账号位于同一操作行；删除账号位于编辑账号右侧；不再出现「浏览文件」按钮。
- A9 文件信息：在账号信息与存储空间下方内嵌完整文件浏览（列表、进入子目录、返回上级、分享、移动、重命名、删除）；系统设置中的「网盘文件」tab 与独立 `/netdisk` 页面移除。
- A10 新增与编辑一致：编辑账号可修改与新增相同的登录方式字段（Cookie、CookieCloud、账号密码、短信、网页登录），平台保持锁定；保存后账号名称立即更新并在列表与详情同步；后端编辑接口按平台解析凭据文本（裸 JWT 识别为 token）。
- A11 平台中文名：新增账号的平台下拉、对话框副标题、列表分组、详情平台标签与移动端分组均显示中文平台名，不再出现 `quark`、`pan139` 等英文 id。
- A12 网页登录弹窗：扫码等待态不再出现上下大块空白，弹窗内容高度随二维码或整页截图自适应，底部倒计时与等待提示紧贴内容；整页截图与 noVNC 两种方式均不溢出。
- A13 登录成功文案：扫码登录检测到登录态后，弹窗下方显示「当前登录用户：<昵称或账号名>」，不再显示状态说明或英文键名；拿不到用户名时显示「当前登录用户：未知」。
- A14 凭据读取权限：账号列表接口继续返回脱敏凭据；完整 Cookie 只通过超级管理员鉴权的单账号详情接口返回；日志、告警与错误信息不出现 Cookie/token 明文。

# 关键决定

- 保活采用「用前惰性续期 + 响应 Set-Cookie 回写 + 鉴权失败重试一次」（用户确认 Q1 选 A）；不新增账号心跳定时任务，保留 6 小时探测但启动即跑一次。
- 文件信息采用内嵌完整浏览并删除独立「网盘文件」页面（用户确认 Q2 选 A）。
- Cookie 在账号详情中默认完整明文展示并可复制（用户确认 Q3 选 B）；仅详情接口返回明文且沿用超管鉴权，列表接口保持脱敏，明文不写日志与告警。
- 编辑账号平台锁定、登录方式全开放（用户确认 Q4 选 A）：Cookie、CookieCloud、账号密码、短信、网页登录均可对已有账号重新授权。
- 一级菜单指网盘账号控制台左侧的账号栏（图 1），不改主侧边栏入口，仍从「设置 → 网盘账号」进入。
- 空间刷新固定 30 分钟执行一次，覆盖全部账号，不新增配置项。
- 账号名称（用户命名 `name`）与平台昵称（`nickname`）分离展示；编辑保存的名称立即生效。
- 不拆分 Supervisor：前后端改动围绕同一账号控制台与账号生命周期，验收项跨前后端，拆分会重复修改同一批验收项。
- 前端改动位于兄弟仓库 `MoviePilot-Frontend`（当前 `v3` 工作区），与后端同批实现；按用户要求不自动提交。

# 约束与不变量

- Cookie/token 明文只允许出现在超管鉴权的详情接口响应与前端页面展示中；不写入日志、告警、错误信息或测试快照。
- 网络错误、超时与鉴权失效必须区分处理：前者不清凭据、不改状态，后者才标记 `expired` 并提示重新登录。
- 定时任务在测试中使用临时 `CONFIG_DIR`，不触碰真实数据库、真实网盘接口或用户配置。
- 新增行为必须有聚焦 pytest 覆盖；不修改既有测试断言来掩盖回归。
- 前端三份 locale 文件同步；不引入新的运行时依赖。
- 不启动服务、不调用真实网盘接口做验证；适配器行为用 mock 或录制数据覆盖。
- 若涉及模块归属或 import 变化，先跑 `tests/test_architecture_dependencies.py`。

# 验证预期

- 后端：聚焦 pytest（`tests/test_netdisk_keepalive.py`、`tests/test_netdisk_quota_refresh.py`、`tests/test_netdisk_account_edit.py` 及受影响的既有网盘用例）；改动文件 pylint；涉及归属/import 时先跑架构测试。
- 前端：`npm run typecheck`、聚焦 `npm run test:run`、`npm run lint`。
- 手工核对：不启动服务，按验收项逐条对照实现与组件结构。
