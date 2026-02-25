"""Tests for config router endpoints."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


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

    def test_get_config_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/config")
        assert response.status_code == 403

    def test_get_config_unauthorized(self, client: TestClient):
        response = client.get("/api/config")
        assert response.status_code == 401


class TestUpdateConfig:

    def test_update_max_task_size(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"max_task_size": 5 * 1024 * 1024 * 1024})
        assert response.status_code == 200
        assert response.json()["max_task_size"] == 5 * 1024 * 1024 * 1024

    def test_update_min_free_disk(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"min_free_disk": 2 * 1024 * 1024 * 1024})
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
        response = admin_client.put("/api/config", json={
            "ws_reconnect_max_delay": 120.0,
            "ws_reconnect_jitter": 0.5,
            "ws_reconnect_factor": 3.0
        })
        assert response.status_code == 200
        data = response.json()
        assert data["ws_reconnect_max_delay"] == 120.0
        assert data["ws_reconnect_jitter"] == 0.5
        assert data["ws_reconnect_factor"] == 3.0

    def test_update_hidden_file_extensions(self, admin_client: TestClient):
        response = admin_client.put("/api/config", json={"hidden_file_extensions": [".txt", "log"]})
        assert response.status_code == 200
        extensions = response.json()["hidden_file_extensions"]
        assert ".txt" in extensions
        assert ".log" in extensions

    def test_update_config_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.put("/api/config", json={"max_task_size": 1024})
        assert response.status_code == 403

    def test_update_config_unauthorized(self, client: TestClient):
        response = client.put("/api/config", json={"max_task_size": 1024})
        assert response.status_code == 401


class TestAria2Version:

    def test_get_aria2_version_success(self, admin_client: TestClient):
        mock_client = AsyncMock()
        mock_client.get_version.return_value = {
            "version": "1.36.0",
            "enabledFeatures": ["BitTorrent", "GZip"]
        }

        with patch("app.aria2.client.Aria2Client", return_value=mock_client):
            response = admin_client.get("/api/config/aria2/version")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["version"] == "1.36.0"
        assert "BitTorrent" in data["enabled_features"]

    def test_get_aria2_version_connection_failed(self, admin_client: TestClient):
        mock_client = AsyncMock()
        mock_client.get_version.side_effect = Exception("Connection refused")

        with patch("app.aria2.client.Aria2Client", return_value=mock_client):
            response = admin_client.get("/api/config/aria2/version")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert "error" in data

    def test_get_aria2_version_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/config/aria2/version")
        assert response.status_code == 403


class TestAria2Test:

    def test_test_aria2_connection_success(self, admin_client: TestClient):
        mock_client = AsyncMock()
        mock_client.get_version.return_value = {
            "version": "1.36.0",
            "enabledFeatures": ["BitTorrent"]
        }

        with patch("app.aria2.client.Aria2Client", return_value=mock_client):
            response = admin_client.post("/api/config/aria2/test", json={
                "aria2_rpc_url": "http://localhost:6800/jsonrpc",
                "aria2_rpc_secret": "test_secret"
            })

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["version"] == "1.36.0"

    def test_test_aria2_connection_failed(self, admin_client: TestClient):
        mock_client = AsyncMock()
        mock_client.get_version.side_effect = Exception("Connection refused")

        with patch("app.aria2.client.Aria2Client", return_value=mock_client):
            response = admin_client.post("/api/config/aria2/test", json={
                "aria2_rpc_url": "http://localhost:6800/jsonrpc"
            })

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False

    def test_test_aria2_empty_url(self, admin_client: TestClient):
        response = admin_client.post("/api/config/aria2/test", json={
            "aria2_rpc_url": ""
        })
        assert response.status_code == 400

    def test_test_aria2_non_admin(self, authenticated_client: TestClient):
        response = authenticated_client.post("/api/config/aria2/test", json={
            "aria2_rpc_url": "http://localhost:6800/jsonrpc"
        })
        assert response.status_code == 403


class TestTokens:

    def test_list_tokens_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/config/tokens")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_token(self, authenticated_client: TestClient):
        response = authenticated_client.post("/api/config/tokens", json={"name": "Test Token"})
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "token" in data
        assert data["name"] == "Test Token"
        assert data["token"].startswith("aria2_")

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

    def test_delete_token(self, authenticated_client: TestClient):
        create_response = authenticated_client.post("/api/config/tokens", json={"name": "To Delete"})
        token_id = create_response.json()["id"]

        response = authenticated_client.delete(f"/api/config/tokens/{token_id}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        list_response = authenticated_client.get("/api/config/tokens")
        assert len(list_response.json()) == 0

    def test_delete_token_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/config/tokens/99999")
        assert response.status_code == 404

    def test_delete_other_user_token(self, client: TestClient, user_session: str, admin_session: str, test_admin: dict, test_user: dict, temp_db: str):
        # Create token as admin
        client.cookies.set(settings.session_cookie_name, admin_session)
        create_response = client.post("/api/config/tokens", json={"name": "Admin Token"})
        token_id = create_response.json()["id"]

        # Try to delete as regular user
        client.cookies.set(settings.session_cookie_name, user_session)
        response = client.delete(f"/api/config/tokens/{token_id}")
        assert response.status_code == 403

    def test_tokens_unauthorized(self, client: TestClient):
        response = client.get("/api/config/tokens")
        assert response.status_code == 401


class TestConfigHelperFunctions:

    def test_get_config_value_exception(self, temp_db: str):
        from app.routers.config import get_config_value, _config_cache
        import sqlite3
        _config_cache.clear()

        with patch.object(sqlite3, "connect", side_effect=Exception("DB error")):
            result = get_config_value("nonexistent_key")
            assert result is None

    @pytest.mark.asyncio
    async def test_get_config_value_async_cache_hit(self, temp_db: str):
        from app.routers.config import get_config_value_async, _config_cache, _config_cache_lock
        from time import time

        async with _config_cache_lock:
            _config_cache["test_key"] = ("cached_value", time())

        result = await get_config_value_async("test_key")
        assert result == "cached_value"

    @pytest.mark.asyncio
    async def test_set_config_value_async_update(self, temp_db: str):
        from app.routers.config import set_config_value_async, get_config_value_async, _config_cache

        _config_cache.clear()
        await set_config_value_async("test_update_key", "initial_value")
        result1 = await get_config_value_async("test_update_key")
        assert result1 == "initial_value"

        await set_config_value_async("test_update_key", "updated_value")
        _config_cache.clear()
        result2 = await get_config_value_async("test_update_key")
        assert result2 == "updated_value"

    @pytest.mark.asyncio
    async def test_set_config_value_async_create(self, temp_db: str):
        from app.routers.config import set_config_value_async, get_config_value_async, _config_cache

        _config_cache.clear()
        await set_config_value_async("new_config_key", "new_value")
        _config_cache.clear()
        result = await get_config_value_async("new_config_key")
        assert result == "new_value"
