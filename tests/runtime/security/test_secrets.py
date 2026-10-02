"""Secret 加密、密钥加载与脱敏不变式测试(计划 01 Task 6 Step 1;架构文档 §8)。

第一个用例为计划文档原文;其余为架构文档写明的不变式:
错误 binding/错误密钥/篡改密文解密失败、AES-GCM associated data 绑定
(workspace_id + connection_type + normalized_endpoint 任一不同即失败)、
nonce 唯一性、RUNTIME_MASTER_KEY 32 字节 Base64 校验、异常与日志不含明文,
以及 redact() 对敏感键的递归脱敏。
"""

import base64
import copy
import logging

import pytest
from pydantic import ValidationError

from runtime.security.redaction import redact
from runtime.security.secrets import (
    MASTER_KEY_ENV_VAR,
    EncryptedSecret,
    MasterKeyInvalid,
    MasterKeyMissing,
    SecretCipher,
    SecretDecryptFailed,
    build_secret_binding,
)


TEST_PLAINTEXT = b"sk-example"
BINDING = b"workspace:model"

# 确定性占位密钥(非真实密钥):32 字节全递增 / 全递减序列的 Base64。
KEY_A_B64 = base64.b64encode(bytes(range(32))).decode("ascii")
KEY_B_B64 = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")


# ---------------------------------------------------------------------------
# 密文往返与绑定
# ---------------------------------------------------------------------------


def test_secret_round_trip_does_not_expose_plaintext(secret_cipher: SecretCipher) -> None:
    """计划原文:密文不含明文,同 binding 解密还原。"""
    encrypted = secret_cipher.encrypt(b"sk-example", binding=b"workspace:model")
    assert b"sk-example" not in encrypted.ciphertext
    assert secret_cipher.decrypt(encrypted, binding=b"workspace:model") == b"sk-example"


def test_wrong_binding_cannot_decrypt(secret_cipher: SecretCipher) -> None:
    """架构文档 §9:Secret 绑定其来源端点,换 binding 即解密失败。"""
    encrypted = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    with pytest.raises(SecretDecryptFailed):
        secret_cipher.decrypt(encrypted, binding=b"workspace:other-endpoint")


@pytest.mark.parametrize(
    "changed_kwargs",
    [
        {
            "workspace_id": "ws-2",
            "connection_type": "MODEL",
            "normalized_endpoint": "https://api.example.com/v1",
        },
        {
            "workspace_id": "ws-1",
            "connection_type": "MCP",
            "normalized_endpoint": "https://api.example.com/v1",
        },
        {
            "workspace_id": "ws-1",
            "connection_type": "MODEL",
            "normalized_endpoint": "https://api.example.com/v2",
        },
    ],
)
def test_binding_covers_workspace_connection_and_endpoint(
    secret_cipher: SecretCipher, changed_kwargs: dict[str, str]
) -> None:
    """associated data 任一分量不同(workspace/连接类型/规范化端点)即解密失败。"""
    binding = build_secret_binding(
        workspace_id="ws-1",
        connection_type="MODEL",
        normalized_endpoint="https://api.example.com/v1",
    )
    encrypted = secret_cipher.encrypt(TEST_PLAINTEXT, binding=binding)
    assert secret_cipher.decrypt(encrypted, binding=binding) == TEST_PLAINTEXT
    with pytest.raises(SecretDecryptFailed):
        secret_cipher.decrypt(encrypted, binding=build_secret_binding(**changed_kwargs))


def test_encryption_nonce_is_unique(secret_cipher: SecretCipher) -> None:
    """同一明文同一 binding 两次加密,nonce 与密文都必须不同。"""
    first = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    second = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_encrypted_secret_is_frozen(secret_cipher: SecretCipher) -> None:
    encrypted = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    with pytest.raises(ValidationError):
        encrypted.ciphertext = b"tampered"


def test_repr_does_not_expose_plaintext(secret_cipher: SecretCipher) -> None:
    encrypted = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    assert "sk-example" not in repr(encrypted)


# ---------------------------------------------------------------------------
# 主密钥加载(RUNTIME_MASTER_KEY,32 字节 Base64)
# ---------------------------------------------------------------------------


def test_master_key_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥从 RUNTIME_MASTER_KEY 注入,不写库不写仓库。"""
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, KEY_A_B64)
    cipher = SecretCipher()
    encrypted = cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    assert cipher.decrypt(encrypted, binding=BINDING) == TEST_PLAINTEXT


def test_wrong_master_key_cannot_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, KEY_A_B64)
    cipher = SecretCipher()
    encrypted = cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, KEY_B_B64)
    with pytest.raises(SecretDecryptFailed):
        SecretCipher().decrypt(encrypted, binding=BINDING)


def test_missing_master_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
    with pytest.raises(MasterKeyMissing):
        SecretCipher()


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-base64-!!!",
        base64.b64encode(bytes(16)).decode("ascii"),
        base64.b64encode(bytes(33)).decode("ascii"),
    ],
)
def test_invalid_master_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch, encoded: str
) -> None:
    """非 Base64 或长度不是 32 字节的主密钥必须被明确拒绝。"""
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, encoded)
    with pytest.raises(MasterKeyInvalid):
        SecretCipher()


# ---------------------------------------------------------------------------
# 篡改与泄漏防护(计划 Step 4:日志捕获中不含测试 Secret)
# ---------------------------------------------------------------------------


def test_tampered_ciphertext_is_rejected(secret_cipher: SecretCipher) -> None:
    encrypted = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    tampered = EncryptedSecret(
        key_version=encrypted.key_version,
        nonce=encrypted.nonce,
        ciphertext=bytes([encrypted.ciphertext[0] ^ 0xFF]) + encrypted.ciphertext[1:],
        tag=encrypted.tag,
    )
    with pytest.raises(SecretDecryptFailed):
        secret_cipher.decrypt(tampered, binding=BINDING)


def test_decryption_failure_does_not_expose_secret(
    secret_cipher: SecretCipher, caplog: pytest.LogCaptureFixture
) -> None:
    """计划 Step 4:异常消息与日志捕获中均不含测试 Secret。"""
    encrypted = secret_cipher.encrypt(TEST_PLAINTEXT, binding=BINDING)
    with caplog.at_level(logging.DEBUG), pytest.raises(SecretDecryptFailed) as excinfo:
        secret_cipher.decrypt(encrypted, binding=b"workspace:other")
    assert "sk-example" not in str(excinfo.value)
    assert "sk-example" not in caplog.text


# ---------------------------------------------------------------------------
# redact():递归脱敏(供 Task 7 审计复用)
# ---------------------------------------------------------------------------


def test_redact_replaces_sensitive_keys_at_any_depth() -> None:
    payload = {
        "api_key": "sk-example",
        "nested": {"password": "hunter2", "model": "glm-4"},
        "items": [{"token": "t-1", "name": "ok"}],
    }
    assert redact(payload) == {
        "api_key": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "model": "glm-4"},
        "items": [{"token": "[REDACTED]", "name": "ok"}],
    }


def test_redact_matches_key_names_case_insensitively() -> None:
    assert redact({"API_KEY": "sk-example"})["API_KEY"] == "[REDACTED]"
    assert redact({"clientSecret": "s"})["clientSecret"] == "[REDACTED]"


def test_redact_keeps_non_sensitive_values() -> None:
    payload = {
        "model": "glm-4",
        "temperature": 0.2,
        "count": 3,
        "empty": None,
        "tags": ["a", "b"],
    }
    assert redact(payload) == payload


def test_redact_scalars_pass_through() -> None:
    assert redact("plain") == "plain"
    assert redact(42) == 42
    assert redact(None) is None


def test_redact_does_not_mutate_input() -> None:
    """纯函数:脱敏返回新结构,不修改原 payload。"""
    payload = {"api_key": "sk-example", "nested": {"token": "t"}}
    snapshot = copy.deepcopy(payload)
    redact(payload)
    assert payload == snapshot
