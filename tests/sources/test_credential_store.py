"""凭据存储测试 — 注册、解析、列名与默认三渠道凭据引用。"""

from sources.credential_store import (
    Credential,
    CredentialStore,
    create_default_credential_store,
)


def test_register_and_resolve():
    store = CredentialStore()
    store.register(Credential(name="taobao_oauth", credential_type="oauth_token", channel="taobao"))
    cred = store.resolve("taobao_oauth")
    assert cred is not None
    assert cred.channel == "taobao"
    assert cred.credential_type == "oauth_token"
    assert store.resolve("nonexistent") is None


def test_list_and_has():
    store = CredentialStore()
    store.register(Credential(name="a", credential_type="api_key"))
    store.register(Credential(name="b", credential_type="api_key"))
    assert store.list_names() == ["a", "b"]
    assert store.has("a") and not store.has("c")


def test_default_store_three_channels():
    """默认凭据存储预置三渠道 oauth 引用；掩码值不含明文密钥。"""
    store = create_default_credential_store()
    assert sorted(store.list_names()) == ["douyin_oauth", "jd_oauth", "taobao_oauth"]
    cred = store.resolve("taobao_oauth")
    assert cred.credential_type == "oauth_token"
    assert cred.masked_secret.endswith("*****")  # 掩码值，不落明文密钥