"""HTTP probe service for pre-checking download URLs.

Performs HEAD requests before creating HTTP(S) download tasks to:
1. Get file size (Content-Length)
2. Get file name (Content-Disposition)
3. Follow redirects to get final URL
4. Validate URL accessibility
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from app.core.security import redact_url_for_log
from app.http.safe_client import (
    UnsafeTargetError,
    create_public_connector,
    normalize_public_http_url,
)

logger = logging.getLogger(__name__)

# Default timeout for HEAD requests (seconds)
DEFAULT_TIMEOUT = 30

# Maximum number of redirects to follow
MAX_REDIRECTS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
GET_FALLBACK_ERROR_PREFIXES = (
    "Connection error:",
    "Request timeout",
    "Unexpected error:",
    "HTTP 405:",
    "HTTP 501:",
)


@dataclass
class ProbeResult:
    """Result of HTTP probe."""
    success: bool
    final_url: str | None = None
    content_length: int | None = None
    filename: str | None = None
    content_type: str | None = None
    error: str | None = None


def _parse_content_disposition(header: str) -> str | None:
    """Parse filename from Content-Disposition header.

    Supports:
    - filename="name.ext"
    - filename*=UTF-8''encoded%20name.ext (RFC 5987)
    - filename=name.ext (unquoted)

    Args:
        header: Content-Disposition header value

    Returns:
        Extracted filename or None
    """
    if not header:
        return None

    # Try RFC 5987 encoded filename first (filename*=)
    match = re.search(r"filename\*\s*=\s*([^;]+)", header, re.IGNORECASE)
    if match:
        value = match.group(1).strip()
        # Format: charset'language'encoded_value
        parts = value.split("'", 2)
        if len(parts) == 3:
            charset, _lang, encoded = parts
            try:
                return unquote(encoded, encoding=charset or "utf-8")
            except (UnicodeDecodeError, LookupError) as e:
                logger.debug(f"Failed to decode filename with charset {charset}: {e}")
                pass

    # Try regular filename parameter
    match = re.search(r'filename\s*=\s*"([^"]+)"', header, re.IGNORECASE)
    if match:
        return match.group(1)

    # Try unquoted filename
    match = re.search(r"filename\s*=\s*([^;\s]+)", header, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return None


def _extract_filename_from_url(url: str) -> str | None:
    """Extract filename from URL path.

    Args:
        url: The URL to extract filename from

    Returns:
        Filename or None if not determinable
    """
    try:
        parsed = urlparse(url)
        path = parsed.path
        if path:
            # Get the last path segment
            segments = [s for s in path.split("/") if s]
            if segments:
                filename = unquote(segments[-1])
                # Only return if it looks like a filename (has extension)
                if "." in filename:
                    return filename
    except ValueError:
        pass
    return None


def _result_from_response(
    response: aiohttp.ClientResponse,
    final_url: str,
) -> ProbeResult:
    if 300 <= response.status < 400:
        return ProbeResult(
            success=False,
            final_url=final_url,
            error=f"HTTP {response.status}: 不支持的重定向响应",
        )
    if response.status >= 400:
        return ProbeResult(
            success=False,
            final_url=final_url,
            error=f"HTTP {response.status}: {response.reason}",
        )

    content_length = None
    raw_content_length = response.headers.get("Content-Length")
    if raw_content_length is not None:
        try:
            parsed_length = int(raw_content_length)
            if parsed_length >= 0:
                content_length = parsed_length
        except (TypeError, ValueError):
            pass

    filename = None
    content_disposition = response.headers.get("Content-Disposition")
    if content_disposition:
        filename = _parse_content_disposition(content_disposition)
    if not filename:
        filename = _extract_filename_from_url(final_url)

    return ProbeResult(
        success=True,
        final_url=final_url,
        content_length=content_length,
        filename=filename,
        content_type=response.headers.get("Content-Type"),
    )


async def _probe_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    max_redirects: int,
) -> ProbeResult:
    try:
        current_url = normalize_public_http_url(url)
    except UnsafeTargetError as exc:
        return ProbeResult(success=False, error=str(exc))
    redirect_count = 0

    while True:
        if method == "HEAD":
            response_context = session.head(current_url, allow_redirects=False)
        else:
            response_context = session.get(
                current_url,
                allow_redirects=False,
                read_until_eof=False,
            )
        async with response_context as response:
            response_url = str(response.url)
            if response.status not in REDIRECT_STATUSES:
                return _result_from_response(response, response_url)

            location = response.headers.get("Location")
            if not location:
                return ProbeResult(
                    success=False,
                    final_url=response_url,
                    error=f"HTTP {response.status}: 重定向响应缺少 Location",
                )
            if redirect_count >= max_redirects:
                return ProbeResult(
                    success=False,
                    final_url=response_url,
                    error="重定向次数超过限制",
                )

            try:
                next_url = normalize_public_http_url(urljoin(response_url, location))
            except UnsafeTargetError as exc:
                return ProbeResult(
                    success=False,
                    final_url=response_url,
                    error=f"重定向目标不安全: {exc}",
                )

            current_url = next_url
            redirect_count += 1


async def probe_http_url(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
) -> ProbeResult:
    """Probe an HTTP(S) URL to get metadata before downloading.

    Sends a HEAD request (with redirect following) to determine:
    - Final URL after redirects
    - File size from Content-Length
    - Filename from Content-Disposition or URL

    Args:
        url: The URL to probe
        timeout: Request timeout in seconds
        max_redirects: Maximum redirects to follow

    Returns:
        ProbeResult with metadata or error information
    """
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(
            timeout=client_timeout,
            connector=create_public_connector(),
        ) as session:
            return await _probe_request(session, "HEAD", url, max_redirects)

    except aiohttp.ClientError as e:
        safe_url = redact_url_for_log(url)
        logger.warning(
            "HTTP probe failed url=%s error=%s",
            safe_url,
            type(e).__name__,
        )
        return ProbeResult(
            success=False,
            error=f"Connection error: {type(e).__name__}",
        )
    except TimeoutError:
        safe_url = redact_url_for_log(url)
        logger.warning(f"HTTP probe timeout for {safe_url}")
        return ProbeResult(
            success=False,
            error="Request timeout",
        )
    except Exception as e:
        safe_url = redact_url_for_log(url)
        logger.warning(
            "HTTP probe unexpected error url=%s error=%s",
            safe_url,
            type(e).__name__,
        )
        return ProbeResult(
            success=False,
            error=f"Unexpected error: {type(e).__name__}",
        )


async def probe_url_with_get_fallback(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
) -> ProbeResult:
    """Validate the HEAD and GET redirect chains and return GET metadata.

    The GET response body is not consumed; the response context is closed as
    soon as the final headers are available.
    """
    # Try HEAD first
    result = await probe_http_url(url, timeout, max_redirects)

    if not result.success and (
        not result.error
        or not result.error.startswith(GET_FALLBACK_ERROR_PREFIXES)
    ):
        return result

    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(
            timeout=client_timeout,
            connector=create_public_connector(),
        ) as session:
            return await _probe_request(session, "GET", url, max_redirects)

    except Exception as e:
        safe_url = redact_url_for_log(url)
        logger.warning(
            "GET probe failed url=%s error=%s",
            safe_url,
            type(e).__name__,
        )
        return ProbeResult(
            success=False,
            error=f"GET 验证失败: {type(e).__name__}",
        )
