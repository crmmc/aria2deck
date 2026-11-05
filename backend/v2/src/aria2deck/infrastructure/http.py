from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import AsyncIterator, Mapping
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from ..application.downloads.models import UpstreamResponse
from ..domain.errors import DomainError, ErrorCode

_REDIRECTS = {301, 302, 303, 307, 308}


def _upstream_error(message: str = "上游下载服务请求失败") -> DomainError:
    return DomainError(ErrorCode.UPSTREAM_FAILURE, message)


def require_public_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "目标地址无效") from None
    if not address.is_global:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "目标地址不是公网地址")


def validate_public_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "目标地址无效") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or host.rstrip(".").lower() == "localhost"
        or host.rstrip(".").lower().endswith(".localhost")
    ):
        raise DomainError(ErrorCode.PERMISSION_DENIED, "目标地址必须是安全的 HTTP(S) 地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    require_public_address(host)


class PublicResolver(AbstractResolver):
    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, port, family=family, type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            raise _upstream_error("目标域名无法解析") from None
        results: list[ResolveResult] = []
        seen: set[tuple[int, str, int]] = set()
        for info_family, _type, _proto, _canonical, sockaddr in infos:
            address = str(sockaddr[0])
            require_public_address(address)
            key = (info_family, address, port)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=info_family,
                    proto=socket.IPPROTO_TCP,
                    flags=0,
                )
            )
        if not results:
            raise _upstream_error("目标域名无法解析")
        return results

    async def close(self) -> None:
        return None


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise _upstream_error("上游 Content-Length 无效") from None
    if parsed < 0:
        raise _upstream_error("上游 Content-Length 无效")
    return parsed


def _content_range(value: str) -> tuple[int, int, int | None]:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", value)
    if match is None:
        raise _upstream_error("上游 Content-Range 无效")
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start or total is not None and (total <= start or end >= total):
        raise _upstream_error("上游 Content-Range 无效")
    return start, end, total


class SafeHttpClient:
    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def open(
        self, *, url: str, headers: Mapping[str, str], range_header: str | None
    ) -> UpstreamResponse:
        resolver = PublicResolver()
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=resolver),
            timeout=timeout,
            # 逐字转发线缆字节：自动解压会让流出字节数超过上游声明的
            # （压缩后）Content-Length，触发网关中途杀流；Range 偏移也随解压失真。
            # shortcut: 无视 Accept-Encoding 强推压缩的源按压缩原样转发，需要透明解压时再升级。
            auto_decompress=False,
        )
        current = url
        current_headers = dict(headers)
        try:
            redirect_count = 0
            while True:
                validate_public_url(current)
                request_headers = dict(current_headers)
                if range_header is not None:
                    request_headers["Range"] = range_header
                if not any(name.lower() == "accept-encoding" for name in request_headers):
                    # 请求原始字节，让声明长度与转发字节天然一致；用户自带的编码偏好优先。
                    request_headers["Accept-Encoding"] = "identity"
                response = await session.get(current, headers=request_headers, allow_redirects=False)
                if response.status in _REDIRECTS:
                    location = response.headers.get("Location")
                    response.release()
                    if location is None:
                        raise _upstream_error("上游重定向缺少目标")
                    following = urljoin(current, location)
                    validate_public_url(following)
                    if _origin(following) != _origin(current):
                        # Source headers are authentication context, not redirect payloads.
                        current_headers = {}
                    current = following
                    redirect_count += 1
                    if redirect_count > 10:
                        raise _upstream_error("上游重定向次数过多")
                    continue
                if response.status == 416:
                    raise DomainError(ErrorCode.RANGE_NOT_SATISFIABLE, "下载范围无效")
                if response.status == 413:
                    raise DomainError(ErrorCode.TASK_TOO_LARGE, "上游内容超过大小限制")
                if response.status not in {200, 206}:
                    raise _upstream_error()
                return self._response(session, response)
        except DomainError:
            await session.close()
            raise
        except (TimeoutError, aiohttp.ClientError, OSError):
            await session.close()
            raise _upstream_error() from None

    def _response(
        self, session: aiohttp.ClientSession, response: aiohttp.ClientResponse
    ) -> UpstreamResponse:
        try:
            length = _content_length(response.headers.get("Content-Length"))
            offset = 0
            total = length
            body_bytes = length
            if response.status == 206:
                offset, end, total = _content_range(
                    response.headers.get("Content-Range", "")
                )
                range_length = end - offset + 1
                if length is not None and length != range_length:
                    raise _upstream_error("上游范围响应长度不一致")
                body_bytes = range_length
            headers = {
                key: response.headers[key]
                for key in ("Content-Length", "Content-Range")
                if key in response.headers
            }
        except DomainError:
            response.close()
            raise

        async def close() -> None:
            response.close()
            await session.close()

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    yield chunk
            finally:
                await close()

        return UpstreamResponse(
            status=response.status,
            headers=headers,
            body=body(),
            total_bytes=total,
            body_bytes=body_bytes,
            close=close,
            content_offset=offset,
        )

    async def fetch_text(self, url: str) -> str:
        response = await self.open(url=url, headers={}, range_header=None)
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in response.body:
                size += len(chunk)
                if size > 1024 * 1024:
                    raise DomainError(ErrorCode.UPSTREAM_FAILURE, "Tracker 远程列表超过 1 MiB")
                chunks.append(chunk)
        finally:
            await response.close()
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            raise _upstream_error("Tracker 远程列表不是 UTF-8 文本") from None
