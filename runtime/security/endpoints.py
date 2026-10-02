"""外部端点安全(计划 01 Task 6;架构文档 §9)。

EndpointPolicy 对模型/MCP 端点 URL 做 SSRF 与凭证泄漏防护:

- 规范化:host 小写、去默认端口、去 fragment;
- URL 携带 credential(userinfo)或 fragment 一律拒绝;
- 仅允许 HTTPS;本地开发可通过 ``allow_http=True`` 显式放开 HTTP;
- host 必须命中部署级 allowlist;
- IP 字面量直接按私网/loopback/link-local/云元数据地址拒绝,公网 IP 仍须在
  allowlist 内;
- 域名先过 allowlist,再经注入式 resolver 解析(默认 ``socket.getaddrinfo``),
  任一解析结果落在禁用网段即拒绝;解析失败或结果为空同样拒绝。

resolver 可注入,测试不做真实网络调用。拒绝消息不回显 URL 中可能敏感的内容。
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from typing import Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict


REASON_URL_INVALID: Final = "ENDPOINT_URL_INVALID"
REASON_SCHEME_FORBIDDEN: Final = "ENDPOINT_SCHEME_FORBIDDEN"
REASON_CREDENTIAL_FORBIDDEN: Final = "ENDPOINT_CREDENTIAL_FORBIDDEN"
REASON_FRAGMENT_FORBIDDEN: Final = "ENDPOINT_FRAGMENT_FORBIDDEN"
REASON_HOST_NOT_IN_ALLOWLIST: Final = "ENDPOINT_HOST_NOT_IN_ALLOWLIST"
REASON_ADDRESS_FORBIDDEN: Final = "ENDPOINT_ADDRESS_FORBIDDEN"
REASON_DNS_FAILED: Final = "ENDPOINT_DNS_FAILED"

DEFAULT_SCHEME_PORT: Final[dict[str, int]] = {"https": 443, "http": 80}

# 云元数据地址(IPv4 IMDS 与 IPv6 IMDS)显式列出以自文档;分别已被
# link-local(169.254.0.0/16)与私网(fc00::/7)规则覆盖。
_CLOUD_METADATA_IPV4: Final = ipaddress.ip_address("169.254.169.254")
_CLOUD_METADATA_IPV6: Final = ipaddress.ip_address("fd00:ec2::254")

Resolver = Callable[[str], Sequence[str]]


def _system_resolver(hostname: str) -> Sequence[str]:
    """默认 resolver:socket.getaddrinfo,返回全部解析结果。"""
    return [str(info[4][0]) for info in socket.getaddrinfo(hostname, None)]


class EndpointRejected(Exception):
    """端点被拒绝;``reason_code`` 说明原因,消息不含 URL 中可能敏感的内容。"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class NormalizedEndpoint(BaseModel):
    """规范化端点;``url`` 已去 credential/fragment/默认端口,host 为小写。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    scheme: str
    host: str
    port: int


def _ensure_allowed_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """拒绝私网、loopback、link-local、元数据、组播与保留地址。"""
    forbidden = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address == _CLOUD_METADATA_IPV4
        or address == _CLOUD_METADATA_IPV6
    )
    if forbidden:
        raise EndpointRejected(
            REASON_ADDRESS_FORBIDDEN, "endpoint resolves to a forbidden address"
        )


class EndpointPolicy:
    """部署级端点 allowlist 与 SSRF 防护;无状态,可注入 resolver。"""

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        resolver: Resolver | None = None,
        allow_http: bool = False,
    ) -> None:
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._resolver: Resolver = resolver if resolver is not None else _system_resolver
        self._allow_http = allow_http

    def validate_model_endpoint(self, url: str) -> NormalizedEndpoint:
        """校验并规范化端点 URL;不抛出即通过。"""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if not scheme or not parts.netloc:
            raise EndpointRejected(REASON_URL_INVALID, "endpoint URL must be absolute")

        allowed_schemes = ("http", "https") if self._allow_http else ("https",)
        if scheme not in allowed_schemes:
            raise EndpointRejected(
                REASON_SCHEME_FORBIDDEN, "endpoint URL scheme is not allowed"
            )
        if parts.username or parts.password:
            raise EndpointRejected(
                REASON_CREDENTIAL_FORBIDDEN, "endpoint URL must not carry credentials"
            )
        if parts.fragment:
            raise EndpointRejected(
                REASON_FRAGMENT_FORBIDDEN, "endpoint URL must not carry a fragment"
            )

        try:
            port = parts.port
        except ValueError as error:
            raise EndpointRejected(
                REASON_URL_INVALID, "endpoint URL has an invalid port"
            ) from error
        hostname = parts.hostname
        if not hostname:
            raise EndpointRejected(REASON_URL_INVALID, "endpoint URL requires a host")

        host = hostname.lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            self._validate_domain(host)
        else:
            # IP 字面量:先拒绝危险地址(即使被 allowlist 收录),公网 IP 仍须
            # 命中 allowlist,且无需 DNS 解析。
            _ensure_allowed_address(address)
            if host not in self._allowed_hosts:
                raise EndpointRejected(
                    REASON_HOST_NOT_IN_ALLOWLIST, "endpoint host is not in the allowlist"
                )

        return self._normalize(parts, scheme, host, port)

    def _validate_domain(self, host: str) -> None:
        """域名:先 allowlist,再 DNS 解析(可注入),逐个地址校验。"""
        if host not in self._allowed_hosts:
            raise EndpointRejected(
                REASON_HOST_NOT_IN_ALLOWLIST, "endpoint host is not in the allowlist"
            )
        try:
            resolved = self._resolver(host)
        except OSError as error:
            raise EndpointRejected(
                REASON_DNS_FAILED, "endpoint host could not be resolved"
            ) from error
        if not resolved:
            raise EndpointRejected(REASON_DNS_FAILED, "endpoint host resolved to no addresses")
        for item in resolved:
            try:
                address = ipaddress.ip_address(item)
            except ValueError as error:
                raise EndpointRejected(
                    REASON_DNS_FAILED, "resolver returned an invalid address"
                ) from error
            _ensure_allowed_address(address)

    @staticmethod
    def _normalize(
        parts: SplitResult, scheme: str, host: str, port: int | None
    ) -> NormalizedEndpoint:
        """去默认端口、去 credential/fragment,重建 netloc。"""
        default_port = DEFAULT_SCHEME_PORT[scheme]
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and port != default_port:
            netloc = f"{netloc}:{port}"
        normalized_url = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
        return NormalizedEndpoint(
            url=normalized_url,
            scheme=scheme,
            host=host,
            port=port if port is not None else default_port,
        )
