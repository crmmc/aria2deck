"""Tests for file sharing feature."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.core.download_limiter import download_config
from app.core.rate_limit_config import rate_limit_config
from app.db.engine import transaction
from app.db.schema import share_links
from app.domain.errors import BadRequestError
from app.domain.shares import MAX_ACTIVE_SHARES_PER_FILE
from app.repositories import shares as shares_repo
from app.services import share_service
from tests.helpers_v0 import now_ms


def _insert_share_v0(
    *,
    share_code: str,
    owner_id: int,
    user_file_id: int,
    status: str = "active",
    expires_at_ms: int | None = None,
    max_downloads: int | None = None,
    download_count: int = 0,
) -> int:
    async def seed() -> int:
        async with transaction() as conn:
            row = (
                await conn.execute(
                    insert(share_links)
                    .values(
                        share_code=share_code,
                        owner_id=owner_id,
                        user_file_id=user_file_id,
                        expires_at_ms=expires_at_ms,
                        max_downloads=max_downloads,
                        download_count=download_count,
                        status=status,
                        created_at_ms=now_ms(),
                    )
                    .returning(share_links.c.id)
                )
            ).one()
        return int(row[0])

    return asyncio.run(seed())


def _create_share(client: TestClient, user_file_id: int, **kwargs) -> dict:
    """Helper to create a share and return response JSON."""
    body = {"user_file_id": user_file_id, **kwargs}
    resp = client.post("/api/shares", json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _share_download_count(share_code: str) -> int:
    async def load() -> int:
        async with transaction() as conn:
            row = (
                await conn.execute(
                    select(share_links.c.download_count).where(
                        share_links.c.share_code == share_code
                    )
                )
            ).one()
        return int(row[0])

    return asyncio.run(load())


def _allow_anonymous_downloads(monkeypatch) -> None:
    monkeypatch.setattr(download_config, "total_connections", 10)
    monkeypatch.setattr(download_config, "anonymous_base_connections", 10)
    monkeypatch.setattr(download_config, "anonymous_borrow_connections", 0)
    monkeypatch.setattr(download_config, "anonymous_per_ip_connections", 10)
    monkeypatch.setattr(download_config, "anonymous_per_file_connections", 10)


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
    def test_create_share_ignores_expired_and_exhausted_in_limit(self, authenticated_client, user_file, temp_db):
        # 先造 10 条“status=active 但实际失效”的分享（过期 + 次数耗尽）
        expired_at_ms = now_ms() - 60 * 60 * 1000
        for i in range(MAX_ACTIVE_SHARES_PER_FILE):
            _insert_share_v0(
                share_code=f"expired{i}",
                owner_id=1,
                user_file_id=user_file["id"],
                expires_at_ms=expired_at_ms,
                max_downloads=1,
                download_count=1,
            )

        # 应该还能创建，因为前面的都不算“活跃有效”
        data = _create_share(authenticated_client, user_file["id"])
        assert data["share_code"]

    def test_create_share_blocked_when_effective_active_reaches_limit(self, authenticated_client, user_file, temp_db):
        # 造满 10 条真正有效的 active 分享
        for i in range(MAX_ACTIVE_SHARES_PER_FILE):
            _insert_share_v0(
                share_code=f"active{i}",
                owner_id=1,
                user_file_id=user_file["id"],
            )

        resp = authenticated_client.post("/api/shares", json={"user_file_id": user_file["id"]})
        assert resp.status_code == 400
        assert "最多" in resp.json()["detail"]

    def test_concurrent_create_share_does_not_exceed_effective_limit(
        self, user_file, temp_db
    ):
        async def create_all():
            return await asyncio.gather(
                *(
                    share_service.create_share(
                        user_id=1,
                        user_file_id=user_file["id"],
                        password=None,
                        expires_in=None,
                        max_downloads=None,
                    )
                    for _ in range(MAX_ACTIVE_SHARES_PER_FILE + 2)
                ),
                return_exceptions=True,
            )

        results = asyncio.run(create_all())

        assert sum(isinstance(result, dict) for result in results) == MAX_ACTIVE_SHARES_PER_FILE
        assert sum(isinstance(result, BadRequestError) for result in results) == 2

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

    def test_full_download_increments_download_count_once(
        self, authenticated_client, client, user_file, monkeypatch
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        resp = client.get(f"/api/s/{share['share_code']}/download")

        assert resp.status_code == 200
        assert len(resp.content) == 1024
        assert _share_download_count(share["share_code"]) == 1

        exhausted = client.get(f"/api/s/{share['share_code']}/download")
        assert exhausted.status_code == 410

    def test_concurrent_download_count_is_consumed_once(
        self, authenticated_client, user_file
    ):
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        async def consume_twice() -> tuple[bool, bool]:
            return await asyncio.gather(
                shares_repo.consume_share_download(share["id"], timestamp_ms=now_ms()),
                shares_repo.consume_share_download(share["id"], timestamp_ms=now_ms()),
            )

        assert sorted(asyncio.run(consume_twice())) == [False, True]
        assert _share_download_count(share["share_code"]) == 1

    def test_range_download_increments_download_count_once(
        self, authenticated_client, client, user_file, monkeypatch
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        range_resp = client.get(
            f"/api/s/{share['share_code']}/download",
            headers={"Range": "BYTES=0-9"},
        )

        assert range_resp.status_code == 206
        assert range_resp.content == b"x" * 10
        assert _share_download_count(share["share_code"]) == 1

        exhausted = client.get(f"/api/s/{share['share_code']}/download")
        assert exhausted.status_code == 410

    @pytest.mark.parametrize(
        "range_header",
        ["invalid", "bytes=2000-3000", "bytes=0-1-2", "bytes=-0"],
    )
    def test_invalid_range_does_not_increment_download_count(
        self,
        authenticated_client,
        client,
        user_file,
        monkeypatch,
        range_header,
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        invalid = client.get(
            f"/api/s/{share['share_code']}/download",
            headers={"Range": range_header},
        )

        assert invalid.status_code == 416
        assert invalid.headers["Content-Range"] == "bytes */1024"
        assert _share_download_count(share["share_code"]) == 0

        valid = client.get(
            f"/api/s/{share['share_code']}/download",
            headers={"Range": "bytes=0-0"},
        )
        assert valid.status_code == 206
        assert _share_download_count(share["share_code"]) == 1

    @pytest.mark.parametrize("range_header", ["items=0-9", "bytes=0-9,20-29"])
    def test_ignored_range_counts_as_full_download(
        self,
        authenticated_client,
        client,
        user_file,
        monkeypatch,
        range_header,
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        response = client.get(
            f"/api/s/{share['share_code']}/download",
            headers={"Range": range_header},
        )

        assert response.status_code == 200
        assert len(response.content) == 1024
        assert "Content-Range" not in response.headers
        assert _share_download_count(share["share_code"]) == 1

        exhausted = client.get(f"/api/s/{share['share_code']}/download")
        assert exhausted.status_code == 410

    def test_invalid_target_does_not_increment_download_count(
        self, authenticated_client, client, user_file, monkeypatch
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        invalid = client.get(
            f"/api/s/{share['share_code']}/download",
            params={"subpath": "other.bin"},
        )

        assert invalid.status_code == 400
        assert _share_download_count(share["share_code"]) == 0

    def test_range_downloads_are_not_limited_by_public_api_rate_limit(
        self, authenticated_client, client, user_file, monkeypatch
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=2)
        original_limit = rate_limit_config.public_api
        rate_limit_config.public_api = 1
        try:
            first = client.get(
                f"/api/s/{share['share_code']}/download",
                headers={"Range": "bytes=0-9"},
            )
            second = client.get(
                f"/api/s/{share['share_code']}/download",
                headers={"Range": "bytes=10-19"},
            )
        finally:
            rate_limit_config.public_api = original_limit

        assert first.status_code == 206
        assert first.content == b"x" * 10
        assert second.status_code == 206
        assert second.content == b"x" * 10
        assert _share_download_count(share["share_code"]) == 2

    def test_empty_range_header_counts_as_full_download(
        self, authenticated_client, client, user_file, monkeypatch
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        resp = client.get(
            f"/api/s/{share['share_code']}/download",
            headers={"Range": ""},
        )

        assert resp.status_code == 200
        assert len(resp.content) == 1024
        assert _share_download_count(share["share_code"]) == 1

        exhausted = client.get(f"/api/s/{share['share_code']}/download")
        assert exhausted.status_code == 410

    def test_exhausted_share_rejects_range_download(
        self, authenticated_client, client, user_file, monkeypatch
    ):
        _allow_anonymous_downloads(monkeypatch)
        share = _create_share(authenticated_client, user_file["id"], max_downloads=1)

        full_resp = client.get(f"/api/s/{share['share_code']}/download")
        assert full_resp.status_code == 200
        assert _share_download_count(share["share_code"]) == 1

        range_resp = client.get(
            f"/api/s/{share['share_code']}/download",
            headers={"Range": "bytes=0-9"},
        )
        assert range_resp.status_code == 410
class TestDeleteProtection:
    @staticmethod
    def assert_pending(resp) -> None:
        assert resp.status_code == 202
        assert resp.json() == {
            "ok": True,
            "state": "pending",
            "accepted": True,
        }

    def test_delete_revokes_active_share(self, authenticated_client, user_file):
        share = _create_share(authenticated_client, user_file["id"])
        self.assert_pending(
            authenticated_client.delete("/api/files/hash_testfile")
        )

        shared = authenticated_client.get(f"/api/s/{share['share_code']}")
        assert shared.status_code == 404

    def test_delete_allowed_after_revoke(self, authenticated_client, user_file):
        share = _create_share(authenticated_client, user_file["id"])
        authenticated_client.put(f"/api/shares/{share['id']}/revoke")
        self.assert_pending(
            authenticated_client.delete("/api/files/hash_testfile")
        )

    def test_delete_allowed_after_share_downloads_exhausted(
        self, authenticated_client, user_file
    ):
        _insert_share_v0(
            share_code="exhausted",
            owner_id=1,
            user_file_id=user_file["id"],
            max_downloads=1,
            download_count=1,
        )
        self.assert_pending(
            authenticated_client.delete("/api/files/hash_testfile")
        )

    def test_delete_allowed_no_shares(self, authenticated_client, user_file):
        self.assert_pending(
            authenticated_client.delete("/api/files/hash_testfile")
        )
