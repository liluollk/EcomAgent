"""argon2 密码哈希与校验(计划 01 Task 5;架构文档 §2)。

密码仅保存强哈希;本模块为纯函数,不依赖数据库与会话状态。
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """生成 argon2id 哈希;每次调用使用随机盐。"""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码;哈希格式非法或密码不匹配一律返回 False。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
