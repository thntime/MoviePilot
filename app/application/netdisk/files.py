"""
个人网盘浏览、下载、分享与文件操作用例。

只编排 Port：账号选择、能力校验、递归列目录与错误分类都在这里完成；
具体协议由各平台适配器实现。
"""

from __future__ import annotations

from typing import Optional

from app.application.netdisk import registry
from app.domain.netdisk import (
    DirectLink,
    DriveCapabilities,
    DriveFile,
    DriveQuota,
    DriveShare,
    NetdiskAccountRecord,
    NetdiskError,
    NetdiskErrorCode,
)


class NetdiskFilesService:
    """
    个人网盘文件用例。
    """

    def account(self, account_id: int) -> NetdiskAccountRecord:
        """
        读取账号记录。

        :param account_id: 账号主键
        :return: 账号记录
        :raises NetdiskError: 账号不存在时抛出
        """
        record = registry.get_account_store().get_record(account_id)
        if record is None:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "网盘账号不存在")
        return record

    def capabilities(self, account_id: int) -> DriveCapabilities:
        """
        返回账号对应平台的能力声明。

        :param account_id: 账号主键
        :return: 能力声明
        """
        record = self.account(account_id)
        return registry.get_adapter(record.platform).drive_capabilities

    def quota(self, account_id: int) -> DriveQuota:
        """
        查询空间配额。

        :param account_id: 账号主键
        :return: 配额
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.quota:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持空间查询", platform=record.platform)
        return adapter.quota(record)

    def list_dir(self, account_id: int, parent_id: str = "") -> list[DriveFile]:
        """
        列出目录。

        :param account_id: 账号主键
        :param parent_id: 父目录标识
        :return: 目录条目
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.browse:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持目录浏览", platform=record.platform)
        return adapter.list_dir(record, parent_id)

    def download(self, account_id: int, file: DriveFile) -> DirectLink:
        """
        获取个人盘文件直链。

        :param account_id: 账号主键
        :param file: 文件条目
        :return: 直链
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.download:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持个人盘下载", platform=record.platform)
        return adapter.download_file(record, file)

    def walk(self, account_id: int, parent_id: str = "", *, max_depth: int = 6) -> list[DriveFile]:
        """
        递归列出目录下的全部文件。

        :param account_id: 账号主键
        :param parent_id: 起始目录
        :param max_depth: 最大递归深度
        :return: 文件列表（不含目录）
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.browse:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持目录浏览", platform=record.platform)
        collected: list[DriveFile] = []
        queue: list[tuple[str, int]] = [(parent_id, 0)]
        while queue:
            current, depth = queue.pop(0)
            for item in adapter.list_dir(record, current):
                if item.is_dir:
                    if depth < max_depth:
                        queue.append((item.file_id, depth + 1))
                else:
                    collected.append(item)
        return collected

    def share(
        self,
        account_id: int,
        files: list[DriveFile],
        *,
        password: str = "",
        expires_in: int = 0,
    ) -> DriveShare:
        """
        创建分享。

        :param account_id: 账号主键
        :param files: 文件列表
        :param password: 提取码
        :param expires_in: 有效期秒数
        :return: 分享结果
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.share:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持创建分享", platform=record.platform)
        return adapter.share_file(record, files, password=password, expires_in=expires_in)

    def move(self, account_id: int, files: list[DriveFile], target_id: str) -> None:
        """
        移动文件。

        :param account_id: 账号主键
        :param files: 文件列表
        :param target_id: 目标目录
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.move:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持移动文件", platform=record.platform)
        adapter.move_files(record, files, target_id)

    def rename(self, account_id: int, file: DriveFile, name: str) -> None:
        """
        重命名文件。

        :param account_id: 账号主键
        :param file: 文件条目
        :param name: 新名称
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.rename:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持重命名", platform=record.platform)
        adapter.rename_file(record, file, name)

    def delete(self, account_id: int, files: list[DriveFile]) -> None:
        """
        删除文件。

        :param account_id: 账号主键
        :param files: 文件列表
        """
        record = self.account(account_id)
        adapter = registry.get_adapter(record.platform)
        if not adapter.drive_capabilities.delete:
            raise NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持删除", platform=record.platform)
        adapter.delete_files(record, files)
