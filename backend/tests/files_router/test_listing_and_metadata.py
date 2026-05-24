from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import user_files
from app.repositories.usage import apply_usage_delta

FIXED_MACHINE_FREE = 8 * 1024**3


class TestListFiles:
    def test_list_files_empty(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        data = response.json()
        assert data["files"] == []
        assert set(data["space"]) == {"used", "frozen", "available"}

    def test_list_files_with_file(
        self, authenticated_client: TestClient, user_file: dict
    ):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        data = response.json()
        assert len(data["files"]) == 1
        assert data["files"][0]["name"] == "test_file.txt"
        assert data["files"][0]["size"] == 1024

    def test_list_files_with_multiple_files(
        self, authenticated_client: TestClient, user_file: dict, user_directory: dict
    ):
        response = authenticated_client.get("/api/files")
        assert response.status_code == 200
        assert len(response.json()["files"]) == 2

    def test_list_files_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files")
        assert response.status_code == 401


class TestSpaceEndpoints:
    def test_get_space(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/files/space")
        assert response.status_code == 200
        assert set(response.json()) == {"used", "frozen", "available", "quota"}

    def test_get_space_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/space")
        assert response.status_code == 401

    def test_get_quota(self, authenticated_client: TestClient):
        disk_usage = SimpleNamespace(free=FIXED_MACHINE_FREE)
        with patch("app.services.storage.shutil.disk_usage", return_value=disk_usage):
            quota_response = authenticated_client.get("/api/files/quota")
            space_response = authenticated_client.get("/api/files/space")

        assert quota_response.status_code == 200
        assert space_response.status_code == 200

        quota_data = quota_response.json()
        space_data = space_response.json()

        assert set(quota_data) == {"used", "total", "percentage"}
        assert quota_data["used"] == space_data["used"]
        assert quota_data["total"] == space_data["used"] + space_data["available"]
        assert quota_data["percentage"] == 0

    def test_get_space_total_matches_stats_visible_total(
        self, authenticated_client: TestClient, test_user: dict
    ):
        import asyncio

        asyncio.run(
            apply_usage_delta(
                test_user["id"],
                used_delta=3 * 1024**3,
                reserved_delta=1024**3,
            )
        )
        disk_usage = SimpleNamespace(free=8 * 1024**3)

        with (
            patch("app.routers.stats.shutil.disk_usage", return_value=disk_usage),
            patch("app.services.storage.shutil.disk_usage", return_value=disk_usage),
        ):
            stats_response = authenticated_client.get("/api/stats")
            space_response = authenticated_client.get("/api/files/space")

        assert stats_response.status_code == 200
        assert space_response.status_code == 200
        stats_data = stats_response.json()
        space_data = space_response.json()

        assert (
            space_data["used"] + space_data["frozen"] + space_data["available"]
            == stats_data["disk_total_space"]
        )

    def test_get_quota_unauthorized(self, client: TestClient, temp_db: str):
        response = client.get("/api/files/quota")
        assert response.status_code == 401


class TestDeleteFile:
    def test_delete_file_success(
        self, authenticated_client: TestClient, user_file: dict
    ):
        response = authenticated_client.delete(
            f"/api/files/{user_file['content_hash']}"
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        import asyncio

        async def file_exists() -> bool:
            async with transaction() as conn:
                row = (
                    await conn.execute(
                        select(user_files.c.id).where(
                            user_files.c.id == user_file["id"]
                        )
                    )
                ).first()
            return row is not None

        assert asyncio.run(file_exists()) is False

    def test_delete_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.delete("/api/files/nonexistent_hash")
        assert response.status_code == 404

    def test_delete_file_unauthorized(
        self, client: TestClient, temp_db: str, user_file: dict
    ):
        response = client.delete(f"/api/files/{user_file['content_hash']}")
        assert response.status_code == 401


class TestRenameFile:
    def test_rename_file_success(
        self, authenticated_client: TestClient, user_file: dict
    ):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "new_name.txt"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        import asyncio

        async def fetch_name() -> str:
            async with transaction() as conn:
                return str(
                    (
                        await conn.execute(
                            select(user_files.c.display_name).where(
                                user_files.c.id == user_file["id"]
                            )
                        )
                    ).scalar_one()
                )

        assert asyncio.run(fetch_name()) == "new_name.txt"

    def test_rename_file_not_found(self, authenticated_client: TestClient):
        response = authenticated_client.put(
            "/api/files/nonexistent_hash/rename",
            json={"name": "new_name.txt"},
        )
        assert response.status_code == 404

    def test_rename_file_empty_name(
        self, authenticated_client: TestClient, user_file: dict
    ):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": ""},
        )
        assert response.status_code == 422

    def test_rename_file_unauthorized(
        self, client: TestClient, temp_db: str, user_file: dict
    ):
        response = client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": "new_name.txt"},
        )
        assert response.status_code == 401

    def test_rename_file_allows_special_chars_without_path_separator(
        self, authenticated_client: TestClient, user_file: dict
    ):
        response = authenticated_client.put(
            f"/api/files/{user_file['content_hash']}/rename",
            json={"name": 'file<>:"|?*.txt'},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        import asyncio

        async def fetch_name() -> str:
            async with transaction() as conn:
                return str(
                    (
                        await conn.execute(
                            select(user_files.c.display_name).where(
                                user_files.c.id == user_file["id"]
                            )
                        )
                    ).scalar_one()
                )

        assert asyncio.run(fetch_name()) == 'file<>:"|?*.txt'


@pytest.mark.parametrize(
    ("name", "detail"),
    [
        ("path/to/file.txt", "名称不能包含路径分隔符"),
        ("..", "名称不合法"),
        ("bad\u0000name.txt", "名称包含非法字符"),
    ],
    ids=["path-separator", "dotdot", "control-char"],
)
def test_rename_file_rejects_invalid_names(
    authenticated_client: TestClient,
    user_file: dict,
    name: str,
    detail: str,
):
    response = authenticated_client.put(
        f"/api/files/{user_file['content_hash']}/rename",
        json={"name": name},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == detail
