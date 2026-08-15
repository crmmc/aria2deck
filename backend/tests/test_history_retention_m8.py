"""M8 Task 4 — history projection + soft expire + GC (no tid DELETE)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.db.engine import transaction
from app.db.schema import download_sources, global_downloads, user_tasks
from app.domain.errors import BadRequestError
from app.repositories.settings import update_settings_row
from app.services import history_retention, history_service, task_retry, task_service
from app.services.http_probe import ProbeResult
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_user_v0


def _now_ms() -> int:
    return int(time.time() * 1000)


DAY_MS = 24 * 60 * 60 * 1000


async def _mark_terminal(
    *,
    pid: int,
    tid: int,
    status: str = "failed",
    finished_at_ms: int | None = None,
    history_expired_at_ms: int | None = None,
) -> None:
    ts = finished_at_ms if finished_at_ms is not None else _now_ms()
    values_task: dict = {
        "status": status,
        "reserved_bytes": 0,
        "updated_at_ms": ts,
        "finished_at_ms": ts,
        "error_message": "boom" if status == "failed" else None,
    }
    if history_expired_at_ms is not None:
        values_task["history_expired_at_ms"] = history_expired_at_ms
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks).where(user_tasks.c.id == pid).values(**values_task)
        )
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == tid)
            .values(
                status=status,
                disk_reserved_bytes=0,
                updated_at_ms=ts,
                error_message="boom" if status == "failed" else None,
                aria2_gid=None,
            )
        )


async def _create_http_task(
    user: dict,
    *,
    uri: str = "http://example.com/hist.zip",
    gid: str | None = None,
) -> dict:
    aria2_gid = gid or f"gid-hist-{_now_ms()}-{uri.split('/')[-1]}"
    client = make_aria2_client(add_uri=aria2_gid)
    probe = ProbeResult(
        success=True,
        final_url=uri,
        content_length=2048,
        filename="hist.zip",
    )
    with (
        patch("app.services.task_service._get_client", return_value=client),
        patch(
            "app.services.task_service.probe_url_with_get_fallback",
            new=AsyncMock(return_value=probe),
        ),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        payload = await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=uri,
            options=None,
        )
    return {
        "pid": int(payload["id"]),
        "tid": int(payload["task_id"]),
        "payload": payload,
        "uri": uri,
    }


async def _fetch_pid(pid: int) -> dict:
    async with transaction() as conn:
        row = (
            (await conn.execute(select(user_tasks).where(user_tasks.c.id == pid)))
            .mappings()
            .one()
        )
    return dict(row)


async def _fetch_tid(tid: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == tid)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _fetch_source(source_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(download_sources).where(download_sources.c.id == source_id)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _attach_second_user_to_tid(
    *,
    user_id: int,
    tid: int,
    status: str = "failed",
    finished_at_ms: int | None = None,
) -> int:
    ts = finished_at_ms if finished_at_ms is not None else _now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    user_tasks.insert()
                    .values(
                        user_id=user_id,
                        global_download_id=tid,
                        status=status,
                        reserved_bytes=0,
                        display_name="shared",
                        error_message="boom" if status == "failed" else None,
                        created_at_ms=ts,
                        updated_at_ms=ts,
                        finished_at_ms=ts,
                    )
                    .returning(user_tasks)
                )
            )
            .mappings()
            .one()
        )
    return int(row["id"])


@pytest.mark.asyncio
async def test_t12_not_due_and_live_untouched(temp_db: str) -> None:
    user = await create_user_v0(username="m8-ret-t12")
    await update_settings_row({"history_retention_days": 30})

    live = await _create_http_task(user, uri="http://example.com/live.zip")
    recent = await _create_http_task(user, uri="http://example.com/recent.zip")
    await _mark_terminal(pid=recent["pid"], tid=recent["tid"], status="failed")

    result = await history_retention.soft_expire_due_history()

    assert result["expired_count"] == 0
    live_pid = await _fetch_pid(live["pid"])
    assert live_pid["history_expired_at_ms"] is None
    assert live_pid["status"] in {"queued", "active", "waiting", "paused"}
    recent_pid = await _fetch_pid(recent["pid"])
    assert recent_pid["history_expired_at_ms"] is None
    tid = await _fetch_tid(recent["tid"])
    assert tid["source_id"] is not None


@pytest.mark.asyncio
async def test_projection_failed_unexpired_retryable(temp_db: str) -> None:
    user = await create_user_v0(username="m8-ret-proj")
    created = await _create_http_task(user)
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="failed")

    items = await history_service.list_history(user["id"])
    match = next(i for i in items if i["id"] == created["pid"])
    assert match["retryable"] is True
    assert match["retry_blocked_reason"] is None


@pytest.mark.asyncio
async def test_t12b_due_soft_expire_history_and_gc(temp_db: str) -> None:
    user = await create_user_v0(username="m8-ret-t12b")
    await update_settings_row({"history_retention_days": 1})
    created = await _create_http_task(user, uri="http://example.com/old.zip")
    old_ts = _now_ms() - 3 * DAY_MS
    await _mark_terminal(
        pid=created["pid"],
        tid=created["tid"],
        status="failed",
        finished_at_ms=old_ts,
    )
    tid_before = await _fetch_tid(created["tid"])
    source_id = int(tid_before["source_id"])
    source_before = await _fetch_source(source_id)
    assert source_before["payload_text"] == "http://example.com/old.zip"
    assert source_before["purged_at_ms"] is None

    result = await history_retention.soft_expire_due_history()
    assert result["expired_count"] == 1

    pid = await _fetch_pid(created["pid"])
    assert pid["history_expired_at_ms"] is not None
    # G9: tid row must still exist
    tid = await _fetch_tid(created["tid"])
    assert tid["source_id"] is None
    source = await _fetch_source(source_id)
    assert source["purged_at_ms"] is not None
    assert not (source["payload_text"] or "").strip()

    items = await history_service.list_history(user["id"])
    match = next(i for i in items if i["id"] == created["pid"])
    assert match["retryable"] is False
    assert match["retry_blocked_reason"] is not None
    assert "已过期" in match["retry_blocked_reason"]

    with pytest.raises(BadRequestError) as exc:
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )
    assert "过期" in exc.value.detail


@pytest.mark.asyncio
async def test_t18_soft_expire_keeps_tid_row(temp_db: str) -> None:
    user = await create_user_v0(username="m8-ret-t18")
    await update_settings_row({"history_retention_days": 1})
    created = await _create_http_task(user)
    await _mark_terminal(
        pid=created["pid"],
        tid=created["tid"],
        status="cancelled",
        finished_at_ms=_now_ms() - 5 * DAY_MS,
    )

    await history_retention.soft_expire_due_history()

    # Must not DELETE tid (CASCADE would remove pid)
    tid = await _fetch_tid(created["tid"])
    pid = await _fetch_pid(created["pid"])
    assert tid["id"] == created["tid"]
    assert pid["id"] == created["pid"]
    assert pid["history_expired_at_ms"] is not None


@pytest.mark.asyncio
async def test_t18b_shared_tid_gc_only_when_all_pids_expired(temp_db: str) -> None:
    user_a = await create_user_v0(username="m8-ret-t18b-a")
    user_b = await create_user_v0(username="m8-ret-t18b-b")
    await update_settings_row({"history_retention_days": 1})

    created = await _create_http_task(user_a, uri="http://example.com/shared.zip")
    old_a = _now_ms() - 4 * DAY_MS
    await _mark_terminal(
        pid=created["pid"],
        tid=created["tid"],
        status="failed",
        finished_at_ms=old_a,
    )
    # B shares same tid but not yet due
    pid_b = await _attach_second_user_to_tid(
        user_id=user_b["id"],
        tid=created["tid"],
        status="failed",
        finished_at_ms=_now_ms(),
    )
    tid_before = await _fetch_tid(created["tid"])
    source_id = int(tid_before["source_id"])

    r1 = await history_retention.soft_expire_due_history()
    assert r1["expired_count"] == 1
    pid_a = await _fetch_pid(created["pid"])
    assert pid_a["history_expired_at_ms"] is not None
    pid_b_row = await _fetch_pid(pid_b)
    assert pid_b_row["history_expired_at_ms"] is None
    tid_mid = await _fetch_tid(created["tid"])
    assert tid_mid["source_id"] == source_id
    source_mid = await _fetch_source(source_id)
    assert source_mid["purged_at_ms"] is None
    assert source_mid["payload_text"] == "http://example.com/shared.zip"

    # Make B due and expire again
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.id == pid_b)
            .values(
                finished_at_ms=_now_ms() - 3 * DAY_MS,
                updated_at_ms=_now_ms() - 3 * DAY_MS,
            )
        )

    r2 = await history_retention.soft_expire_due_history()
    assert r2["expired_count"] == 1
    tid_after = await _fetch_tid(created["tid"])
    assert tid_after["source_id"] is None
    source_after = await _fetch_source(source_id)
    assert source_after["purged_at_ms"] is not None
    assert not (source_after["payload_text"] or "").strip()


@pytest.mark.asyncio
async def test_completed_projection_not_retryable(temp_db: str) -> None:
    user = await create_user_v0(username="m8-ret-completed")
    created = await _create_http_task(user)
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="completed")

    items = await history_service.list_history(user["id"])
    match = next(i for i in items if i["id"] == created["pid"])
    assert match["retryable"] is False
    assert match["retry_blocked_reason"] is not None
    assert "完成" in match["retry_blocked_reason"]


async def _tid_exists(tid: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads.c.id).where(global_downloads.c.id == tid)
            )
        ).first()
    return row is not None


async def _pid_exists(pid: int) -> bool:
    async with transaction() as conn:
        row = (
            await conn.execute(select(user_tasks.c.id).where(user_tasks.c.id == pid))
        ).first()
    return row is not None


@pytest.mark.asyncio
async def test_t10_delete_failed_history_removes_pid_and_tid_shell(
    temp_db: str,
) -> None:
    """T10 / AC-9a: hard-delete failed history → retry fails; failed tid shell gone."""
    from app.domain.errors import NotFoundError

    user = await create_user_v0(username="m8-ret-t10")
    created = await _create_http_task(user, uri="http://example.com/t10-fail.zip")
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="failed")
    tid_before = await _fetch_tid(created["tid"])
    source_id = int(tid_before["source_id"])

    result = await history_service.delete_history(user["id"], created["pid"])
    assert result["ok"] is True

    assert await _pid_exists(created["pid"]) is False
    items = await history_service.list_history(user["id"])
    assert all(i["id"] != created["pid"] for i in items)

    # failed + no completed_file_id → tid shell deleted
    assert await _tid_exists(created["tid"]) is False

    with pytest.raises(NotFoundError):
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )

    # orphan S stripped (T11 overlap)
    source = await _fetch_source(source_id)
    assert source["purged_at_ms"] is not None
    assert not (source["payload_text"] or "").strip()


@pytest.mark.asyncio
async def test_t10b_delete_completed_keeps_tid_shell_for_attach(
    temp_db: str,
) -> None:
    """T10b: zero-pid completed+completed_file_id keeps tid; register can attach."""
    from pathlib import Path

    from app.core.config import settings
    from app.modules.task_core.register import ResourceSpec, register
    from tests.helpers_v0 import create_user_file_v0

    user = await create_user_v0(username="m8-ret-t10b", quota_bytes=10_000_000)
    created = await _create_http_task(user, uri="http://example.com/t10b-done.zip")
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="completed")

    store_path = Path(settings.download_dir) / "store" / "t10b_hash"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"done-content")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="t10b_hash",
        display_name="t10b-done.zip",
        size_bytes=12,
    )
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == created["tid"])
            .values(
                completed_file_id=user_file["stored_file_id"],
                completed_bytes=12,
                completed_at_ms=_now_ms(),
            )
        )

    tid_before = await _fetch_tid(created["tid"])
    resource_key = str(tid_before["resource_key"])
    source_id = int(tid_before["source_id"])

    await history_service.delete_history(user["id"], created["pid"])

    assert await _pid_exists(created["pid"]) is False
    # completed + completed_file_id → MUST keep tid shell
    tid_after = await _fetch_tid(created["tid"])
    assert tid_after["id"] == created["tid"]
    assert tid_after["status"] == "completed"
    assert tid_after["completed_file_id"] == user_file["stored_file_id"]
    assert tid_after["source_id"] is None

    source = await _fetch_source(source_id)
    assert source["purged_at_ms"] is not None

    # same resource_key register → attach_completed (instant transfer)
    user2 = await create_user_v0(username="m8-ret-t10b-b", quota_bytes=10_000_000)
    result = await register(
        user_id=user2["id"],
        quota_bytes=user2["quota_bytes"],
        resource=ResourceSpec(
            resource_key=resource_key,
            source_uri="http://example.com/t10b-done.zip",
            resource_kind="http",
            display_name="t10b-done.zip",
            size_bytes=12,
            size_known=True,
        ),
    )
    assert result.outcome == "attached_completed"
    assert result.tid == created["tid"]


@pytest.mark.asyncio
async def test_t11_delete_history_gcs_orphan_source_payload(
    temp_db: str,
) -> None:
    """T11 / AC-9b: after hard-delete leaves orphan S, payload is stripped."""
    user = await create_user_v0(username="m8-ret-t11")
    created = await _create_http_task(user, uri="http://example.com/t11-orphan.zip")
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="cancelled")
    tid_before = await _fetch_tid(created["tid"])
    source_id = int(tid_before["source_id"])
    source_before = await _fetch_source(source_id)
    assert source_before["payload_text"] == "http://example.com/t11-orphan.zip"
    assert source_before["purged_at_ms"] is None

    await history_service.delete_history(user["id"], created["pid"])

    assert await _tid_exists(created["tid"]) is False
    source = await _fetch_source(source_id)
    assert source["purged_at_ms"] is not None
    assert not (source["payload_text"] or "").strip()
    assert source["selection_json"] is None
    assert source["options_json"] is None


@pytest.mark.asyncio
async def test_t13_admin_retention_and_purge_soft_expire(
    temp_db: str,
) -> None:
    """T13 / AC-11: admin retention config + purge older_than_days/before_ms."""
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app
    from tests.helpers_v0 import create_session_v0, create_user_v0

    admin = await create_user_v0(username="m8-ret-t13-admin", is_admin=True)
    user = await create_user_v0(username="m8-ret-t13-user")
    session = await create_session_v0(admin["id"], "m8-t13-admin-sess")

    live = await _create_http_task(user, uri="http://example.com/t13-live.zip")
    due = await _create_http_task(user, uri="http://example.com/t13-due.zip")
    old_ts = _now_ms() - 10 * DAY_MS
    await _mark_terminal(
        pid=due["pid"],
        tid=due["tid"],
        status="failed",
        finished_at_ms=old_ts,
    )
    tid_before = await _fetch_tid(due["tid"])
    source_id = int(tid_before["source_id"])

    # Match conftest client fixture: no context manager (avoids lifespan secret check).
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, session)

    get_cfg = client.get("/api/config")
    assert get_cfg.status_code == 200
    assert "history_retention_days" in get_cfg.json()
    assert get_cfg.json()["history_retention_days"] >= 1

    put_cfg = client.put("/api/config", json={"history_retention_days": 7})
    assert put_cfg.status_code == 200
    assert put_cfg.json()["history_retention_days"] == 7

    get_cfg2 = client.get("/api/config")
    assert get_cfg2.json()["history_retention_days"] == 7

    # older_than_days soft-expires due terminal; live untouched
    purge1 = client.post(
        "/api/admin/history/purge",
        json={"older_than_days": 3},
    )
    assert purge1.status_code == 200
    body1 = purge1.json()
    for key in (
        "expired_user_tasks",
        "detached_source_tids",
        "gcs_sources",
        "skipped_live",
    ):
        assert key in body1
    assert body1["expired_user_tasks"] >= 1
    assert body1["detached_source_tids"] >= 1
    assert body1["gcs_sources"] >= 1

    pid = await _fetch_pid(due["pid"])
    assert pid["history_expired_at_ms"] is not None
    tid = await _fetch_tid(due["tid"])
    assert tid["source_id"] is None
    # G9: tid still exists
    assert await _tid_exists(due["tid"]) is True

    live_pid = await _fetch_pid(live["pid"])
    assert live_pid["history_expired_at_ms"] is None
    assert live_pid["status"] in {"queued", "active", "waiting", "paused"}
    live_tid = await _fetch_tid(live["tid"])
    assert live_tid["source_id"] is not None

    source = await _fetch_source(source_id)
    assert source["purged_at_ms"] is not None

    # before_ms path (already expired → 0 new expires, still valid response)
    purge2 = client.post(
        "/api/admin/history/purge",
        json={"before_ms": _now_ms() - DAY_MS},
    )
    assert purge2.status_code == 200
    body2 = purge2.json()
    assert body2["expired_user_tasks"] == 0
    for key in (
        "expired_user_tasks",
        "detached_source_tids",
        "gcs_sources",
        "skipped_live",
    ):
        assert key in body2

    # missing both params → 400
    bad = client.post("/api/admin/history/purge", json={})
    assert bad.status_code == 400
