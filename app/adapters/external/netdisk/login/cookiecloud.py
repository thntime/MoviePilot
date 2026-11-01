"""
从 CookieCloud 导入平台 Cookie。

复用宿主 `CookieCloudHelper` 的下载与解密能力，只做域名筛选与字段整理。
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from app.adapters.external.cookiecloud import CookieCloudHelper
from app.runtime.log import logger


class CookieCloudImportAdapter:
    """
    CookieCloud 导入端口实现。
    """

    def import_cookies(self, domains: Sequence[str]) -> Optional[Mapping[str, str]]:
        """
        按域名后缀导入 Cookie。

        :param domains: 平台域名后缀列表
        :return: Cookie 字段映射；未配置或无匹配时返回 None
        """
        try:
            cookies, message = CookieCloudHelper().download()
        except Exception as error:  # noqa: BLE001 - CookieCloud 异常只记录
            logger.warning(f"CookieCloud 导入失败：error={type(error).__name__}")
            return None
        if not cookies:
            if message:
                logger.info(f"CookieCloud 无可用数据：{message}")
            return None
        suffixes = tuple(domain.lower().lstrip(".") for domain in domains)
        result: dict[str, str] = {}
        for domain, header in cookies.items():
            normalized = str(domain or "").lower().lstrip(".")
            if suffixes and not any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in suffixes):
                continue
            for part in str(header or "").split(";"):
                item = part.strip()
                if "=" not in item:
                    continue
                name, value = item.split("=", 1)
                if name.strip():
                    result[name.strip()] = value.strip()
        return result or None
