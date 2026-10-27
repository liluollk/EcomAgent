"""进程启动配置。

这里是启动路径上唯一读取环境变量的地方。装配差异**不在这里表达**:
生产入口 ``apps/api/main.py`` 与演示入口 ``apps/api/demo.py`` 各自决定装什么,
本模块只回答"启动所需的值是否齐全合法",没有模式枚举,也没有开关。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from runtime.access.tokens import resolve_secret
from runtime.security.secrets import load_master_key


class StartupConfigurationError(RuntimeError):
    """启动所需配置缺失或非法;在服务开始接收请求之前抛出。"""


@dataclass(frozen=True)
class StartupSettings:
    """启动所需的三个值;主密钥不进 repr,避免随日志或异常输出泄漏。"""

    database_url: str
    jwt_secret: str
    master_key: bytes = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "StartupSettings":
        """从进程环境组装并校验;任一项缺失都直接失败,不回退到默认值。"""
        source = os.environ if environ is None else environ
        database_url = (source.get("RUNTIME_DATABASE_URL") or "").strip()
        if not database_url:
            raise StartupConfigurationError("RUNTIME_DATABASE_URL is not set")
        try:
            jwt_secret = resolve_secret(source)
        except Exception as error:
            raise StartupConfigurationError(str(error)) from error
        try:
            master_key = load_master_key(source)
        except Exception as error:
            raise StartupConfigurationError(str(error)) from error
        return cls(database_url=database_url, jwt_secret=jwt_secret, master_key=master_key)
