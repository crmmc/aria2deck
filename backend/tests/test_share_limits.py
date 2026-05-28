import pytest
from fastapi import HTTPException

from app.core.rate_limit_config import rate_limit_config
from app.core.request_rate_guard import ensure_share_access_allowed


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

