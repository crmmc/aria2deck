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
from app.core.security import (
    HTTP_URI_SCHEMES,
    check_url_ssrf,
    redact_url_for_log,
)

logger = logging.getLogger(__name__)

# Default timeout for HEAD requests (seconds)
DEFAULT_TIMEOUT = 30

# Maximum number of redirects to follow
MAX_REDIRECTS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


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


def _probe_result_from_response(
    response: aiohttp.ClientResponse,
    final_url: str,
) -> ProbeResult:
    if response.status >= 400:
        return ProbeResult(
            success=False,
            final_url=final_url,
            error=f"HTTP {response.status}: {response.reason}",
        )

    content_length = None
    if "Content-Length" in response.headers:
        try:
            content_length = int(response.headers["Content-Length"])
        except ValueError:
            pass

    filename = None
    if "Content-Disposition" in response.headers:
        filename = _parse_content_disposition(
            response.headers["Content-Disposition"]
        )
    if not filename:
        filename = _extract_filename_from_url(final_url)

    return ProbeResult(
        success=True,
        final_url=final_url,
        content_length=content_length,
        filename=filename,
        content_type=response.headers.get("Content-Type"),
    )


async def _probe_with_method(
    session: aiohttp.ClientSession,
    url: str,
    method: str,
    max_redirects: int,
) -> ProbeResult:
    current_url = url
    redirect_count = 0

    while True:
        ssrf_error = await check_url_ssrf(
            current_url,
            allowed_schemes=HTTP_URI_SCHEMES,
        )
        if ssrf_error:
            prefix = "重定向目标不安全: " if redirect_count else ""
            return ProbeResult(
                success=False,
                final_url=current_url,
                error=f"{prefix}{ssrf_error}",
            )

        request = (
            session.head(current_url, allow_redirects=False)
            if method == "HEAD"
            else session.get(current_url, allow_redirects=False)
        )
        async with request as response:
            if response.status not in REDIRECT_STATUSES:
                return _probe_result_from_response(response, current_url)

            location = response.headers.get("Location")
            if not location:
                return ProbeResult(
                    success=False,
                    final_url=current_url,
                    error="重定向响应缺少 Location",
                )
            if redirect_count >= max_redirects:
                return ProbeResult(
                    success=False,
                    final_url=current_url,
                    error="重定向次数过多",
                )

            current_url = urljoin(current_url, location)
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
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            return await _probe_with_method(session, url, "HEAD", max_redirects)

    except aiohttp.ClientError as exc:
        logger.warning(
            "HTTP probe failed url=%s error_type=%s",
            redact_url_for_log(url),
            type(exc).__name__,
        )
        return ProbeResult(
            success=False,
            error=f"Connection error: {type(exc).__name__}",
        )
    except TimeoutError:
        logger.warning("HTTP probe timeout url=%s", redact_url_for_log(url))
        return ProbeResult(
            success=False,
            error="Request timeout",
        )
    except Exception as exc:
        logger.warning(
            "HTTP probe unexpected error url=%s error_type=%s",
            redact_url_for_log(url),
            type(exc).__name__,
        )
        return ProbeResult(
            success=False,
            error=f"Unexpected error: {type(exc).__name__}",
        )


async def probe_url_with_get_fallback(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
) -> ProbeResult:
    """Probe URL with GET fallback if HEAD fails.

    Some servers don't support HEAD requests properly.
    Falls back to GET with immediate close if HEAD fails.

    Args:
        url: The URL to probe
        timeout: Request timeout in seconds
        max_redirects: Maximum redirects to follow

    Returns:
        ProbeResult with metadata or error information
    """
    # Try HEAD first
    result = await probe_http_url(url, timeout, max_redirects)

    fallback_errors = (
        "Connection error:",
        "Request timeout",
        "Unexpected error:",
        "HTTP 405:",
        "HTTP 501:",
    )
    if result.success or not (result.error or "").startswith(fallback_errors):
        return result

    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            return await _probe_with_method(session, url, "GET", max_redirects)

    except Exception as exc:
        # Return original HEAD error if GET also fails
        logger.warning(
            "GET fallback failed url=%s error_type=%s",
            redact_url_for_log(url),
            type(exc).__name__,
        )
        return result
