"""Tests for v0 task router endpoints."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.errors import BadRequestError
from app.repositories.downloads import get_global_by_resource_key, get_user_task
from app.services.download_service import create_user_download
from app.services.hash import get_uri_hash
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

        with pytest.raises(BadRequestError) as exc_info:
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

    @patch("app.services.task_service.probe_url_with_get_fallback")
    @patch("app.services.task_service._get_client")
    @patch("app.services.task_service.check_disk_space")
    def test_create_task_success_creates_v0_user_task(
        self,
        mock_disk: MagicMock,
        mock_get_client: MagicMock,
        mock_probe: AsyncMock,
        authenticated_client: TestClient,
        mock_aria2_client: AsyncMock,
        test_user: dict,
    ) -> None:
        mock_disk.return_value = (True, 100 * 1024 * 1024 * 1024)
        mock_get_client.return_value = mock_aria2_client
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.final_url = "http://example.com/file.zip"
        mock_result.filename = "file.zip"
        mock_result.content_length = 100 * 1024 * 1024
        mock_probe.return_value = mock_result

        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["uri"] == "http://example.com/file.zip"
        assert data["status"] == "active"
        assert data["name"] == "file.zip"
        assert data["total_length"] == 100 * 1024 * 1024
        assert data["frozen_space"] == 100 * 1024 * 1024

        global_download = asyncio.run(
            get_global_by_resource_key(get_uri_hash("http://example.com/file.zip"))
        )
        assert global_download is not None
        task = asyncio.run(get_user_task(test_user["id"], global_download["id"]))
        assert task is not None
        assert data["id"] == task["id"]
        mock_aria2_client.add_uri.assert_awaited_once()
        call_args = mock_aria2_client.add_uri.call_args
        assert call_args[0][0] == ["http://example.com/file.zip"]
        opts = call_args[0][1]
        assert "dir" in opts
        assert opts["dir"].endswith(f"/downloading/{global_download['id']}")
        assert opts["seed-time"] == "0"


class TestTorrentPreview:
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
    def test_create_torrent_unauthorized(self, client: TestClient) -> None:
        response = client.post("/api/tasks/torrent", json={"torrent": "abc123"})

        assert response.status_code == 401

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
        large_torrent = base64.b64encode(b"x" * (15 * 1024 * 1024)).decode()

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
                "options": {"select-file": "2", "bt-tracker": "http://tracker.example.com/announce"},
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
        assert opts["bt-tracker"] == "http://tracker.example.com/announce"

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
    from app.core.state import AppState
    from app.services.task_broadcast import broadcast_task_update_to_subscribers

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
    state = AppState()
    websocket = _FakeWebSocket()
    state.ws_connections[test_user["id"]] = {websocket}

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(state, global_download["id"]))

    task = websocket.messages[0]["task"]
    assert task["download_speed"] == 16384
    assert task["upload_speed"] == 512


def test_broadcast_task_update_fetches_live_status_once_for_shared_download(
    test_user: dict,
) -> None:
    from app.core.state import AppState
    from app.services.task_broadcast import broadcast_task_update_to_subscribers

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

    state = AppState()
    first_socket = _FakeWebSocket()
    second_socket = _FakeWebSocket()
    state.ws_connections[test_user["id"]] = {first_socket}
    state.ws_connections[second_user["id"]] = {second_socket}

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(state, global_download["id"]))

    client.tell_status.assert_awaited_once_with("gid-shared-broadcast")
    assert first_socket.messages[0]["task"]["download_speed"] == 2048
    assert second_socket.messages[0]["task"]["download_speed"] == 2048


def test_broadcast_task_update_reuses_live_status_cache_within_ttl(
    test_user: dict,
) -> None:
    from app.core.state import AppState
    from app.services.task_broadcast import broadcast_task_update_to_subscribers

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

    state = AppState()
    websocket = _FakeWebSocket()
    state.ws_connections[test_user["id"]] = {websocket}

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(state, global_download["id"]))
        asyncio.run(broadcast_task_update_to_subscribers(state, global_download["id"]))
        if hasattr(state, "live_status_cache"):
            state.live_status_cache["gid-cache-broadcast"].fetched_at = -1_000_000.0
        asyncio.run(broadcast_task_update_to_subscribers(state, global_download["id"]))

    assert client.tell_status.await_count == 2
    assert [message["task"]["download_speed"] for message in websocket.messages] == [
        1000,
        1000,
        2000,
    ]


def test_broadcast_task_update_prunes_unrequested_stale_live_status_cache(
    test_user: dict,
) -> None:
    from app.core.state import AppState, LiveStatusCacheEntry
    from app.services.task_broadcast import broadcast_task_update_to_subscribers

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

    state = AppState()
    state.live_status_cache["stale-unrequested-gid"] = LiveStatusCacheEntry(
        status={"gid": "stale-unrequested-gid"},
        fetched_at=-1_000_000.0,
    )
    websocket = _FakeWebSocket()
    state.ws_connections[test_user["id"]] = {websocket}

    with patch("app.services.task_broadcast.get_aria2_client", return_value=client):
        asyncio.run(broadcast_task_update_to_subscribers(state, global_download["id"]))

    assert "stale-unrequested-gid" not in state.live_status_cache
    assert "gid-cache-prune" in state.live_status_cache


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
    @patch("app.services.task_service.ensure_authenticated_allowed")
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

    @patch("app.services.task_service.ensure_authenticated_allowed")
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

    @patch("app.services.task_service.ensure_authenticated_allowed")
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
