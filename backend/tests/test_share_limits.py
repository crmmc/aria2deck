import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.core.config import settings
from app.core.download_limiter import download_config, download_limiter
from app.core.rate_limit_config import rate_limit_config
from app.core.request_rate_guard import ensure_share_access_allowed
from app.db.engine import transaction
from app.db.schema import stored_file_entries
from tests.helpers_v0 import create_user_file_v0


def _create_share(client: TestClient, user_file_id: int, **kwargs) -> dict:
    response = client.post("/api/shares", json={"user_file_id": user_file_id, **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def _create_directory_file(test_user: dict, temp_db: str) -> int:
    del temp_db
    directory = Path(settings.download_dir) / "store" / "shared_dir"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "nested.txt").write_text("shared-content")

    user_file = asyncio.run(
        create_user_file_v0(
            user_id=test_user["id"],
            real_path=directory,
            content_hash="share_dir_hash",
            display_name="shared_dir",
            size_bytes=0,
            is_directory=True,
        )
    )

    async def add_entries() -> None:
        async with transaction() as conn:
            await conn.execute(
                insert(stored_file_entries),
                [
                    {
                        "stored_file_id": user_file["stored_file_id"],
                        "relative_path": ".",
                        "parent_path": "",
                        "name": "shared_dir",
                        "size_bytes": 0,
                        "is_dir": 1,
                        "mtime_ms": None,
                        "sort_key": "\0/shared_dir",
                    },
                    {
                        "stored_file_id": user_file["stored_file_id"],
                        "relative_path": "nested.txt",
                        "parent_path": "",
                        "name": "nested.txt",
                        "size_bytes": len("shared-content"),
                        "is_dir": 0,
                        "mtime_ms": None,
                        "sort_key": "\1nested.txt",
                    },
                ],
            )

    asyncio.run(add_entries())
    return int(user_file["id"])


class TestShareAccessRateLimit:
    @pytest.mark.asyncio
    async def test_share_access_limit_is_tracked_by_share_code(self):
        original_limit = rate_limit_config.share_access
        rate_limit_config.share_access = 1
        try:
            await ensure_share_access_allowed("10.0.0.1", "share-1")
            with pytest.raises(HTTPException) as exc_info:
                await ensure_share_access_allowed("10.0.0.2", "share-1")
        finally:
            rate_limit_config.share_access = original_limit

        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_share_access_limit_is_tracked_by_ip(self):
        original_limit = rate_limit_config.share_access
        rate_limit_config.share_access = 1
        try:
            await ensure_share_access_allowed("10.0.0.1", "share-1")
            with pytest.raises(HTTPException) as exc_info:
                await ensure_share_access_allowed("10.0.0.1", "share-2")
        finally:
            rate_limit_config.share_access = original_limit

        assert exc_info.value.status_code == 429


class TestSharePublicRateLimitBuckets:
    def test_browse_share_does_not_consume_anonymous_download_quota(
        self,
        authenticated_client: TestClient,
        client: TestClient,
        test_user: dict,
        temp_db: str,
    ):
        user_file_id = _create_directory_file(test_user, temp_db)
        share = _create_share(authenticated_client, user_file_id)

        original_public_limit = rate_limit_config.public_api
        original_download_limit = rate_limit_config.anonymous_download
        original_anonymous_base = download_config.anonymous_base_connections
        original_anonymous_borrow = download_config.anonymous_borrow_connections
        original_anonymous_per_ip = download_config.anonymous_per_ip_connections
        original_anonymous_per_file = download_config.anonymous_per_file_connections
        rate_limit_config.public_api = 1
        rate_limit_config.anonymous_download = 1
        download_config.anonymous_base_connections = 1
        download_config.anonymous_borrow_connections = 0
        download_config.anonymous_per_ip_connections = 1
        download_config.anonymous_per_file_connections = 1
        try:
            browse_response = client.get(f"/api/s/{share['share_code']}/browse")
            download_response = client.get(
                f"/api/s/{share['share_code']}/download",
                params={"subpath": "nested.txt"},
            )
            browse_limited = client.get(f"/api/s/{share['share_code']}/browse")
            download_limited = client.get(
                f"/api/s/{share['share_code']}/download",
                params={"subpath": "nested.txt"},
            )
        finally:
            rate_limit_config.public_api = original_public_limit
            rate_limit_config.anonymous_download = original_download_limit
            download_config.anonymous_base_connections = original_anonymous_base
            download_config.anonymous_borrow_connections = original_anonymous_borrow
            download_config.anonymous_per_ip_connections = original_anonymous_per_ip
            download_config.anonymous_per_file_connections = original_anonymous_per_file
            asyncio.run(download_limiter.clear_all())

        assert browse_response.status_code == 200
        assert download_response.status_code == 200
        assert browse_limited.status_code == 429
        assert browse_limited.json()["detail"] == "请求过于频繁"
        assert download_limited.status_code == 429
        assert download_limited.json()["detail"] == "下载请求过于频繁，请稍后再试"
