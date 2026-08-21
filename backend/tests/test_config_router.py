"""Tests for config router endpoints."""

import logging

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import app_settings
from tests.fakes import make_aria2_client


def test_tokens_use_current_schema(authenticated_client: TestClient):
    create_response = authenticated_client.post(
        "/api/config/tokens", json={"name": "config token"}
    )
    assert create_response.status_code == 200
    token = create_response.json()
    assert token["name"] == "config token"
    assert token["token"].startswith("aria2_")
    assert isinstance(token["created_at"], str)
    assert "T" in token["created_at"]

    list_response = authenticated_client.get("/api/config/tokens")
    assert list_response.status_code == 200
    rows = list_response.json()
    assert len(rows) == 1
    assert rows[0]["id"] == token["id"]
    assert rows[0]["name"] == "config token"


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


ARIA2_LOG_URL = (
    "https://probe-user:probe-password@example.com/jsonrpc?token=probe-token"
    "&signature=probe-signature#probe-fragment"
)


def assert_aria2_log_redacted(caplog):
    assert "https://example.com/jsonrpc" in caplog.text
    for secret in (
        "probe-user",
        "probe-password",
        "probe-token",
        "probe-signature",
        "probe-fragment",
    ):
        assert secret not in caplog.text


class TestGetConfig:
    def test_get_config_admin(self, admin_client: TestClient):
        response = admin_client.get("/api/config")
        assert response.status_code == 200
        data = response.json()
        assert "max_task_size" in data
        assert "min_free_disk" in data
        assert "aria2_rpc_url" in data
        assert "aria2_rpc_secret" in data
        assert "hidden_file_extensions" in data
        assert "pack_format" in data
        assert "pack_compression_level" in data
        assert "pack_memory_limit" not in data
        assert "ws_reconnect_max_delay" in data
        assert "ws_reconnect_jitter" in data
        assert "ws_reconnect_factor" in data
        assert "aria2_bt_stop_timeout_seconds" in data
        assert "rate_limit_account_security" in data
        assert "rate_limit_authenticated_api" in data
        assert "rate_limit_public_api" in data
        assert "rate_limit_share_access" in data
        assert "download_total_connections" in data
        assert "download_authenticated_reserved_connections" in data
        assert "download_anonymous_base_connections" in data
        assert "history_retention_days" in data
        assert isinstance(data["history_retention_days"], int)
        assert data["history_retention_days"] >= 1
        assert "rate_limit_login" not in data
        assert "download_rate_limit" not in data
        assert "rate_limit_authenticated_download" not in data
        assert "rate_limit_anonymous_download" not in data

    def test_get_config_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/config")
        assert response.status_code == 403

    def test_get_config_unauthorized(self, client: TestClient):
        response = client.get("/api/config")
        assert response.status_code == 401


class TestUpdateConfig:
    def test_update_persists_typed_app_settings_and_get_returns_values(
        self, admin_client: TestClient
    ):
        payload = {
            "max_task_size": 3 * 1024 * 1024 * 1024,
            "min_free_disk": 512 * 1024 * 1024,
            "hidden_file_extensions": [".Tmp", "log"],
            "ws_reconnect_jitter": 0.35,
            "ws_reconnect_factor": 2.5,
            "aria2_bt_stop_timeout_seconds": 3600,
        }

        response = admin_client.put("/api/config", json=payload)
        assert response.status_code == 200

        import asyncio

        async def fetch_settings_row() -> dict:
            async with transaction() as conn:
                row = (
                    (
                        await conn.execute(
                            select(app_settings).where(app_settings.c.id == 1)
                        )
                    )
                    .mappings()
                    .one()
                )
            return dict(row)

        row = asyncio.run(fetch_settings_row())
        assert row["max_task_size_bytes"] == payload["max_task_size"]
        assert row["min_free_disk_bytes"] == payload["min_free_disk"]
        assert row["hidden_file_extensions_json"] == '[".tmp", ".log"]'
        assert row["ws_reconnect_jitter"] == "0.35"
        assert row["ws_reconnect_factor"] == "2.5"
        assert row["aria2_bt_stop_timeout_seconds"] == 3600

        get_response = admin_client.get("/api/config")
        assert get_response.status_code == 200
        data = get_response.json()
        assert data["max_task_size"] == payload["max_task_size"]
        assert data["min_free_disk"] == payload["min_free_disk"]
        assert data["hidden_file_extensions"] == [".tmp", ".log"]
        assert data["ws_reconnect_jitter"] == 0.35
        assert data["ws_reconnect_factor"] == 2.5
        assert data["aria2_bt_stop_timeout_seconds"] == 3600

    def test_update_max_task_size(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config", json={"max_task_size": 5 * 1024 * 1024 * 1024}
        )
        assert response.status_code == 200
        assert response.json()["max_task_size"] == 5 * 1024 * 1024 * 1024

    def test_update_min_free_disk(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config", json={"min_free_disk": 2 * 1024 * 1024 * 1024}
        )
        assert response.status_code == 200
        assert response.json()["min_free_disk"] == 2 * 1024 * 1024 * 1024

    def test_update_pack_format(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"pack_format": "tar.zst"})
        assert response.status_code == 200
        assert response.json()["pack_format"] == "tar.zst"

    def test_update_pack_format_legacy_7z_maps_tar_zst(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"pack_format": "7z"})
        assert response.status_code == 200
        assert response.json()["pack_format"] == "tar.zst"

    def test_update_pack_compression_level(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"pack_compression_level": 9})
        assert response.status_code == 200
        assert response.json()["pack_compression_level"] == 9

    def test_update_compression_level_out_of_range(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"pack_compression_level": 100})
        assert response.status_code == 422

    def test_update_compression_level_negative(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"pack_compression_level": -1})
        assert response.status_code == 422

    def test_update_compression_level_zero(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"pack_compression_level": 0})
        assert response.status_code == 200
        assert response.json()["pack_compression_level"] == 0

    def test_update_ws_reconnect_params(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config",
            json={
                "ws_reconnect_max_delay": 120.0,
                "ws_reconnect_jitter": 0.5,
                "ws_reconnect_factor": 3.0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ws_reconnect_max_delay"] == 120.0
        assert data["ws_reconnect_jitter"] == 0.5
        assert data["ws_reconnect_factor"] == 3.0

    def test_update_hidden_file_extensions(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config", json={"hidden_file_extensions": [".txt", "log"]}
        )
        assert response.status_code == 200
        extensions = response.json()["hidden_file_extensions"]
        assert ".txt" in extensions
        assert ".log" in extensions

    def test_update_rate_limit_groups(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config",
            json={
                "rate_limit_account_security": 6,
                "rate_limit_authenticated_api": 90,
                "rate_limit_public_api": 40,
                "rate_limit_share_access": 3,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["rate_limit_account_security"] == 6
        assert data["rate_limit_authenticated_api"] == 90
        assert data["rate_limit_public_api"] == 40
        assert data["rate_limit_share_access"] == 3

    def test_update_download_connection_pools(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config",
            json={
                "download_total_connections": 80,
                "download_authenticated_reserved_connections": 45,
                "download_authenticated_per_user_connections": 12,
                "download_authenticated_per_file_connections": 6,
                "download_anonymous_base_connections": 15,
                "download_anonymous_borrow_connections": 20,
                "download_anonymous_per_ip_connections": 3,
                "download_anonymous_per_file_connections": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["download_total_connections"] == 80
        assert data["download_authenticated_reserved_connections"] == 45
        assert data["download_authenticated_per_user_connections"] == 12
        assert data["download_authenticated_per_file_connections"] == 6
        assert data["download_anonymous_base_connections"] == 15
        assert data["download_anonymous_borrow_connections"] == 20
        assert data["download_anonymous_per_ip_connections"] == 3
        assert data["download_anonymous_per_file_connections"] == 1

    def test_update_download_connection_pools_rejects_invalid_allocation(
        self, admin_client: TestClient
    ):
        response = admin_client.put(
            "/api/config",
            json={
                "download_total_connections": 10,
                "download_authenticated_reserved_connections": 6,
                "download_anonymous_base_connections": 3,
                "download_anonymous_borrow_connections": 2,
            },
        )
        assert response.status_code == 400
        assert "不能超过系统总连接上限" in response.json()["detail"]

    def test_update_rejects_removed_download_rate_limit_fields(
        self, admin_client: TestClient
    ):
        response = admin_client.put(
            "/api/config",
            json={
                "rate_limit_authenticated_download": 300,
                "rate_limit_anonymous_download": 60,
            },
        )

        assert response.status_code == 422

    def test_update_config_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.put("/api/config", json={"max_task_size": 1024})
        assert response.status_code == 403

    def test_update_config_unauthorized(self, client: TestClient):
        response = client.put("/api/config", json={"max_task_size": 1024})
        assert response.status_code == 401


class TestAria2Version:
    def test_get_aria2_version_success(self, admin_client: TestClient):
        mock_client = make_aria2_client(
            get_version={"version": "1.36.0", "enabledFeatures": ["BitTorrent", "GZip"]}
        )

        with patch("app.aria2.gateway.create_aria2_client", return_value=mock_client):
            response = admin_client.get("/api/config/aria2/version")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["version"] == "1.36.0"
        assert "BitTorrent" in data["enabled_features"]

    def test_get_aria2_version_connection_failed(self, admin_client: TestClient):
        mock_client = make_aria2_client(get_version=Exception("Connection refused"))

        with patch("app.aria2.gateway.create_aria2_client", return_value=mock_client):
            response = admin_client.get("/api/config/aria2/version")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert "error" in data

    def test_get_aria2_version_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/config/aria2/version")
        assert response.status_code == 403


class TestAria2Test:
    def test_test_aria2_connection_success(self, admin_client: TestClient, caplog):
        mock_client = make_aria2_client(
            get_version={"version": "1.36.0", "enabledFeatures": ["BitTorrent"]}
        )

        with patch("app.aria2.gateway.create_aria2_client", return_value=mock_client), caplog.at_level(
            logging.INFO, logger="app.services.aria2_admin_service"
        ):
            response = admin_client.post(
                "/api/config/aria2/test",
                json={
                    "aria2_rpc_url": ARIA2_LOG_URL,
                    "aria2_rpc_secret": "test_secret",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["version"] == "1.36.0"
        assert_aria2_log_redacted(caplog)

    def test_test_aria2_connection_failed(self, admin_client: TestClient, caplog):
        mock_client = make_aria2_client(get_version=RuntimeError(ARIA2_LOG_URL))

        with patch("app.aria2.gateway.create_aria2_client", return_value=mock_client), caplog.at_level(
            logging.WARNING, logger="app.services.aria2_admin_service"
        ):
            response = admin_client.post(
                "/api/config/aria2/test",
                json={"aria2_rpc_url": ARIA2_LOG_URL},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert_aria2_log_redacted(caplog)

    def test_test_aria2_empty_url(self, admin_client: TestClient):
        response = admin_client.post(
            "/api/config/aria2/test", json={"aria2_rpc_url": ""}
        )
        assert response.status_code == 400

    def test_test_aria2_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/config/aria2/test",
            json={"aria2_rpc_url": "http://localhost:6800/jsonrpc"},
        )
        assert response.status_code == 403


class TestTokens:
    def test_list_tokens_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/config/tokens")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_token(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/config/tokens", json={"name": "Test Token"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "token" in data
        assert data["name"] == "Test Token"
        assert data["token"].startswith("aria2_")
        assert isinstance(data["created_at"], str)
        assert "T" in data["created_at"]

    def test_create_token_without_name(self, authenticated_client: TestClient):
        response = authenticated_client.post("/api/config/tokens", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["token"].startswith("aria2_")
        assert data["name"] is None

    def test_list_tokens_with_token(self, authenticated_client: TestClient):
        authenticated_client.post("/api/config/tokens", json={"name": "My Token"})
        response = authenticated_client.get("/api/config/tokens")
        assert response.status_code == 200
        tokens = response.json()
        assert len(tokens) == 1
        assert tokens[0]["name"] == "My Token"
        assert isinstance(tokens[0]["created_at"], str)
        assert "T" in tokens[0]["created_at"]

    def test_delete_token(self, authenticated_client: TestClient):
        create_response = authenticated_client.post(
            "/api/config/tokens", json={"name": "To Delete"}
        )
        token_id = create_response.json()["id"]

        response = authenticated_client.delete(f"/api/config/tokens/{token_id}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        list_response = authenticated_client.get("/api/config/tokens")
        assert len(list_response.json()) == 0

    def test_delete_token_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/config/tokens/99999")
        assert response.status_code == 404

    def test_delete_other_user_token(
        self,
        client: TestClient,
        user_session: str,
        admin_session: str,
        test_admin: dict,
        test_user: dict,
        temp_db: str,
    ):
        # Create token as admin
        client.cookies.set(settings.session_cookie_name, admin_session)
        create_response = client.post(
            "/api/config/tokens", json={"name": "Admin Token"}
        )
        token_id = create_response.json()["id"]

        # Try to delete as regular user
        client.cookies.set(settings.session_cookie_name, user_session)
        response = client.delete(f"/api/config/tokens/{token_id}")
        assert response.status_code == 404

    def test_tokens_unauthorized(self, client: TestClient):
        response = client.get("/api/config/tokens")
        assert response.status_code == 401


class TestConfigHelperFunctions:
    def test_get_config_value_sync_unknown_key(self, temp_db: str):
        from app.services.settings_service import clear_config_cache, get_config_value_sync

        clear_config_cache()

        result = get_config_value_sync("nonexistent_key")
        assert result is None

    def test_get_config_value_sync_default_before_runtime_load(self, temp_db: str):
        from app.services.settings_service import clear_config_cache, get_config_value_sync

        clear_config_cache()

        assert get_config_value_sync("site_title") == "Aria2 控制器"

    def test_get_config_value_sync_serves_stale_library_value_after_ttl(
        self, temp_db: str
    ):
        import time

        from app.services.settings_service import (
            _CACHE_TTL,
            _cache_settings_row,
            clear_config_cache,
            get_config_value_sync,
        )

        clear_config_cache()
        _cache_settings_row(
            {"max_task_size_bytes": 107374182400},
            timestamp=time.time() - _CACHE_TTL - 1,
        )

        assert get_config_value_sync("max_task_size") == "107374182400"

    def test_get_max_task_size_serves_stale_library_value_after_ttl(
        self, temp_db: str
    ):
        import time

        from app.services.settings_service import (
            _CACHE_TTL,
            _cache_settings_row,
            clear_config_cache,
            get_max_task_size,
        )

        clear_config_cache()
        _cache_settings_row(
            {"max_task_size_bytes": 107374182400},
            timestamp=time.time() - _CACHE_TTL - 1,
        )

        assert get_max_task_size() == 107374182400

    def test_get_config_value_sync_stale_read_does_not_pollute_cache(
        self, temp_db: str
    ):
        import time

        from app.services.settings_service import (
            _CACHE_TTL,
            _cache_settings_row,
            clear_config_cache,
            get_config_value_sync,
        )

        clear_config_cache()
        _cache_settings_row(
            {"max_task_size_bytes": 107374182400},
            timestamp=time.time() - _CACHE_TTL - 1,
        )

        assert get_config_value_sync("max_task_size") == "107374182400"
        assert get_config_value_sync("max_task_size") == "107374182400"

    @pytest.mark.asyncio
    async def test_get_config_value_cache_hit(self, temp_db: str):
        from app.services.settings_service import (
            clear_config_cache_async,
            get_config_value,
            set_config_value,
        )

        await clear_config_cache_async()
        await set_config_value("site_title", "cached_value")

        result = await get_config_value("site_title")
        assert result == "cached_value"

    @pytest.mark.asyncio
    async def test_set_config_value_update(self, temp_db: str):
        from app.services.settings_service import (
            clear_config_cache_async,
            get_config_value,
            set_config_value,
        )

        await clear_config_cache_async()
        await set_config_value("site_title", "Initial Title")
        result1 = await get_config_value("site_title")
        assert result1 == "Initial Title"

        await set_config_value("site_title", "Updated Title")
        await clear_config_cache_async()
        result2 = await get_config_value("site_title")
        assert result2 == "Updated Title"

    @pytest.mark.asyncio
    async def test_set_config_value_unsupported_key(self, temp_db: str):
        from app.services.settings_service import (
            clear_config_cache_async,
            get_config_value,
            set_config_value,
        )

        await clear_config_cache_async()
        await set_config_value("unsupported_config_key", "new_value")
        await clear_config_cache_async()
        result = await get_config_value("unsupported_config_key")
        assert result is None
