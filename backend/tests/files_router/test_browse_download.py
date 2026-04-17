from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute


def _insert_owned_path(
    test_user: dict,
    content_hash: str,
    real_path: Path,
    *,
    is_directory: bool,
    size: int,
    display_name: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    stored_file_id = execute(
        """INSERT INTO stored_files (content_hash, real_path, size, is_directory, ref_count, original_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [content_hash, str(real_path), size, int(is_directory), 1, display_name, now],
    )
    execute(
        """INSERT INTO user_files (owner_id, stored_file_id, display_name, created_at)
           VALUES (?, ?, ?, ?)""",
        [test_user["id"], stored_file_id, display_name, now],
    )


def _create_range_file(test_user: dict, content_hash: str, filename: str) -> bytes:
    data = bytes(range(100))
    file_path = Path(settings.download_dir) / "store" / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(data)
    _insert_owned_path(
        test_user,
        content_hash,
        file_path,
        is_directory=False,
        size=len(data),
        display_name=filename,
    )
    return data


class TestBrowseFile:
    def test_browse_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/nonexistent_hash/browse")
        assert response.status_code == 404

    def test_browse_file_not_directory(self, authenticated_client: TestClient, user_file: dict):
        response = authenticated_client.get(f"/api/files/{user_file['content_hash']}/browse")
        assert response.status_code == 400

    def test_browse_file_unauthorized(self, client: TestClient, temp_db: str, user_directory: dict):
        response = client.get(f"/api/files/{user_directory['content_hash']}/browse")
        assert response.status_code == 401

    def test_browse_directory_path_traversal(self, authenticated_client: TestClient, user_directory: dict):
        response = authenticated_client.get(
            f"/api/files/{user_directory['content_hash']}/browse?path=../../../etc"
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "无权访问此路径"


class TestDownloadFile:
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
        assert response.status_code == 403
        assert response.json()["detail"] == "无权访问此路径"


class TestBrowseDirectoryRealFiles:
    def test_browse_directory_base_not_exists(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        _insert_owned_path(
            test_user,
            "browse_nonexistent_base",
            Path("/nonexistent/path"),
            is_directory=True,
            size=0,
            display_name="nonexistent",
        )

        response = authenticated_client.get("/api/files/browse_nonexistent_base/browse")
        assert response.status_code == 404

    def test_browse_directory_subpath_not_exists(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        test_dir = Path(settings.download_dir) / "store" / "browse_test_dir"
        test_dir.mkdir(parents=True, exist_ok=True)
        _insert_owned_path(
            test_user,
            "browse_subpath_test",
            test_dir,
            is_directory=True,
            size=0,
            display_name="browse_test_dir",
        )

        response = authenticated_client.get("/api/files/browse_subpath_test/browse?path=nonexistent")
        assert response.status_code == 404
        assert response.json()["detail"] == "路径不存在"

    def test_browse_directory_subpath_is_file(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        test_dir = Path(settings.download_dir) / "store" / "browse_file_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "file.txt").write_text("content")
        _insert_owned_path(
            test_user,
            "browse_file_test",
            test_dir,
            is_directory=True,
            size=0,
            display_name="browse_file_test",
        )

        response = authenticated_client.get("/api/files/browse_file_test/browse?path=file.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "路径不是文件夹"

    def test_browse_directory_with_contents(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        test_dir = Path(settings.download_dir) / "store" / "browse_contents_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "file1.txt").write_text("content1")
        (test_dir / "file2.txt").write_text("content2")
        (test_dir / "subdir").mkdir(exist_ok=True)
        _insert_owned_path(
            test_user,
            "browse_contents_test",
            test_dir,
            is_directory=True,
            size=0,
            display_name="browse_contents_test",
        )

        response = authenticated_client.get("/api/files/browse_contents_test/browse")
        assert response.status_code == 200
        assert len(response.json()) == 3


class TestDownloadFileRealFiles:
    def test_download_file_base_not_exists(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        _insert_owned_path(
            test_user,
            "download_nonexistent_base",
            Path("/nonexistent/file.txt"),
            is_directory=False,
            size=100,
            display_name="file.txt",
        )

        response = authenticated_client.get("/api/files/download_nonexistent_base/download")
        assert response.status_code == 404

    def test_download_file_path_on_non_directory(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        test_file = Path(settings.download_dir) / "store" / "test_single_file.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("single file content")
        _insert_owned_path(
            test_user,
            "single_file_hash",
            test_file,
            is_directory=False,
            size=19,
            display_name="test_single_file.txt",
        )

        response = authenticated_client.get("/api/files/single_file_hash/download?path=subpath")
        assert response.status_code == 400


@pytest.mark.parametrize(
    ("content_hash", "filename", "range_header", "status", "content_range", "content_length", "expected"),
    [
        ("range_test_start_end", "range_test_start_end.bin", "bytes=0-9", 206, "bytes 0-9/100", "10", bytes(range(10))),
        ("range_test_start_only", "range_test_start_only.bin", "bytes=90-", 206, "bytes 90-99/100", "10", bytes(range(90, 100))),
        ("range_test_suffix", "range_test_suffix.bin", "bytes=-20", 206, "bytes 80-99/100", "20", bytes(range(80, 100))),
        ("range_test_exceed", "range_test_exceed.bin", "bytes=90-200", 206, "bytes 90-99/100", "10", bytes(range(90, 100))),
    ],
    ids=["start-end", "start-only", "suffix", "end-exceeds-size"],
)
def test_download_range_supported_cases(
    authenticated_client: TestClient,
    test_user: dict,
    temp_db: str,
    content_hash: str,
    filename: str,
    range_header: str,
    status: int,
    content_range: str,
    content_length: str,
    expected: bytes,
):
    _create_range_file(test_user, content_hash, filename)

    response = authenticated_client.get(
        f"/api/files/{content_hash}/download",
        headers={"Range": range_header},
    )

    assert response.status_code == status
    assert response.headers["Content-Range"] == content_range
    assert response.headers["Content-Length"] == content_length
    assert response.content == expected


@pytest.mark.parametrize(
    ("content_hash", "filename", "range_header"),
    [
        ("range_test_invalid", "range_test_invalid.bin", "invalid"),
        ("range_test_oob", "range_test_oob.bin", "bytes=200-300"),
    ],
    ids=["invalid-format", "out-of-bounds"],
)
def test_download_range_rejects_invalid_requests(
    authenticated_client: TestClient,
    test_user: dict,
    temp_db: str,
    content_hash: str,
    filename: str,
    range_header: str,
):
    _create_range_file(test_user, content_hash, filename)

    response = authenticated_client.get(
        f"/api/files/{content_hash}/download",
        headers={"Range": range_header},
    )
    assert response.status_code == 416


def test_download_without_range_advertises_accept_ranges(
    authenticated_client: TestClient,
    test_user: dict,
    temp_db: str,
):
    _create_range_file(test_user, "range_test_100", "range_test_100bytes.bin")

    response = authenticated_client.get("/api/files/range_test_100/download")
    assert response.status_code == 200
    assert response.headers["Accept-Ranges"] == "bytes"
