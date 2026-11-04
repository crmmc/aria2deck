from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urldefrag, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class UnsafeTargetError(OSError):
    pass


def normalize_public_http_url(url: str) -> str:
    try:
        normalized, _ = urldefrag(url)
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeTargetError("上游地址无效") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeTargetError("上游地址无效或协议不受支持")
    if hostname.lower() in {
        "localhost",
        "localhost.localdomain",
        "0.0.0.0",  # noqa: S104  # SSRF blacklist literal, not a bind address
        "::",
    }:
        raise UnsafeTargetError("不允许访问本机地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized
    if not address.is_global:
        raise UnsafeTargetError("不允许访问非公网地址")
    return normalized


def _resolver_result(
    *, hostname: str, port: int, family: int, address: str
) -> dict[str, object]:
    return {
        "hostname": hostname,
        "host": address,
        "port": port,
        "family": family,
        "proto": 0,
        "flags": socket.AI_NUMERICHOST,
    }


class PublicOnlyResolver(AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        results: list[dict[str, object]] = []
        seen: set[tuple[int, str]] = set()
        for resolved_family, _type, _proto, _canonname, sockaddr in infos:
            address = str(sockaddr[0])
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeTargetError("域名解析结果无效") from exc
            if not parsed_address.is_global:
                raise UnsafeTargetError("域名解析到非公网地址")
            key = (resolved_family, address)
            if key not in seen:
                seen.add(key)
                results.append(
                    _resolver_result(
                        hostname=host,
                        port=port,
                        family=resolved_family,
                        address=address,
                    )
                )
        if not results:
            raise UnsafeTargetError("域名没有可用的公网地址")
        return results

    async def close(self) -> None:
        return None


def create_public_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        resolver=PublicOnlyResolver(),
        use_dns_cache=False,
        force_close=True,
        limit_per_host=1,
    )
