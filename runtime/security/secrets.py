"""Secret 加密(计划 01 Task 6;架构文档 §8)。

不变式:
- 主密钥从进程环境变量 ``RUNTIME_MASTER_KEY`` 注入,32 字节 Base64 编码,
  不写入数据库或仓库;缺失与格式错误抛出明确异常;
- AES-GCM 认证加密;associated data 为 ``workspace_id + connection_type +
  normalized_endpoint``(经 :func:`build_secret_binding` 构造),任一分量不同
  即解密失败,Secret 不能被另一个端点复用;
- 加密记录保存 key version、nonce、ciphertext 与认证标签;
- 任何异常消息都不包含明文或密钥材料。
"""

import base64
import binascii
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict


MASTER_KEY_ENV_VAR: Final = "RUNTIME_MASTER_KEY"
KEY_SIZE_BYTES: Final = 32
NONCE_SIZE_BYTES: Final = 12
TAG_SIZE_BYTES: Final = 16

_DEFAULT_KEY_VERSION: Final = 1
# 分隔符防止分量拼接歧义(例如 "a"+"bc" 与 "ab"+"c");\x1f 不允许出现在
# workspace_id/连接类型/规范化端点中。
_BINDING_SEPARATOR: Final = "\x1f"


class SecretCipherError(Exception):
    """Secret 加解密失败的基类;消息不含明文与密钥材料。"""


class MasterKeyMissing(SecretCipherError):
    """未设置 RUNTIME_MASTER_KEY。"""


class MasterKeyInvalid(SecretCipherError):
    """RUNTIME_MASTER_KEY 不是合法 Base64,或解码后不是 32 字节。"""


class SecretDecryptFailed(SecretCipherError):
    """密钥、binding 或密文不匹配;不区分具体原因以避免侧信道泄漏。"""


class EncryptedSecret(BaseModel):
    """一条加密记录;认证标签单独保存,解密时与 ciphertext 重组。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key_version: int
    nonce: bytes
    ciphertext: bytes
    tag: bytes


def build_secret_binding(
    *, workspace_id: str, connection_type: str, normalized_endpoint: str
) -> bytes:
    """构造 AES-GCM associated data;调用方传入规范化端点(EndpointPolicy 产出)。"""
    return _BINDING_SEPARATOR.join(
        (workspace_id, connection_type, normalized_endpoint)
    ).encode("utf-8")


def load_master_key() -> bytes:
    """从环境读取并校验主密钥;缺失与格式错误抛出明确异常。"""
    encoded = os.environ.get(MASTER_KEY_ENV_VAR)
    if encoded is None:
        raise MasterKeyMissing(f"environment variable {MASTER_KEY_ENV_VAR} is not set")
    try:
        key = base64.b64decode(encoded, validate=True)
    except binascii.Error as error:
        raise MasterKeyInvalid(f"{MASTER_KEY_ENV_VAR} is not valid Base64") from error
    if len(key) != KEY_SIZE_BYTES:
        raise MasterKeyInvalid(
            f"{MASTER_KEY_ENV_VAR} must decode to {KEY_SIZE_BYTES} bytes, got {len(key)}"
        )
    return key


class SecretCipher:
    """AES-GCM 加解密;构造时加载并校验主密钥。"""

    def __init__(self, *, key_version: int = _DEFAULT_KEY_VERSION) -> None:
        self._key = load_master_key()
        self._key_version = key_version

    def encrypt(self, plaintext: bytes, *, binding: bytes) -> EncryptedSecret:
        """加密明文;每次调用生成新的随机 nonce。"""
        nonce = os.urandom(NONCE_SIZE_BYTES)
        sealed = AESGCM(self._key).encrypt(nonce, plaintext, binding)
        return EncryptedSecret(
            key_version=self._key_version,
            nonce=nonce,
            ciphertext=sealed[:-TAG_SIZE_BYTES],
            tag=sealed[-TAG_SIZE_BYTES:],
        )

    def decrypt(self, encrypted: EncryptedSecret, *, binding: bytes) -> bytes:
        """解密;密钥、binding 或密文任一不匹配都抛出 SecretDecryptFailed。"""
        try:
            return AESGCM(self._key).decrypt(
                encrypted.nonce, encrypted.ciphertext + encrypted.tag, binding
            )
        except InvalidTag:
            raise SecretDecryptFailed(
                "secret decryption failed: wrong key, mismatched binding, "
                "or tampered ciphertext"
            ) from None
