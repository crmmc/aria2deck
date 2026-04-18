from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.rate_limit_config import rate_limit_config
from app.core.request_rate_guard import ensure_share_access_allowed
from app.db import execute


def _create_share(client: TestClient, user_file_id: int, **kwargs) -> dict:
    response = client.post("/api/shares", json={"user_file_id": user_file_id, **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def _create_directory_file(test_user: dict, temp_db: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    directory = Path(settings.download_dir) / "store" / "shared_dir"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "nested.txt").write_text("shared-content")

    stored_file_id = execute(
        """
        INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["share_dir_hash", str(directory), 0, 1, 1, "shared_dir", now],
    )
    return execute(
        """
        INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [test_user["id"], stored_file_id, "shared_dir", now],
    )


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
        rate_limit_config.public_api = 1
        rate_limit_config.anonymous_download = 1
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

        assert browse_response.status_code == 200
        assert download_response.status_code == 200
        assert browse_limited.status_code == 429
        assert browse_limited.json()["detail"] == "请求过于频繁"
        assert download_limited.status_code == 429
        assert download_limited.json()["detail"] == "下载请求过于频繁，请稍后再试"
