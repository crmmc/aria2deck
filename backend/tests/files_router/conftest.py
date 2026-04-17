from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


@pytest.fixture
def user_file(test_user: dict, temp_db: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    real_path = Path(settings.download_dir) / "store" / "hash123.bin"
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_bytes(b"x" * 1024)

    stored_file_id = execute(
        """
        INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["hash123", str(real_path), 1024, 0, 1, "test_file.txt", now],
    )
    user_file_id = execute(
        """
        INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [test_user["id"], stored_file_id, "test_file.txt", now],
    )
    return {
        "id": user_file_id,
        "content_hash": "hash123",
        "stored_file_id": stored_file_id,
        "display_name": "test_file.txt",
        "size": 1024,
        "real_path": str(real_path),
    }


@pytest.fixture
def user_directory(test_user: dict, temp_db: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    real_path = Path(settings.download_dir) / "store" / "dirhash456"
    real_path.mkdir(parents=True, exist_ok=True)

    stored_file_id = execute(
        """
        INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["dirhash456", str(real_path), 0, 1, 1, "test_folder", now],
    )
    user_file_id = execute(
        """
        INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [test_user["id"], stored_file_id, "test_folder", now],
    )
    return {
        "id": user_file_id,
        "content_hash": "dirhash456",
        "stored_file_id": stored_file_id,
        "display_name": "test_folder",
        "is_directory": True,
        "real_path": str(real_path),
    }
