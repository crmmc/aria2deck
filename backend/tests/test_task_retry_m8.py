"""M8 Task 3 — POST /api/tasks/{id}/retry + lazy migrate + expired reject."""

from __future__ import annotations

import base64
import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.db.engine import transaction
from app.db.schema import download_sources, global_downloads, user_tasks
from app.domain.errors import BadRequestError, ForbiddenError, NotFoundError
from app.services import task_retry, task_service
from app.services.http_probe import ProbeResult
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_user_v0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _multi_file_torrent_payload() -> str:
    def bstr(value: bytes) -> bytes:
        return str(len(value)).encode("ascii") + b":" + value

    def bint(value: int) -> bytes:
        return b"i" + str(value).encode("ascii") + b"e"

    def bdict(items):
        return b"d" + b"".join(bstr(k) + v for k, v in items) + b"e"

    def blist(values):
        return b"l" + b"".join(values) + b"e"

    info = bdict(
        [
            (b"name", bstr(b"multi")),
            (
                b"files",
                blist(
                    [
                        bdict([(b"length", bint(100)), (b"path", blist([bstr(b"a.bin")]))]),
                        bdict([(b"length", bint(200)), (b"path", blist([bstr(b"b.bin")]))]),
                        bdict([(b"length", bint(300)), (b"path", blist([bstr(b"c.bin")]))]),
                    ]
                ),
            ),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"1" * 20)),
        ]
    )
    torrent = bdict(
        [(b"announce", bstr(b"http://tracker.example.com")), (b"info", info)]
    )
    return base64.b64encode(torrent).decode("ascii")


async def _mark_terminal(
    *,
    pid: int,
    tid: int,
    status: str = "failed",
    history_expired_at_ms: int | None = None,
) -> None:
    ts = _now_ms()
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


async def _fetch_pid(pid: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(user_tasks).where(user_tasks.c.id == pid)
                )
            )
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


async def _create_http_failed(user: dict, *, uri: str = "http://example.com/r.zip") -> dict:
    client = make_aria2_client(add_uri="gid-retry-http")
    probe = ProbeResult(
        success=True,
        final_url=uri,
        content_length=2048,
        filename="r.zip",
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
    pid = int(payload["id"])
    tid = int(payload["task_id"])
    await _mark_terminal(pid=pid, tid=tid, status="failed")
    return {"pid": pid, "tid": tid, "payload": payload, "uri": uri}


@pytest.mark.asyncio
async def test_t1_http_failed_retry_new_pid_old_stays_failed(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-http")
    created = await _create_http_failed(user)
    client = make_aria2_client(add_uri="gid-retry-http-2")
    probe = ProbeResult(
        success=True,
        final_url=created["uri"],
        content_length=2048,
        filename="r.zip",
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
        retry_payload = await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )

    assert int(retry_payload["id"]) != created["pid"]
    old = await _fetch_pid(created["pid"])
    assert old["status"] == "failed"
    new_pid = await _fetch_pid(int(retry_payload["id"]))
    assert new_pid["status"] in {"queued", "active", "waiting", "paused"}


@pytest.mark.asyncio
async def test_t2_magnet_failed_retry(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-magnet")
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    canonical = f"magnet:?xt=urn:btih:{info_hash}"
    client = make_aria2_client(add_uri="gid-retry-magnet")
    with patch("app.services.task_service._get_client", return_value=client):
        payload = await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=canonical,
            options=None,
        )
    pid = int(payload["id"])
    tid = int(payload["task_id"])
    await _mark_terminal(pid=pid, tid=tid, status="failed")

    client2 = make_aria2_client(add_uri="gid-retry-magnet-2")
    with patch("app.services.task_service._get_client", return_value=client2):
        retry_payload = await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=pid,
            quota_bytes=user["quota_bytes"],
        )

    assert int(retry_payload["id"]) != pid
    assert (await _fetch_pid(pid))["status"] == "failed"


@pytest.mark.asyncio
async def test_t3_torrent_partial_with_source_retries_selection(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="m8-retry-torrent-partial")
    torrent_data = _multi_file_torrent_payload()
    client = make_aria2_client(add_torrent="gid-retry-t-partial")
    with (
        patch("app.services.task_service._get_client", return_value=client),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        payload = await task_service.create_torrent_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            torrent=torrent_data,
            selected_file_indexes=[1, 3],
            options=None,
        )
    pid = int(payload["id"])
    tid = int(payload["task_id"])
    await _mark_terminal(pid=pid, tid=tid, status="failed")

    client2 = make_aria2_client(add_torrent="gid-retry-t-partial-2")
    with (
        patch("app.services.task_service._get_client", return_value=client2),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        retry_payload = await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=pid,
            quota_bytes=user["quota_bytes"],
        )

    assert int(retry_payload["id"]) != pid
    _, _, opts = client2.add_torrent.await_args.args
    assert opts["select-file"] == "1,3"


@pytest.mark.asyncio
async def test_t5_partial_digest_without_source_hard_fails(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-incomplete")
    ts = _now_ms()
    async with transaction() as conn:
        gd = (
            (
                await conn.execute(
                    global_downloads.insert()
                    .values(
                        resource_key="abc:files:deadbeef",
                        resource_kind="torrent",
                        source_uri="torrent:abc",
                        source_id=None,
                        display_name="partial-old",
                        status="failed",
                        total_bytes=100,
                        completed_bytes=0,
                        size_known=1,
                        created_at_ms=ts,
                        updated_at_ms=ts,
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
                    user_tasks.insert()
                    .values(
                        user_id=user["id"],
                        global_download_id=gd["id"],
                        status="failed",
                        display_name="partial-old",
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

    with pytest.raises(BadRequestError) as exc:
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=int(task["id"]),
            quota_bytes=user["quota_bytes"],
        )
    assert "不完整" in exc.value.detail


@pytest.mark.asyncio
async def test_t6_completed_rejected(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-completed")
    created = await _create_http_failed(user)
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="completed")

    with pytest.raises(BadRequestError) as exc:
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )
    assert "已完成" in exc.value.detail


@pytest.mark.asyncio
async def test_t7_cancelled_retry_new_pid(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-cancelled")
    created = await _create_http_failed(user)
    await _mark_terminal(pid=created["pid"], tid=created["tid"], status="cancelled")
    client = make_aria2_client(add_uri="gid-retry-cancel-2")
    probe = ProbeResult(
        success=True,
        final_url=created["uri"],
        content_length=2048,
        filename="r.zip",
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
        retry_payload = await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )
    assert int(retry_payload["id"]) != created["pid"]
    assert (await _fetch_pid(created["pid"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_t8_non_owner_not_found(temp_db: str) -> None:
    owner = await create_user_v0(username="m8-retry-owner")
    other = await create_user_v0(username="m8-retry-other")
    created = await _create_http_failed(owner)

    with pytest.raises(NotFoundError) as exc:
        await task_retry.retry_task(
            user_id=other["id"],
            user_task_id=created["pid"],
            quota_bytes=other["quota_bytes"],
        )
    assert exc.value.detail == "任务不存在"


@pytest.mark.asyncio
async def test_t12b_api_history_expired_rejects(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-expired")
    created = await _create_http_failed(user)
    await _mark_terminal(
        pid=created["pid"],
        tid=created["tid"],
        status="failed",
        history_expired_at_ms=_now_ms(),
    )

    with pytest.raises(BadRequestError) as exc:
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )
    assert "过期" in exc.value.detail


@pytest.mark.asyncio
async def test_lazy_migrate_http_without_source_id(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-lazy-http")
    uri = "http://example.com/legacy.zip"
    ts = _now_ms()
    async with transaction() as conn:
        gd = (
            (
                await conn.execute(
                    global_downloads.insert()
                    .values(
                        resource_key="legacy-http-key",
                        resource_kind="http",
                        source_uri=uri,
                        source_id=None,
                        display_name="legacy.zip",
                        status="failed",
                        total_bytes=1024,
                        completed_bytes=0,
                        size_known=1,
                        created_at_ms=ts,
                        updated_at_ms=ts,
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
                    user_tasks.insert()
                    .values(
                        user_id=user["id"],
                        global_download_id=gd["id"],
                        status="failed",
                        display_name="legacy.zip",
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

    client = make_aria2_client(add_uri="gid-lazy-http")
    probe = ProbeResult(
        success=True,
        final_url=uri,
        content_length=1024,
        filename="legacy.zip",
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
        retry_payload = await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=int(task["id"]),
            quota_bytes=user["quota_bytes"],
        )

    assert int(retry_payload["id"]) != int(task["id"])
    old_tid = await _fetch_tid(int(gd["id"]))
    assert old_tid["source_id"] is not None
    async with transaction() as conn:
        source = (
            (
                await conn.execute(
                    select(download_sources).where(
                        download_sources.c.id == old_tid["source_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
    assert source["payload_text"] == uri


@pytest.mark.asyncio
async def test_live_task_cannot_retry(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-live")
    client = make_aria2_client(add_uri="gid-live")
    uri = "http://example.com/live.zip"
    probe = ProbeResult(
        success=True, final_url=uri, content_length=100, filename="live.zip"
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

    with pytest.raises(BadRequestError) as exc:
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=int(payload["id"]),
            quota_bytes=user["quota_bytes"],
        )
    assert "进行中" in exc.value.detail or "不可重试" in exc.value.detail


@pytest.mark.asyncio
async def test_t9_retry_insufficient_quota_rejected(temp_db: str) -> None:
    user = await create_user_v0(username="m8-retry-quota")
    created = await _create_http_failed(user)
    probe = ProbeResult(
        success=True,
        final_url=created["uri"],
        content_length=2048,
        filename="r.zip",
    )
    client = make_aria2_client(add_uri="gid-retry-quota")

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
        pytest.raises(ForbiddenError, match="超过可用空间"),
    ):
        await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=1024,
        )

    # 资格不满足时不产生新 pid；旧历史仍在。
    async with transaction() as conn:
        pids = (
            await conn.execute(
                select(user_tasks.c.id).where(user_tasks.c.user_id == user["id"])
            )
        ).fetchall()
    assert [int(row[0]) for row in pids] == [created["pid"]]
    assert (await _fetch_pid(created["pid"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_t15_retry_joins_existing_live_tid(temp_db: str) -> None:
    from tests.helpers_v0 import create_global_download_v0, create_user_task_v0

    user = await create_user_v0(username="m8-retry-join")
    other = await create_user_v0(username="m8-retry-join-other")
    created = await _create_http_failed(user)
    failed_tid = await _fetch_tid(created["tid"])
    resource_key = str(failed_tid["resource_key"])

    live = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=created["uri"],
        resource_kind="http",
        status="active",
        aria2_gid="gid-live-join",
        total_bytes=2048,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=other["id"],
        global_download_id=int(live["id"]),
        status="active",
        reserved_bytes=2048,
    )

    client = make_aria2_client(add_uri="gid-retry-join")
    probe = ProbeResult(
        success=True,
        final_url=created["uri"],
        content_length=2048,
        filename="r.zip",
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
        retry_payload = await task_retry.retry_task(
            user_id=user["id"],
            user_task_id=created["pid"],
            quota_bytes=user["quota_bytes"],
        )

    new_pid = await _fetch_pid(int(retry_payload["id"]))
    assert int(new_pid["global_download_id"]) == int(live["id"])
    assert new_pid["status"] in {"queued", "active", "waiting", "paused"}

    async with transaction() as conn:
        live_count = (
            await conn.execute(
                select(global_downloads.c.id).where(
                    global_downloads.c.resource_key == resource_key,
                    global_downloads.c.status.in_(
                        ("queued", "active", "waiting", "paused")
                    ),
                )
            )
        ).fetchall()
    assert len(live_count) == 1
    assert (await _fetch_tid(created["tid"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_t14_retry_endpoint_uses_create_task_rate_limit(
    authenticated_client,
    test_user: dict,
) -> None:
    """T14: POST /retry shares CREATE_TASK rate-limit scope."""
    from fastapi import HTTPException

    created = await _create_http_failed(test_user)

    with patch(
        "app.routers.tasks.ensure_authenticated_allowed",
        new=AsyncMock(
            side_effect=HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
        ),
    ):
        resp = authenticated_client.post(f"/api/tasks/{created['pid']}/retry")

    assert resp.status_code == 429
    assert "频繁" in resp.json()["detail"]
