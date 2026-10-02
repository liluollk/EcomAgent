"""Security 测试夹具(计划 01 Task 6)。

提供:
- ``master_key_env``:把确定性占位主密钥写入 ``RUNTIME_MASTER_KEY``
  (仅测试用,不是真实密钥);
- ``secret_cipher``:从环境变量构建的 SecretCipher;
- ``endpoint_policy``:默认生产策略(HTTPS-only、allowlist、注入 fake resolver,
  不做真实网络调用)。
"""

import base64

import pytest

from runtime.security.endpoints import EndpointPolicy
from runtime.security.secrets import SecretCipher


# bytes(range(32)):确定性占位密钥,仅用于测试,不是真实密钥。
_TEST_MASTER_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")

_ALLOWED_HOSTS = frozenset({"api.example.com", "93.184.216.34"})


@pytest.fixture()
def master_key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("RUNTIME_MASTER_KEY", _TEST_MASTER_KEY_B64)
    return _TEST_MASTER_KEY_B64


@pytest.fixture()
def secret_cipher(master_key_env: str) -> SecretCipher:
    return SecretCipher()


@pytest.fixture()
def endpoint_policy() -> EndpointPolicy:
    """默认生产策略;域名解析走 fake resolver,命中 allowlist 的域名解析到公网 IP。"""

    def fake_resolver(hostname: str) -> list[str]:
        routes: dict[str, list[str]] = {"api.example.com": ["93.184.216.34"]}
        if hostname not in routes:
            raise OSError(f"cannot resolve {hostname!r}")
        return routes[hostname]

    return EndpointPolicy(allowed_hosts=_ALLOWED_HOSTS, resolver=fake_resolver)
