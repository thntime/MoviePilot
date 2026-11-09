"""
调度器兼容 Chain。
"""

from typing import Any, Callable, Dict, Optional

from app.application.database import get_database_governance
from app.application.netdisk.accounts import NetdiskAccountService
from app.chain.base import ChainBase
from app.domain.netdisk import NetdiskError, NetdiskErrorCode, NetdiskPlatform
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

    def clear_netdisk_recycle_bins(self) -> Dict[str, int]:
        """
        清理 115 账号回收站；单账号失败只记录告警，不中断其余账号。

        账号开启回收站安全密钥属于可预期的跳过，不计入失败。

        :return: 检查、已清空、跳过与失败账号数量
        """
        service = NetdiskAccountService()
        views = [
            view
            for view in service.store.list_accounts(NetdiskPlatform.PAN115)
            if view.status == "active"
        ]
        cleared = 0
        skipped = 0
        failed = 0
        for view in views:
            try:
                result = service.clear_recycle_bin(view.id)
            except NetdiskError as error:
                if (
                    error.code is NetdiskErrorCode.UNSUPPORTED
                    and "安全密钥" in error.message
                ):
                    skipped += 1
                    logger.info(
                        f"网盘回收站清理跳过：platform={view.platform} "
                        f"account={view.name} reason=安全密钥"
                    )
                    continue
                failed += 1
                logger.warning(
                    f"网盘回收站清理失败：platform={view.platform} "
                    f"account={view.name} error={type(error).__name__}"
                )
                continue
            except Exception as error:  # noqa: BLE001 - 单账号异常不中断整轮
                failed += 1
                logger.warning(
                    f"网盘回收站清理失败：platform={view.platform} "
                    f"account={view.name} error={type(error).__name__}"
                )
                continue
            if result.get("cleared"):
                cleared += 1
        logger.info(
            f"网盘回收站清理完成：检查 {len(views)} 个，"
            f"已清空 {cleared} 个，跳过 {skipped} 个，失败 {failed} 个"
        )
        return {
            "checked": len(views),
            "cleared": cleared,
            "skipped": skipped,
            "failed": failed,
        }


# 旧插件可能持久化或比较完整类路径，保持迁移前可观察身份。
SchedulerChain.__module__ = "app.scheduler"
