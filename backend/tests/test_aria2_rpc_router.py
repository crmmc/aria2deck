"""Tests for aria2 RPC router."""
import base64
import json
import pytest
from fastapi.testclient import TestClient

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

    def test_batch_auth_per_request(self, client: TestClient, rpc_user: dict):
        response = client.post("/aria2/jsonrpc", json=[
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": [f"token:{rpc_user['rpc_secret']}"],
                "id": "1",
            },
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": ["token:invalid_secret"],
                "id": "2",
            },
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": [],
                "id": "3",
            },
        ])

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 3
        assert data[0]["id"] == "1"
        if "error" in data[0]:
            assert data[0]["error"]["message"] == "Internal server error"
        else:
            assert "result" in data[0]
        assert data[1]["error"]["message"] == "Invalid token"
        assert data[2]["error"]["message"] == "Missing token parameter"

    def test_batch_invalid_item_does_not_block_other_items(self, client: TestClient, rpc_user: dict):
        response = client.post("/aria2/jsonrpc", json=[
            "not_an_object",
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": [f"token:{rpc_user['rpc_secret']}"],
                "id": "2",
            },
        ])

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert "error" in data[0]
        assert data[0]["error"]["message"] == "Invalid request in batch"
        assert data[1]["id"] == "2"
        if "error" in data[1]:
            assert data[1]["error"]["message"] == "Internal server error"
        else:
            assert "result" in data[1]

    def test_batch_item_params_must_be_array(self, client: TestClient, rpc_user: dict):
        response = client.post("/aria2/jsonrpc", json=[
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": "not_an_array",
                "id": "1",
            },
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": [f"token:{rpc_user['rpc_secret']}"],
                "id": "2",
            },
        ])

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert "error" in data[0]
        assert "array" in data[0]["error"]["message"].lower()
        assert data[1]["id"] == "2"
        if "error" in data[1]:
            assert data[1]["error"]["message"] == "Internal server error"
        else:
            assert "result" in data[1]

    def test_invalid_request_type(self, client: TestClient, temp_db: str):
        response = client.post("/aria2/jsonrpc", json="string_not_object")
        assert response.status_code == 200
        data = response.json()
        assert "error" in data

    def test_get_query_json_params(self, client: TestClient, rpc_user: dict):
        response = client.get(
            "/aria2/jsonrpc",
            params={
                "jsonrpc": "2.0",
                "method": "aria2.pause",
                "id": "get-json-1",
                "params": json.dumps([f"token:{rpc_user['rpc_secret']}", "dummy-gid"]),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "get-json-1"
        assert data["result"] == "dummy-gid"

    def test_get_query_base64_params(self, client: TestClient, rpc_user: dict):
        encoded_params = base64.b64encode(
            json.dumps([f"token:{rpc_user['rpc_secret']}", "dummy-gid"]).encode("utf-8")
        ).decode("ascii")

        response = client.get(
            "/aria2/jsonrpc",
            params={
                "jsonrpc": "2.0",
                "method": "aria2.pause",
                "id": "get-b64-1",
                "params": encoded_params,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "get-b64-1"
        assert data["result"] == "dummy-gid"

    def test_uses_refreshed_aria2_client_config(self, client: TestClient, rpc_user: dict, monkeypatch):
        from app.aria2.client import Aria2Client

        app_state = client.app.state.app_state
        app_state._cached_rpc_url = "http://new-rpc:6800/jsonrpc"
        app_state._cached_rpc_secret = "new-secret"
        client.app.state.aria2_client = Aria2Client("http://old-rpc:6800/jsonrpc", "old-secret")

        async def fake_get_version(self):
            return {"rpc_url": self._rpc_url, "secret": self._secret}

        monkeypatch.setattr(Aria2Client, "get_version", fake_get_version)

        response = client.post(
            "/aria2/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": [f"token:{rpc_user['rpc_secret']}"],
                "id": "cfg-sync-1",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["rpc_url"] == "http://new-rpc:6800/jsonrpc"
        assert data["result"]["secret"] == "new-secret"
        assert client.app.state.aria2_client._rpc_url == "http://new-rpc:6800/jsonrpc"

    def test_get_query_invalid_params_encoding(self, client: TestClient):
        response = client.get(
            "/aria2/jsonrpc",
            params={
                "jsonrpc": "2.0",
                "method": "aria2.pause",
                "id": "get-invalid-1",
                "params": "%%%",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == -32602

    def test_cors_preflight_rejects_null_origin(self, client: TestClient):
        response = client.options(
            "/aria2/jsonrpc",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
            },
        )
        # "null" origin should not be allowed (CSRF vector)
        assert response.headers.get("access-control-allow-origin") != "null"

    def test_cors_get_allows_ariang_origin(self, client: TestClient, rpc_user: dict):
        response = client.get(
            "/aria2/jsonrpc",
            params={
                "jsonrpc": "2.0",
                "method": "aria2.pause",
                "id": "cors-get-1",
                "params": json.dumps([f"token:{rpc_user['rpc_secret']}", "dummy-gid"]),
            },
            headers={"Origin": "https://ariang.mayswind.net"},
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "https://ariang.mayswind.net"


@pytest.mark.asyncio
class TestGetUserByRpcSecret:

    async def test_get_user_valid_secret(self, temp_db: str, rpc_user: dict):
        from app.routers.aria2_rpc import get_user_by_rpc_secret
        user = await get_user_by_rpc_secret(rpc_user["rpc_secret"])
        assert user is not None
        assert user["username"] == "rpcuser"

    async def test_get_user_invalid_secret(self, temp_db: str):
        from app.routers.aria2_rpc import get_user_by_rpc_secret
        user = await get_user_by_rpc_secret("nonexistent_secret")
        assert user is None

    async def test_get_user_duplicate_secret_returns_none(self, temp_db: str):
        from app.core.security import hash_password
        from datetime import datetime, timezone
        from app.routers.aria2_rpc import get_user_by_rpc_secret

        duplicate_secret = "dup_secret_for_rpc_test"
        execute(
            """
            INSERT INTO users (username, password_hash, is_admin, created_at, quota, rpc_secret)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["dup_user_1", hash_password("p1"), 0, datetime.now(timezone.utc).isoformat(), 10 * 1024**3, duplicate_secret],
        )
        execute(
            """
            INSERT INTO users (username, password_hash, is_admin, created_at, quota, rpc_secret)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["dup_user_2", hash_password("p2"), 0, datetime.now(timezone.utc).isoformat(), 10 * 1024**3, duplicate_secret],
        )

        user = await get_user_by_rpc_secret(duplicate_secret)
        assert user is None
