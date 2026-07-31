"""Tests for v0 task router endpoints."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.db.engine import transaction
from app.core.config import get_internal_base_url
from app.db.schema import global_downloads
from app.domain.errors import BadRequestError
from app.domain.torrent_metadata import MAX_TORRENT_BASE64_LENGTH
from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import create_user_download
from app.services.hash import get_uri_hash
from app.services.http_probe import ProbeResult
from app.services.internal_fetch import (
    CAPABILITY_HEADER,
    http_resource_identity,
    source_request_options,
    verify_capability,
)
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


def _valid_torrent_payload() -> tuple[str, str]:
    info_dict = b"d4:name4:test6:lengthi1024e12:piece lengthi16384e6:pieces20:01234567890123456789e"
    torrent = b"d8:announce26:http://tracker.example.com4:info" + info_dict + b"e"
    return base64.b64encode(torrent).decode("ascii"), hashlib.sha1(
        info_dict
    ).hexdigest()


def _torrent_with_network_field(field: bytes, url: bytes) -> str:
    def bstr(value: bytes) -> bytes:
        return str(len(value)).encode("ascii") + b":" + value

    info = b"d4:name4:test6:lengthi1e12:piece lengthi16384e6:pieces20:01234567890123456789e"
    torrent = b"d" + bstr(field) + bstr(url) + b"4:info" + info + b"e"
    return base64.b64encode(torrent).decode("ascii")


def _multi_file_torrent_payload() -> tuple[str, str]:
    def bstr(value: bytes) -> bytes:
        return str(len(value)).encode("ascii") + b":" + value

    def bint(value: int) -> bytes:
        return b"i" + str(value).encode("ascii") + b"e"

    def bdict(items: list[tuple[bytes, bytes]]) -> bytes:
        return b"d" + b"".join(bstr(key) + value for key, value in items) + b"e"

    def blist(values: list[bytes]) -> bytes:
        return b"l" + b"".join(values) + b"e"

    info = bdict(
        [
            (b"name", bstr(b"Fedora Workstation")),
            (
                b"files",
                blist(
                    [
                        bdict([(b"length", bint(4096)), (b"path", blist([bstr(b"iso.bin")]))]),
                        bdict([(b"length", bint(48)), (b"path", blist([bstr(b"docs"), bstr(b"release.pdf")]))]),
                        bdict([(b"length", bint(90)), (b"path", blist([bstr(b"docs"), bstr(b"install.pdf")]))]),
                    ]
                ),
            ),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"1" * 20)),
        ]
    )
    torrent = bdict([(b"announce", bstr(b"http://tracker.example.com")), (b"info", info)])
    return base64.b64encode(torrent).decode("ascii"), hashlib.sha1(info).hexdigest()


def _public_dns_result() -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 80))]


def _response_context(response: MagicMock) -> AsyncMock:
    return AsyncMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=None),
    )


async def _set_global_error_message(download_id: int, error_message: str) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download_id)
            .values(error_message=error_message)
        )


class TestSSRFProtection:
    def test_is_private_ip_loopback(self) -> None:
        import ipaddress

        from app.core.security import is_private_ip

        assert is_private_ip(ipaddress.ip_address("127.0.0.1")) is True
        assert is_private_ip(ipaddress.ip_address("::1")) is True

    def test_is_private_ip_private_ranges(self) -> None:
        import ipaddress

        from app.core.security import is_private_ip

        assert is_private_ip(ipaddress.ip_address("10.0.0.1")) is True
        assert is_private_ip(ipaddress.ip_address("172.16.0.1")) is True
        assert is_private_ip(ipaddress.ip_address("192.168.1.1")) is True
        assert is_private_ip(ipaddress.ip_address("100.64.0.1")) is True

    def test_is_private_ip_public(self) -> None:
        import ipaddress

        from app.core.security import is_private_ip

        assert is_private_ip(ipaddress.ip_address("8.8.8.8")) is False
        assert is_private_ip(ipaddress.ip_address("1.1.1.1")) is False

    @pytest.mark.asyncio
    async def test_check_url_safety_localhost(self) -> None:
        from app.services.task_service import check_url_safety

        with pytest.raises(BadRequestError) as exc_info:
            await check_url_safety("http://localhost/file.zip")
        assert "本机地址" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_check_url_safety_private_ip(self) -> None:
        from app.services.task_service import check_url_safety

        with pytest.raises(BadRequestError) as exc_info:
            await check_url_safety("http://192.168.1.1/file.zip")
        assert "内网地址" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_check_url_safety_public_url(self) -> None:
        from app.services.task_service import check_url_safety

        with patch("app.core.security.socket.getaddrinfo", return_value=_public_dns_result()):
            await check_url_safety("http://example.com/file.zip")
            await check_url_safety("https://github.com/file.zip")

    @pytest.mark.asyncio
    async def test_check_url_safety_magnet(self) -> None:
        from app.services.task_service import check_url_safety

        await check_url_safety("magnet:?xt=urn:btih:abc123")

    @pytest.mark.asyncio
    async def test_check_url_safety_no_hostname(self) -> None:
        from app.services.task_service import check_url_safety

        with pytest.raises(BadRequestError):
            await check_url_safety("http:///file.zip")

    @pytest.mark.asyncio
    async def test_check_url_safety_dns_resolves_to_private(self) -> None:
        import socket

        from app.services.task_service import check_url_safety

        mock_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.100", 80))
        ]

        with patch("app.core.security.socket.getaddrinfo", return_value=mock_result):
            with pytest.raises(BadRequestError) as exc_info:
                await check_url_safety("http://evil.example.com/file.zip")
        assert "内网地址" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_check_url_safety_dns_failure_rejected(self) -> None:
        import socket

        from app.services.task_service import check_url_safety

        with patch(
            "app.core.security.socket.getaddrinfo",
            side_effect=socket.gaierror("DNS failed"),
        ):
            with pytest.raises(BadRequestError) as exc_info:
                await check_url_safety("http://nonexistent.example.com/file.zip")
        assert "无法解析" in exc_info.value.detail


class TestHelperFunctions:
    def test_check_disk_space(self) -> None:
        from app.services.task_service import check_disk_space

        ok, free = check_disk_space()
        assert isinstance(ok, bool)
        assert isinstance(free, int)
        assert free > 0


class TestCreateTask:
    @pytest.fixture(autouse=True)
    def public_dns(self):
        with patch("app.core.security.socket.getaddrinfo", return_value=_public_dns_result()):
            yield

    def test_create_task_unauthorized(self, client: TestClient) -> None:
        response = client.post(
            "/api/tasks", json={"uri": "http://example.com/file.zip"}
        )

        assert response.status_code == 401

    def test_create_task_ssrf_blocked(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://localhost/file.zip"},
        )

        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    def test_create_task_invalid_magnet(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "magnet:?invalid"},
        )

        assert response.status_code == 400
        assert "无效的磁力链接" in response.json()["detail"]

    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_task_canonicalizes_magnet_before_submit(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_get_client.return_value = mock_aria2_client
        info_hash = "0123456789ABCDEF0123456789ABCDEF01234567"
        normalized_hash = info_hash.lower()
        canonical_uri = f"magnet:?xt=urn:btih:{normalized_hash}"
        markers = (
            "rest-tracker-secret.example",
            "rest-webseed-secret.example",
            "rest-acceptable-secret.example",
            "rest-source-secret.example",
        )
        magnet_uri = (
            f"magnet:?xt=urn:btih:{info_hash}&tr=https://{markers[0]}/announce"
            f"&ws=https://{markers[1]}/payload&as=https://{markers[2]}/payload"
            f"&xs=https://{markers[3]}/metadata&dn=unsafe-name"
        )

        response = authenticated_client.post("/api/tasks", json={"uri": magnet_uri})

        assert response.status_code == 201
        assert response.json()["uri"] == canonical_uri
        submitted_uris = mock_aria2_client.add_uri.await_args.args[0]
        stored = asyncio.run(get_global_by_resource_key(normalized_hash))
        assert submitted_uris == [canonical_uri]
        assert stored is not None and stored["source_uri"] == canonical_uri
        assert all(
            marker not in repr(mock_aria2_client.add_uri.await_args)
            for marker in markers
        )

    @patch("app.services.task_service.probe_url_with_get_fallback")
    def test_create_task_probe_failure(
        self,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error = "Connection refused"
        mock_probe.return_value = mock_result

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"},
        )

        assert response.status_code == 400
        assert "无法访问下载链接" in response.json()["detail"]

    def test_create_task_rejects_private_get_redirect_before_transport(
        self,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        original_url = "http://example.com/download"
        private_url = "http://127.0.0.1/admin"
        redirect = MagicMock(
            status=302,
            url=original_url,
            reason="Found",
            headers={"Location": private_url},
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(
            return_value=_response_context(redirect)
        )

        with patch(
            "app.services.http_probe.probe_http_url",
            new=AsyncMock(
                return_value=ProbeResult(
                    success=True,
                    final_url=original_url,
                    content_length=1024,
                    filename="download.bin",
                )
            ),
        ), patch("aiohttp.ClientSession", return_value=mock_session), patch(
            "app.services.task_service._get_client",
            return_value=mock_aria2_client,
        ):
            response = authenticated_client.post(
                "/api/tasks",
                json={"uri": original_url},
            )

        assert response.status_code == 400
        assert "重定向目标不安全" in response.json()["detail"]
        mock_session.get.assert_called_once_with(
            original_url,
            allow_redirects=False,
            read_until_eof=False,
        )
        mock_aria2_client.add_uri.assert_not_awaited()

    @patch("app.services.task_service.create_user_download")
    @patch("app.services.task_service.probe_url_with_get_fallback")
    def test_create_task_admits_unknown_content_length_as_unknown_size(
        self,
        mock_probe: AsyncMock,
        mock_create_download: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/stream"
        mock_result.filename = "stream.bin"
        mock_result.content_length = None
        mock_probe.return_value = mock_result
        mock_create_download.return_value = {
            "id": 1,
            "global_download_id": 999,
            "status": "active",
            "display_name": "stream.bin",
        }

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/stream"},
        )

        assert response.status_code == 201
        assert response.json()["total_length"] == 0
        assert mock_create_download.await_args.kwargs["size_known"] is False

    @patch("app.services.task_service.probe_url_with_get_fallback")
    def test_create_task_rejects_credentialed_url(
        self,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://user:secret@example.com/file.zip"},
        )

        assert response.status_code == 400
        assert "用户名或密码" in response.json()["detail"]
        mock_probe.assert_not_called()

    @patch("app.services.task_service.check_disk_space")
    def test_create_task_disk_full(
        self,
        mock_disk: MagicMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_disk.return_value = (False, 100 * 1024 * 1024)

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"},
        )

        assert response.status_code == 403
        assert "磁盘空间不足" in response.json()["detail"]

    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service.get_max_task_size")
    def test_create_task_exceeds_max_size(
        self,
        mock_max_size: MagicMock,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/file.zip"
        mock_result.filename = "file.zip"
        mock_result.content_length = 100 * 1024 * 1024 * 1024
        mock_probe.return_value = mock_result
        mock_max_size.return_value = 10 * 1024 * 1024 * 1024

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"},
        )

        assert response.status_code == 403
        assert "超过系统限制" in response.json()["detail"]

    @patch("app.services.task_service.create_user_download")
    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service.get_usage")
    @patch("app.services.task_service.get_max_task_size")
    def test_create_task_exceeds_user_quota(
        self,
        mock_max_size: MagicMock,
        mock_usage: AsyncMock,
        mock_probe: AsyncMock,
        mock_create_download: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/file.zip"
        mock_result.filename = "file.zip"
        mock_result.content_length = 50 * 1024 * 1024 * 1024
        mock_probe.return_value = mock_result
        mock_max_size.return_value = 100 * 1024 * 1024 * 1024
        mock_usage.return_value = {
            "used_bytes": 0,
            "reserved_bytes": 0,
            "available_bytes": 10 * 1024 * 1024 * 1024,
            "quota_bytes": 100 * 1024 * 1024 * 1024,
        }

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"},
        )

        assert response.status_code == 403
        assert "超过可用空间" in response.json()["detail"]
        mock_create_download.assert_not_awaited()

    @patch("app.services.task_service.create_user_download")
    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_task_service_errors_are_mapped(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        mock_probe: AsyncMock,
        mock_create_download: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_get_client.return_value = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/error.zip"
        mock_result.filename = "error.zip"
        mock_result.content_length = 1024
        mock_probe.return_value = mock_result

        for exc, expected_status in [
            (ValueError("quota exceeded"), 403),
            (ValueError("bad input"), 400),
            (LookupError("stale"), 409),
            (RuntimeError("aria2 unavailable"), 502),
            (OSError("network down"), 502),
        ]:
            mock_create_download.reset_mock(side_effect=True)
            mock_create_download.side_effect = exc
            response = authenticated_client.post(
                "/api/tasks",
                json={"uri": "http://example.com/error.zip"},
            )
            assert response.status_code == expected_status

    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_task_rejects_path_like_out_option(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_get_client.return_value = mock_aria2_client
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/bad-out.zip"
        mock_result.filename = "bad-out.zip"
        mock_result.content_length = 1024
        mock_probe.return_value = mock_result

        response = authenticated_client.post(
            "/api/tasks",
            json={
                "uri": "http://example.com/bad-out.zip",
                "options": {"out": "../evil"},
            },
        )

        assert response.status_code == 400
        assert "invalid out option" in response.json()["detail"]
        mock_aria2_client.add_uri.assert_not_awaited()

    def test_create_task_submits_get_verified_final_url(
        self,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
        test_user: dict,
    ) -> None:
        original_url = "http://example.com/download"
        final_url = "http://example.com/files/final.zip"
        redirect = MagicMock(
            status=302,
            url=original_url,
            reason="Found",
            headers={"Location": "/files/final.zip"},
        )
        final = MagicMock(
            status=200,
            url=final_url,
            reason="OK",
            headers={
                "Content-Length": "1024",
                "Content-Disposition": 'attachment; filename="final.zip"',
            },
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(
            side_effect=[_response_context(redirect), _response_context(final)]
        )

        with patch(
            "app.services.http_probe.probe_http_url",
            new=AsyncMock(
                return_value=ProbeResult(
                    success=True,
                    final_url=original_url,
                    content_length=1024,
                )
            ),
        ), patch("aiohttp.ClientSession", return_value=mock_session), patch(
            "app.services.task_service._get_client",
            return_value=mock_aria2_client,
        ):
            response = authenticated_client.post(
                "/api/tasks",
                json={"uri": original_url},
            )

        assert response.status_code == 201
        assert response.json()["uri"] == final_url
        assert [call.args[0] for call in mock_session.get.call_args_list] == [
            original_url,
            final_url,
        ]
        global_download = asyncio.run(
            get_global_by_resource_key(get_uri_hash(final_url))
        )
        assert global_download is not None
        assert global_download["source_uri"] == final_url
        task = asyncio.run(get_user_task(test_user["id"], global_download["id"]))
        assert task is not None
        mock_aria2_client.add_uri.assert_awaited_once()
        uris, options = mock_aria2_client.add_uri.await_args.args
        assert uris == [
            f"{get_internal_base_url()}/_internal/fetch/{global_download['id']}/0"
        ]
        assert "max-http-redirections" not in options
        capability_header = options["header"]
        assert isinstance(capability_header, list)
        header_name, capability = capability_header[0].split(": ", 1)
        assert header_name == CAPABILITY_HEADER
        verified = verify_capability(
            capability,
            int(global_download["id"]),
            final_url,
        )
        assert verified.headers == ()

    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_task_submits_validated_final_url(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
        test_user: dict,
    ) -> None:
        initial_url = "http://example.com/start"
        final_url = "http://cdn.example.com/file.zip"
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_get_client.return_value = mock_aria2_client
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = final_url
        mock_result.filename = "file.zip"
        mock_result.content_length = 100 * 1024 * 1024
        mock_probe.return_value = mock_result

        response = authenticated_client.post(
            "/api/tasks",
            json={
                "uri": initial_url,
                "options": {
                    "out": "user-selected.bin",
                    "header": "X-Upstream-Key: source-secret",
                    "http-user": "alice",
                    "http-passwd": "password",
                    "max-connection-per-server": "9",
                },
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["uri"] == final_url
        assert data["status"] == "active"
        assert data["name"] == "file.zip"
        assert data["total_length"] == 100 * 1024 * 1024
        assert data["frozen_space"] == 100 * 1024 * 1024

        base_resource_key = get_uri_hash(final_url)
        assert base_resource_key is not None
        resource_key = http_resource_identity(
            base_resource_key,
            source_request_options(
                {
                    "header": "X-Upstream-Key: source-secret",
                    "http-user": "alice",
                    "http-passwd": "password",
                }
            ),
        )
        global_download = asyncio.run(get_global_by_resource_key(resource_key))
        assert global_download is not None
        assert "source-secret" not in global_download["resource_key"]
        assert "alice" not in global_download["resource_key"]
        assert global_download["source_uri"] == "http://cdn.example.com/file.zip"
        task = asyncio.run(get_user_task(test_user["id"], global_download["id"]))
        assert task is not None
        assert data["id"] == task["id"]
        mock_aria2_client.add_uri.assert_awaited_once()
        uris, opts = mock_aria2_client.add_uri.await_args.args
        assert uris == [
            f"{get_internal_base_url()}/_internal/fetch/{global_download['id']}/0"
        ]
        assert final_url not in uris
        assert opts["dir"].endswith(f"/downloading/{global_download['id']}")
        assert opts["seed-time"] == "0"
        assert opts["out"] == "payload"
        assert opts["split"] == "1"
        assert opts["max-connection-per-server"] == "1"
        assert "max-http-redirections" not in opts
        assert "http-user" not in opts
        assert "http-passwd" not in opts
        header_name, capability = opts["header"][0].split(": ", 1)
        assert header_name == CAPABILITY_HEADER
        verified = verify_capability(
            capability,
            int(global_download["id"]),
            str(global_download["source_uri"]),
        )
        assert verified.headers == (("x-upstream-key", "source-secret"),)
        assert verified.username == "alice"
        assert verified.password == "password"

    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_gateway_capability_is_not_persisted_or_logged_on_submit_failure(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
        test_user: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024**3)
        mock_get_client.return_value = mock_aria2_client
        mock_probe.return_value = ProbeResult(
            success=True,
            final_url="https://cdn.example.com/protected.bin",
            content_length=8,
            filename="protected.bin",
        )
        captured: list[str] = []

        async def reject_submission(_uris, options):
            capability = options["header"][0].split(": ", 1)[1]
            captured.append(capability)
            raise RuntimeError(capability)

        mock_aria2_client.add_uri.side_effect = reject_submission
        with caplog.at_level(logging.WARNING):
            response = authenticated_client.post(
                "/api/tasks",
                json={
                    "uri": "https://example.com/protected.bin",
                    "options": {"header": "X-Api-Key: source-secret"},
                },
            )

        assert response.status_code == 502
        assert len(captured) == 1
        capability = captured[0]
        assert capability not in response.text
        assert capability not in caplog.text
        base_key = get_uri_hash("https://cdn.example.com/protected.bin")
        assert base_key is not None
        resource_key = http_resource_identity(
            base_key,
            source_request_options({"header": "X-Api-Key: source-secret"}),
        )
        global_download = asyncio.run(get_global_by_resource_key(resource_key))
        assert global_download is not None
        task = asyncio.run(get_user_task(test_user["id"], global_download["id"]))
        assert task is not None
        assert task["error_message"] == "内部下载任务提交失败"
        assert capability not in repr(global_download)
        assert capability not in repr(task)


class TestTorrentPreview:
    @pytest.fixture(autouse=True)
    def public_dns(self):
        with patch(
            "app.core.security.socket.getaddrinfo",
            return_value=_public_dns_result(),
        ):
            yield

    def test_torrent_preview_unauthorized(self, client: TestClient) -> None:
        response = client.post("/api/tasks/torrent/preview", json={"torrent": "abc123"})

        assert response.status_code == 401

    def test_torrent_preview_invalid_base64(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post(
            "/api/tasks/torrent/preview",
            json={"torrent": "not-valid-base64!!!"},
        )

        assert response.status_code == 400
        assert "无效的种子文件" in response.json()["detail"]

    def test_torrent_preview_returns_metadata_without_creating_records(
        self,
        authenticated_client: TestClient,
    ) -> None:
        torrent_data, info_hash = _multi_file_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent/preview",
            json={"torrent": torrent_data},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["info_hash"] == info_hash
        assert data["name"] == "Fedora Workstation"
        assert data["file_count"] == 3
        assert data["total_size"] == 4234
        assert data["default_selection"] == "all"
        assert data["limits"]["max_files"] == 5000
        assert data["files"][1]["index"] == 2
        assert data["files"][1]["path"] == ["Fedora Workstation", "docs", "release.pdf"]
        assert data["tree"][0]["type"] == "directory"

        global_download = asyncio.run(get_global_by_resource_key(info_hash))
        assert global_download is None


class TestCreateTorrentTask:
    @pytest.fixture(autouse=True)
    def public_dns(self):
        with patch(
            "app.core.security.socket.getaddrinfo",
            return_value=_public_dns_result(),
        ):
            yield

    def test_create_torrent_unauthorized(self, client: TestClient) -> None:
        response = client.post("/api/tasks/torrent", json={"torrent": "abc123"})

        assert response.status_code == 401

    @pytest.mark.parametrize("field", [b"announce", b"url-list"])
    def test_create_torrent_rejects_private_embedded_endpoint(
        self,
        field: bytes,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        torrent = _torrent_with_network_field(
            field,
            b"http://100.64.0.4/private",
        )

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent},
        )

        assert response.status_code == 400
        expected = "内网地址" if field == b"announce" else "webseeds"
        assert expected in response.json()["detail"]
        mock_aria2_client.add_torrent.assert_not_awaited()

    def test_create_torrent_invalid_base64(
        self,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": "not-valid-base64!!!"},
        )

        assert response.status_code == 400
        assert "无效的种子文件" in response.json()["detail"]

    def test_create_torrent_too_large(self, authenticated_client: TestClient) -> None:
        large_torrent = "x" * (MAX_TORRENT_BASE64_LENGTH + 1)

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": large_torrent},
        )

        assert response.status_code == 413
        assert "种子文件过大" in response.json()["detail"]

    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_disk_full(
        self,
        mock_disk: MagicMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_disk.return_value = (False, 100 * 1024 * 1024)
        torrent_data, _ = _valid_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent_data},
        )

        assert response.status_code == 403
        assert "磁盘空间不足" in response.json()["detail"]

    @patch("app.services.task_service.get_usage")
    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_insufficient_space(
        self,
        mock_disk: MagicMock,
        mock_usage: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_usage.return_value = {
            "used_bytes": 0,
            "reserved_bytes": 0,
            "available_bytes": 100,
            "quota_bytes": 100 * 1024 * 1024 * 1024,
        }
        torrent_data, _ = _valid_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent_data},
        )

        assert response.status_code == 403
        assert "超过可用空间" in response.json()["detail"]

    @patch("app.services.task_service.create_user_torrent_download")
    @patch("app.services.task_service.get_usage")
    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_service_errors_are_mapped(
        self,
        mock_disk: MagicMock,
        mock_usage: AsyncMock,
        mock_create_torrent: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_usage.return_value = {
            "used_bytes": 0,
            "reserved_bytes": 0,
            "available_bytes": 100 * 1024 * 1024 * 1024,
            "quota_bytes": 100 * 1024 * 1024 * 1024,
        }
        torrent_data, _ = _valid_torrent_payload()

        for exc, expected_status in [
            (ValueError("quota exceeded"), 403),
            (ValueError("bad input"), 400),
            (LookupError("stale"), 409),
            (RuntimeError("aria2 unavailable"), 502),
            (OSError("network down"), 502),
        ]:
            mock_create_torrent.reset_mock(side_effect=True)
            mock_create_torrent.side_effect = exc
            response = authenticated_client.post(
                "/api/tasks/torrent",
                json={"torrent": torrent_data},
            )
            assert response.status_code == expected_status

    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_success_creates_v0_task_and_calls_add_torrent(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
        test_user: dict,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_aria2_client.add_torrent.return_value = "gid-torrent-api"
        mock_get_client.return_value = mock_aria2_client
        torrent_data, info_hash = _valid_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent_data, "options": {"dir": "/tmp/downloads"}},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["uri"] == f"magnet:?xt=urn:btih:{info_hash}"
        assert data["status"] == "active"
        assert data["total_length"] == 1024
        assert data["frozen_space"] == 1024

        global_download = asyncio.run(get_global_by_resource_key(info_hash))
        assert global_download is not None
        assert global_download["resource_kind"] == "torrent"
        assert global_download["source_uri"] == f"magnet:?xt=urn:btih:{info_hash}"
        task = asyncio.run(get_user_task(test_user["id"], global_download["id"]))
        assert task is not None
        assert data["id"] == task["id"]
        mock_aria2_client.add_torrent.assert_awaited_once()
        call_args = mock_aria2_client.add_torrent.call_args
        assert call_args[0][0] == torrent_data
        assert call_args[0][1] == []
        opts = call_args[0][2]
        assert "dir" in opts
        assert opts["dir"].endswith(f"/downloading/{global_download['id']}")
        assert opts["seed-time"] == "0"

    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_partial_selection_sets_select_file_and_selected_size(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_aria2_client.add_torrent.return_value = "gid-torrent-partial"
        mock_get_client.return_value = mock_aria2_client
        torrent_data, info_hash = _multi_file_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={
                "torrent": torrent_data,
                "selected_file_indexes": [3, 1],
                "options": {"select-file": "2"},
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["total_length"] == 4186
        assert data["frozen_space"] == 4186

        global_downloads_for_info = asyncio.run(get_global_by_resource_key(info_hash))
        assert global_downloads_for_info is None

        call_args = mock_aria2_client.add_torrent.call_args
        opts = call_args[0][2]
        assert opts["select-file"] == "1,3"

    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_rejects_bt_tracker_before_submit(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_get_client.return_value = mock_aria2_client
        torrent_data, _ = _valid_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={
                "torrent": torrent_data,
                "options": {"bt-tracker": "http://127.0.0.1/announce"},
            },
        )

        assert response.status_code == 400
        assert "bt-tracker" in response.json()["detail"]
        mock_aria2_client.add_torrent.assert_not_awaited()

    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_torrent_full_selection_keeps_info_hash_key_and_no_select_file(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_aria2_client.add_torrent.return_value = "gid-torrent-full"
        mock_get_client.return_value = mock_aria2_client
        torrent_data, info_hash = _multi_file_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent_data, "selected_file_indexes": [1, 2, 3]},
        )

        assert response.status_code == 201
        assert response.json()["total_length"] == 4234
        global_download = asyncio.run(get_global_by_resource_key(info_hash))
        assert global_download is not None
        opts = mock_aria2_client.add_torrent.call_args[0][2]
        assert "select-file" not in opts

    def test_create_torrent_rejects_invalid_selected_indexes(
        self,
        authenticated_client: TestClient,
    ) -> None:
        torrent_data, _ = _multi_file_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent_data, "selected_file_indexes": [1, 1]},
        )

        assert response.status_code == 400
        assert "duplicate" in response.json()["detail"]

    def test_create_torrent_rejects_non_integer_selected_indexes(
        self,
        authenticated_client: TestClient,
    ) -> None:
        torrent_data, _ = _multi_file_torrent_payload()

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": torrent_data, "selected_file_indexes": ["1"]},
        )

        assert response.status_code == 400
        assert "integers" in response.json()["detail"]


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def test_broadcast_task_update_uses_live_speed(test_user: dict) -> None:
    from app.services.task_broadcast import (
        broadcast_task_update_to_subscribers,
        clear_connections,
        set_connections_for_user,
    )
    from app.services.task_runtime import clear_live_status_cache

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-broadcast-speed",
        "downloadSpeed": "16384",
        "uploadSpeed": "512",
    }
    global_download = asyncio.run(
        create_global_download_v0(
            resource_key="http:broadcast-speed",
            resource_kind="http",
            source_uri="https://example.com/broadcast-speed.bin",
            status="active",
            aria2_gid="gid-broadcast-speed",
            display_name="broadcast-speed.bin",
            total_bytes=100,
        )
    )
    asyncio.run(
        create_user_task_v0(
            user_id=test_user["id"],
            global_download_id=global_download["id"],
            status="active",
            display_name="broadcast-speed.bin",
        )
    )
    websocket = _FakeWebSocket()
    asyncio.run(clear_connections())
    asyncio.run(clear_live_status_cache())
    asyncio.run(set_connections_for_user(test_user["id"], {websocket}))

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(global_download["id"]))

    task = websocket.messages[0]["task"]
    assert task["download_speed"] == 16384
    assert task["upload_speed"] == 512


def test_broadcast_task_update_fetches_live_status_once_for_shared_download(
    test_user: dict,
) -> None:
    from app.services.task_broadcast import (
        broadcast_task_update_to_subscribers,
        clear_connections,
        set_connections_for_user,
    )
    from app.services.task_runtime import clear_live_status_cache

    second_user = asyncio.run(create_user_v0(username="broadcast-second-user"))
    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-shared-broadcast",
        "downloadSpeed": "2048",
        "uploadSpeed": "128",
    }
    global_download = asyncio.run(
        create_global_download_v0(
            resource_key="http:shared-broadcast",
            resource_kind="http",
            source_uri="https://example.com/shared-broadcast.bin",
            status="active",
            aria2_gid="gid-shared-broadcast",
            display_name="shared-broadcast.bin",
            total_bytes=100,
        )
    )
    for user_id in (test_user["id"], second_user["id"]):
        asyncio.run(
            create_user_task_v0(
                user_id=user_id,
                global_download_id=global_download["id"],
                status="active",
                display_name="shared-broadcast.bin",
            )
        )

    first_socket = _FakeWebSocket()
    second_socket = _FakeWebSocket()
    asyncio.run(clear_connections())
    asyncio.run(clear_live_status_cache())
    asyncio.run(set_connections_for_user(test_user["id"], {first_socket}))
    asyncio.run(set_connections_for_user(second_user["id"], {second_socket}))

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(global_download["id"]))

    client.tell_status.assert_awaited_once_with("gid-shared-broadcast")
    assert first_socket.messages[0]["task"]["download_speed"] == 2048
    assert second_socket.messages[0]["task"]["download_speed"] == 2048


def test_broadcast_task_update_reuses_live_status_cache_within_ttl(
    test_user: dict,
) -> None:
    from app.services.task_broadcast import (
        broadcast_task_update_to_subscribers,
        clear_connections,
        set_connections_for_user,
    )
    from app.services.task_runtime import (
        clear_live_status_cache,
        force_expire_live_status_cache_entry,
    )

    client = AsyncMock()
    client.tell_status.side_effect = [
        {
            "gid": "gid-cache-broadcast",
            "downloadSpeed": "1000",
            "uploadSpeed": "50",
        },
        {
            "gid": "gid-cache-broadcast",
            "downloadSpeed": "2000",
            "uploadSpeed": "75",
        },
        {
            "gid": "gid-cache-broadcast",
            "downloadSpeed": "3000",
            "uploadSpeed": "100",
        },
    ]
    global_download = asyncio.run(
        create_global_download_v0(
            resource_key="http:cache-broadcast",
            resource_kind="http",
            source_uri="https://example.com/cache-broadcast.bin",
            status="active",
            aria2_gid="gid-cache-broadcast",
            display_name="cache-broadcast.bin",
            total_bytes=100,
        )
    )
    asyncio.run(
        create_user_task_v0(
            user_id=test_user["id"],
            global_download_id=global_download["id"],
            status="active",
            display_name="cache-broadcast.bin",
        )
    )

    websocket = _FakeWebSocket()
    asyncio.run(clear_connections())
    asyncio.run(clear_live_status_cache())
    asyncio.run(set_connections_for_user(test_user["id"], {websocket}))

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(global_download["id"]))
        asyncio.run(broadcast_task_update_to_subscribers(global_download["id"]))
        asyncio.run(force_expire_live_status_cache_entry("gid-cache-broadcast"))
        asyncio.run(broadcast_task_update_to_subscribers(global_download["id"]))

    assert client.tell_status.await_count == 2
    assert [message["task"]["download_speed"] for message in websocket.messages] == [
        1000,
        1000,
        2000,
    ]


def test_broadcast_task_update_prunes_unrequested_stale_live_status_cache(
    test_user: dict,
) -> None:
    from app.services.task_broadcast import (
        broadcast_task_update_to_subscribers,
        clear_connections,
        set_connections_for_user,
    )
    from app.services.task_runtime import (
        clear_live_status_cache,
        live_status_cache_keys,
        set_live_status_cache_entry,
    )

    client = AsyncMock()
    client.tell_status.return_value = {
        "gid": "gid-cache-prune",
        "downloadSpeed": "100",
        "uploadSpeed": "5",
    }
    global_download = asyncio.run(
        create_global_download_v0(
            resource_key="http:cache-prune",
            resource_kind="http",
            source_uri="https://example.com/cache-prune.bin",
            status="active",
            aria2_gid="gid-cache-prune",
            display_name="cache-prune.bin",
            total_bytes=100,
        )
    )
    asyncio.run(
        create_user_task_v0(
            user_id=test_user["id"],
            global_download_id=global_download["id"],
            status="active",
            display_name="cache-prune.bin",
        )
    )

    asyncio.run(clear_connections())
    asyncio.run(clear_live_status_cache())
    asyncio.run(set_live_status_cache_entry(
        "stale-unrequested-gid",
        status={"gid": "stale-unrequested-gid"},
        fetched_at=-1_000_000.0,
    ))
    websocket = _FakeWebSocket()
    asyncio.run(set_connections_for_user(test_user["id"], {websocket}))

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(global_download["id"]))

    cache_keys = asyncio.run(live_status_cache_keys())
    assert "stale-unrequested-gid" not in cache_keys
    assert "gid-cache-prune" in cache_keys


class TestListTasks:
    def test_list_tasks_unauthorized(self, client: TestClient) -> None:
        response = client.get("/api/tasks")

        assert response.status_code == 401

    def test_list_tasks_empty(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.get("/api/tasks")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_tasks_returns_v0_user_tasks(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        client = AsyncMock()
        client.add_uri.return_value = "gid-list-v0"
        task = asyncio.run(
            create_user_download(
                user_id=test_user["id"],
                quota_bytes=test_user["quota_bytes"],
                uri="https://example.com/list-v0.bin",
                resource_key="http:list-v0",
                resource_kind="http",
                display_name="list-v0.bin",
                total_bytes=1234,
                aria2_client=client,
            )
        )

        response = authenticated_client.get("/api/tasks")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == task["id"]
        assert data[0]["task_id"] == task["global_download_id"]
        assert data[0]["status"] == "active"
        assert data[0]["name"] == "list-v0.bin"
        assert data[0]["uri"] == "https://example.com/list-v0.bin"
        assert data[0]["total_length"] == 1234
        assert data[0]["frozen_space"] == 1234

    @patch("app.services.task_service._get_client")
    def test_list_tasks_uses_live_speed_for_active_rows(
        self,
        mock_get_client: MagicMock,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        client = AsyncMock()
        client.tell_active.return_value = [
            {
                "gid": "gid-speed-task",
                "downloadSpeed": "8192",
                "uploadSpeed": "256",
            }
        ]
        mock_get_client.return_value = client
        global_download = asyncio.run(
            create_global_download_v0(
                resource_key="http:speed-task",
                resource_kind="http",
                source_uri="https://example.com/speed-task.bin",
                status="active",
                aria2_gid="gid-speed-task",
                display_name="speed-task.bin",
                total_bytes=100,
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=global_download["id"],
                status="active",
                reserved_bytes=100,
                display_name="speed-task.bin",
            )
        )

        response = authenticated_client.get("/api/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data[0]["download_speed"] == 8192
        assert data[0]["upload_speed"] == 256
        client.tell_active.assert_awaited_once()

    def test_list_tasks_status_filters(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        active_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:filter-active",
                resource_kind="http",
                source_uri="https://example.com/active.bin",
                status="active",
                display_name="active.bin",
                total_bytes=100,
            )
        )
        completed_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:filter-completed",
                resource_kind="http",
                source_uri="https://example.com/completed.bin",
                status="completed",
                display_name="completed.bin",
                total_bytes=200,
                completed_bytes=200,
            )
        )
        failed_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:filter-failed",
                resource_kind="http",
                source_uri="https://example.com/failed.bin",
                status="failed",
                display_name="failed.bin",
                total_bytes=300,
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=active_global["id"],
                status="active",
                reserved_bytes=100,
                display_name="active.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=completed_global["id"],
                status="completed",
                display_name="completed.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=failed_global["id"],
                status="failed",
                display_name="failed.bin",
                error_message="failed",
            )
        )

        active_response = authenticated_client.get("/api/tasks?status_filter=active")
        complete_response = authenticated_client.get(
            "/api/tasks?status_filter=complete"
        )
        error_response = authenticated_client.get("/api/tasks?status_filter=error")
        current_response = authenticated_client.get("/api/tasks?status_filter=current")

        assert [row["name"] for row in active_response.json()] == ["active.bin"]
        assert [row["name"] for row in complete_response.json()] == ["completed.bin"]
        assert [row["name"] for row in error_response.json()] == ["failed.bin"]
        assert [row["name"] for row in current_response.json()] == ["active.bin"]

    def test_list_tasks_rejects_unknown_status_filter(
        self,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.get("/api/tasks?status_filter=bogus")

        assert response.status_code == 400
        assert response.json()["detail"] == "Unsupported status_filter: bogus"

    def test_list_tasks_filters_by_effective_status(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        active_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:effective-active",
                resource_kind="http",
                source_uri="https://example.com/active.bin",
                status="active",
                display_name="active.bin",
                total_bytes=10,
                completed_bytes=2,
            )
        )
        completed_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:effective-completed",
                resource_kind="http",
                source_uri="https://example.com/completed.bin",
                status="completed",
                display_name="completed.bin",
                total_bytes=20,
                completed_bytes=20,
            )
        )
        failed_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:effective-failed",
                resource_kind="http",
                source_uri="https://example.com/failed.bin",
                status="failed",
                display_name="failed.bin",
                total_bytes=30,
                completed_bytes=3,
            )
        )
        asyncio.run(_set_global_error_message(failed_global["id"], "global failed"))
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=active_global["id"],
                status="active",
                display_name="active.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=completed_global["id"],
                status="active",
                display_name="completed.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=failed_global["id"],
                status="active",
                display_name="failed.bin",
            )
        )

        current = authenticated_client.get("/api/tasks?status_filter=current").json()
        complete = authenticated_client.get("/api/tasks?status_filter=complete").json()
        error = authenticated_client.get("/api/tasks?status_filter=error").json()

        assert [row["name"] for row in current] == ["active.bin"]
        assert [(row["name"], row["status"]) for row in complete] == [
            ("completed.bin", "complete")
        ]
        assert [(row["name"], row["status"], row["error"]) for row in error] == [
            ("failed.bin", "error", "global failed")
        ]


class TestCancelTask:
    def test_cancel_task_unauthorized(self, client: TestClient) -> None:
        response = client.delete("/api/tasks/1")

        assert response.status_code == 401

    def test_cancel_task_not_found(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.delete("/api/tasks/99999")

        assert response.status_code == 404
        assert "任务不存在" in response.json()["detail"]

    def test_cancel_task_success(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        setup_client = AsyncMock()
        setup_client.add_uri.return_value = "gid-cancel-basic"
        task = asyncio.run(
            create_user_download(
                user_id=test_user["id"],
                quota_bytes=test_user["quota_bytes"],
                uri="https://example.com/cancel-basic.bin",
                resource_key="http:cancel-basic",
                resource_kind="http",
                display_name="cancel-basic.bin",
                total_bytes=500,
                aria2_client=setup_client,
            )
        )
        cancel_client = AsyncMock()
        cancel_client.force_remove.return_value = "gid-cancel-basic"

        with patch("app.services.task_service._get_client", return_value=cancel_client):
            response = authenticated_client.delete(f"/api/tasks/{task['id']}")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        stored_task = asyncio.run(
            get_user_task(test_user["id"], task["global_download_id"])
        )
        assert stored_task is not None
        assert stored_task["status"] == "cancelled"
        assert stored_task["reserved_bytes"] == 0


class TestClearHistory:
    def test_clear_history_unauthorized(self, client: TestClient) -> None:
        response = client.delete("/api/tasks")

        assert response.status_code == 401

    def test_clear_history_empty(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.delete("/api/tasks")

        assert response.status_code == 200
        assert response.json() == {"ok": True, "count": 0}

    def test_clear_history_removes_terminal_v0_tasks(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        active_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:clear-active",
                source_uri="https://example.com/active.bin",
                resource_kind="http",
                status="active",
                display_name="active.bin",
            )
        )
        completed_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:clear-completed",
                source_uri="https://example.com/completed.bin",
                resource_kind="http",
                status="completed",
                display_name="completed.bin",
            )
        )
        failed_global = asyncio.run(
            create_global_download_v0(
                resource_key="http:clear-failed",
                source_uri="https://example.com/failed.bin",
                resource_kind="http",
                status="failed",
                display_name="failed.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=active_global["id"],
                status="active",
                display_name="active.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=completed_global["id"],
                status="completed",
                display_name="completed.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_user["id"],
                global_download_id=failed_global["id"],
                status="failed",
                display_name="failed.bin",
            )
        )

        response = authenticated_client.delete("/api/tasks")
        remaining_response = authenticated_client.get("/api/tasks")

        assert response.status_code == 200
        assert response.json() == {"ok": True, "count": 2}
        assert [row["name"] for row in remaining_response.json()] == ["active.bin"]


class TestRateLimiting:
    @patch("app.routers.tasks.ensure_authenticated_allowed")
    def test_create_task_rate_limited(
        self,
        mock_limiter: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_limiter.side_effect = HTTPException(429, "操作过于频繁，请稍后再试")

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"},
        )

        assert response.status_code == 429
        assert "操作过于频繁" in response.json()["detail"]

    @patch("app.routers.tasks.ensure_authenticated_allowed")
    def test_create_torrent_rate_limited(
        self,
        mock_limiter: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_limiter.side_effect = HTTPException(429, "操作过于频繁，请稍后再试")

        response = authenticated_client.post(
            "/api/tasks/torrent",
            json={"torrent": "abc123"},
        )

        assert response.status_code == 429
        assert "操作过于频繁" in response.json()["detail"]

    @patch("app.routers.tasks.ensure_authenticated_allowed")
    def test_torrent_preview_rate_limited(
        self,
        mock_limiter: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        mock_limiter.side_effect = HTTPException(429, "操作过于频繁，请稍后再试")

        response = authenticated_client.post(
            "/api/tasks/torrent/preview",
            json={"torrent": "abc123"},
        )

        assert response.status_code == 429
        assert "操作过于频繁" in response.json()["detail"]


class TestV2TaskPagination:
    def test_v2_tasks_paginate_filtered_rows_and_only_refresh_page(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        test_admin: dict,
    ) -> None:
        task_ids = []
        for index, status in enumerate(("active", "active", "completed")):
            download = asyncio.run(
                create_global_download_v0(
                    resource_key=f"v2-tasks-{index}",
                    resource_kind="http",
                    status=status,
                    aria2_gid=f"v2-gid-{index}",
                    display_name=f"v2-{index}.bin",
                )
            )
            task = asyncio.run(
                create_user_task_v0(
                    user_id=test_user["id"],
                    global_download_id=download["id"],
                    status=status,
                    display_name=f"v2-{index}.bin",
                )
            )
            task_ids.append(task["id"])

        foreign_download = asyncio.run(
            create_global_download_v0(
                resource_key="v2-tasks-foreign",
                resource_kind="http",
                status="active",
                display_name="foreign.bin",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=test_admin["id"],
                global_download_id=foreign_download["id"],
                status="active",
                display_name="foreign.bin",
            )
        )

        with patch(
            "app.services.task_service.fetch_active_live_statuses_by_gid",
            new=AsyncMock(return_value={}),
        ) as fetch_live:
            response = authenticated_client.get(
                "/api/v2/tasks?status_filter=current&page=1&page_size=1"
            )

        assert isinstance(authenticated_client.get("/api/tasks").json(), list)
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"items", "total", "page", "page_size"}
        assert payload["total"] == 2
        assert payload["page"] == 1
        assert payload["page_size"] == 1
        assert payload["items"][0]["id"] == task_ids[1]
        fetch_call = fetch_live.await_args
        assert fetch_call is not None
        assert len(fetch_call.args[0]) == 1
        assert authenticated_client.get("/api/v2/tasks?page=9&page_size=1").json()["items"] == []
