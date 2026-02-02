"""Tests for aria2 RPC router."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute


@pytest.fixture
def rpc_user(temp_db: str) -> dict:
    from app.core.security import hash_password
    from datetime import datetime, timezone
    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota, rpc_secret)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ["rpcuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(),
         100 * 1024 * 1024 * 1024, "test_rpc_secret_123"]
    )
    return {"id": user_id, "username": "rpcuser", "rpc_secret": "test_rpc_secret_123"}


class TestRpcRateLimiter:

    def test_rate_limiter_allows_requests(self, client: TestClient, rpc_user: dict):
        from app.routers.aria2_rpc import rpc_limiter
        rpc_limiter._requests.clear()

        response = client.post("/aria2/jsonrpc", json={
            "jsonrpc": "2.0",
            "method": "aria2.getVersion",
            "params": [f"token:{rpc_user['rpc_secret']}"],
            "id": "1"
        })
        assert response.status_code == 200


class TestExtractSecret:

    def test_extract_secret_with_token(self):
        from app.routers.aria2_rpc import extract_secret_from_params
        secret, remaining = extract_secret_from_params(["token:mysecret", "param1", "param2"])
        assert secret == "mysecret"
        assert remaining == ["param1", "param2"]

    def test_extract_secret_without_token(self):
        from app.routers.aria2_rpc import extract_secret_from_params
        secret, remaining = extract_secret_from_params(["param1", "param2"])
        assert secret is None
        assert remaining == ["param1", "param2"]

    def test_extract_secret_empty_params(self):
        from app.routers.aria2_rpc import extract_secret_from_params
        secret, remaining = extract_secret_from_params([])
        assert secret is None
        assert remaining == []


class TestBuildJsonrpcResponse:

    def test_build_success_response(self):
        from app.routers.aria2_rpc import build_jsonrpc_response
        response = build_jsonrpc_response({"version": "1.36.0"}, "1")
        assert response == {
            "jsonrpc": "2.0",
            "result": {"version": "1.36.0"},
            "id": "1"
        }

    def test_build_error_response(self):
        from app.routers.aria2_rpc import build_jsonrpc_error
        response = build_jsonrpc_error(-32600, "Invalid Request", "1")
        assert response == {
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request"},
            "id": "1"
        }

    def test_build_error_response_with_data(self):
        from app.routers.aria2_rpc import build_jsonrpc_error
        response = build_jsonrpc_error(-32600, "Invalid Request", "1", {"detail": "extra"})
        assert response == {
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request", "data": {"detail": "extra"}},
            "id": "1"
        }


class TestJsonrpcHandler:

    def test_missing_token(self, client: TestClient, temp_db: str):
        response = client.post("/aria2/jsonrpc", json={
            "jsonrpc": "2.0",
            "method": "aria2.getVersion",
            "params": [],
            "id": "1"
        })
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["message"] == "Missing token parameter"

    def test_invalid_token(self, client: TestClient, temp_db: str):
        response = client.post("/aria2/jsonrpc", json={
            "jsonrpc": "2.0",
            "method": "aria2.getVersion",
            "params": ["token:invalid_secret"],
            "id": "1"
        })
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["message"] == "Invalid token"

    def test_invalid_json(self, client: TestClient, temp_db: str):
        response = client.post(
            "/aria2/jsonrpc",
            content="not valid json",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "Parse error" in data["error"]["message"]

    def test_invalid_jsonrpc_version(self, client: TestClient, rpc_user: dict):
        response = client.post("/aria2/jsonrpc", json={
            "jsonrpc": "1.0",
            "method": "aria2.getVersion",
            "params": [f"token:{rpc_user['rpc_secret']}"],
            "id": "1"
        })
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "2.0" in data["error"]["message"]

    def test_missing_method(self, client: TestClient, rpc_user: dict):
        response = client.post("/aria2/jsonrpc", json={
            "jsonrpc": "2.0",
            "params": [f"token:{rpc_user['rpc_secret']}"],
            "id": "1"
        })
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "Method is required" in data["error"]["message"]

    def test_params_not_array(self, client: TestClient, temp_db: str):
        response = client.post("/aria2/jsonrpc", json={
            "jsonrpc": "2.0",
            "method": "aria2.getVersion",
            "params": "not_an_array",
            "id": "1"
        })
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "array" in data["error"]["message"].lower()

    def test_empty_batch_request(self, client: TestClient, temp_db: str):
        response = client.post("/aria2/jsonrpc", json=[])
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "Empty batch" in data["error"]["message"]

    def test_invalid_request_type(self, client: TestClient, temp_db: str):
        response = client.post("/aria2/jsonrpc", json="string_not_object")
        assert response.status_code == 200
        data = response.json()
        assert "error" in data


class TestGetUserByRpcSecret:

    def test_get_user_valid_secret(self, temp_db: str, rpc_user: dict):
        from app.routers.aria2_rpc import get_user_by_rpc_secret
        user = get_user_by_rpc_secret(rpc_user["rpc_secret"])
        assert user is not None
        assert user["username"] == "rpcuser"

    def test_get_user_invalid_secret(self, temp_db: str):
        from app.routers.aria2_rpc import get_user_by_rpc_secret
        user = get_user_by_rpc_secret("nonexistent_secret")
        assert user is None
