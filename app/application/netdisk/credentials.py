"""
网盘凭据的对称加密实现。

Fernet 密钥由宿主 `SECRET_KEY` 派生，保证同一部署内可稳定解密；
密钥轮换后旧凭据无法解密，按过期处理并提示重新录入。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from app.application.security.token import decrypt, encrypt_message


def derive_fernet_key(secret_key: str) -> bytes:
    """
    由宿主密钥派生 Fernet 需要的 32 字节 urlsafe base64 密钥。

    :param secret_key: 宿主签名密钥
    :return: Fernet 密钥字节
    """
    digest = hashlib.sha256(str(secret_key or "").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class FernetCredentialCipher:
    """
    使用 Fernet 加解密网盘凭据。
    """

    def __init__(self, secret_key: str) -> None:
        """
        保存派生后的 Fernet 密钥。

        :param secret_key: 宿主签名密钥
        """
        self._key = derive_fernet_key(secret_key)

    def encrypt(self, plaintext: str) -> str:
        """
        加密凭据文本。

        :param plaintext: 明文
        :return: 密文字符串
        """
        return encrypt_message(plaintext, self._key)

    def decrypt(self, token: str) -> Optional[str]:
        """
        解密凭据文本。

        :param token: 密文字符串
        :return: 明文；密钥不匹配或数据损坏时返回 None
        """
        if not token:
            return None
        raw = decrypt(str(token).encode("utf-8"), self._key)
        if raw is None:
            return None
        return raw.decode("utf-8")
