import pytest
from unittest.mock import patch
from sqlalchemy import select
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.services.aria2_lifecycle_service import handle_aria2_event
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_global_download_v0, create_user_task_v0, create_user_v0

@pytest.mark.asyncio
async def test_external_pause_keeps_task_visible_with_admin_message(temp_db):
    user = await create_user_v0(username="pause_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:pause-admin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-pause-admin",
        total_bytes=1000,
        size_known=True,
        disk_reserved_bytes=1000,
        completed_bytes=100,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="active"
    )
    client = make_aria2_client()
    status = {
        "gid": "gid-pause-admin",
        "status": "paused",
        "totalLength": "1000",
        "completedLength": "100",
        "files": [{"path": "/tmp/file.bin", "length": "1000", "selected": "true"}],
    }
    await handle_aria2_event(client=client, gid="gid-pause-admin", event="pause", aria2_status=status)
    async with transaction() as conn:
        g = (await conn.execute(select(global_downloads).where(global_downloads.c.id == download["id"]))).mappings().one()
        t = (await conn.execute(select(user_tasks).where(user_tasks.c.id == task["id"]))).mappings().one()
    assert g["status"] == "paused"
    assert t["status"] == "paused"
    assert g["error_code"] == "admin_paused"
    assert "管理员暂停" in (g["error_message"] or "")
    assert "管理员暂停" in (t["error_message"] or "")
    assert int(g["disk_reserved_bytes"]) == 100
