import threading

import pytest
from fastapi import HTTPException

from app.core.rate_limit_config import rate_limit_config
from app.core.request_rate_guard import ensure_share_access_allowed
from app.services import share_service


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


class TestShareCreationRateLimit:
    def test_create_limit_runs_before_password_hash(
        self, authenticated_client, user_file, monkeypatch
    ):
        original_limit = rate_limit_config.create_share
        rate_limit_config.create_share = 1
        hash_calls: list[str] = []
        monkeypatch.setattr(
            share_service,
            "hash_password",
            lambda password: hash_calls.append(password) or "unused",
        )
        try:
            first = authenticated_client.post(
                "/api/shares",
                json={"user_file_id": user_file["id"]},
            )
            second = authenticated_client.post(
                "/api/shares",
                json={"user_file_id": user_file["id"], "password": "secret"},
            )
        finally:
            rate_limit_config.create_share = original_limit

        assert first.status_code == 201
        assert second.status_code == 429
        assert second.json()["detail"] == "创建分享过于频繁，请稍后再试"
        assert hash_calls == []

    @pytest.mark.asyncio
    async def test_password_hash_runs_off_event_loop_thread(
        self, user_file, monkeypatch
    ):
        event_loop_thread = threading.get_ident()
        hash_threads: list[int] = []

        def fake_hash(_password: str) -> str:
            hash_threads.append(threading.get_ident())
            return "encoded-password"

        monkeypatch.setattr(share_service, "hash_password", fake_hash)

        result = await share_service.create_share(
            user_id=1,
            user_file_id=user_file["id"],
            password="secret",
            expires_in=None,
            max_downloads=None,
        )

        assert result["has_password"] is True
        assert hash_threads and hash_threads[0] != event_loop_thread

        verify_threads: list[int] = []

        def fake_verify(_password: str, _encoded: str) -> bool:
            verify_threads.append(threading.get_ident())
            return True

        monkeypatch.setattr(share_service, "verify_password", fake_verify)
        access = await share_service.access_share(result["share_code"], "secret")

        assert access["access_token"]
        assert verify_threads and verify_threads[0] != event_loop_thread

