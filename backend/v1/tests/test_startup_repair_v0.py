from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select

from app.core.config import get_internal_base_url, settings
from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files, user_files
from app.repositories.task.user_tasks import get_user_task_by_id
from app.repositories.task.downloads import get_global_download_by_id
from app.services.lifecycle.repair import (
    LEGACY_HTTP_STOP_ERROR,
    reconcile_legacy_http_downloads_v0,
)
from app.services.repair import repair_task_associations, run_startup_repair
from app.services.storage import get_task_download_dir
from app.services.storage_index import StorageScan
from app.services.usage_service import get_usage, reserve_bytes
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
)


async def _create_stored_file(name: str, size: int) -> dict:
    path = Path(settings.download_dir) / "store" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash=f"repair_{name}",
                        real_path=str(path),
                        size_bytes=size,
                        is_directory=0,
                        original_name=name,
                        created_at_ms=timestamp,
                    )
                    .returning(stored_files)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


@pytest.mark.asyncio
async def test_startup_repair_without_candidates_is_safe(temp_db: str) -> None:
    result = await run_startup_repair()

    assert result["orphan_files_found"] == 0
    assert result["unresolved_files"] == 0
    assert result["errors"] == []
    assert result["safe_for_cleanup"] is True


@pytest.mark.asyncio
async def test_startup_repair_reports_hash_mismatch_as_unresolved(
    temp_db: str,
) -> None:
    content_hash = "a" * 64
    candidate = (
        Path(settings.download_dir) / "store" / content_hash[:2] / content_hash
    )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"does not match the path hash")

    result = await run_startup_repair()

    assert result["orphan_files_found"] == 1
    assert result["stored_files_created"] == 0
    assert result["unresolved_files"] == 1
    assert result["errors"]
    assert result["safe_for_cleanup"] is False
    assert candidate.read_bytes() == b"does not match the path hash"


@pytest.mark.asyncio
async def test_startup_repair_reports_unconfirmed_registration_failure(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_hash = "b" * 64
    candidate = (
        Path(settings.download_dir) / "store" / content_hash[:2] / content_hash
    )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"valid candidate")
    lookup = AsyncMock(return_value=None)
    create = AsyncMock(side_effect=RuntimeError("database write failed"))
    monkeypatch.setattr(
        "app.services.repair.scan_storage_path_async",
        AsyncMock(
            return_value=StorageScan(
                content_hash=content_hash,
                size_bytes=candidate.stat().st_size,
                is_directory=False,
                entry_templates=[],
            )
        ),
    )
    monkeypatch.setattr("app.services.repair.get_stored_file_by_content_hash", lookup)
    monkeypatch.setattr("app.services.repair.create_stored_file_with_entries", create)

    result = await run_startup_repair()

    assert result["unresolved_files"] == 1
    assert result["safe_for_cleanup"] is False
    assert lookup.await_count == 2
    create.assert_awaited_once()
    assert candidate.read_bytes() == b"valid candidate"


@pytest.mark.asyncio
async def test_repair_does_not_bind_unique_name_size_without_content_identity(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="repair_full", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    stored = await _create_stored_file("payload.bin", 7)
    download = await create_global_download_v0(
        resource_key="repair:payload",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="completed",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
        display_name="payload.bin",
    )

    repaired = await repair_task_associations()
    startup_result = await run_startup_repair()

    async with transaction() as conn:
        updated_download = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download["id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        user_file = (
            (
                await conn.execute(
                    select(user_files).where(
                        user_files.c.user_id == user["id"],
                        user_files.c.stored_file_id == stored["id"],
                    )
                )
            )
            .mappings()
            .first()
        )
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired["repaired"] == 0
    assert repaired["unresolved"] == 1
    assert repaired["errors"]
    assert startup_result["unresolved_files"] == 1
    assert startup_result["safe_for_cleanup"] is False
    assert updated_download["completed_file_id"] is None
    assert updated_task is not None
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert user_file is None
    assert usage["reserved_bytes"] == 7
    assert usage["used_bytes"] == 0


@pytest.mark.asyncio
async def test_repair_over_quota_without_identity_remains_unresolved(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="repair_quota", quota_bytes=5)
    await reserve_bytes(user["id"], 4, quota_bytes=user["quota_bytes"])
    stored = await _create_stored_file("quota.bin", 6)
    stored_path = Path(str(stored["real_path"]))
    download = await create_global_download_v0(
        resource_key="repair:quota",
        resource_kind="http",
        source_uri="https://example.com/quota.bin",
        status="completed",
        aria2_gid="stale-repair-gid",
        display_name="quota.bin",
        total_bytes=6,
        completed_bytes=6,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4,
        display_name="quota.bin",
    )

    repaired = await repair_task_associations()

    async with transaction() as conn:
        updated_download = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download["id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        stored_row = (
            (
                await conn.execute(
                    select(stored_files).where(stored_files.c.id == stored["id"])
                )
            )
            .mappings()
            .one()
        )
        user_file = (
            await conn.execute(
                select(user_files.c.id).where(
                    user_files.c.user_id == user["id"],
                    user_files.c.stored_file_id == stored["id"],
                )
            )
        ).first()
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired["repaired"] == 0
    assert repaired["unresolved"] == 1
    assert repaired["errors"]
    assert updated_download["status"] == "completed"
    assert updated_download["aria2_gid"] == "stale-repair-gid"
    assert updated_download["completed_file_id"] is None
    assert updated_task is not None
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 4
    assert user_file is None
    assert usage["used_bytes"] == 0
    assert usage["reserved_bytes"] == 4
    assert stored_row["id"] == stored["id"]
    assert stored_path.exists()


@pytest.mark.asyncio
async def test_repair_task_associations_skips_unsafe_size_match(temp_db: str) -> None:
    user = await create_user_v0(username="repair_skip", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    await _create_stored_file("payload.bin", 8)
    download = await create_global_download_v0(
        resource_key="repair:skip",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="completed",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
        display_name="payload.bin",
    )

    repaired = await repair_task_associations()
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired["repaired"] == 0
    assert repaired["unresolved"] == 1
    assert updated_task is not None
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7


async def _create_active_http_download(
    name: str,
    *,
    gid: str | None = None,
    status: str = "active",
) -> tuple[dict, dict, dict, Path]:
    user = await create_user_v0(username=name, quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    download = await create_global_download_v0(
        resource_key=f"upgrade:{name}",
        resource_kind="http",
        source_uri=f"https://example.com/{name}.bin",
        status=status,
        aria2_gid=gid,
        total_bytes=7,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status=status,
        reserved_bytes=7,
    )
    task_dir = get_task_download_dir(int(download["id"]))
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "partial").write_bytes(b"partial")
    return user, download, task, task_dir


@pytest.mark.asyncio
async def test_startup_reconciliation_fails_direct_http_and_is_idempotent(
    temp_db: str,
) -> None:
    user, download, task, task_dir = await _create_active_http_download(
        "direct_http", gid="gid-direct-http"
    )
    client = make_aria2_client()
    client.get_uris.return_value = [
        {"uri": "https://example.com/direct_http.bin", "status": "used"}
    ]

    assert await reconcile_legacy_http_downloads_v0(client) == 1
    assert await reconcile_legacy_http_downloads_v0(client) == 0

    stored_download = await get_global_download_by_id(int(download["id"]))
    stored_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert stored_download is not None and stored_download["status"] == "failed"
    assert stored_download["aria2_gid"] is None
    assert stored_task is not None and stored_task["status"] == "failed"
    assert stored_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert not task_dir.exists()
    client.force_remove.assert_awaited_once_with("gid-direct-http")


async def _assert_reconciliation_failed(
    user: dict,
    download: dict,
    task: dict,
    task_dir: Path,
) -> None:
    stored_download = await get_global_download_by_id(int(download["id"]))
    stored_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert stored_download is not None and stored_download["status"] == "failed"
    assert stored_download["aria2_gid"] is None
    assert stored_task is not None and stored_task["status"] == "failed"
    assert stored_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert not task_dir.exists()


async def _assert_reconciliation_intact(
    user: dict,
    download: dict,
    task: dict,
    task_dir: Path,
) -> None:
    stored_download = await get_global_download_by_id(int(download["id"]))
    stored_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert stored_download is not None and stored_download["status"] == "active"
    assert stored_download["aria2_gid"] == download["aria2_gid"]
    assert stored_task is not None and stored_task["status"] == "active"
    assert stored_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7
    assert task_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("uri_state", ["direct", "get_uris_error"])
async def test_startup_reconciliation_preserves_state_when_remote_stop_fails(
    temp_db: str,
    uri_state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = f"fake-stop-capability-{uri_state}"
    gid = f"gid-stop-failure-{uri_state}"
    user, download, task, task_dir = await _create_active_http_download(
        f"stop_failure_{uri_state}", gid=gid
    )
    client = make_aria2_client(force_remove=RuntimeError(secret))
    if uri_state == "direct":
        client.get_uris.return_value = [{"uri": "https://example.com/direct.bin"}]
    else:
        client.get_uris.side_effect = RuntimeError("getUris unavailable")

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as exc_info:
        await reconcile_legacy_http_downloads_v0(client)

    assert str(exc_info.value) == LEGACY_HTTP_STOP_ERROR
    assert secret not in str(exc_info.value)
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
    await _assert_reconciliation_intact(user, download, task, task_dir)
    client.force_remove.assert_awaited_once_with(gid)
    client.remove_download_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_reconciliation_accepts_missing_gid_as_stopped(
    temp_db: str,
) -> None:
    gid = "gid-already-missing"
    user, download, task, task_dir = await _create_active_http_download(
        "already_missing", gid=gid
    )
    client = make_aria2_client(force_remove=RuntimeError(f"GID#{gid} not found"))
    client.get_uris.return_value = [{"uri": "https://example.com/direct.bin"}]

    assert await reconcile_legacy_http_downloads_v0(client) == 1

    await _assert_reconciliation_failed(user, download, task, task_dir)
    client.force_remove.assert_awaited_once_with(gid)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting", "paused"])
async def test_startup_reconciliation_fails_direct_active_like_http(
    temp_db: str,
    status: str,
) -> None:
    gid = f"gid-direct-{status}"
    user, download, task, task_dir = await _create_active_http_download(
        f"direct_{status}", gid=gid, status=status
    )
    client = make_aria2_client()
    client.get_uris.return_value = [{"uri": "https://example.com/direct.bin"}]

    assert await reconcile_legacy_http_downloads_v0(client) == 1

    await _assert_reconciliation_failed(user, download, task, task_dir)
    client.force_remove.assert_awaited_once_with(gid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["query", "fragment", "extra_path", "wrong_id", "foreign_base"],
)
async def test_startup_reconciliation_rejects_malformed_internal_uri(
    temp_db: str,
    case: str,
) -> None:
    gid = f"gid-malformed-{case}"
    user, download, task, task_dir = await _create_active_http_download(
        f"malformed_{case}", gid=gid
    )
    base = get_internal_base_url()
    path = f"/_internal/fetch/{download['id']}/0"
    malformed_uri = {
        "query": f"{base}{path}?source=remote",
        "fragment": f"{base}{path}#remote",
        "extra_path": f"{base}{path}/extra",
        "wrong_id": f"{base}/_internal/fetch/{int(download['id']) + 1}/0",
        "foreign_base": f"https://foreign.internal{path}",
    }[case]
    client = make_aria2_client()
    client.get_uris.return_value = [{"uri": malformed_uri}]

    assert await reconcile_legacy_http_downloads_v0(client) == 1

    await _assert_reconciliation_failed(user, download, task, task_dir)
    client.force_remove.assert_awaited_once_with(gid)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "waiting", "paused"])
async def test_startup_reconciliation_preserves_valid_gateway_http(
    temp_db: str,
    status: str,
) -> None:
    gid = f"gid-gateway-{status}"
    user, download, task, task_dir = await _create_active_http_download(
        f"gateway_http_{status}", gid=gid, status=status
    )
    client = make_aria2_client()
    client.get_uris.return_value = [
        {"uri": f"{get_internal_base_url()}/_internal/fetch/{download['id']}/0"}
    ]

    assert await reconcile_legacy_http_downloads_v0(client) == 0

    stored_download = await get_global_download_by_id(int(download["id"]))
    stored_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert stored_download is not None and stored_download["status"] == status
    assert stored_download["aria2_gid"] == gid
    assert stored_task is not None and stored_task["status"] == status
    assert usage["reserved_bytes"] == 7
    assert task_dir.exists()
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["get_uris_error", "empty_uris", "missing_gid"])
async def test_startup_reconciliation_fails_unverifiable_http_and_releases_reservation(
    temp_db: str,
    mode: str,
) -> None:
    gid = None if mode == "missing_gid" else f"gid-{mode}"
    status = "queued" if mode == "missing_gid" else "active"
    user, download, task, task_dir = await _create_active_http_download(
        f"unverifiable_{mode}", gid=gid, status=status
    )
    client = make_aria2_client()
    if mode == "get_uris_error":
        client.get_uris.side_effect = RuntimeError("aria2 unavailable")
    else:
        client.get_uris.return_value = []

    if mode == "missing_gid":
        # M24 fencing：queued/gid NULL 是 planned submission 候选，
        # legacy reconciliation 必须跳过，交给 recover/stale cleanup。
        assert await reconcile_legacy_http_downloads_v0(client) == 0

        stored_download = await get_global_download_by_id(int(download["id"]))
        stored_task = await get_user_task_by_id(user["id"], task["id"])
        usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
        assert stored_download is not None and stored_download["status"] == "queued"
        assert stored_download["aria2_gid"] is None
        assert stored_task is not None and stored_task["status"] == "queued"
        assert stored_task["reserved_bytes"] == 7
        assert usage["reserved_bytes"] == 7
        assert task_dir.exists()
        client.get_uris.assert_not_awaited()
        client.force_remove.assert_not_awaited()
        return

    assert await reconcile_legacy_http_downloads_v0(client) == 1

    stored_download = await get_global_download_by_id(int(download["id"]))
    stored_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])
    assert stored_download is not None and stored_download["status"] == "failed"
    assert stored_task is not None and stored_task["status"] == "failed"
    assert stored_task["reserved_bytes"] == 0
    assert usage["reserved_bytes"] == 0
    assert not task_dir.exists()
    client.force_remove.assert_awaited_once_with(gid)


@pytest.mark.asyncio
async def test_repair_task_associations_skips_ambiguous_name_size_match(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="repair_ambiguous", quota_bytes=1000)
    await reserve_bytes(user["id"], 7, quota_bytes=user["quota_bytes"])
    await _create_stored_file("payload.bin", 7)
    await _create_stored_file("payload-copy.bin", 7)
    timestamp = now_ms()
    second_path = Path(settings.download_dir) / "store" / "payload-copy.bin"
    async with transaction() as conn:
        await conn.execute(
            stored_files.update()
            .where(stored_files.c.real_path == str(second_path))
            .values(
                content_hash="repair_payload_duplicate",
                original_name="payload.bin",
                created_at_ms=timestamp,
            )
        )
    download = await create_global_download_v0(
        resource_key="repair:ambiguous",
        resource_kind="http",
        source_uri="https://example.com/payload.bin",
        status="completed",
        display_name="payload.bin",
        total_bytes=7,
        completed_bytes=7,
        completed_file_id=None,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=7,
        display_name="payload.bin",
    )

    repaired = await repair_task_associations()
    updated_task = await get_user_task_by_id(user["id"], task["id"])
    usage = await get_usage(user["id"], quota_bytes=user["quota_bytes"])

    assert repaired["repaired"] == 0
    assert repaired["unresolved"] == 1
    assert updated_task is not None
    assert updated_task["status"] == "active"
    assert updated_task["reserved_bytes"] == 7
    assert usage["reserved_bytes"] == 7
