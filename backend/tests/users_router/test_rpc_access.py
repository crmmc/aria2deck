from fastapi.testclient import TestClient


class TestRpcAccess:
    def test_get_rpc_access_disabled(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/users/me/rpc-access")
        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert "secret" not in response.json()

    def test_enable_rpc_access(self, authenticated_client: TestClient):
        response = authenticated_client.put("/api/users/me/rpc-access", json={"enabled": True})
        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert response.json()["secret"].startswith("aria2_")

    def test_disable_rpc_access(self, authenticated_client: TestClient):
        authenticated_client.put("/api/users/me/rpc-access", json={"enabled": True})
        response = authenticated_client.put("/api/users/me/rpc-access", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert "secret" not in response.json()

    def test_refresh_rpc_secret(self, authenticated_client: TestClient):
        enable_response = authenticated_client.put("/api/users/me/rpc-access", json={"enabled": True})
        old_secret = enable_response.json()["secret"]

        response = authenticated_client.post("/api/users/me/rpc-access/refresh")
        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert response.json()["secret"] != old_secret
        assert response.json()["secret"].startswith("aria2_")

    def test_refresh_rpc_secret_not_enabled(self, authenticated_client: TestClient):
        response = authenticated_client.post("/api/users/me/rpc-access/refresh")
        assert response.status_code == 400
        assert "未开启" in response.json()["detail"]

    def test_rpc_access_unauthorized(self, client: TestClient, temp_db: str, test_user: dict):
        response = client.get("/api/users/me/rpc-access")
        assert response.status_code == 401
