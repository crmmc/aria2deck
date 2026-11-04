from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.helpers_v0 import create_user_file_v0


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


@pytest.fixture
def user_file(test_user: dict, temp_db: str) -> dict:
    import asyncio

    real_path = Path(settings.download_dir) / "store" / "hash123.bin"
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_bytes(b"x" * 1024)
    return asyncio.run(
        create_user_file_v0(
            user_id=test_user["id"],
            real_path=real_path,
            content_hash="hash123",
            display_name="test_file.txt",
            size_bytes=1024,
        )
    )


@pytest.fixture
def user_directory(test_user: dict, temp_db: str) -> dict:
    import asyncio

    real_path = Path(settings.download_dir) / "store" / "dirhash456"
    real_path.mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        create_user_file_v0(
            user_id=test_user["id"],
            real_path=real_path,
            content_hash="dirhash456",
            display_name="test_folder",
            size_bytes=0,
            is_directory=True,
        )
    )
