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
from email.utils import decode_rfc2231
from urllib.parse import unquote, urlparse

import aiohttp

logger = logging.getLogger(__name__)

# Default timeout for HEAD requests (seconds)
DEFAULT_TIMEOUT = 30

# Maximum number of redirects to follow
MAX_REDIRECTS = 10


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
            except Exception:
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
    except Exception:
        pass
    return None


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
            # Use HEAD request with redirect following
            async with session.head(
                url,
                allow_redirects=True,
                max_redirects=max_redirects,
            ) as response:
                # Get final URL after redirects
                final_url = str(response.url)

                # Check for successful response
                if response.status >= 400:
                    return ProbeResult(
                        success=False,
                        final_url=final_url,
                        error=f"HTTP {response.status}: {response.reason}",
                    )

                # Extract Content-Length
                content_length = None
                if "Content-Length" in response.headers:
                    try:
                        content_length = int(response.headers["Content-Length"])
                    except ValueError:
                        pass

                # Extract filename
                filename = None
                if "Content-Disposition" in response.headers:
                    filename = _parse_content_disposition(
                        response.headers["Content-Disposition"]
                    )

                # Fallback to URL path
                if not filename:
                    filename = _extract_filename_from_url(final_url)

                # Get content type
                content_type = response.headers.get("Content-Type")

                return ProbeResult(
                    success=True,
                    final_url=final_url,
                    content_length=content_length,
                    filename=filename,
                    content_type=content_type,
                )

    except aiohttp.ClientError as e:
        logger.warning(f"HTTP probe failed for {url}: {e}")
        return ProbeResult(
            success=False,
            error=f"Connection error: {type(e).__name__}",
        )
    except TimeoutError:
        logger.warning(f"HTTP probe timeout for {url}")
        return ProbeResult(
            success=False,
            error="Request timeout",
        )
    except Exception as e:
        logger.warning(f"HTTP probe unexpected error for {url}: {e}")
        return ProbeResult(
            success=False,
            error=f"Unexpected error: {type(e).__name__}",
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

    # If HEAD succeeded or returned a clear error, return it
    if result.success or (result.error and "HTTP" in result.error):
        return result

    # Try GET as fallback (some servers don't support HEAD)
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(
                url,
                allow_redirects=True,
                max_redirects=max_redirects,
            ) as response:
                # Get final URL after redirects
                final_url = str(response.url)

                if response.status >= 400:
                    return ProbeResult(
                        success=False,
                        final_url=final_url,
                        error=f"HTTP {response.status}: {response.reason}",
                    )

                # Extract headers (same as HEAD)
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

                content_type = response.headers.get("Content-Type")

                return ProbeResult(
                    success=True,
                    final_url=final_url,
                    content_length=content_length,
                    filename=filename,
                    content_type=content_type,
                )

    except Exception as e:
        # Return original HEAD error if GET also fails
        logger.warning(f"GET fallback also failed for {url}: {e}")
        return result
