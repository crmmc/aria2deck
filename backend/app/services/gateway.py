from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp

from app.core.config import get_internal_base_url, settings
from app.domain.error_text import over_limit
from app.domain.status import TERMINAL_DOWNLOAD_STATUSES
from app.http.safe_client import (
    UnsafeTargetError,
    create_public_connector,
    normalize_public_http_url,
)
from app.repositories.task.downloads import get_global_download_by_id
from app.services.settings_service import get_max_task_size

CAPABILITY_HEADER = "X-Aria2Deck-Fetch-Capability"
_CAPABILITY_DOMAIN = b"aria2deck:internal-fetch:v1"
_RESOURCE_IDENTITY_DOMAIN = b"aria2deck:http-resource-identity:v1"
_MAX_CAPABILITY_LENGTH = 16 * 1024
_MAX_SOURCE_HEADER_BYTES = 8 * 1024
_MAX_RANGE_HEADER_LENGTH = 128
_MAX_SIZE_HEADER_LENGTH = 128
_MAX_DECIMAL_DIGITS = 20
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_BLOCKED_SOURCE_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "range",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        CAPABILITY_HEADER.lower(),
    }
)


class GatewayDownloadNotFound(Exception):
    pass


class GatewayDownloadUnavailable(Exception):
    def __init__(self, *, terminal: bool) -> None:
        self.terminal = terminal


class InvalidCapabilityError(Exception):
    pass


class InvalidRangeError(Exception):
    pass


class GatewayTargetError(Exception):
    pass


class GatewayUpstreamError(Exception):
    pass


class GatewaySizeExceeded(Exception):
    pass


@dataclass(frozen=True)
class SourceRequestOptions:
    headers: tuple[tuple[str, str], ...] = ()
    username: str | None = None
    password: str | None = None
    mirrors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RangeRequest:
    start: int
    end: int | None


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidCapabilityError("下载凭证无效") from exc


def _domain_key(domain: bytes) -> bytes:
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        domain,
        hashlib.sha256,
    ).digest()


def _capability_key() -> bytes:
    return _domain_key(_CAPABILITY_DOMAIN)


def _source_header_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        return list(value)
    raise ValueError("header 选项必须是字符串或字符串列表")


def _normalize_source_headers(value: object) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    total_bytes = 0
    for raw_header in _source_header_values(value):
        if ":" not in raw_header:
            raise ValueError("header 选项格式无效")
        name, header_value = raw_header.split(":", 1)
        name = name.strip()
        header_value = header_value.strip()
        lowered = name.lower()
        if (
            not _HEADER_NAME_RE.fullmatch(name)
            or lowered in _BLOCKED_SOURCE_HEADERS
            or "\r" in header_value
            or "\n" in header_value
            or "\x00" in header_value
        ):
            raise ValueError("header 选项包含不允许的请求头")
        total_bytes += len(name.encode()) + len(header_value.encode()) + 2
        if total_bytes > _MAX_SOURCE_HEADER_BYTES:
            raise ValueError("header 选项过大")
        normalized.append((lowered, header_value))
    return tuple(sorted(normalized))


def source_request_options(
    options: Mapping[str, object] | None,
    *,
    mirrors: list[str] | None = None,
) -> SourceRequestOptions:
    options = options or {}
    headers: tuple[tuple[str, str], ...] = ()
    if "header" in options:
        headers = _normalize_source_headers(options["header"])
    username = str(options["http-user"]) if "http-user" in options else None
    password = str(options["http-passwd"]) if "http-passwd" in options else None
    for value in (username, password):
        if value is not None and (
            len(value.encode()) > 4096
            or "\r" in value
            or "\n" in value
            or "\x00" in value
        ):
            raise ValueError("HTTP 认证选项无效")
    normalized_mirrors: list[str] = []
    for mirror in mirrors or []:
        if len(normalized_mirrors) >= 15:
            raise ValueError("HTTP 镜像地址过多")
        try:
            normalized = normalize_public_http_url(str(mirror))
        except UnsafeTargetError as exc:
            raise ValueError(str(exc)) from exc
        if normalized not in normalized_mirrors:
            normalized_mirrors.append(normalized)
    return SourceRequestOptions(
        headers,
        username,
        password,
        tuple(normalized_mirrors),
    )


def _payload(options: SourceRequestOptions) -> dict[str, object]:
    payload: dict[str, object] = {"v": 1}
    if options.headers:
        payload["h"] = [list(item) for item in options.headers]
    if options.username is not None:
        payload["u"] = options.username
    if options.password is not None:
        payload["p"] = options.password
    if options.mirrors:
        payload["m"] = list(options.mirrors)
    return payload


def http_resource_identity(
    resource_key: str,
    options: SourceRequestOptions,
) -> str:
    if options == SourceRequestOptions():
        return resource_key
    context = json.dumps(
        _payload(options), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    message = resource_key.encode("utf-8") + b"\n" + context
    return hmac.new(
        _domain_key(_RESOURCE_IDENTITY_DOMAIN),
        message,
        hashlib.sha256,
    ).hexdigest()


def create_capability(
    download_id: int,
    source_uri: str,
    options: SourceRequestOptions,
) -> str:
    payload = json.dumps(
        _payload(options), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    encoded_payload = _encode(payload)
    message = (
        f"{download_id}\n{source_uri}\n{encoded_payload}".encode()
    )
    signature = hmac.new(_capability_key(), message, hashlib.sha256).digest()
    capability = f"{encoded_payload}.{_encode(signature)}"
    if len(capability) > _MAX_CAPABILITY_LENGTH:
        raise ValueError("HTTP 请求选项过大")
    return capability


def _options_from_payload(payload: object) -> SourceRequestOptions:
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise InvalidCapabilityError("下载凭证无效")
    if set(payload) - {"v", "h", "u", "p", "m"}:
        raise InvalidCapabilityError("下载凭证无效")
    raw_headers = payload.get("h", [])
    if not isinstance(raw_headers, list):
        raise InvalidCapabilityError("下载凭证无效")
    header_lines: list[str] = []
    for item in raw_headers:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise InvalidCapabilityError("下载凭证无效")
        header_lines.append(f"{item[0]}: {item[1]}")
    username = payload.get("u")
    password = payload.get("p")
    if username is not None and not isinstance(username, str):
        raise InvalidCapabilityError("下载凭证无效")
    if password is not None and not isinstance(password, str):
        raise InvalidCapabilityError("下载凭证无效")
    raw_mirrors = payload.get("m", [])
    if not isinstance(raw_mirrors, list) or not all(
        isinstance(item, str) for item in raw_mirrors
    ):
        raise InvalidCapabilityError("下载凭证无效")
    try:
        return source_request_options(
            {
                "header": header_lines,
                **({"http-user": username} if username is not None else {}),
                **({"http-passwd": password} if password is not None else {}),
            },
            mirrors=raw_mirrors,
        )
    except ValueError as exc:
        raise InvalidCapabilityError("下载凭证无效") from exc


def verify_capability(
    capability: str,
    download_id: int,
    source_uri: str,
) -> SourceRequestOptions:
    if not capability or len(capability) > _MAX_CAPABILITY_LENGTH:
        raise InvalidCapabilityError("下载凭证无效")
    try:
        encoded_payload, encoded_signature = capability.split(".")
    except ValueError as exc:
        raise InvalidCapabilityError("下载凭证无效") from exc
    signature = _decode(encoded_signature)
    message = (
        f"{download_id}\n{source_uri}\n{encoded_payload}".encode()
    )
    expected = hmac.new(_capability_key(), message, hashlib.sha256).digest()
    if len(signature) != len(expected) or not hmac.compare_digest(
        signature, expected
    ):
        raise InvalidCapabilityError("下载凭证无效")
    try:
        payload = json.loads(_decode(encoded_payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCapabilityError("下载凭证无效") from exc
    return _options_from_payload(payload)


async def authorize_gateway_request(
    download_id: int,
    source_index: int,
    capability: str,
) -> tuple[str, SourceRequestOptions]:
    download = await get_global_download_by_id(download_id)
    if download is None:
        raise GatewayDownloadNotFound
    source_uri = str(download.get("source_uri") or "")
    options = verify_capability(capability, download_id, source_uri)
    if str(download.get("resource_kind") or "") != "http":
        raise GatewayDownloadUnavailable(terminal=False)
    if str(download.get("status") or "") in TERMINAL_DOWNLOAD_STATUSES:
        raise GatewayDownloadUnavailable(terminal=True)
    sources = (source_uri, *options.mirrors)
    if source_index < 0 or source_index >= len(sources):
        raise GatewayDownloadNotFound
    return sources[source_index], options


def build_gateway_submission(
    *,
    download_id: int,
    source_uri: str,
    options: Mapping[str, object] | None,
    source_uris: list[str] | None = None,
) -> tuple[list[str], dict[str, object]]:
    source_options = source_request_options(
        options,
        mirrors=(source_uris or [])[1:],
    )
    capability = create_capability(
        download_id,
        source_uri,
        source_options,
    )
    gateway_base = f"{get_internal_base_url()}/_internal/fetch/{download_id}"
    gateway_uris = [
        f"{gateway_base}/{index}"
        for index in range(1 + len(source_options.mirrors))
    ]
    return gateway_uris, {
        "header": [f"{CAPABILITY_HEADER}: {capability}"],
        "out": "payload",
        "split": "1",
        "max-connection-per-server": "1",
        "auto-file-renaming": "false",
        "allow-overwrite": "true",
        "continue": "true",
    }


def parse_range_header(value: str | None) -> RangeRequest | None:
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) > _MAX_RANGE_HEADER_LENGTH:
        raise InvalidRangeError("Range 请求头过长")
    match = _RANGE_RE.fullmatch(normalized)
    if not match:
        raise InvalidRangeError("仅支持单段 bytes Range")
    raw_start, raw_end = match.groups()
    if len(raw_start) > _MAX_DECIMAL_DIGITS or len(raw_end) > _MAX_DECIMAL_DIGITS:
        raise InvalidRangeError("Range 数值过大")
    try:
        start = int(raw_start)
        end = int(raw_end) if raw_end else None
    except ValueError as exc:
        raise InvalidRangeError("Range 数值无效") from exc
    if end is not None and end < start:
        raise InvalidRangeError("Range 范围无效")
    return RangeRequest(start=start, end=end)


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), str(parsed.hostname).lower(), parsed.port or default_port


def _upstream_headers(
    options: SourceRequestOptions,
    *,
    include_source_headers: bool,
    range_header: str | None,
) -> list[tuple[str, str]]:
    headers = list(options.headers) if include_source_headers else []
    has_authorization = any(name == "authorization" for name, _ in headers)
    if include_source_headers and options.username is not None and not has_authorization:
        raw_credentials = f"{options.username}:{options.password or ''}".encode()
        headers.append(
            ("Authorization", f"Basic {base64.b64encode(raw_credentials).decode()}")
        )
    headers.append(("Accept-Encoding", "identity"))
    if range_header is not None:
        headers.append(("Range", range_header))
    return headers


def _caused_by_unsafe_target(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, UnsafeTargetError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _parse_content_length(response: aiohttp.ClientResponse) -> int | None:
    raw_value = response.headers.get("Content-Length")
    if raw_value is None:
        return None
    if (
        len(raw_value) > _MAX_SIZE_HEADER_LENGTH
        or len(raw_value) > _MAX_DECIMAL_DIGITS
        or not raw_value.isascii()
        or not raw_value.isdigit()
    ):
        raise GatewayUpstreamError("上游 Content-Length 无效")
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise GatewayUpstreamError("上游 Content-Length 无效") from exc
    return parsed


def _content_range(response: aiohttp.ClientResponse) -> tuple[int, int, int | None]:
    raw_value = response.headers.get("Content-Range", "")
    if len(raw_value) > _MAX_SIZE_HEADER_LENGTH:
        raise GatewayUpstreamError("上游 Content-Range 无效")
    match = _CONTENT_RANGE_RE.fullmatch(raw_value)
    if not match:
        raise GatewayUpstreamError("上游 Content-Range 无效")
    raw_start, raw_end, raw_total = match.groups()
    decimal_fields = (raw_start, raw_end) + (() if raw_total == "*" else (raw_total,))
    if any(len(field) > _MAX_DECIMAL_DIGITS for field in decimal_fields):
        raise GatewayUpstreamError("上游 Content-Range 无效")
    try:
        start = int(raw_start)
        end = int(raw_end)
        total = None if raw_total == "*" else int(raw_total)
    except ValueError as exc:
        raise GatewayUpstreamError("上游 Content-Range 无效") from exc
    if end < start or (total is not None and end >= total):
        raise GatewayUpstreamError("上游 Content-Range 无效")
    return start, end, total


def _response_headers(response: aiohttp.ClientResponse) -> dict[str, str]:
    allowed = (
        "Accept-Ranges",
        "Content-Length",
        "Content-Range",
        "Content-Type",
        "ETag",
        "Last-Modified",
    )
    return {
        name: response.headers[name]
        for name in allowed
        if name in response.headers
    }


def _stream_budget(
    response: aiohttp.ClientResponse,
    requested_range: RangeRequest | None,
    max_size: int,
) -> int:
    if response.status not in {200, 206}:
        return 0
    content_length = _parse_content_length(response)
    start = 0
    if response.status == 206:
        start, end, total = _content_range(response)
        if requested_range is None and start != 0:
            raise GatewayUpstreamError("上游返回了未请求的 Range")
        if requested_range is not None:
            if start != requested_range.start:
                raise GatewayUpstreamError("上游 Range 起点不匹配")
            if requested_range.end is not None and end > requested_range.end:
                raise GatewayUpstreamError("上游 Range 终点不匹配")
        if content_length is not None and content_length != end - start + 1:
            raise GatewayUpstreamError("上游 Range 长度不匹配")
        if total is not None and total > max_size:
            raise GatewaySizeExceeded(
                over_limit("下载内容", total, "超过系统大小限制", max_size)
            )
    elif requested_range is not None:
        if requested_range.start != 0 or requested_range.end is not None:
            raise GatewayUpstreamError("上游未响应 Range 请求")

    if start > max_size:
        raise GatewaySizeExceeded(
            over_limit("Range 起点", start, "超过系统大小限制", max_size)
        )
    budget = max_size - start
    if requested_range is not None and requested_range.end is not None:
        budget = min(budget, requested_range.end - start + 1)
    if content_length is not None and content_length > budget:
        raise GatewaySizeExceeded(
            over_limit("下载内容", content_length, "超过系统大小限制", max_size)
        )
    return budget


@dataclass
class GatewayStream:
    session: aiohttp.ClientSession
    response: aiohttp.ClientResponse
    status_code: int
    headers: dict[str, str]
    budget: int
    stream_body: bool
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.response.close()
        await self.session.close()

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        forwarded = 0
        try:
            if not self.stream_body:
                return
            async for chunk in self.response.content.iter_chunked(64 * 1024):
                if not chunk:
                    continue
                remaining = self.budget - forwarded
                if len(chunk) > remaining:
                    if remaining > 0:
                        yield chunk[:remaining]
                    raise GatewaySizeExceeded(
                        over_limit(
                            "下载内容",
                            forwarded + len(chunk),
                            "超过当前生效上限",
                            self.budget,
                        )
                    )
                forwarded += len(chunk)
                yield chunk
        except GatewaySizeExceeded:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GatewayUpstreamError("上游下载中断") from None
        finally:
            await self.close()


async def open_gateway_stream(
    *,
    source_uri: str,
    options: SourceRequestOptions,
    range_header: str | None,
    max_redirects: int = 10,
) -> GatewayStream:
    requested_range = parse_range_header(range_header)
    try:
        current_url = normalize_public_http_url(source_uri)
    except UnsafeTargetError as exc:
        raise GatewayTargetError(str(exc)) from None
    source_origin = _origin(current_url)
    include_source_headers = True
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=10,
        sock_connect=10,
        sock_read=60,
    )
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=create_public_connector(),
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
    )
    response: aiohttp.ClientResponse | None = None
    redirect_count = 0
    try:
        while True:
            headers = _upstream_headers(
                options,
                include_source_headers=include_source_headers,
                range_header=range_header,
            )
            try:
                response = await session.get(
                    current_url,
                    headers=headers,
                    allow_redirects=False,
                )
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                if _caused_by_unsafe_target(exc):
                    raise GatewayTargetError("上游域名解析到非公网地址") from None
                raise GatewayUpstreamError("无法连接上游下载地址") from None

            if response.status not in _REDIRECT_STATUSES:
                break
            location = response.headers.get("Location")
            response.close()
            response = None
            if not location:
                raise GatewayUpstreamError("上游重定向缺少 Location")
            if redirect_count >= max_redirects:
                raise GatewayUpstreamError("上游重定向次数超过限制")
            try:
                next_url = normalize_public_http_url(
                    urljoin(current_url, location)
                )
            except UnsafeTargetError as exc:
                raise GatewayTargetError(str(exc)) from None
            if _origin(next_url) != source_origin:
                include_source_headers = False
            current_url = next_url
            redirect_count += 1

        if response is None:
            raise GatewayUpstreamError("上游响应缺少状态")
        if response.status < 200:
            raise GatewayUpstreamError("上游返回不支持的协议切换响应")
        if 300 <= response.status < 400:
            raise GatewayUpstreamError("上游返回不支持的重定向响应")
        content_encoding = response.headers.get("Content-Encoding", "identity")
        if content_encoding.lower().strip() != "identity":
            raise GatewayUpstreamError("上游返回不支持的内容编码")
        stream_body = response.status in {200, 206}
        budget = _stream_budget(response, requested_range, get_max_task_size())
        response_headers = _response_headers(response)
        if not stream_body:
            response_headers.pop("Content-Length", None)
        return GatewayStream(
            session=session,
            response=response,
            status_code=response.status,
            headers=response_headers,
            budget=budget,
            stream_body=stream_body,
        )
    except Exception:
        if response is not None:
            response.close()
        await session.close()
        raise
