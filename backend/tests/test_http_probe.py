"""Tests for HTTP probe service."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp

from app.services.http_probe import (
    _parse_content_disposition,
    _extract_filename_from_url,
    probe_http_url,
    probe_url_with_get_fallback,
    ProbeResult,
)


class TestParseContentDisposition:

    def test_quoted_filename(self):
        header = 'attachment; filename="test file.zip"'
        result = _parse_content_disposition(header)
        assert result == "test file.zip"

    def test_unquoted_filename(self):
        header = "attachment; filename=testfile.zip"
        result = _parse_content_disposition(header)
        assert result == "testfile.zip"

    def test_rfc5987_encoded_filename(self):
        header = "attachment; filename*=UTF-8''test%20file.zip"
        result = _parse_content_disposition(header)
        assert result == "test file.zip"

    def test_rfc5987_with_language(self):
        header = "attachment; filename*=UTF-8'en'test%20file.zip"
        result = _parse_content_disposition(header)
        assert result == "test file.zip"

    def test_empty_header(self):
        result = _parse_content_disposition("")
        assert result is None

    def test_none_header(self):
        result = _parse_content_disposition(None)
        assert result is None

    def test_no_filename(self):
        header = "attachment"
        result = _parse_content_disposition(header)
        assert result is None

    def test_both_filename_and_filename_star(self):
        header = 'attachment; filename="fallback.zip"; filename*=UTF-8\'\'preferred.zip'
        result = _parse_content_disposition(header)
        assert result == "preferred.zip"


class TestExtractFilenameFromUrl:

    def test_url_with_filename(self):
        url = "https://example.com/path/to/file.zip"
        result = _extract_filename_from_url(url)
        assert result == "file.zip"

    def test_url_without_extension(self):
        url = "https://example.com/path/to/file"
        result = _extract_filename_from_url(url)
        assert result is None

    def test_url_with_encoded_filename(self):
        url = "https://example.com/path/to/test%20file.zip"
        result = _extract_filename_from_url(url)
        assert result == "test file.zip"

    def test_url_with_query_params(self):
        url = "https://example.com/file.zip?token=abc123"
        result = _extract_filename_from_url(url)
        assert result == "file.zip"

    def test_url_with_no_path(self):
        url = "https://example.com"
        result = _extract_filename_from_url(url)
        assert result is None

    def test_url_with_trailing_slash(self):
        url = "https://example.com/path/"
        result = _extract_filename_from_url(url)
        assert result is None


class TestProbeHttpUrl:

    @pytest.mark.asyncio
    async def test_successful_probe(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.url = "https://example.com/file.zip"
        mock_response.reason = "OK"
        mock_response.headers = {
            "Content-Length": "1024",
            "Content-Disposition": 'attachment; filename="test.zip"',
            "Content-Type": "application/zip"
        }

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None)
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/file.zip")

        assert result.success is True
        assert result.final_url == "https://example.com/file.zip"
        assert result.content_length == 1024
        assert result.filename == "test.zip"
        assert result.content_type == "application/zip"

    @pytest.mark.asyncio
    async def test_http_error(self):
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.url = "https://example.com/notfound.zip"
        mock_response.reason = "Not Found"
        mock_response.headers = {}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None)
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/notfound.zip")

        assert result.success is False
        assert "404" in result.error

    @pytest.mark.asyncio
    async def test_connection_error(self):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(side_effect=aiohttp.ClientError("Connection failed"))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/file.zip")

        assert result.success is False
        assert "Connection error" in result.error

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(side_effect=TimeoutError())

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/file.zip")

        assert result.success is False
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_content_length(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.url = "https://example.com/file.zip"
        mock_response.reason = "OK"
        mock_response.headers = {}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None)
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/file.zip")

        assert result.success is True
        assert result.content_length is None

    @pytest.mark.asyncio
    async def test_filename_from_url_fallback(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.url = "https://example.com/download/myfile.zip"
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Length": "1024"}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None)
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/download/myfile.zip")

        assert result.success is True
        assert result.filename == "myfile.zip"


class TestProbeUrlWithGetFallback:

    @pytest.mark.asyncio
    async def test_head_succeeds(self):
        with patch("app.services.http_probe.probe_http_url") as mock_probe:
            mock_probe.return_value = ProbeResult(
                success=True,
                final_url="https://example.com/file.zip",
                content_length=1024,
                filename="file.zip"
            )
            result = await probe_url_with_get_fallback("https://example.com/file.zip")

        assert result.success is True
        assert result.filename == "file.zip"

    @pytest.mark.asyncio
    async def test_head_fails_with_http_error(self):
        with patch("app.services.http_probe.probe_http_url") as mock_probe:
            mock_probe.return_value = ProbeResult(
                success=False,
                error="HTTP 405: Method Not Allowed"
            )
            result = await probe_url_with_get_fallback("https://example.com/file.zip")

        assert result.success is False
        assert "HTTP" in result.error

    @pytest.mark.asyncio
    async def test_head_fails_get_succeeds(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.url = "https://example.com/file.zip"
        mock_response.reason = "OK"
        mock_response.headers = {
            "Content-Length": "2048",
            "Content-Disposition": 'attachment; filename="fallback.zip"'
        }

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None)
        ))

        with patch("app.services.http_probe.probe_http_url") as mock_probe:
            mock_probe.return_value = ProbeResult(
                success=False,
                error="Connection error: ClientError"
            )
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await probe_url_with_get_fallback("https://example.com/file.zip")

        assert result.success is True
        assert result.content_length == 2048
        assert result.filename == "fallback.zip"

    @pytest.mark.asyncio
    async def test_both_fail(self):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(side_effect=Exception("GET also failed"))

        with patch("app.services.http_probe.probe_http_url") as mock_probe:
            mock_probe.return_value = ProbeResult(
                success=False,
                error="Connection error: ClientError"
            )
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await probe_url_with_get_fallback("https://example.com/file.zip")

        assert result.success is False
        assert "Connection error" in result.error


class TestFilenameExtractionEdgeCases:

    def test_extract_filename_from_url_exception(self):
        from app.services.http_probe import _extract_filename_from_url

        result = _extract_filename_from_url("not-a-valid-url")
        assert result is None

    def test_extract_filename_from_url_no_extension(self):
        from app.services.http_probe import _extract_filename_from_url

        result = _extract_filename_from_url("https://example.com/path/noextension")
        assert result is None


class TestProbeHttpUrlEdgeCases:

    @pytest.mark.asyncio
    async def test_probe_http_error_status(self):
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.reason = "Not Found"
        mock_response.url = "https://example.com/notfound.zip"
        mock_response.headers = {}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None)
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await probe_http_url("https://example.com/notfound.zip")

        assert result.success is False
        assert "404" in result.error
