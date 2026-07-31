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
