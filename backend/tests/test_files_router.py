"""Tests for files router endpoints."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from app.core.config import settings
from app.db import execute


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


@pytest.fixture
def user_file(test_user: dict, temp_db: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    stored_file_id = execute(
        """
        INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["hash123", "/downloads/store/hash123", 1024, 0, 1, "test_file.txt", now]
    )
    user_file_id = execute(
        """
        INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [test_user["id"], stored_file_id, "test_file.txt", now]
    )
    return {
        "id": user_file_id,
        "content_hash": "hash123",
        "stored_file_id": stored_file_id,
        "display_name": "test_file.txt",
        "size": 1024
    }


@pytest.fixture
def user_directory(test_user: dict, temp_db: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    stored_file_id = execute(
        """
        INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["dirhash456", "/downloads/store/dirhash456", 0, 1, 1, "test_folder", now]
    )
    user_file_id = execute(
        """
        INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [test_user["id"], stored_file_id, "test_folder", now]
    )
    return {
        "id": user_file_id,
        "content_hash": "dirhash456",
        "stored_file_id": stored_file_id,
        "display_name": "test_folder",
        "is_directory": True
    }


class TestListFiles:

    def test_list_files_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        data = response.json()
        assert "files" in data
        assert "space" in data
        assert data["files"] == []

    def test_list_files_with_file(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        data = response.json()
        assert len(data["files"]) == 1
        assert data["files"][0]["name"] == "test_file.txt"
        assert data["files"][0]["size"] == 1024

    def test_list_files_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files")
        assert response.status_code == 401


class TestGetSpace:

    def test_get_space(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/space")
        assert response.status_code == 200
        data = response.json()
        assert "used" in data
        assert "frozen" in data
        assert "available" in data
        assert "quota" in data

    def test_get_space_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/space")
        assert response.status_code == 401


class TestGetQuota:

    def test_get_quota(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/quota")
        assert response.status_code == 200
        data = response.json()
        assert "quota" in data or "used" in data or "available" in data

    def test_get_quota_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/quota")
        assert response.status_code == 401


class TestDeleteFile:

    def test_delete_file_success(self, authenticated_client: TestClient, user_file: dict):
        with patch("app.services.storage.delete_user_file_reference", new_callable=AsyncMock, return_value=True):
            response = authenticated_client.delete(f"/api/files/{user_file['content_hash']}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_delete_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/files/nonexistent_hash")
        assert response.status_code == 404

    def test_delete_file_unauthorized(self, client: TestClient, temp_db: str, user_file: dict):
        response = client.delete(f"/api/files/{user_file['content_hash']}")
        assert response.status_code == 401


class TestRenameFile:

    def test_rename_file_success(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "new_name.txt"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("name") == "new_name.txt" or data.get("display_name") == "new_name.txt" or data.get("ok") is True

    def test_rename_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.put(
            "/api/files/nonexistent_hash/rename",
            json={"name": "new_name.txt"}
        )
        assert response.status_code == 404

    def test_rename_file_empty_name(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": ""}
        )
        assert response.status_code == 422

    def test_rename_file_unauthorized(self, client: TestClient, temp_db: str, user_file: dict):
        response = client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "new_name.txt"}
        )
        assert response.status_code == 401


class TestPackTasks:

    def test_list_pack_tasks_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_pack_tasks_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/pack")
        assert response.status_code == 401


class TestCalculateSize:

    def test_calculate_size_with_file_ids(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": [user_file["id"]]}
        )
        assert response.status_code == 200
        data = response.json()
        assert "total_size" in data
        assert data["total_size"] == user_file["size"]

    def test_calculate_size_unauthorized(self, client: TestClient, temp_db: str):
        response = client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": [1]}
        )
        assert response.status_code == 401


class TestAvailableSpace:

    def test_get_available_space(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack/available-space")
        assert response.status_code == 200
        data = response.json()
        assert "available" in data
        assert "quota" in data
        assert "used" in data

    def test_get_available_space_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/pack/available-space")
        assert response.status_code == 401


class TestBrowseFile:
    """Tests for browsing directory contents."""

    def test_browse_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/nonexistent_hash/browse")
        assert response.status_code == 404

    def test_browse_file_not_directory(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.get(f"/api/files/{user_file['content_hash']}/browse")
        assert response.status_code == 400

    def test_browse_file_unauthorized(self, client: TestClient, temp_db: str, user_directory: dict):
        response = client.get(f"/api/files/{user_directory['content_hash']}/browse")
        assert response.status_code == 401


class TestDownloadFile:
    """Tests for file download endpoint."""

    def test_download_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/nonexistent_hash/download")
        assert response.status_code == 404

    def test_download_file_unauthorized(self, client: TestClient, temp_db: str, user_file: dict):
        response = client.get(f"/api/files/{user_file['content_hash']}/download")
        assert response.status_code == 401

    def test_download_file_path_traversal(self, authenticated_client: TestClient, user_directory: dict):
        response = authenticated_client.get(
            f"/api/files/{user_directory['content_hash']}/download?path=../../../etc/passwd"
        )
        assert response.status_code in [400, 403, 404]


class TestPackTaskOperations:
    """Tests for pack task CRUD operations."""

    def test_get_pack_task_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack/99999")
        assert response.status_code == 404

    def test_delete_pack_task_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/files/pack/99999")
        assert response.status_code == 404

    def test_create_pack_task_nonexistent_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [99999], "output_name": "test.7z"}
        )
        assert response.status_code == 404

    def test_create_pack_task_empty_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [], "output_name": "test.7z"}
        )
        assert response.status_code == 422


class TestFileListWithSpace:
    """Tests for file list with space information."""

    def test_list_files_includes_space_info(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        data = response.json()
        assert "space" in data
        space = data["space"]
        assert "used" in space
        assert "frozen" in space
        assert "available" in space

    def test_list_files_with_multiple_files(
        self, authenticated_client: TestClient, user_file: dict, user_directory: dict
    ):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        data = response.json()
        assert len(data["files"]) == 2


class TestRenameFileValidation:
    """Tests for file rename validation."""

    def test_rename_file_with_path_separator(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "path/to/file.txt"}
        )
        assert response.status_code in [200, 400]

    def test_rename_file_with_special_chars(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "file<>:\"|?*.txt"}
        )
        assert response.status_code in [200, 400]

    def test_rename_file_whitespace_only(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "   "}
        )
        assert response.status_code in [200, 400]


class TestPackTaskCreate:

    def test_create_pack_task_nonexistent_file_ids(self, authenticated_client: TestClient, temp_db: str):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [99999], "output_name": "test.7z"}
        )
        assert response.status_code == 404

    def test_create_pack_task_no_file_ids_provided(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={}
        )
        assert response.status_code in [400, 422]

    def test_create_pack_task_empty_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [], "output_name": "test.7z"}
        )
        assert response.status_code == 422

    def test_create_pack_task_default_output_name_uses_display_name(
        self,
        authenticated_client: TestClient,
        user_file: dict,
    ):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [user_file["id"]]},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["output_name"] == "test_file"

    def test_create_pack_task_rejects_too_long_output_name(
        self,
        authenticated_client: TestClient,
        user_file: dict,
    ):
        long_name = "a" * 201

        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [user_file["id"]], "output_name": long_name},
        )

        assert response.status_code == 400
        assert "200" in response.json()["detail"]

    def test_create_pack_task_rejects_invalid_output_name_chars(
        self,
        authenticated_client: TestClient,
        user_file: dict,
    ):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"file_ids": [user_file["id"]], "output_name": 'bad:name'},
        )

        assert response.status_code == 400
        assert "非法字符" in response.json()["detail"]


class TestBrowseDirectory:

    def test_browse_directory_path_traversal(self, authenticated_client: TestClient, user_directory: dict):
        response = authenticated_client.get(
            f"/api/files/{user_directory['content_hash']}/browse?path=../../../etc"
        )
        assert response.status_code in [400, 403, 404]


class TestCalculateSizeEndpoint:

    def test_calculate_size_empty_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": []}
        )
        # Empty list now returns 422 due to min_length=1 validation
        assert response.status_code == 422

    def test_calculate_size_nonexistent_file_ids(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"file_ids": [99999]}
        )
        assert response.status_code == 404


class TestBrowseDirectoryWithRealFiles:

    def test_browse_directory_base_not_exists(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["browse_nonexistent_base", "/nonexistent/path", 0, 1, 1, "nonexistent", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "nonexistent", now]
        )

        response = authenticated_client.get(f"/api/files/browse_nonexistent_base/browse")
        assert response.status_code == 404


class TestDownloadFileWithRealFiles:

    def test_download_file_base_not_exists(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["download_nonexistent_base", "/nonexistent/file.txt", 100, 0, 1, "file.txt", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "file.txt", now]
        )

        response = authenticated_client.get("/api/files/download_nonexistent_base/download")
        assert response.status_code == 404

    def test_download_file_path_on_non_directory(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "test_single_file.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("single file content")

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["single_file_hash", str(test_file), 19, 0, 1, "test_single_file.txt", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "test_single_file.txt", now]
        )

        response = authenticated_client.get("/api/files/single_file_hash/download?path=subpath")
        assert response.status_code == 400


class TestBrowseDirectoryEdgeCases:

    def test_browse_directory_subpath_not_exists(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_dir = Path(settings.download_dir) / "store" / "browse_test_dir"
        test_dir.mkdir(parents=True, exist_ok=True)

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["browse_subpath_test", str(test_dir), 0, 1, 1, "browse_test_dir", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "browse_test_dir", now]
        )

        response = authenticated_client.get(f"/api/files/browse_subpath_test/browse?path=nonexistent")
        assert response.status_code in [403, 404]

    def test_browse_directory_subpath_is_file(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_dir = Path(settings.download_dir) / "store" / "browse_file_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "file.txt").write_text("content")

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["browse_file_test", str(test_dir), 0, 1, 1, "browse_file_test", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "browse_file_test", now]
        )

        response = authenticated_client.get("/api/files/browse_file_test/browse?path=file.txt")
        assert response.status_code in [400, 403]

    def test_browse_directory_with_contents(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_dir = Path(settings.download_dir) / "store" / "browse_contents_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "file1.txt").write_text("content1")
        (test_dir / "file2.txt").write_text("content2")
        (test_dir / "subdir").mkdir(exist_ok=True)

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["browse_contents_test", str(test_dir), 0, 1, 1, "browse_contents_test", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "browse_contents_test", now]
        )

        response = authenticated_client.get(f"/api/files/browse_contents_test/browse")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3

    def test_browse_directory_path_traversal_blocked(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_dir = Path(settings.download_dir) / "store" / "browse_traversal_test"
        test_dir.mkdir(parents=True, exist_ok=True)

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["browse_traversal_test", str(test_dir), 0, 1, 1, "browse_traversal_test", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "browse_traversal_test", now]
        )

        response = authenticated_client.get(f"/api/files/browse_traversal_test/browse?path=../../../etc")
        assert response.status_code == 403


class TestDownloadFileRange:

    def test_download_no_range_returns_accept_ranges(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_100bytes.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_100", str(test_file), 100, 0, 1, "range_test_100bytes.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_100bytes.bin", now]
        )

        response = authenticated_client.get(f"/api/files/range_test_100/download")
        assert response.status_code == 200
        assert "Accept-Ranges" in response.headers
        assert response.headers["Accept-Ranges"] == "bytes"

    def test_download_range_start_end(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_start_end.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_start_end", str(test_file), 100, 0, 1, "range_test_start_end.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_start_end.bin", now]
        )

        response = authenticated_client.get(
            f"/api/files/range_test_start_end/download",
            headers={"Range": "bytes=0-9"}
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == "bytes 0-9/100"
        assert response.headers["Content-Length"] == "10"
        assert response.content == bytes(range(10))

    def test_download_range_start_only(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_start_only.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_start_only", str(test_file), 100, 0, 1, "range_test_start_only.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_start_only.bin", now]
        )

        response = authenticated_client.get(
            f"/api/files/range_test_start_only/download",
            headers={"Range": "bytes=90-"}
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == "bytes 90-99/100"
        assert response.headers["Content-Length"] == "10"
        assert response.content == bytes(range(90, 100))

    def test_download_range_suffix(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_suffix.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_suffix", str(test_file), 100, 0, 1, "range_test_suffix.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_suffix.bin", now]
        )

        response = authenticated_client.get(
            f"/api/files/range_test_suffix/download",
            headers={"Range": "bytes=-20"}
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == "bytes 80-99/100"
        assert response.headers["Content-Length"] == "20"
        assert response.content == bytes(range(80, 100))

    def test_download_range_invalid_format(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_invalid.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_invalid", str(test_file), 100, 0, 1, "range_test_invalid.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_invalid.bin", now]
        )

        response = authenticated_client.get(
            f"/api/files/range_test_invalid/download",
            headers={"Range": "invalid"}
        )
        assert response.status_code == 416

    def test_download_range_out_of_bounds(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_oob.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_oob", str(test_file), 100, 0, 1, "range_test_oob.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_oob.bin", now]
        )

        response = authenticated_client.get(
            f"/api/files/range_test_oob/download",
            headers={"Range": "bytes=200-300"}
        )
        assert response.status_code == 416

    def test_download_range_end_exceeds_size(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        from pathlib import Path
        from app.db import execute
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        test_file = Path(settings.download_dir) / "store" / "range_test_exceed.bin"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(bytes(range(100)))

        stored_file_id = execute(
            """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ["range_test_exceed", str(test_file), 100, 0, 1, "range_test_exceed.bin", now]
        )
        user_file_id = execute(
            """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            [test_user["id"], stored_file_id, "range_test_exceed.bin", now]
        )

        response = authenticated_client.get(
            f"/api/files/range_test_exceed/download",
            headers={"Range": "bytes=90-200"}
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == "bytes 90-99/100"
        assert response.headers["Content-Length"] == "10"
        assert response.content == bytes(range(90, 100))
