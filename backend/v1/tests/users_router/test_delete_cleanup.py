from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import global_downloads, pack_tasks, sessions, user_files, user_tasks
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import create_user_file_v0, now_ms


async def fetch_one(stmt) -> dict | None:
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def create_user_task(user_id: int) -> int:
    timestamp = now_ms()
    async with transaction() as conn:
        download = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key="http:abc123",
                        resource_kind="http",
                        source_uri="http://example.com/file.zip",
                        display_name="file.zip",
                        aria2_gid=None,
                        status="completed",
                        total_bytes=0,
                        completed_bytes=0,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(global_downloads)
                )
            )
            .mappings()
            .one()
        )
        task = (
            (
                await conn.execute(
                    insert(user_tasks)
                    .values(
                        user_id=user_id,
                        global_download_id=download["id"],
                        status="completed",
                        display_name="file.zip",
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(user_tasks)
                )
            )
            .mappings()
            .one()
        )
    return int(task["id"])


async def create_pack_task(user_id: int) -> int:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    insert(pack_tasks)
                    .values(
                        user_id=user_id,
                        source_user_file_ids_json="[]",
                        source_size_bytes=1000,
                        reserved_bytes=0,
                        output_name="test_folder.tar.zst",
                        status="pending",
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    .returning(pack_tasks)
                )
            )
            .mappings()
            .one()
        )
    return int(row["id"])


class TestDeleteUserCleanup:
    def test_delete_user_clears_sessions(
        self, admin_client: TestClient, test_user: dict, user_session: str
    ):
        import asyncio

        response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 202
        asyncio.run(DeletionCleanupManager.run_once())
        assert (
            asyncio.run(
                fetch_one(select(sessions).where(sessions.c.user_id == test_user["id"]))
            )
            is None
        )

    def test_delete_user_clears_tasks(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        import asyncio

        asyncio.run(create_user_task(test_user["id"]))

        response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 202
        asyncio.run(DeletionCleanupManager.run_once())
        assert (
            asyncio.run(
                fetch_one(
                    select(user_tasks).where(user_tasks.c.user_id == test_user["id"])
                )
            )
            is None
        )

    def test_delete_user_clears_pack_tasks(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        import asyncio

        asyncio.run(create_pack_task(test_user["id"]))

        response = admin_client.delete(f"/api/users/{test_user['id']}")
        assert response.status_code == 202
        asyncio.run(DeletionCleanupManager.run_once())
        assert (
            asyncio.run(
                fetch_one(
                    select(pack_tasks).where(pack_tasks.c.user_id == test_user["id"])
                )
            )
            is None
        )

    def test_delete_user_with_user_files(
        self, admin_client: TestClient, test_user: dict, temp_db: str
    ):
        import asyncio
        from pathlib import Path

        real_path = Path(settings.download_dir) / "store" / "abc123"
        real_path.parent.mkdir(parents=True, exist_ok=True)
        real_path.write_bytes(b"stored")
        asyncio.run(
            create_user_file_v0(
                user_id=test_user["id"],
                real_path=real_path,
                content_hash="abc123hash",
                display_name="test_file.txt",
                size_bytes=1000,
            )
        )

        response = admin_client.delete(f"/api/users/{test_user['id']}")

        assert response.status_code == 202
        asyncio.run(DeletionCleanupManager.run_once())
        assert (
            asyncio.run(
                fetch_one(
                    select(user_files).where(user_files.c.user_id == test_user["id"])
                )
            )
            is None
        )
        assert not real_path.exists()
