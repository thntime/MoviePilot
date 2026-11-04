---
generated_from_state_version: 8
---

# 验证

## 当前结果

- 结果: **已归档**
- 验证情况: **已完成检查，验证结果已确认**
- 目标周期: 1
- 迭代: 1
- 验证器尝试次数: 1
- 完成时间: 2026-11-04T13:56:06.969Z
- 摘要: 逐项核对 A1–A3 的代码路径与测试证据并与 runtime 绑定证据交叉验证：迅雷页面昵称经 Nuxt store 读入凭据并优先于 about 接口；百度 poll 不再关窗、login_ready 要求 BDUSS、校验通过后才由服务层切回主页面且默认只置前；弹窗白名单只含微信/百度登录域名且永不关主页面，非百度平台 no-op。Verifier 独立复跑 45 项聚焦测试全部通过，接受 runtime 已绑定的 416 项全量与 pylint 证据，未重复全量回归。三项验收全部通过，verdict: pass。

## 验收

| 编号 | 结果 | 来源 | 验收项 | 原因 |
| --- | --- | --- | --- | --- |
| A1 | passed | brief.md | A1 迅雷昵称：迅雷网页登录检测到登录态后，「当前登录用户」显示真实昵称（优先读取页面登录态，其次凭据字段与 about 接口），不再显示「未知」；全部来源都取不到时才显示明确回退文案。 | browser.py:95-107 迅雷凭据脚本读取 window.$nuxt.$store.state.users.curUser 的 nick_name/nickname 并优先作为 nickname 返回（pageNick 优先于 data.* 各字段）；browser.py:831-833 _xunlei_fields 把非空 nickname 写入凭据；xunlei.py:1112-1153 login_account_name 先取凭据 nickname/nick_name/nickName/user_name/userName/name，缺失时才带 access/device 调 /drive/v1/about，并在 data、data.user 与响应顶层按同序匹配；取不到时返回空串由前端回退文案兜底（netdisk.qr.unknownAccount）。测试覆盖：test_netdisk_xunlei_nickname.py::test_xunlei_credential_includes_page_nickname 验证页面昵称写入凭据，test_login_account_name_prefers_credential 验证凭据昵称优先不发请求，另有 about 三种嵌套结构用例。Verifier 复跑 pytest tests/test_netdisk_xunlei_nickname.py → 12 passed, EXIT=0。 |
| A2 | passed | brief.md | A2 百度弹窗：百度网页登录中，微信扫码弹窗在登录态校验通过之前不会被关闭，用户可正常扫码；校验通过后画面回到主页面，且默认只置前、不强制关闭弹窗，弹窗最终随确认或取消销毁。 | browser.py:712-737 poll() 只读凭据并返回 success，不再调用 _focus_main_page（diff 确认原「if platform is BAIDU: self._focus_main_page(session)」已删除）；服务层 login.py:349-380 仅在 validate_credential 返回 active 后通过可选端口调用 focus_main_page(session_id)，切页失败仅记录不影响 detected；browser.py:1005 focus_main_page 默认 close_popups=False，只 bring_to_front 并 _switch_stream 切画面；browser.py:691-698 start() 在启动时记录 main_page 引用，:1062-1088 _main_page 优先用该引用（校验 is_closed 与 pages 成员资格），失效时按 pan.baidu.com 主域名回退、再取第一个非弹窗页，pages[0] 仅为最后兜底；弹窗最终随 confirm_qr/cancel_qr 的 cancel() 关闭整个浏览器会话销毁（login.py:417、428）。测试覆盖：test_netdisk_login_popup.py 匿名 BAIDID 下微信弹窗 0 次关闭、默认只置前、close_popups 只关微信域名、主页面引用优先、非百度 no-op；test_netdisk_login.py 新增 detected 后调用 focus 与 login_ready 未就绪不切页两用例。Verifier 复跑 → 45 passed（popup 12 + login/login_fixes 33），EXIT=0。 |
| A3 | passed | brief.md | A3 隔离与回归：匿名 Cookie 不再被判定为登录完成；关闭弹窗只针对明确的微信/百度登录域名且永不关闭主页面；夸克、UC、139、迅雷、光鸭的网页登录行为与现有测试保持不变。 | baidu.py:1238-1248 login_ready 要求 Cookie 头含 "BDUSS="（_credential_cookie 兼容 cookie/BDUSS 形态），匿名 BAIDUID 返回 False，login.py:346-348 未就绪时返回 pending 不判登录完成；browser.py:43-45 白名单 _AUTH_POPUP_HOSTS 仅 open/login.weixin.qq.com，_BAIDU_AUTH_HOSTS 仅 passport/wappass.baidu.com 且需 opener() 才视为弹窗，:1043-1050 关闭循环显式跳过 main，主页面永不关闭；focus_main_page:1016-1018 对非 BAIDU 平台直接 return（夸克/UC/139/光鸭/迅雷 no-op，测试 test_focus_main_page_ignores_non_baidu 验证）。139 与 base 的 login_ready 为既有实现未改动（c139.py:655、base.py:797 在本次 diff 之外）。Runtime 已绑定证据：39 个 tests/test_netdisk_*.py 全量 416 passed EXIT=0（含夸克/UC/139/光鸭/迅雷既有登录测试），改动文件 pylint 10.00；Verifier 抽查 diff 确认改动仅触及 baidu/browser/login.py 与 3 个测试文件，与 brief 范围一致。 |

## 检查

| 检查 | 命令 | 工作目录 | 状态 | 退出码 | 耗时 |
| --- | --- | --- | --- | ---: | ---: |
| 网盘后端测试 | -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] PATH=/home/zxl/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --locked --no-sync python -m pytest tests/test_netdisk_account_edit.py tests/test_netdisk_account_refresh.py tests/test_netdisk_account_repository.py tests/test_netdisk_accounts.py tests/test_netdisk_adapters_baidu.py tests/test_netdisk_adapters_c139.py tests/test_netdisk_adapters_guangya.py tests/test_netdisk_adapters_pan115.py tests/test_netdisk_adapters_pan123.py tests/test_netdisk_adapters_quark.py tests/test_netdisk_adapters_uc.py tests/test_netdisk_adapters_xunlei.py tests/test_netdisk_baidu_drive.py tests/test_netdisk_c139_drive.py tests/test_netdisk_domain.py tests/test_netdisk_engine.py tests/test_netdisk_failure_reconcile.py tests/test_netdisk_guangya_drive.py tests/test_netdisk_hls.py tests/test_netdisk_keepalive.py tests/test_netdisk_lifecycle.py tests/test_netdisk_login_fixes.py tests/test_netdisk_login.py tests/test_netdisk_login_popup.py tests/test_netdisk_migration.py tests/test_netdisk_module_naming.py tests/test_netdisk_module.py tests/test_netdisk_nickname_flow.py tests/test_netdisk_pan115_drive.py tests/test_netdisk_pan123_drive.py tests/test_netdisk_quota_refresh.py tests/test_netdisk_resolve.py tests/test_netdisk_search_flow.py tests/test_netdisk_selection.py tests/test_netdisk_subscription_episodes.py tests/test_netdisk_uc_drive.py tests/test_netdisk_vnc.py tests/test_netdisk_xunlei_drive.py tests/test_netdisk_xunlei_nickname.py -q | . | passed | 0 | 15655 ms |
| 后端改动文件 pylint | -u [REDACTED] -u [REDACTED] -u [REDACTED] -u [REDACTED] PATH=/home/zxl/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --locked --no-sync python -m pylint app/adapters/external/netdisk/login/browser.py app/adapters/external/netdisk/baidu.py app/application/netdisk/login.py tests/test_netdisk_login_popup.py tests/test_netdisk_login.py tests/test_netdisk_xunlei_nickname.py | . | passed | 0 | 3255 ms |

### Builder 报告的证据

以下为 Builder 报告，不等同于 Runtime 检查凭据或独立验收结果。

- 网盘后端全量测试: passed — uv run --locked --no-sync python -m pytest tests/test_netdisk_*.py -q → 416 passed, EXIT=0
- 登录相关聚焦测试: passed — pytest tests/test_netdisk_login_popup.py tests/test_netdisk_login.py tests/test_netdisk_login_fixes.py tests/test_netdisk_adapters_baidu.py tests/test_netdisk_xunlei_nickname.py -q → 62 passed, EXIT=0
- 后端改动文件 pylint: passed — browser.py / baidu.py / login.py / 3 个测试文件 → 10.00/10
- git diff --check: passed — 无输出
- 已知限制: 未接真实百度/迅雷账号，微信弹窗存活与迅雷昵称读取需在真实扫码流程中确认。
- 已知限制: 迅雷昵称依赖页面 Nuxt store 的 users.curUser.nick_name 路径；若迅雷前端改版该路径失效，会回退到凭据字段与 about 接口。
- 已知限制: bring_to_front 在 Xvfb/openbox 下的可见效果未实测；若主页面仍被弹窗遮挡，可启用 focus_main_page(..., close_popups=True)。
- 已知限制: 未启动前后端服务，未做浏览器视觉验收。

## 阻塞项

_无。_

## 风险与跳过的工作

- 未接真实百度/迅雷账号做真实扫码验证（builder 已声明 known_limit，非本次验收阻塞项）；迅雷昵称依赖页面 Nuxt store 路径，前端改版时回退到凭据字段与 about 接口。
- bring_to_front 在 Xvfb/openbox 下的实际置前效果未实测；若弹窗遮挡主页面，可启用 close_popups=True 逃生开关。
- 百度 login_ready 只检查 BDUSS 不含 STOKEN（与 brief 关键决定一致，避免把可用账号判为过期），属有意取舍。

## 之前的迭代

| 目标周期 | 迭代 | 尝试 | 结果 | 未解决项 | 摘要 | 完成时间 |
| ---: | ---: | ---: | --- | --- | --- | --- |
| 1 | 1 | 1 | pass | — | 逐项核对 A1–A3 的代码路径与测试证据并与 runtime 绑定证据交叉验证：迅雷页面昵称经 Nuxt store 读入凭据并优先于 about 接口；百度 poll 不再关窗、login_ready 要求 BDUSS、校验通过后才由服务层切回主页面且默认只置前；弹窗白名单只含微信/百度登录域名且永不关主页面，非百度平台 no-op。Verifier 独立复跑 45 项聚焦测试全部通过，接受 runtime 已绑定的 416 项全量与 pylint 证据，未重复全量回归。三项验收全部通过，verdict: pass。 | 2026-11-04T13:56:06.969Z |



## 结论

逐项核对 A1–A3 的代码路径与测试证据并与 runtime 绑定证据交叉验证：迅雷页面昵称经 Nuxt store 读入凭据并优先于 about 接口；百度 poll 不再关窗、login_ready 要求 BDUSS、校验通过后才由服务层切回主页面且默认只置前；弹窗白名单只含微信/百度登录域名且永不关主页面，非百度平台 no-op。Verifier 独立复跑 45 项聚焦测试全部通过，接受 runtime 已绑定的 416 项全量与 pylint 证据，未重复全量回归。三项验收全部通过，verdict: pass。
