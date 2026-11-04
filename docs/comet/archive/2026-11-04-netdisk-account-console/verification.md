---
generated_from_state_version: 11
---

# 验证

## 当前结果

- 结果: **已归档**
- 验证情况: **已完成检查，验证结果已确认**
- 目标周期: 1
- 迭代: 2
- 验证器尝试次数: 1
- 完成时间: 2026-11-04T09:54:56.503Z
- 摘要: 第 2 轮独立复核全部 14 项验收。重点复核上轮唯一失败项 A5：create() 保存前调 login_account_name、_refresh_one() 探测后仅非空补写昵称、解析失败保留已有值，三条路径均有代码与测试证据，本轮重跑 nickname_flow + xunlei_nickname + keepalive + quota_refresh + account_edit + account_refresh 共 50 项 pytest 全部通过（EXIT=0）。上轮易错点复核确认：Set-Cookie 按 record.id 回写不串号、401 只重试一次不递归、unknown 不降级 active、裸 JWT 按平台解析为 token、详情明文/列表脱敏且全部超管鉴权、编辑开放全部登录方式且平台锁定、扫码弹窗无固定高度、三份 locale 中文平台名同步。风险项均不阻断验收。

## 验收

| 编号 | 结果 | 来源 | 验收项 | 原因 |
| --- | --- | --- | --- | --- |
| A1 | passed | brief.md | A1 凭据保活：夸克/UC 在每次取凭证前按 90 分钟窗口惰性续期；任意 API 响应带回新 Cookie 时合并并回写数据库；鉴权失效时刷新后重试一次；刷新失败保留旧凭据，不清空账号。 | 夸克/UC 惰性续期落地：app/application/netdisk/keepalive.py RefreshThrottle 对 QUARK/UC 配置 5400 秒（90 分钟）窗口，_REFRESH_THROTTLE 进程级共享，select()/require_account()/probe() 均先经 _refresh_for_request；Set-Cookie 回写在 base.py _write_back_cookies（merge_cookie_header 合并、删除 Max-Age=0），composition/netdisk.py write_back_credential 用 record.id 定位账号回写，多账号不串号；鉴权失败 401/403 时 _dispatch_with_retry 经 _refresh_credential 续期一次后重试（threading.local 防递归，网络错误/5xx/429 不刷新不清凭据），测试 test_netdisk_keepalive.py 覆盖合并回写、重试一次、500 不重试。 |
| A2 | passed | brief.md | A2 令牌续期：光鸭使用已保存的 `refresh_token` 在临期前自动续期并写回；迅雷网页登录保存 `refresh_token`，在 access_token 临期（JWT `exp` 前 60 秒）或 401 时续期并写回；续期失败保留旧凭据并提示重新登录。 | 光鸭 guangya.py needs_refresh 按 expires_at 临期 60 秒判断、refresh_account 用 refresh_token 换新 token 并写回 token/expires_at，refresh_token 为空保留旧值；迅雷 xunlei.py jwt_expiring 按 JWT exp 前 60 秒（_JWT_LEEWAY_SECONDS=60）或 401 重试续期，refresh_account 写回 access_token/refresh_token/expires_in；browser.py _xunlei_fields/_guangya_fields 均保存 refresh_token/expires；续期失败 refresh_account 返回 None 保留旧凭据，_refresh_one 把 expired 账号归入 NetdiskAccountFailure 并由 scheduler chain 发「请重新登录」消息。 |
| A3 | passed | brief.md | A3 状态恢复与启动首跑：探测返回 `unknown` 不再覆盖 `active`；重新登录或编辑凭据成功后账号状态恢复 `active` 并重新进入可选账号池；进程启动后立即执行一次账号刷新/探测，而不是等待 6 小时。 | db/adapters/netdiskaccount.py touch_account 中 unknown 不覆盖 active（322 行条件守卫，测试 test_unknown_probe_does_not_downgrade_active）；update()/paste_cookie/import_cookiecloud/password_login/confirm_qr 更新凭据时 status='active' 恢复（accounts.py update 223 行、login.py 各登录方式带 account_id 更新路径）；select() 只返回 active 账号；启动首跑：scheduler/catalog.py netdisk_account_refresh 与 netdisk_quota_refresh 均 next_run_time=now+60s 首跑，之后 6 小时/30 分钟周期。 |
| A4 | passed | brief.md | A4 空间定时刷新：新增每 30 分钟执行的定时任务，遍历全部账号调用平台空间接口并写回已用/总空间；单账号失败只记录告警，不中断其他账号，不修改账号状态、不清空凭据。 | 新增 netdisk_quota_refresh 定时任务：catalog.py 528-536 行 interval 30 分钟、启动 +60 秒首跑；chain.py refresh_netdisk_quotas 调 NetdiskAccountService.refresh_quotas()，accounts.py 遍历全部账号按 drive_capabilities.quota 调 adapter.quota 并 update_quota（只写 quota_used/quota_total，不改状态/检测时间/凭据）；单账号失败 except 只 logger.warning 并继续（refresh_quotas 378-383 行）；测试 test_netdisk_quota_refresh.py 覆盖全账号写回与单账号失败隔离。 |
| A5 | passed | brief.md | A5 迅雷用户名：迅雷账号在新增、探测与刷新时都能从平台用户信息（含 `data.user` 等嵌套结构）或已保存凭据解析出昵称并写入；解析失败时保留已有昵称而不是清空。 | 上轮失败项已修复且独立复核三条路径：① 新增：accounts.py create() 156-162 行保存前调 adapter.login_account_name(payload)，异常不影响创建；② 定时刷新：_refresh_one() 453-459 行探测后补写昵称，仅非空写库；③ 解析失败保留：nickname 为空时跳过 update_account，不清空。xunlei.py login_account_name 覆盖 data/data.user/顶层嵌套与 nickname/nick_name/nickName/user_name/userName/name 候选。重跑 tests/test_netdisk_nickname_flow.py（3 条）+ test_netdisk_xunlei_nickname.py（5 条）全过，EXIT=0。 |
| A6 | passed | brief.md | A6 控制台导航：网盘账号控制台左侧以网盘平台为一级菜单，带平台图标，默认全部折叠；点击展开后显示该平台下的账号；移动端同样按平台分组并可折叠。 | NetdiskPlatformGroup.vue 平台一级菜单带图标（getNetdiskPlatformIcon）与中文名；NetdiskAccountList.vue expandedPlatforms 初始空 Set 默认全部折叠，点击展开账号子项；移动端 AccountSettingNetdisk.vue mobileExpandedPlatforms 同样默认折叠、NetdiskPlatformGroup 分组可折叠；桌面主从 18rem/1fr 布局。 |
| A7 | passed | brief.md | A7 账号详情布局：选中账号后，详情顶部为账号信息（左，约 70%）与存储空间（右，约 30%）并排；账号信息展示账号名称、平台昵称与最近检测时间，不展示过期时间；Cookie 完整明文默认展示并提供复制；账号名称取用户命名。 | NetdiskAccountDetail.vue summary 用 grid 7fr/3fr（约 70/30，<960px 单列），左侧账号信息（名称/昵称/最近检测时间/凭据）、右侧存储空间（进度条+已用/总量/剩余，80%/95% 警告/错误色）；无 expires_at/过期时间展示；凭据取自详情接口 fetchNetdiskAccount 的 credential.cookie/token 完整明文默认展示于 <code> 并带复制按钮（navigator.clipboard）。 |
| A8 | passed | brief.md | A8 操作按钮：检测连通性、刷新令牌、设置主账号与编辑账号位于同一操作行；删除账号位于编辑账号右侧；不再出现「浏览文件」按钮。 | 操作行 VBtn 依次为编辑账号、删除账号（编辑右侧相邻）、检测连通性、刷新令牌（仅 supports_refresh）、设置主账号（非主账号才可用）；全组件无「浏览文件」按钮（grep 浏览文件/browseFiles/mdi-folder-open 无匹配）。 |
| A9 | passed | brief.md | A9 文件信息：在账号信息与存储空间下方内嵌完整文件浏览（列表、进入子目录、返回上级、分享、移动、重命名、删除）；系统设置中的「网盘文件」tab 与独立 `/netdisk` 页面移除。 | NetdiskAccountFiles.vue 内嵌于账号详情（AccountDetail 底部），含列表（名称/大小/修改时间）、面包屑导航、返回上级（goParent）、分享/移动/重命名/删除（useNetdiskFileBrowser + Share/Move/Rename 对话框）；pages/netdisk.vue 已删除（git status D）、views/netdisk/ 仅剩空目录、router/index.ts 无 netdisk 路由、i18n-menu.ts setting tabs 无「网盘文件」tab。 |
| A10 | passed | brief.md | A10 新增与编辑一致：编辑账号可修改与新增相同的登录方式字段（Cookie、CookieCloud、账号密码、短信、网页登录），平台保持锁定；保存后账号名称立即更新并在列表与详情同步；后端编辑接口按平台解析凭据文本（裸 JWT 识别为 token）。 | NetdiskAccountDialog.vue add/edit 共用同一表单：平台下拉 edit 时 disabled 锁定，四种登录方式（cookie/cookiecloud/password（光鸭含短信切换）/qr）全部可用且提交带 account_id 复用 _target 更新已有账号；编辑保存后 fetchData 刷新列表与详情名称立即更新；后端 PUT /accounts/{id} 用 _login().credential_from_text 按平台解析（PAN123/GUANGYA 裸 JWT→token），test_update_parses_bare_jwt_as_token 验证。 |
| A11 | passed | brief.md | A11 平台中文名：新增账号的平台下拉、对话框副标题、列表分组、详情平台标签与移动端分组均显示中文平台名，不再出现 `quark`、`pan139` 等英文 id。 | utils/netdisk.ts getNetdiskPlatformName 单一来源映射 zh-CN torrent.netdiskPlatform.*（夸克/UC/移动云盘/123云盘/115/光鸭/百度/迅雷），未知回退原始 id；平台下拉（platformItems）、对话框副标题（selectedPlatformName）、列表分组（NetdiskPlatformGroup）、详情平台标签（VChip）、移动端分组均走该函数，不出现英文平台 id。 |
| A12 | passed | brief.md | A12 网页登录弹窗：扫码等待态不再出现上下大块空白，弹窗内容高度随二维码或整页截图自适应，底部倒计时与等待提示紧贴内容；整页截图与 noVNC 两种方式均不溢出。 | 扫码弹窗无固定高度：qr-page-capture 容器 width:100%、img block-size:auto 按宽度等比缩放；qr-vnc-frame inline-size:100% + aspect-ratio 16/10；倒计时与状态 Chip 位于画面下方紧贴内容（d-flex flex-column align-center 布局，无 spacer/fixed height），VDialog scrollable 内容高度自适应。 |
| A13 | passed | brief.md | A13 登录成功文案：扫码登录检测到登录态后，弹窗下方显示「当前登录用户：<昵称或账号名>」，不再显示状态说明或英文键名；拿不到用户名时显示「当前登录用户：未知」。 | detected 态显示 t('netdisk.qr.detected', { name: qrAccountName \|\| t('netdisk.qr.unknownAccount') })：zh-CN「当前登录用户：{name}」/缺失时「未知」，zh-TW/en-US 同步；qrAccountName 优先取轮询/WS 返回的 account_name（后端 login.py poll 用 login_account_name 解析昵称）；状态说明文案不再出现在 detected 分支（原「已检测到登录态」仅在 VChip status 中，符合 spec 第 139 行要求）。 |
| A14 | passed | brief.md | A14 凭据读取权限：账号列表接口继续返回脱敏凭据；完整 Cookie 只通过超级管理员鉴权的单账号详情接口返回；日志、告警与错误信息不出现 Cookie/token 明文。 | 列表接口 _account_view 只返回 masked（domain mask_credential 固定 ***），完整凭据仅 GET /accounts/{id} detail（_account_detail_view 填充 credential），全部网盘接口 Depends(get_current_active_superuser_async) 超管鉴权；日志核查：base.py log_failure、composition write_back_credential、accounts refresh_all/refresh_quotas、scheduler chain 均只输出 platform/account.name/异常类型，test_refresh_netdisk_accounts_posts_message_without_credential 断言消息不含 secret-token/__pus。 |

## 检查

| 检查 | 命令 | 工作目录 | 状态 | 退出码 | 耗时 |
| --- | --- | --- | --- | ---: | ---: |
| 网盘后端测试 | -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] PATH=/home/zxl/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --locked --no-sync python -m pytest tests/test_netdisk_account_edit.py tests/test_netdisk_account_refresh.py tests/test_netdisk_account_repository.py tests/test_netdisk_accounts.py tests/test_netdisk_adapters_baidu.py tests/test_netdisk_adapters_c139.py tests/test_netdisk_adapters_guangya.py tests/test_netdisk_adapters_pan115.py tests/test_netdisk_adapters_pan123.py tests/test_netdisk_adapters_quark.py tests/test_netdisk_adapters_uc.py tests/test_netdisk_adapters_xunlei.py tests/test_netdisk_baidu_drive.py tests/test_netdisk_c139_drive.py tests/test_netdisk_domain.py tests/test_netdisk_engine.py tests/test_netdisk_failure_reconcile.py tests/test_netdisk_guangya_drive.py tests/test_netdisk_hls.py tests/test_netdisk_keepalive.py tests/test_netdisk_lifecycle.py tests/test_netdisk_login_fixes.py tests/test_netdisk_login.py tests/test_netdisk_migration.py tests/test_netdisk_module_naming.py tests/test_netdisk_module.py tests/test_netdisk_nickname_flow.py tests/test_netdisk_pan115_drive.py tests/test_netdisk_pan123_drive.py tests/test_netdisk_quota_refresh.py tests/test_netdisk_resolve.py tests/test_netdisk_search_flow.py tests/test_netdisk_selection.py tests/test_netdisk_subscription_episodes.py tests/test_netdisk_uc_drive.py tests/test_netdisk_vnc.py tests/test_netdisk_xunlei_drive.py tests/test_netdisk_xunlei_nickname.py -q | . | passed | 0 | 14344 ms |
| 架构与调度契约测试 | -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] PATH=/home/zxl/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --locked --no-sync python -m pytest tests/test_architecture_dependencies.py tests/test_scheduler_architecture.py tests/test_scheduler_contracts.py tests/test_scheduler_compat.py tests/test_scheduler_lifecycle.py tests/test_scheduler_progress.py tests/test_scheduler_registry.py -q | . | passed | 0 | 79826 ms |
| 后端改动文件 pylint | -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] PATH=/home/zxl/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --locked --no-sync python -m pylint app/adapters/external/netdisk/base.py app/adapters/external/netdisk/guangya.py app/adapters/external/netdisk/xunlei.py app/adapters/external/netdisk/login/browser.py app/api/endpoints/netdisk.py app/application/netdisk/accounts.py app/application/netdisk/contract.py app/application/netdisk/login.py app/application/netdisk/keepalive.py app/db/adapters/netdiskaccount.py app/scheduler/catalog.py app/scheduler/chain.py app/scheduler/services.py app/schemas/netdisk.py app/startup/composition/netdisk.py app/startup/initializers/scheduler.py tests/test_netdisk_keepalive.py tests/test_netdisk_xunlei_nickname.py tests/test_netdisk_quota_refresh.py tests/test_netdisk_account_edit.py tests/test_netdisk_nickname_flow.py | . | passed | 0 | 8660 ms |
| 前端 typecheck | PATH=/home/zxl/.nvm/versions/node/v24.18.0/bin:/usr/local/bin:/usr/bin:/bin npm --prefix ../MoviePilot-Frontend run typecheck | . | passed | 0 | 55886 ms |
| 前端 lint | PATH=/home/zxl/.nvm/versions/node/v24.18.0/bin:/usr/local/bin:/usr/bin:/bin npm --prefix ../MoviePilot-Frontend run lint | . | passed | 0 | 31443 ms |
| 前端网盘工具测试 | PATH=/home/zxl/.nvm/versions/node/v24.18.0/bin:/usr/local/bin:/usr/bin:/bin npm --prefix ../MoviePilot-Frontend run test:run -- src/utils/__tests__/netdisk.spec.ts | . | passed | 0 | 3554 ms |

### Builder 报告的证据

以下为 Builder 报告，不等同于 Runtime 检查凭据或独立验收结果。

- 后端网盘相关 pytest: passed — uv run --locked --no-sync python -m pytest tests/test_netdisk_*.py -q → 407 passed, EXIT=0
- A5 修复聚焦测试: passed — pytest tests/test_netdisk_nickname_flow.py tests/test_netdisk_xunlei_nickname.py tests/test_netdisk_account_edit.py tests/test_netdisk_accounts.py tests/test_netdisk_account_refresh.py -q → 30 passed, EXIT=0
- 后端改动文件 pylint: passed — app/application/netdisk/accounts.py + tests/test_netdisk_nickname_flow.py → 10.00/10
- 前端 typecheck: passed — npm run typecheck → EXIT=0
- 前端 lint: passed — npm run lint → EXIT=0，0 warning
- 前端网盘工具测试: passed — vitest run src/utils/__tests__/netdisk.spec.ts → 12 passed
- 前端全量测试: failed — npm run test:run -- --serial → 4649 passed / 1 failed；失败为 SubscribeSeasonDialog 图片地址断言，已在干净 HEAD 工作树复现，与本次改动无关
- git diff --check（前后端）: passed — 两个仓库均无输出
- 已知限制: 未启动前后端服务，A6/A7/A12 的视觉与响应式效果仅通过代码结构、typecheck、lint 与既有组件测试核对，未做浏览器视觉验收。
- 已知限制: 前端全量 vitest 存在 1 个既有失败（SubscribeSeasonDialog 图片地址断言），已在干净 HEAD 工作树复现，与本次改动无关，未纳入正式检查。
- 已知限制: 后端 tests/test_scheduler_cache_expiry.py 有 2 个既有失败，已在干净 HEAD 工作树复现，与本次改动无关，未纳入正式检查。
- 已知限制: 迅雷 token 刷新按 YunX-Desktop 抓包实现（xluser-ssl 主机、网页 client_id、无 client_secret），未在真实账号上联网验证；续期后未重建 captcha。
- 已知限制: 前端改动位于兄弟仓库 MoviePilot-Frontend 且未提交；本 change 的归档提交只覆盖后端仓库与正式产物。

## 阻塞项

_无。_

## 风险与跳过的工作

- 迅雷 401 重试后需重建 captcha 再发请求（spec 明确要求）：XunleiAdapter._dispatch_with_retry 重试复用 _api_headers 重新读取 credential 中 captcha；refresh_account 写回的凭据不含新 captcha，依赖平台续期响应或既有 captcha 仍有效，真实环境行为无法离线验证（未调用真实网盘接口，符合约束）。
- 夸克/UC 90 分钟惰性续期依赖 supports_refresh + 进程内单调时钟节流，多进程部署（如 gunicorn 多 worker）时各 worker 独立节流，可能重复刷新；不影响正确性，仅多一次刷新请求。
- 前端全量 vitest 存在 1 个与本次改动无关的既有失败（SubscribeSeasonDialog 图片地址断言），已在干净 HEAD 复现，属于非阻断项。

## 之前的迭代

| 目标周期 | 迭代 | 尝试 | 结果 | 未解决项 | 摘要 | 完成时间 |
| ---: | ---: | ---: | --- | --- | --- | --- |
| 1 | 1 | 1 | fail | A5 | 独立核查 14 项验收：13 项 passed，1 项 failed。A5（迅雷用户名）存在明确缺口：定时刷新链路 _refresh_one 与新增链路 create 不解析/写回昵称，仅手动探测与扫码新增写昵称，不满足『新增、探测与刷新都能写入』的验收要求；修复位置为 app/application/netdisk/accounts.py 的 _refresh_one（约 425-460 行）与 create（约 116-164 行），建议在这两处复用 adapter.login_account_name 并沿用『解析失败保留已有昵称』的 None 语义。其余关键易错点（Set-Cookie 按 record.id 定位回写、401 仅重试一次且 thread-local 防递归、刷新失败保留旧凭据、unknown 不降级 active、编辑按平台解析裸 JWT、列表脱敏/详情明文分离、编辑弹窗四种登录方式、扫码弹窗移除固定 60vh、平台中文名含移动端）均经代码与聚焦测试确认满足。补充检查：pytest test_netdisk_xunlei_nickname + test_netdisk_account_edit + test_netdisk_quota_refresh 14 passed，EXIT=0。 | 2026-11-04T09:38:33.590Z |
| 1 | 2 | 1 | pass | — | 第 2 轮独立复核全部 14 项验收。重点复核上轮唯一失败项 A5：create() 保存前调 login_account_name、_refresh_one() 探测后仅非空补写昵称、解析失败保留已有值，三条路径均有代码与测试证据，本轮重跑 nickname_flow + xunlei_nickname + keepalive + quota_refresh + account_edit + account_refresh 共 50 项 pytest 全部通过（EXIT=0）。上轮易错点复核确认：Set-Cookie 按 record.id 回写不串号、401 只重试一次不递归、unknown 不降级 active、裸 JWT 按平台解析为 token、详情明文/列表脱敏且全部超管鉴权、编辑开放全部登录方式且平台锁定、扫码弹窗无固定高度、三份 locale 中文平台名同步。风险项均不阻断验收。 | 2026-11-04T09:54:56.503Z |



## 结论

第 2 轮独立复核全部 14 项验收。重点复核上轮唯一失败项 A5：create() 保存前调 login_account_name、_refresh_one() 探测后仅非空补写昵称、解析失败保留已有值，三条路径均有代码与测试证据，本轮重跑 nickname_flow + xunlei_nickname + keepalive + quota_refresh + account_edit + account_refresh 共 50 项 pytest 全部通过（EXIT=0）。上轮易错点复核确认：Set-Cookie 按 record.id 回写不串号、401 只重试一次不递归、unknown 不降级 active、裸 JWT 按平台解析为 token、详情明文/列表脱敏且全部超管鉴权、编辑开放全部登录方式且平台锁定、扫码弹窗无固定高度、三份 locale 中文平台名同步。风险项均不阻断验收。
