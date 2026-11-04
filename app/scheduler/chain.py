"""
调度器兼容 Chain。
"""

from typing import Any, Callable, Dict, Optional

from app.application.database import get_database_governance
from app.application.netdisk.accounts import NetdiskAccountService
from app.chain.base import ChainBase
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import MessageType


class SchedulerChain(ChainBase):
    """
    保留插件使用的公共定时任务与数据治理入口。
    """

    DEFAULT_BATCH_SIZE = 500

    def cleanup(
        self,
        batch_size: Optional[int] = None,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """
        按配置保留期执行分批清理。
        """
        return get_database_governance().cleanup(
            batch_size=batch_size,
            progress_callback=progress_callback,
        )

    def refresh_netdisk_accounts(self) -> Dict[str, int]:
        """
        刷新并探测全部个人网盘账号，异常账号通过系统消息提醒重新登录。

        :return: 已检查与失败账号数量
        """
        service = NetdiskAccountService()
        checked = len(service.store.list_accounts())
        failures = service.refresh_all()
        if failures:
            lines = [f"- {item.platform} / {item.name}" for item in failures]
            self.post_message(
                Message(
                    mtype=MessageType.Manual,
                    title="网盘账号登录态异常",
                    text="以下账号需要重新登录：\n" + "\n".join(lines),
                )
            )
        logger.info(f"网盘账号登录态刷新完成：检查 {checked} 个，异常 {len(failures)} 个")
        return {"checked": checked, "failed": len(failures)}

    def refresh_netdisk_quotas(self) -> Dict[str, int]:
        """
        刷新全部个人网盘账号的已用与总空间。

        :return: 检查、更新与失败账号数量
        """
        result = NetdiskAccountService().refresh_quotas()
        logger.info(
            f"网盘账号空间刷新完成：检查 {result['checked']} 个，"
            f"更新 {result['updated']} 个，失败 {result['failed']} 个"
        )
        return result


# 旧插件可能持久化或比较完整类路径，保持迁移前可观察身份。
SchedulerChain.__module__ = "app.scheduler"
