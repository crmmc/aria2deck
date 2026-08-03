from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.main import lifespan


@pytest.mark.asyncio
async def test_startup_failure_releases_singleton_lease(temp_db):
    lease = MagicMock()

    with (
        patch(
            "app.services.singleton_lease.ApplicationSingletonLease.acquire",
            return_value=lease,
        ),
        patch(
            "app.main.verify_download_dir_writable",
            side_effect=RuntimeError("download directory unavailable"),
        ),
        patch("app.main.Aria2Client.close_session", new=AsyncMock()),
        patch("app.main.dispose_engine", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="download directory unavailable"):
            async with lifespan(FastAPI()):
                pass

    lease.release.assert_called_once()
