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
            response = authenticated_client.delete(f"/api/files/{user_file['id']}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_delete_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/files/99999")
        assert response.status_code == 404

    def test_delete_file_unauthorized(self, client: TestClient, temp_db: str, user_file: dict):
        response = client.delete(f"/api/files/{user_file['id']}")
        assert response.status_code == 401


class TestRenameFile:

    def test_rename_file_success(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['id']}/rename",
            json={"name": "new_name.txt"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("name") == "new_name.txt" or data.get("display_name") == "new_name.txt" or data.get("ok") is True

    def test_rename_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.put(
            "/api/files/99999/rename",
            json={"name": "new_name.txt"}
        )
        assert response.status_code == 404

    def test_rename_file_empty_name(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['id']}/rename",
            json={"name": ""}
        )
        assert response.status_code == 400

    def test_rename_file_unauthorized(self, client: TestClient, temp_db: str, user_file: dict):
        response = client.put(
            f"/api/files/{user_file['id']}/rename",
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

    def test_calculate_size_with_paths(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"paths": ["/nonexistent/path"]}
        )
        assert response.status_code in [200, 400, 403, 404]

    def test_calculate_size_unauthorized(self, client: TestClient, temp_db: str):
        response = client.post(
            "/api/files/pack/calculate-size",
            json={"paths": ["/some/path"]}
        )
        assert response.status_code == 401


class TestAvailableSpace:

    def test_get_available_space(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack/available-space")
        assert response.status_code == 200
        data = response.json()
        assert "server_available" in data or "user_available" in data or "available_space" in data

    def test_get_available_space_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/pack/available-space")
        assert response.status_code == 401


class TestBrowseFile:
    """Tests for browsing directory contents."""

    def test_browse_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/99999/browse")
        assert response.status_code == 404

    def test_browse_file_not_directory(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.get(f"/api/files/{user_file['id']}/browse")
        assert response.status_code == 400

    def test_browse_file_unauthorized(self, client: TestClient, temp_db: str, user_directory: dict):
        response = client.get(f"/api/files/{user_directory['id']}/browse")
        assert response.status_code == 401


class TestDownloadFile:
    """Tests for file download endpoint."""

    def test_download_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/99999/download")
        assert response.status_code == 404

    def test_download_file_unauthorized(self, client: TestClient, temp_db: str, user_file: dict):
        response = client.get(f"/api/files/{user_file['id']}/download")
        assert response.status_code == 401

    def test_download_file_path_traversal(self, authenticated_client: TestClient, user_directory: dict):
        response = authenticated_client.get(
            f"/api/files/{user_directory['id']}/download?path=../../../etc/passwd"
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

    def test_download_pack_task_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/pack/99999/download")
        assert response.status_code == 404

    def test_create_pack_task_path_traversal(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": "../../../etc"}
        )
        assert response.status_code == 403

    def test_create_pack_task_nonexistent_folder(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": "nonexistent_folder_12345"}
        )
        assert response.status_code == 404


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
            f"/api/files/{user_file['id']}/rename",
            json={"name": "path/to/file.txt"}
        )
        assert response.status_code in [200, 400]

    def test_rename_file_with_special_chars(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['id']}/rename",
            json={"name": "file<>:\"|?*.txt"}
        )
        assert response.status_code in [200, 400]

    def test_rename_file_whitespace_only(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.put(
            f"/api/files/{user_file['id']}/rename",
            json={"name": "   "}
        )
        assert response.status_code in [200, 400]


class TestPackTaskCreate:

    def test_create_pack_task_empty_folder(self, authenticated_client: TestClient, temp_db: str):
        from pathlib import Path
        user_dir = Path(settings.download_dir) / "1"
        user_dir.mkdir(parents=True, exist_ok=True)
        empty_folder = user_dir / "empty_test_folder"
        empty_folder.mkdir(exist_ok=True)

        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": "empty_test_folder"}
        )
        assert response.status_code in [400, 404]

    def test_create_pack_task_incomplete_folder(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={"folder_path": ".incomplete/test"}
        )
        assert response.status_code == 403

    def test_create_pack_task_no_path_provided(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack",
            json={}
        )
        assert response.status_code in [400, 422]


class TestPackTaskDownload:

    def test_download_pack_task_not_done(self, authenticated_client: TestClient, test_user: dict, temp_db: str):
        from app.db import execute, utc_now
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder", 1000, 1000, "pending", utc_now(), utc_now()]
        )

        response = authenticated_client.get(f"/api/files/pack/{task_id}/download")
        assert response.status_code == 400

    def test_download_pack_task_output_missing(self, authenticated_client: TestClient, test_user: dict, temp_db: str):
        from app.db import execute, utc_now
        task_id = execute(
            """INSERT INTO pack_tasks
               (owner_id, folder_path, folder_size, reserved_space, output_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [test_user["id"], "folder", 1000, 0, "/nonexistent/path.zip", "done", utc_now(), utc_now()]
        )

        response = authenticated_client.get(f"/api/files/pack/{task_id}/download")
        assert response.status_code in [403, 404]


class TestBrowseDirectory:

    def test_browse_directory_path_traversal(self, authenticated_client: TestClient, user_directory: dict):
        response = authenticated_client.get(
            f"/api/files/{user_directory['id']}/browse?path=../../../etc"
        )
        assert response.status_code in [400, 403, 404]


class TestCalculateSizeEndpoint:

    def test_calculate_size_empty_paths(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"paths": []}
        )
        assert response.status_code in [200, 400, 422]

    def test_calculate_size_path_traversal(self, authenticated_client: TestClient):
        response = authenticated_client.post(
            "/api/files/pack/calculate-size",
            json={"paths": ["../../../etc/passwd"]}
        )
        assert response.status_code in [400, 403]


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

        response = authenticated_client.get(f"/api/files/{user_file_id}/browse")
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

        response = authenticated_client.get(f"/api/files/{user_file_id}/download")
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

        response = authenticated_client.get(f"/api/files/{user_file_id}/download?path=subpath")
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

        response = authenticated_client.get(f"/api/files/{user_file_id}/browse?path=nonexistent")
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

        response = authenticated_client.get(f"/api/files/{user_file_id}/browse?path=file.txt")
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

        response = authenticated_client.get(f"/api/files/{user_file_id}/browse")
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

        response = authenticated_client.get(f"/api/files/{user_file_id}/browse?path=../../../etc")
        assert response.status_code == 403
