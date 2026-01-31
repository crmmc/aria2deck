"""Test race condition handling for user creation."""
import asyncio
import os
import tempfile

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlmodel import select
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db
from app.models import User
from app.routers.users import create_user
from app.schemas import UserCreate


@pytest.fixture(scope="function")
def temp_db_user_race():
    """Create a fresh temporary database for user race tests."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    download_dir = os.path.join(temp_dir, "downloads")
    os.makedirs(download_dir, exist_ok=True)

    original_db_path = settings.database_path
    original_download_dir = settings.download_dir
    settings.database_path = db_path
    settings.download_dir = download_dir

    reset_engine()
    init_db()
    asyncio.run(init_sqlmodel_db())

    yield {
        "db_path": db_path,
        "download_dir": download_dir,
        "temp_dir": temp_dir,
    }

    asyncio.run(dispose_engine())
    settings.database_path = original_db_path
    settings.download_dir = original_download_dir
    reset_engine()


class TestConcurrentFirstUserCreation:
    """Test concurrent first-user creation."""

    @pytest.mark.asyncio
    async def test_only_one_first_user_created(self, temp_db_user_race):
        """Only one concurrent first-user creation should succeed."""
        payload1 = UserCreate(username="firstuser1", password="hash1", is_admin=True)
        payload2 = UserCreate(username="firstuser2", password="hash2", is_admin=True)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/users",
                "headers": [],
                "client": ("test", 1234),
            }
        )

        with patch("app.routers.users._has_any_user", new=AsyncMock(return_value=False)):
            results = await asyncio.gather(
                create_user(payload1, request),
                create_user(payload2, request),
                return_exceptions=True,
            )

        successes = [r for r in results if isinstance(r, dict)]
        errors = [r for r in results if isinstance(r, HTTPException)]

        assert len(successes) == 1
        assert len(errors) == 1
        assert errors[0].status_code == 403

        async with get_session() as db:
            result = await db.exec(select(User))
            users = result.all()
            assert len(users) == 1
