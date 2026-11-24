"""删除用户时清理 v0 用户归属记录测试。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import pack_tasks, user_tasks
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import (
    create_global_download_v0,
    create_pack_task_v0,
    create_user_task_v0,
)


async def _count_rows(table, user_id: int) -> int:
    async with transaction() as conn:
        value = (
            await conn.execute(
                select(func.count())
                .select_from(table)
                .where(table.c.user_id == user_id)
            )
        ).scalar_one()
    return int(value or 0)


class TestDeleteUserCleanup:
    """删除用户时清理任务测试套件。"""

    def _create_user_via_api(
        self, client: TestClient, admin_session: str, username: str
    ) -> int:
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": username,
                "password": "password123",
                "is_admin": False,
            },
        )
        assert response.status_code == 200
        return int(response.json()["id"])

    def test_delete_user_removes_user_tasks(
        self, client: TestClient, test_admin: dict, admin_session: str
    ) -> None:
        user_id = self._create_user_via_api(client, admin_session, "testuser_tasks")
        download = asyncio.run(
            create_global_download_v0(
                resource_key="delete-user-task",
                source_uri="https://example.com/file.zip",
                resource_kind="http",
                status="completed",
                display_name="file.zip",
            )
        )
        asyncio.run(
            create_user_task_v0(
                user_id=user_id,
                global_download_id=download["id"],
                status="completed",
                display_name="file.zip",
            )
        )
        assert asyncio.run(_count_rows(user_tasks, user_id)) == 1

        response = client.delete(f"/api/users/{user_id}")
        assert response.status_code == 202
        assert response.json() == {
            "ok": True,
            "state": "pending",
            "accepted": True,
        }
        asyncio.run(DeletionCleanupManager.run_once())

        assert asyncio.run(_count_rows(user_tasks, user_id)) == 0

    def test_delete_user_removes_pack_tasks(
        self, client: TestClient, test_admin: dict, admin_session: str
    ) -> None:
        user_id = self._create_user_via_api(client, admin_session, "testuser_pack")
        asyncio.run(
            create_pack_task_v0(
                user_id=user_id,
                source_user_file_ids=[1, 2],
                source_size_bytes=1_000_000,
                reserved_bytes=1_000_000,
                status="completed",
                output_name="archive.tar.zst",
            )
        )
        assert asyncio.run(_count_rows(pack_tasks, user_id)) == 1

        response = client.delete(f"/api/users/{user_id}")
        assert response.status_code == 202
        asyncio.run(DeletionCleanupManager.run_once())

        assert asyncio.run(_count_rows(pack_tasks, user_id)) == 0

    def test_delete_user_multiple_tasks(
        self, client: TestClient, test_admin: dict, admin_session: str
    ) -> None:
        user_id = self._create_user_via_api(
            client, admin_session, "testuser_multi_tasks"
        )
        for index in range(5):
            download = asyncio.run(
                create_global_download_v0(
                    resource_key=f"delete-user-task-{index}",
                    source_uri=f"https://example.com/file{index}.zip",
                    resource_kind="http",
                    status="completed",
                    display_name=f"file{index}.zip",
                )
            )
            asyncio.run(
                create_user_task_v0(
                    user_id=user_id,
                    global_download_id=download["id"],
                    status="completed",
                    display_name=f"file{index}.zip",
                )
            )

        assert asyncio.run(_count_rows(user_tasks, user_id)) == 5

        response = client.delete(f"/api/users/{user_id}")
        assert response.status_code == 202
        asyncio.run(DeletionCleanupManager.run_once())

        assert asyncio.run(_count_rows(user_tasks, user_id)) == 0
