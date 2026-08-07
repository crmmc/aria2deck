import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.services.aria2_lifecycle_service import handle_aria2_event
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_global_download_v0, create_user_task_v0, create_user_v0


def _paused_status(*, gid: str = "gid-pause-admin") -> dict:
    return {
        "gid": gid,
        "status": "paused",
        "totalLength": "1000",
        "completedLength": "100",
        "files": [{"path": "/tmp/file.bin", "length": "1000", "selected": "true"}],
    }


def _active_status(*, gid: str = "gid-pause-admin") -> dict:
    return {
        "gid": gid,
        "status": "active",
        "totalLength": "1000",
        "completedLength": "100",
        "downloadSpeed": "1000",
        "uploadSpeed": "0",
        "files": [{"path": "/tmp/file.bin", "length": "1000", "selected": "true"}],
    }


@pytest.mark.asyncio
async def test_size_known_pause_auto_resumes_instead_of_external_paused(temp_db):
    """Admitted payloads that become paused must resume, not stick on external_paused."""
    user = await create_user_v0(username="pause_user", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:pause-autoresume",
        resource_kind="http",
        status="active",
        aria2_gid="gid-pause-autoresume",
        total_bytes=1000,
        size_known=True,
        disk_reserved_bytes=1000,
        completed_bytes=100,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="active"
    )
    client = make_aria2_client(
        tell_status=_active_status(gid="gid-pause-autoresume"),
    )
    status = _paused_status(gid="gid-pause-autoresume")
    await handle_aria2_event(
        client=client,
        gid="gid-pause-autoresume",
        event="pause",
        aria2_status=status,
    )
    async with transaction() as conn:
        g = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download["id"])
            )
        ).mappings().one()
        t = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    assert g["status"] == "active"
    assert t["status"] == "active"
    assert g["error_code"] in (None, "")
    assert g["error_message"] in (None, "")
    client.unpause.assert_awaited()


@pytest.mark.asyncio
async def test_external_pause_when_auto_resume_cannot_unpause(temp_db):
    """If unpause cannot leave paused, project external_paused for active→paused."""
    user = await create_user_v0(username="pause_user_ext", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:pause-external",
        resource_kind="http",
        status="active",
        aria2_gid="gid-pause-external",
        total_bytes=1000,
        size_known=True,
        disk_reserved_bytes=1000,
        completed_bytes=100,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="active"
    )
    client = make_aria2_client(
        unpause=RuntimeError("refused"),
        tell_status=_paused_status(gid="gid-pause-external"),
    )
    status = _paused_status(gid="gid-pause-external")
    await handle_aria2_event(
        client=client,
        gid="gid-pause-external",
        event="pause",
        aria2_status=status,
    )
    async with transaction() as conn:
        g = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download["id"])
            )
        ).mappings().one()
        t = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    assert g["status"] == "paused"
    assert t["status"] == "paused"
    assert g["error_code"] == "external_paused"
    assert "外部暂停" in (g["error_message"] or "")
    assert "外部暂停" in (t["error_message"] or "")
    assert int(g["disk_reserved_bytes"]) == 100


@pytest.mark.asyncio
async def test_already_paused_sync_auto_resumes_when_size_known(temp_db):
    """Sticky paused admitted tasks should resume on Sync, not stay stuck."""
    user = await create_user_v0(username="pause_user2", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:pause-sticky",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-pause-sticky",
        total_bytes=1000,
        size_known=True,
        disk_reserved_bytes=100,
        completed_bytes=100,
        error_code="external_paused",
        error_message="任务已被外部暂停，请联系管理员处理",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
        error_message="任务已被外部暂停，请联系管理员处理",
    )
    client = make_aria2_client(
        tell_status=_active_status(gid="gid-pause-sticky"),
    )
    status = _paused_status(gid="gid-pause-sticky")
    await handle_aria2_event(
        client=client,
        gid="gid-pause-sticky",
        event=None,
        aria2_status=status,
    )
    async with transaction() as conn:
        g = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download["id"])
            )
        ).mappings().one()
        t = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    assert g["status"] == "active"
    assert t["status"] == "active"
    assert g["error_code"] in (None, "")
    assert g["error_message"] in (None, "")
    client.unpause.assert_awaited()


@pytest.mark.asyncio
async def test_resume_from_external_pause_clears_error_hint(temp_db):
    user = await create_user_v0(username="pause_user3", quota_bytes=10_000)
    download = await create_global_download_v0(
        resource_key="http:pause-resume",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-pause-resume",
        total_bytes=1000,
        size_known=True,
        disk_reserved_bytes=100,
        completed_bytes=100,
        error_code="external_paused",
        error_message="任务已被外部暂停，请联系管理员处理",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
        error_message="任务已被外部暂停，请联系管理员处理",
    )
    client = make_aria2_client(
        tell_status=_active_status(gid="gid-pause-resume"),
    )
    status = _active_status(gid="gid-pause-resume")
    status["completedLength"] = "200"
    await handle_aria2_event(
        client=client,
        gid="gid-pause-resume",
        event="start",
        aria2_status=status,
    )
    async with transaction() as conn:
        g = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download["id"])
            )
        ).mappings().one()
        t = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    assert g["status"] == "active"
    assert g["error_code"] in (None, "")
    assert g["error_message"] in (None, "")
    assert t["status"] == "active"
    assert t["error_message"] in (None, "")
