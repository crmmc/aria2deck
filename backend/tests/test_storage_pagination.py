from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import stored_files, user_files
from app.repositories import storage as storage_repo
from app.services import storage_admin_service
from tests.helpers_v0 import create_user_v0


async def _seed_storage_files() -> dict[str, int]:
    user = await create_user_v0(username="storage_page_user")
    names = ["alpha-one", "alpha-two", "alpha-three", "beta-one", "beta-two"]
    ids: dict[str, int] = {}
    async with transaction() as conn:
        for name in names:
            row = (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash=f"hash-{name}",
                        real_path=f"/missing/{name}",
                        size_bytes=1,
                        is_directory=0,
                        original_name=name,
                        created_at_ms=100,
                    )
                    .returning(stored_files.c.id)
                )
            ).one()
            ids[name] = int(row[0])
        for name in ("alpha-two", "beta-one"):
            await conn.execute(
                insert(user_files).values(
                    user_id=user["id"],
                    stored_file_id=ids[name],
                    display_name=name,
                    created_at_ms=100,
                    updated_at_ms=100,
                )
            )
    return ids


@pytest.mark.asyncio
async def test_repository_pages_and_filtered_totals_are_consistent(temp_db: str) -> None:
    ids = await _seed_storage_files()

    total, first = await storage_repo.list_stored_files(
        "", False, offset=0, limit=2
    )
    _, middle = await storage_repo.list_stored_files("", False, offset=2, limit=2)
    _, last = await storage_repo.list_stored_files("", False, offset=4, limit=2)
    _, beyond = await storage_repo.list_stored_files("", False, offset=6, limit=2)

    assert total == 5
    assert [row["id"] for row in first] == [ids["beta-two"], ids["beta-one"]]
    assert [row["id"] for row in middle] == [ids["alpha-three"], ids["alpha-two"]]
    assert [row["id"] for row in last] == [ids["alpha-one"]]
    assert beyond == []

    search_total, search_rows = await storage_repo.list_stored_files(
        "alpha", False, offset=0, limit=10
    )
    orphan_total, orphan_rows = await storage_repo.list_stored_files(
        "", True, offset=0, limit=10
    )
    combined_total, combined_rows = await storage_repo.list_stored_files(
        "alpha", True, offset=0, limit=10
    )

    assert search_total == len(search_rows) == 3
    assert orphan_total == len(orphan_rows) == 3
    assert combined_total == len(combined_rows) == 2
    assert {row["original_name"] for row in combined_rows} == {
        "alpha-one",
        "alpha-three",
    }


@pytest.mark.asyncio
async def test_service_caps_page_size_and_checks_only_current_page() -> None:
    rows = [
        {
            "id": index,
            "content_hash": f"hash-{index}",
            "original_name": f"file-{index}",
            "size_bytes": 1,
            "is_directory": 0,
            "ref_count": 0,
            "created_at_ms": 100,
            "real_path": f"/missing/{index}",
        }
        for index in (1, 2)
    ]
    with (
        patch.object(
            storage_repo,
            "list_stored_files",
            new=AsyncMock(return_value=(250, rows)),
        ) as list_mock,
        patch.object(storage_admin_service.Path, "exists", return_value=False) as exists,
    ):
        result = await storage_admin_service.list_stored_files(
            "needle", True, page=3, page_size=200
        )

    list_mock.assert_awaited_once_with(
        "needle", True, offset=200, limit=storage_admin_service.MAX_PAGE_SIZE
    )
    assert exists.call_count == len(rows)
    assert result["page"] == 3
    assert result["page_size"] == storage_admin_service.MAX_PAGE_SIZE
    assert result["total"] == 250
    assert len(result["files"]) == len(rows)


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


def test_storage_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/admin/storage/files").status_code == 401


def test_storage_list_requires_admin(authenticated_client: TestClient) -> None:
    assert authenticated_client.get("/api/admin/storage/files").status_code == 403


@pytest.mark.parametrize("query", ["page=0", "page_size=101"])
def test_storage_list_rejects_invalid_pagination(
    admin_client: TestClient, query: str
) -> None:
    assert admin_client.get(f"/api/admin/storage/files?{query}").status_code == 422


def test_storage_list_returns_stable_empty_page_contract(
    admin_client: TestClient,
) -> None:
    result = {"files": [], "total": 0, "page": 4, "page_size": 20}
    with patch.object(
        storage_admin_service,
        "list_stored_files",
        new=AsyncMock(return_value=result),
    ) as list_mock:
        response = admin_client.get(
            "/api/admin/storage/files?page=4&page_size=20&search=none"
        )

    assert response.status_code == 200
    assert response.json() == result
    list_mock.assert_awaited_once_with(
        "none", False, page=4, page_size=20
    )
