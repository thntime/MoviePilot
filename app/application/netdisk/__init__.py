"""
网盘取链、账号与登录的应用层。

用例只依赖本包声明的 Port；具体 Adapter 由组合根在启动时注入。
子模块直接从 `app.application.netdisk.<module>` 导入，包初始化不聚合导入，
避免父包与子模块形成 import 环。
"""
