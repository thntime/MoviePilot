# 目标

修复网盘网页登录的两个缺陷：迅雷网页登录检测到登录态后仍显示「当前登录用户：未知」；百度使用微信登录时，微信扫码弹窗约 1 秒后被自动关闭，无法扫码。

# 范围

## 目录结构

### 新建

- `tests/test_netdisk_login_popup.py` — 百度匿名 Cookie 不关弹窗、`focus_main_page` 默认只置前、只关闭登录域名弹窗、服务层在校验通过后才切页的用例。

### 修改

- `app/adapters/external/netdisk/login/browser.py` — `poll()` 不再关闭页面；`_QrSession` 记录主页面引用；新增公开 `focus_main_page(session_id, close_popups=False)`、`_main_page()`、`_is_auth_popup()`；补充页面切换日志；迅雷凭据脚本增加从页面登录态（Nuxt store）读取昵称。
- `app/adapters/external/netdisk/baidu.py` — 新增 `login_ready()`，要求凭据包含 `BDUSS`，匿名 `BAIDUID` 不算登录完成。
- `app/application/netdisk/login.py` — 在 `validate_credential` 返回 active 后，通过可选端口调用 `focus_main_page(session_id)`；失败不影响 detected 结果。
- `app/adapters/external/netdisk/xunlei.py` — 保持 `login_account_name` 的凭据优先与 about 兜底逻辑，确保页面读取到的昵称可直接使用。
- `tests/test_netdisk_xunlei_nickname.py` — 补充「凭据中的页面昵称优先于 about 请求」用例。

### 删除

无

### 明确不建

- 不改变 noVNC/VNC 桥接方式，不引入新的浏览器引擎。
- 不改动其他网盘平台（夸克/UC/139/光鸭）的登录流程。
- 不新增外部依赖或前端接口。

# 非目标

- 不重做网页登录 UI 与扫码交互。
- 不改变账号保存、凭据加密与状态恢复语义。
- 不实现「自动识别百度是否已登录并跳过扫码」的产品流程。

# 验收示例

- A1 迅雷昵称：迅雷网页登录检测到登录态后，「当前登录用户」显示真实昵称（优先读取页面登录态，其次凭据字段与 about 接口），不再显示「未知」；全部来源都取不到时才显示明确回退文案。
- A2 百度弹窗：百度网页登录中，微信扫码弹窗在登录态校验通过之前不会被关闭，用户可正常扫码；校验通过后画面回到主页面，且默认只置前、不强制关闭弹窗，弹窗最终随确认或取消销毁。
- A3 隔离与回归：匿名 Cookie 不再被判定为登录完成；关闭弹窗只针对明确的微信/百度登录域名且永不关闭主页面；夸克、UC、139、迅雷、光鸭的网页登录行为与现有测试保持不变。

# 关键决定

- 百度根因：`poll()` 以「Cookie 非空」触发 `_focus_main_page()` 关闭 `pages[1:]`，而百度登录页一打开就写入匿名 `BAIDUID`；服务层 `BDUSS` 校验发生在之后，来不及阻止关窗。修复采用 Council 汇总方案：切页/关窗移到 `validate_credential` 返回 active 之后，`poll()` 只读凭据。
- 关闭时机采用保守默认：校验通过后只 `bring_to_front()` 并切换画面，不默认关闭弹窗；`close_popups=True` 作为可选逃生开关，仅在校验通过后、仅关闭明确登录域名、永不关闭主页面。弹窗最终由确认/取消时的 `cancel()` 销毁。
- 主页面不使用 `pages[0]` 盲猜：`start()` 记录主页面引用，失效时按 `pan.baidu.com` 域名回退。
- 百度 `login_ready` 只检查 `BDUSS`（与仓库现有必填字段一致）；不擅自加入 `STOKEN`，避免把可用账号判为过期。
- 迅雷昵称优先从页面登录态读取：迅雷网页是 Nuxt 应用，`window.$nuxt.$store.state.users.curUser.nick_name` 保存当前登录用户昵称；读取结果写入凭据 `nickname`，`login_account_name` 会优先使用它，about 接口作为兜底。

# 验证预期

- 后端：新增 `tests/test_netdisk_login_popup.py`，回归 `tests/test_netdisk_login.py`、`tests/test_netdisk_login_fixes.py`、`tests/test_netdisk_adapters_baidu.py`、`tests/test_netdisk_xunlei_nickname.py`；改动文件 pylint。
- 不启动服务、不接真实百度/迅雷账号；真实扫码验证需用户授权后进行。
