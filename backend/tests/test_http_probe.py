"""Tests for HTTP probe service."""
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch
import aiohttp

from app.services.http_probe import (
    _parse_content_disposition,
    _extract_filename_from_url,
    probe_http_url,
    probe_url_with_get_fallback,
    ProbeResult,
)


SENSITIVE_PROBE_URL = (
    "https://probe-user:probe-password@example.com/download?token=probe-token"
    "&signature=probe-signature#probe-fragment"
)


def assert_probe_log_redacted(caplog):
    assert "https://example.com/download" in caplog.text
    for secret in (
        "probe-user",
        "probe-password",
        "probe-token",
        "probe-signature",
        "probe-fragment",
    ):
        assert secret not in caplog.text


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
    async def test_connection_error(self, caplog):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(side_effect=aiohttp.ClientError(SENSITIVE_PROBE_URL))

        with patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ), patch("aiohttp.ClientSession", return_value=mock_session), caplog.at_level(
            logging.WARNING, logger="app.services.http_probe"
        ):
            result = await probe_http_url(SENSITIVE_PROBE_URL)

        assert result.success is False
        assert "Connection error" in result.error
        assert_probe_log_redacted(caplog)

    @pytest.mark.asyncio
    async def test_timeout_error(self, caplog):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(side_effect=TimeoutError())

        with patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ), patch("aiohttp.ClientSession", return_value=mock_session), caplog.at_level(
            logging.WARNING, logger="app.services.http_probe"
        ):
            result = await probe_http_url(SENSITIVE_PROBE_URL)

        assert result.success is False
        assert "timeout" in result.error.lower()
        assert_probe_log_redacted(caplog)

    @pytest.mark.asyncio
    async def test_unexpected_error(self, caplog):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(side_effect=RuntimeError(SENSITIVE_PROBE_URL))

        with patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ), patch("aiohttp.ClientSession", return_value=mock_session), caplog.at_level(
            logging.WARNING, logger="app.services.http_probe"
        ):
            result = await probe_http_url(SENSITIVE_PROBE_URL)

        assert result.success is False
        assert result.error == "Unexpected error: RuntimeError"
        assert_probe_log_redacted(caplog)

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

    @pytest.mark.asyncio
    async def test_private_redirect_target_is_never_requested(self):
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "http://100.64.0.8/secret"}
        redirect_context = AsyncMock(
            __aenter__=AsyncMock(return_value=redirect),
            __aexit__=AsyncMock(return_value=None),
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=redirect_context)

        dns_result = [(None, None, None, None, ("8.8.8.8", 443))]
        with patch("aiohttp.ClientSession", return_value=mock_session), patch(
            "app.core.security.socket.getaddrinfo",
            return_value=dns_result,
        ):
            result = await probe_http_url("https://example.com/start")

        assert result.success is False
        assert result.error == "重定向目标不安全: 不允许下载内网地址"
        assert mock_session.head.call_args_list == [
            call("https://example.com/start", allow_redirects=False)
        ]

    @pytest.mark.asyncio
    async def test_follows_relative_redirect_after_validation(self):
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "/files/final.zip"}
        success = AsyncMock()
        success.status = 200
        success.headers = {"Content-Length": "12"}
        contexts = [
            AsyncMock(
                __aenter__=AsyncMock(return_value=response),
                __aexit__=AsyncMock(return_value=None),
            )
            for response in (redirect, success)
        ]
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(side_effect=contexts)

        with patch("aiohttp.ClientSession", return_value=mock_session), patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await probe_http_url("https://example.com/start")

        assert result.success is True
        assert result.final_url == "https://example.com/files/final.zip"
        assert mock_session.head.call_args_list == [
            call("https://example.com/start", allow_redirects=False),
            call("https://example.com/files/final.zip", allow_redirects=False),
        ]

    @pytest.mark.asyncio
    async def test_stops_at_redirect_limit(self):
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "/next"}
        redirect_context = AsyncMock(
            __aenter__=AsyncMock(return_value=redirect),
            __aexit__=AsyncMock(return_value=None),
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.head = MagicMock(return_value=redirect_context)

        with patch("aiohttp.ClientSession", return_value=mock_session), patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await probe_http_url(
                "https://example.com/start",
                max_redirects=0,
            )

        assert result.success is False
        assert result.error == "重定向次数过多"
        mock_session.head.assert_called_once_with(
            "https://example.com/start",
            allow_redirects=False,
        )


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
    async def test_head_method_not_allowed_falls_back_to_get(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Length": "10"}
        mock_context = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=mock_context)

        with patch("app.services.http_probe.probe_http_url") as mock_probe, patch(
            "aiohttp.ClientSession",
            return_value=mock_session,
        ), patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ):
            mock_probe.return_value = ProbeResult(
                success=False,
                error="HTTP 405: Method Not Allowed",
            )
            result = await probe_url_with_get_fallback(
                "https://example.com/file.zip"
            )

        assert result.success is True
        mock_session.get.assert_called_once_with(
            "https://example.com/file.zip",
            allow_redirects=False,
        )

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
    async def test_get_fallback_never_requests_private_redirect(self):
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "http://127.0.0.1/private"}
        redirect_context = AsyncMock(
            __aenter__=AsyncMock(return_value=redirect),
            __aexit__=AsyncMock(return_value=None),
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=redirect_context)

        with patch("app.services.http_probe.probe_http_url") as mock_probe, patch(
            "aiohttp.ClientSession",
            return_value=mock_session,
        ), patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            side_effect=[None, "不允许下载本机地址"],
        ):
            mock_probe.return_value = ProbeResult(
                success=False,
                error="Connection error: ClientError",
            )
            result = await probe_url_with_get_fallback(
                "https://example.com/file.zip"
            )

        assert result.success is False
        assert result.error == "重定向目标不安全: 不允许下载本机地址"
        mock_session.get.assert_called_once_with(
            "https://example.com/file.zip",
            allow_redirects=False,
        )

    @pytest.mark.asyncio
    async def test_both_fail(self, caplog):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(side_effect=RuntimeError(SENSITIVE_PROBE_URL))

        with patch("app.services.http_probe.probe_http_url") as mock_probe, patch(
            "app.services.http_probe.check_url_ssrf",
            new_callable=AsyncMock,
            return_value=None,
        ), patch("aiohttp.ClientSession", return_value=mock_session), caplog.at_level(
            logging.WARNING, logger="app.services.http_probe"
        ):
            mock_probe.return_value = ProbeResult(
                success=False,
                error="Connection error: ClientError",
            )
            result = await probe_url_with_get_fallback(SENSITIVE_PROBE_URL)

        assert result.success is False
        assert "Connection error" in result.error
        assert_probe_log_redacted(caplog)


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
