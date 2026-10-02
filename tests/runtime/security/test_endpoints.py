"""外部端点 SSRF 防护不变式测试(计划 01 Task 6 Step 1;架构文档 §9)。

第一条为计划文档原文;其余为架构文档写明的规则:HTTPS-only(本地开发可显式
放开 http)、禁 credential/fragment、部署级 allowlist、默认端口与大小写归一、
IP 字面量与 DNS 解析结果中的私网/loopback/link-local/元数据地址拒绝、
解析失败拒绝。所有域名解析均通过注入的 fake resolver,不做真实网络调用。
"""

from collections.abc import Sequence

import pytest

from runtime.security.endpoints import (
    REASON_ADDRESS_FORBIDDEN,
    REASON_CREDENTIAL_FORBIDDEN,
    REASON_DNS_FAILED,
    REASON_FRAGMENT_FORBIDDEN,
    REASON_HOST_NOT_IN_ALLOWLIST,
    REASON_SCHEME_FORBIDDEN,
    EndpointPolicy,
    EndpointRejected,
    NormalizedEndpoint,
)


PUBLIC_IP = "93.184.216.34"


# ---------------------------------------------------------------------------
# 计划原文:三条不安全 URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8000/v1",
        "https://user:pass@example.com/v1",
    ],
)
def test_endpoint_policy_blocks_unsafe_urls(
    endpoint_policy: EndpointPolicy, url: str
) -> None:
    """计划原文:元数据地址、loopback、URL credential 一律拒绝。"""
    with pytest.raises(EndpointRejected):
        endpoint_policy.validate_model_endpoint(url)


# ---------------------------------------------------------------------------
# URL 规范化与结构规则
# ---------------------------------------------------------------------------


def test_endpoint_normalizes_default_port_and_host_case(
    endpoint_policy: EndpointPolicy,
) -> None:
    normalized = endpoint_policy.validate_model_endpoint("https://API.Example.COM:443/v1")
    assert normalized == NormalizedEndpoint(
        url="https://api.example.com/v1",
        scheme="https",
        host="api.example.com",
        port=443,
    )


def test_endpoint_keeps_explicit_non_default_port(
    endpoint_policy: EndpointPolicy,
) -> None:
    normalized = endpoint_policy.validate_model_endpoint("https://api.example.com:8443/v1")
    assert normalized.url == "https://api.example.com:8443/v1"
    assert normalized.port == 8443


def test_url_credentials_are_rejected_with_reason(endpoint_policy: EndpointPolicy) -> None:
    with pytest.raises(EndpointRejected) as excinfo:
        endpoint_policy.validate_model_endpoint("https://user:pass@example.com/v1")
    assert excinfo.value.reason_code == REASON_CREDENTIAL_FORBIDDEN


def test_fragment_is_rejected_with_reason(endpoint_policy: EndpointPolicy) -> None:
    with pytest.raises(EndpointRejected) as excinfo:
        endpoint_policy.validate_model_endpoint("https://api.example.com/v1#section")
    assert excinfo.value.reason_code == REASON_FRAGMENT_FORBIDDEN


@pytest.mark.parametrize(
    "url",
    ["", "not-a-url", "https://", "/relative/path", "ftp://api.example.com"],
)
def test_malformed_or_disallowed_scheme_urls_are_rejected(
    endpoint_policy: EndpointPolicy, url: str
) -> None:
    with pytest.raises(EndpointRejected):
        endpoint_policy.validate_model_endpoint(url)


def test_non_https_scheme_is_rejected_with_reason(endpoint_policy: EndpointPolicy) -> None:
    with pytest.raises(EndpointRejected) as excinfo:
        endpoint_policy.validate_model_endpoint("ftp://api.example.com/v1")
    assert excinfo.value.reason_code == REASON_SCHEME_FORBIDDEN


# ---------------------------------------------------------------------------
# HTTPS-only 与本地开发显式放开
# ---------------------------------------------------------------------------


def test_plain_http_is_rejected_by_default(endpoint_policy: EndpointPolicy) -> None:
    with pytest.raises(EndpointRejected) as excinfo:
        endpoint_policy.validate_model_endpoint("http://api.example.com/v1")
    assert excinfo.value.reason_code == REASON_SCHEME_FORBIDDEN


def test_http_can_be_explicitly_allowed_for_local_development() -> None:
    """架构文档 §9:本地开发如需 http,必须通过显式配置开启。"""
    policy = EndpointPolicy(
        allowed_hosts=frozenset({"api.example.com"}),
        resolver=lambda hostname: [PUBLIC_IP],
        allow_http=True,
    )
    normalized = policy.validate_model_endpoint("http://API.example.com:80/v1")
    assert normalized.url == "http://api.example.com/v1"
    assert normalized.scheme == "http"
    assert normalized.port == 80


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------


def test_host_outside_allowlist_is_rejected_without_resolution() -> None:
    def exploding_resolver(hostname: str) -> Sequence[str]:
        raise AssertionError("resolver must not be called for unknown hosts")

    policy = EndpointPolicy(
        allowed_hosts=frozenset({"api.example.com"}), resolver=exploding_resolver
    )
    with pytest.raises(EndpointRejected) as excinfo:
        policy.validate_model_endpoint("https://evil.example.com/v1")
    assert excinfo.value.reason_code == REASON_HOST_NOT_IN_ALLOWLIST


def test_allowlist_matching_and_resolution_use_normalized_host() -> None:
    """allowlist 匹配与 resolver 调用都使用小写归一化 host。"""
    seen: list[str] = []

    def recording_resolver(hostname: str) -> Sequence[str]:
        seen.append(hostname)
        return [PUBLIC_IP]

    policy = EndpointPolicy(
        allowed_hosts=frozenset({"API.example.COM"}), resolver=recording_resolver
    )
    normalized = policy.validate_model_endpoint("https://API.example.COM/v1")
    assert seen == ["api.example.com"]
    assert normalized.host == "api.example.com"


# ---------------------------------------------------------------------------
# IP 字面量:私网/loopback/link-local/元数据地址
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",
        "https://127.0.0.1/v1",
        "https://10.0.0.5/v1",
        "https://172.16.0.9/v1",
        "https://192.168.1.1/v1",
        "https://[::1]/v1",
        "https://[fc00::1]/v1",
        "https://[fe80::1]/v1",
    ],
)
def test_private_and_metadata_ip_literals_are_rejected(
    endpoint_policy: EndpointPolicy, url: str
) -> None:
    with pytest.raises(EndpointRejected) as excinfo:
        endpoint_policy.validate_model_endpoint(url)
    assert excinfo.value.reason_code == REASON_ADDRESS_FORBIDDEN


def test_private_ip_is_rejected_even_if_allowlisted() -> None:
    """危险地址无条件拒绝,allowlist 不能放行私网 IP。"""
    policy = EndpointPolicy(
        allowed_hosts=frozenset({"127.0.0.1"}), resolver=lambda hostname: ["127.0.0.1"]
    )
    with pytest.raises(EndpointRejected) as excinfo:
        policy.validate_model_endpoint("https://127.0.0.1/v1")
    assert excinfo.value.reason_code == REASON_ADDRESS_FORBIDDEN


def test_public_ip_literal_in_allowlist_passes(endpoint_policy: EndpointPolicy) -> None:
    normalized = endpoint_policy.validate_model_endpoint(f"https://{PUBLIC_IP}/v1")
    assert normalized.host == PUBLIC_IP
    assert normalized.url == f"https://{PUBLIC_IP}/v1"


# ---------------------------------------------------------------------------
# DNS 解析后校验(fake resolver,不做真实网络调用)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resolved",
    [
        ["10.1.2.3"],
        [PUBLIC_IP, "127.0.0.1"],
        ["fe80::1"],
    ],
)
def test_dns_resolution_to_forbidden_address_is_rejected(resolved: list[str]) -> None:
    """任一解析结果落在禁用网段即拒绝,不允许多地址中混入私网。"""
    policy = EndpointPolicy(
        allowed_hosts=frozenset({"api.example.com"}), resolver=lambda hostname: resolved
    )
    with pytest.raises(EndpointRejected) as excinfo:
        policy.validate_model_endpoint("https://api.example.com/v1")
    assert excinfo.value.reason_code == REASON_ADDRESS_FORBIDDEN


def test_public_dns_result_passes() -> None:
    policy = EndpointPolicy(
        allowed_hosts=frozenset({"api.example.com"}), resolver=lambda hostname: [PUBLIC_IP]
    )
    normalized = policy.validate_model_endpoint("https://api.example.com/v1")
    assert normalized.url == "https://api.example.com/v1"


def test_dns_resolution_failure_is_rejected() -> None:
    def failing_resolver(hostname: str) -> Sequence[str]:
        raise OSError("resolution failed")

    policy = EndpointPolicy(
        allowed_hosts=frozenset({"api.example.com"}), resolver=failing_resolver
    )
    with pytest.raises(EndpointRejected) as excinfo:
        policy.validate_model_endpoint("https://api.example.com/v1")
    assert excinfo.value.reason_code == REASON_DNS_FAILED


def test_empty_resolution_is_rejected() -> None:
    policy = EndpointPolicy(
        allowed_hosts=frozenset({"api.example.com"}), resolver=lambda hostname: []
    )
    with pytest.raises(EndpointRejected) as excinfo:
        policy.validate_model_endpoint("https://api.example.com/v1")
    assert excinfo.value.reason_code == REASON_DNS_FAILED


# ---------------------------------------------------------------------------
# 拒绝消息不得携带 userinfo
# ---------------------------------------------------------------------------


def test_rejection_message_does_not_contain_userinfo(
    endpoint_policy: EndpointPolicy,
) -> None:
    with pytest.raises(EndpointRejected) as excinfo:
        endpoint_policy.validate_model_endpoint("https://user:pass@example.com/v1")
    message = str(excinfo.value)
    assert "user" not in message
    assert "pass" not in message
    assert excinfo.value.reason_code == REASON_CREDENTIAL_FORBIDDEN
