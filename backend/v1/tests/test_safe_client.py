from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock

import pytest

from app.http.safe_client import (
    PublicOnlyResolver,
    UnsafeTargetError,
    normalize_public_http_url,
)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "100.64.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1"],
)
@pytest.mark.asyncio
async def test_resolver_rejects_non_global_address_before_returning_to_connector(
    monkeypatch,
    address: str,
) -> None:
    loop = asyncio.get_running_loop()
    getaddrinfo = AsyncMock(
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 80),
            )
        ]
    )
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)

    with pytest.raises(UnsafeTargetError, match="非公网地址"):
        await PublicOnlyResolver().resolve("rebinding.example", 80)

    getaddrinfo.assert_awaited_once_with(
        "rebinding.example",
        80,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )


@pytest.mark.asyncio
async def test_resolver_returns_only_the_checked_public_addresses(
    monkeypatch,
) -> None:
    loop = asyncio.get_running_loop()
    getaddrinfo = AsyncMock(
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
    )
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)

    results = await PublicOnlyResolver().resolve("example.com", 443)

    assert results == [
        {
            "hostname": "example.com",
            "host": "93.184.216.34",
            "port": 443,
            "family": socket.AF_INET,
            "proto": 0,
            "flags": socket.AI_NUMERICHOST,
        }
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://10.0.0.1/private",
        "http://[::1]/private",
        "http://100.64.0.1/shared",
        "http://192.0.2.1/documentation",
        "http://198.51.100.1/documentation",
        "http://203.0.113.1/documentation",
        "file:///etc/passwd",
        "https://user:password@example.com/file",
    ],
)
def test_url_validation_rejects_private_or_credentialed_target(url: str) -> None:
    with pytest.raises(UnsafeTargetError):
        normalize_public_http_url(url)


@pytest.mark.parametrize(
    "url",
    [None, "http://[::1", "http://example.com:bad/port"],
)
def test_url_validation_rejects_invalid_input(url) -> None:
    with pytest.raises(UnsafeTargetError, match="上游地址无效"):
        normalize_public_http_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://localhost/x", "http://LOCALHOST.localdomain/x", "http://0.0.0.0/x", "http://[::]/x"],
)
def test_url_validation_rejects_local_hostnames(url: str) -> None:
    with pytest.raises(UnsafeTargetError, match="本机地址"):
        normalize_public_http_url(url)


def test_url_validation_rejects_private_ip_literal() -> None:
    with pytest.raises(UnsafeTargetError, match="非公网地址"):
        normalize_public_http_url("http://169.254.1.1/x")


@pytest.mark.asyncio
async def test_resolver_rejects_unparseable_address(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    getaddrinfo = AsyncMock(
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("not-an-ip", 80))]
    )
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    with pytest.raises(UnsafeTargetError, match="域名解析结果无效"):
        await PublicOnlyResolver().resolve("broken.example", 80)


@pytest.mark.asyncio
async def test_resolver_rejects_empty_resolution(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[]))
    with pytest.raises(UnsafeTargetError, match="域名没有可用的公网地址"):
        await PublicOnlyResolver().resolve("empty.example", 80)


@pytest.mark.asyncio
async def test_resolver_close_is_noop() -> None:
    assert await PublicOnlyResolver().close() is None


@pytest.mark.asyncio
async def test_create_public_connector_uses_public_resolver() -> None:
    from app.http.safe_client import create_public_connector

    connector = create_public_connector()
    assert isinstance(connector._resolver, PublicOnlyResolver)
    await connector.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/file.zip",
        "https://EXAMPLE.com/path#frag",
        "http://93.184.216.34/file",
        "http://[2606:2800:220:1:248:1893:25c8:1946]/file",
    ],
)
def test_url_validation_accepts_public_targets(url: str) -> None:
    assert normalize_public_http_url(url) is not None
