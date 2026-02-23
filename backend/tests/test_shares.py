"""Tests for file sharing feature."""
import pytest
from fastapi.testclient import TestClient
from app.core.config import settings
from app.db import execute
from app.main import app


def _create_share(client: TestClient, user_file_id: int, **kwargs) -> dict:
    """Helper to create a share and return response JSON."""
    body = {"user_file_id": user_file_id, **kwargs}
    resp = client.post("/api/shares", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreateShare:
    def test_basic(self, authenticated_client, user_file):
        data = _create_share(authenticated_client, user_file["id"])
        assert data["share_code"]
        assert data["status"] == "active"
        assert data["has_password"] is False
        assert data["download_count"] == 0
        assert data["file_name"] == "testfile.bin"
        assert data["file_size"] == 1024

    def test_with_password(self, authenticated_client, user_file):
        data = _create_share(authenticated_client, user_file["id"], password="secret123")
        assert data["has_password"] is True

    def test_with_expiry(self, authenticated_client, user_file):
        data = _create_share(authenticated_client, user_file["id"], expires_in=3600)
        assert data["expires_at"] is not None

    def test_with_max_downloads(self, authenticated_client, user_file):
        data = _create_share(authenticated_client, user_file["id"], max_downloads=5)
        assert data["max_downloads"] == 5

    def test_nonexistent_file(self, authenticated_client, user_file):
        resp = authenticated_client.post("/api/shares", json={"user_file_id": 99999})
        assert resp.status_code == 404

    def test_unauthenticated(self, client, user_file):
        resp = client.post("/api/shares", json={"user_file_id": user_file["id"]})
        assert resp.status_code == 401
class TestShareManagement:
    def test_list_shares(self, authenticated_client, user_file):
        _create_share(authenticated_client, user_file["id"])
        _create_share(authenticated_client, user_file["id"])
        resp = authenticated_client.get("/api/shares")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
    def test_revoke_share(self, authenticated_client, user_file):
        share = _create_share(authenticated_client, user_file["id"])
        resp = authenticated_client.put(f"/api/shares/{share['id']}/revoke")
        assert resp.status_code == 200
        # Verify in list
        shares = authenticated_client.get("/api/shares").json()
        revoked = [s for s in shares if s["id"] == share["id"]]
        assert revoked[0]["status"] == "revoked"
    def test_revoke_already_revoked(self, authenticated_client, user_file):
        share = _create_share(authenticated_client, user_file["id"])
        authenticated_client.put(f"/api/shares/{share['id']}/revoke")
        resp = authenticated_client.put(f"/api/shares/{share['id']}/revoke")
        assert resp.status_code == 400
    def test_delete_share(self, authenticated_client, user_file):
        share = _create_share(authenticated_client, user_file["id"])
        resp = authenticated_client.delete(f"/api/shares/{share['id']}")
        assert resp.status_code == 200
        shares = authenticated_client.get("/api/shares").json()
        assert all(s["id"] != share["id"] for s in shares)
    def test_revoke_all(self, authenticated_client, user_file):
        for _ in range(3):
            _create_share(authenticated_client, user_file["id"])
        resp = authenticated_client.put("/api/shares/revoke-all")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3
        shares = authenticated_client.get("/api/shares").json()
        assert all(s["status"] == "revoked" for s in shares)
class TestPublicShareAccess:
    def test_get_share_info(self, authenticated_client, client, user_file):
        share = _create_share(authenticated_client, user_file["id"])
        # Access without auth
        resp = client.get(f"/api/s/{share['share_code']}")
        assert resp.status_code == 200
        info = resp.json()
        assert info["file_name"] == "testfile.bin"
        assert info["file_size"] == 1024
        assert info["has_password"] is False
        assert info["is_expired"] is False
        assert info["is_exhausted"] is False
    def test_get_share_info_with_password(self, authenticated_client, client, user_file):
        share = _create_share(authenticated_client, user_file["id"], password="secret")
        resp = client.get(f"/api/s/{share['share_code']}")
        assert resp.status_code == 200
        assert resp.json()["has_password"] is True
    def test_get_share_info_not_found(self, client, temp_db):
        resp = client.get("/api/s/nonexist")
        assert resp.status_code == 404
    def test_access_correct_password(self, authenticated_client, client, user_file):
        share = _create_share(authenticated_client, user_file["id"], password="secret")
        resp = client.post(
            f"/api/s/{share['share_code']}/access",
            json={"password": "secret"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()
    def test_access_wrong_password(self, authenticated_client, client, user_file):
        share = _create_share(authenticated_client, user_file["id"], password="secret")
        resp = client.post(
            f"/api/s/{share['share_code']}/access",
            json={"password": "wrong"},
        )
        assert resp.status_code == 403
    def test_access_revoked_share(self, authenticated_client, client, user_file):
        share = _create_share(authenticated_client, user_file["id"], password="secret")
        authenticated_client.put(f"/api/shares/{share['id']}/revoke")
        resp = client.post(
            f"/api/s/{share['share_code']}/access",
            json={"password": "secret"},
        )
        assert resp.status_code == 410
class TestDeleteProtection:
    def test_delete_blocked_by_active_share(self, authenticated_client, user_file):
        """File with active share cannot be deleted."""
        _create_share(authenticated_client, user_file["id"])
        resp = authenticated_client.delete("/api/files/hash_testfile")
        assert resp.status_code == 403
        assert "分享" in resp.json()["detail"]
    def test_delete_allowed_after_revoke(self, authenticated_client, user_file):
        """File can be deleted after all shares are revoked."""
        share = _create_share(authenticated_client, user_file["id"])
        authenticated_client.put(f"/api/shares/{share['id']}/revoke")
        resp = authenticated_client.delete("/api/files/hash_testfile")
        assert resp.status_code == 200
    def test_delete_allowed_no_shares(self, authenticated_client, user_file):
        """File without shares can be deleted normally."""
        resp = authenticated_client.delete("/api/files/hash_testfile")
        assert resp.status_code == 200