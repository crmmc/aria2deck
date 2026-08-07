from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.repositories import auth as auth_repo
from app.repositories.pack import (
    PackAdmissionError,
    create_pending_pack_with_reservation,
)
from app.repositories.downloads import (
    DownloadAdmissionError,
    get_active_physical_commitment_bytes,
    get_global_by_resource_key,
    get_user_task,
    mark_global_download_failed,
    reconcile_download_size,
    SizeReconcileResult,
)
from app.db.engine import transaction
from app.db.schema import global_downloads, user_storage_usage, user_tasks
from app.services.aria2_lifecycle_service import (
    coordinate_reported_size,
    switch_to_followed_download,
)
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode
from app.services.download_service import (
    candidate_size_from_status,
    complete_global_download,
    create_user_download,
)
from app.services.repair import rebuild_active_download_accounting
from app.services.storage import get_task_download_dir
from app.services.usage_service import get_usage, reserve_bytes
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_pack_task_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _global(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download_id)
            )
        ).mappings().one()
    return dict(row)


async def _required_download(resource_key: str) -> dict[str, Any]:
    download = await get_global_by_resource_key(resource_key)
    assert download is not None
    return download


async def _required_task(user_id: int, download_id: int) -> dict[str, Any]:
    task = await get_user_task(user_id, download_id)
    assert task is not None
    return task


@pytest.mark.asyncio
async def test_unknown_http_never_unpauses_without_trusted_size(temp_db: str) -> None:
    user = await create_user_v0(username="unknown_http", quota_bytes=1000)
    client = make_aria2_client(
        add_uri="gid-unknown-http",
        tell_status={
            "status": "paused",
            "totalLength": "0",
            "completedLength": "123",
            "files": [],
        },
    )

    with pytest.raises(DownloadAdmissionError, match="unknown_size"):
        await create_user_download(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="https://example.com/unknown.bin",
            resource_key="quota:unknown-http",
            resource_kind="http",
            display_name="unknown.bin",
            total_bytes=0,
            size_known=False,
            aria2_client=client,
        )

    download = await _required_download("quota:unknown-http")
    task = await _required_task(user["id"], int(download["id"]))
    assert client.add_uri.call_args.args[1]["pause"] == "true"
    client.unpause.assert_not_awaited()
    assert client.force_remove.await_count >= 1
    assert client.force_remove.await_args.args[-1] == "gid-unknown-http"
    assert download["status"] == "failed"
    assert download["disk_reserved_bytes"] == 0
    assert task["status"] == "failed"
    assert task["reserved_bytes"] == 0


def test_progress_only_status_is_not_a_trusted_total() -> None:
    status = {"totalLength": "0", "completedLength": "123", "files": []}
    partial_selection = {
        "totalLength": "0",
        "completedLength": "123",
        "files": [
            {"selected": "true", "length": "100"},
            {"selected": "true"},
        ],
    }

    assert candidate_size_from_status(status) == (123, 123)
    assert candidate_size_from_status(status, require_trusted_total=True) is None
    assert candidate_size_from_status(
        partial_selection, require_trusted_total=True
    ) is None


@pytest.mark.asyncio
async def test_unknown_rpc_uri_never_unpauses_without_trusted_size(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.core.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("93.184.216.34", 443))
        ],
    )
    user = await create_user_v0(username="unknown_rpc", quota_bytes=1000)
    client = make_aria2_client(
        add_uri="gid-unknown-rpc",
        tell_status={
            "status": "paused", "totalLength": "0",
            "completedLength": "0", "files": [],
        },
    )
    handler = Aria2RpcHandler(user["id"], client)

    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addUri", [["https://example.com/unknown-rpc.bin"]]
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert "可信文件大小" in exc_info.value.message
    client.unpause.assert_not_awaited()
    assert client.force_remove.await_count >= 1
    assert client.force_remove.await_args.args[-1] == "gid-unknown-rpc"


@pytest.mark.asyncio
async def test_magnet_followed_stays_paused_until_size_admission(temp_db: str) -> None:
    user = await create_user_v0(username="magnet_admit", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-meta")
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    task = await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri=f"magnet:?xt=urn:btih:{info_hash}",
        resource_key=info_hash,
        resource_kind="magnet",
        display_name=None,
        total_bytes=0,
        size_known=False,
        aria2_client=client,
    )
    client.tell_status.return_value = {
        "gid": "gid-payload",
        "status": "paused",
        "totalLength": "500",
        "completedLength": "0",
        "files": [{"path": str(get_task_download_dir(task["global_download_id"]) / "payload"), "length": "500", "selected": "true"}],
        "bittorrent": {"info": {"name": "payload"}},
    }
    download = await _required_download(info_hash)

    changed = await switch_to_followed_download(
        client=client,
        download=download,
        metadata_gid="gid-meta",
        followed_gid="gid-payload",
        display_name_fallback=None,
        log_prefix="[Test]",
    )

    download = await _global(int(download["id"]))
    task = await _required_task(user["id"], int(download["id"]))
    usage = await get_usage(user["id"], user["quota_bytes"])
    assert changed is True
    assert client.add_uri.call_args.args[1]["pause-metadata"] == "true"
    client.unpause.assert_awaited_once_with("gid-payload")
    assert download["aria2_gid"] == "gid-payload"
    assert download["size_known"] == 1
    assert download["disk_reserved_bytes"] == 500
    assert task["reserved_bytes"] == 500
    assert usage["reserved_bytes"] == 500


@pytest.mark.asyncio
async def test_magnet_handoff_progress_only_size_fails_closed(temp_db: str) -> None:
    user = await create_user_v0(username="magnet_progress_only", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="quota:magnet-progress-only",
        resource_kind="magnet",
        status="waiting",
        aria2_gid="gid-meta-progress-only",
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="waiting"
    )
    task_dir = get_task_download_dir(download["id"])
    client = make_aria2_client(
        tell_status={
            "status": "paused", "totalLength": "0",
            "completedLength": "123",
            "files": [
                {"path": str(task_dir / "known.bin"), "selected": "true", "length": "100"},
                {"path": str(task_dir / "unknown.bin"), "selected": "true"},
            ],
        },
    )

    changed = await switch_to_followed_download(
        client=client, download=download, metadata_gid="gid-meta-progress-only",
        followed_gid="gid-payload-progress-only", display_name_fallback=None,
        log_prefix="[Test]",
    )

    # M3: unknown-size paused payload waits for next reconcile rather than
    # terminalizing immediately (spec §9.2).
    assert changed is False
    updated = await _global(download["id"])
    updated_task = await _required_task(user["id"], download["id"])
    assert updated["status"] == "waiting"
    assert updated["aria2_gid"] == "gid-meta-progress-only"
    assert updated_task["status"] == "waiting"
    assert updated_task["reserved_bytes"] == 0
    client.unpause.assert_not_awaited()


async def _unknown_shared_download(
    *, users: list[dict], resource_key: str, gid: str, limit: int = 1000
) -> dict:
    download = await create_global_download_v0(
        resource_key=resource_key,
        status="active",
        aria2_gid=gid,
        size_known=False,
        size_limit_bytes=limit,
    )
    for user in users:
        await create_user_task_v0(
            user_id=user["id"],
            global_download_id=download["id"],
            status="active",
        )
    return download


@pytest.mark.asyncio
async def test_size_admission_keeps_only_subscriber_with_quota(temp_db: str) -> None:
    high = await create_user_v0(username="quota_high", quota_bytes=1000)
    low = await create_user_v0(username="quota_low", quota_bytes=100)
    download = await _unknown_shared_download(
        users=[high, low], resource_key="quota:shared", gid="gid-shared"
    )

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-shared",
        candidate_bytes=500,
        completed_bytes=0,
        size_limit_bytes=1000,
        disk_available_bytes=1000,
    )

    high_task = await _required_task(high["id"], download["id"])
    low_task = await _required_task(low["id"], download["id"])
    stored = await _global(download["id"])
    assert result.admitted
    assert high_task["status"] == "active"
    assert high_task["reserved_bytes"] == 500
    assert low_task["status"] == "failed"
    assert low_task["reserved_bytes"] == 0
    assert stored["disk_reserved_bytes"] == 500


@pytest.mark.asyncio
async def test_all_quota_failures_cancel_download(temp_db: str) -> None:
    first = await create_user_v0(username="all_low_a", quota_bytes=100)
    second = await create_user_v0(username="all_low_b", quota_bytes=200)
    download = await _unknown_shared_download(
        users=[first, second], resource_key="quota:none", gid="gid-none"
    )

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-none",
        candidate_bytes=500,
        completed_bytes=0,
        size_limit_bytes=1000,
        disk_available_bytes=1000,
    )

    stored = await _global(download["id"])
    assert result["outcome"] == "no_subscribers"
    assert stored["status"] == "cancelled"
    assert stored["aria2_gid"] == "gid-none"
    assert stored["disk_reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_candidate_over_snapshot_limit_fails_download(temp_db: str) -> None:
    user = await create_user_v0(username="max_limit", quota_bytes=1000)
    download = await _unknown_shared_download(
        users=[user], resource_key="quota:max", gid="gid-max", limit=400
    )

    result = await reconcile_download_size(
        download_id=download["id"],
        expected_gid="gid-max",
        candidate_bytes=401,
        completed_bytes=0,
        size_limit_bytes=999,
        disk_available_bytes=1000,
    )

    stored = await _global(download["id"])
    task = await _required_task(user["id"], download["id"])
    assert result["outcome"] == "max_task_size"
    assert stored["status"] == "failed"
    assert task["status"] == "failed"


@pytest.mark.asyncio
async def test_concurrent_downloads_contend_on_user_quota(temp_db: str) -> None:
    user = await create_user_v0(username="quota_race", quota_bytes=1000)
    first = await _unknown_shared_download(
        users=[user], resource_key="quota:race-a", gid="gid-race-a"
    )
    second = await _unknown_shared_download(
        users=[user], resource_key="quota:race-b", gid="gid-race-b"
    )

    results = await asyncio.gather(
        reconcile_download_size(
            download_id=first["id"], expected_gid="gid-race-a",
            candidate_bytes=600, completed_bytes=0, size_limit_bytes=1000,
            disk_available_bytes=2000,
        ),
        reconcile_download_size(
            download_id=second["id"], expected_gid="gid-race-b",
            candidate_bytes=600, completed_bytes=0, size_limit_bytes=1000,
            disk_available_bytes=2000,
        ),
    )
    usage = await get_usage(user["id"], user["quota_bytes"])
    assert sorted(result["outcome"] for result in results) == [
        "admitted", "no_subscribers"
    ]
    assert usage["reserved_bytes"] == 600


@pytest.mark.asyncio
async def test_concurrent_downloads_contend_on_disk_budget(temp_db: str) -> None:
    first_user = await create_user_v0(username="disk_race_a", quota_bytes=2000)
    second_user = await create_user_v0(username="disk_race_b", quota_bytes=2000)
    first = await _unknown_shared_download(
        users=[first_user], resource_key="disk:race-a", gid="gid-disk-a"
    )
    second = await _unknown_shared_download(
        users=[second_user], resource_key="disk:race-b", gid="gid-disk-b"
    )

    results = await asyncio.gather(
        reconcile_download_size(
            download_id=first["id"], expected_gid="gid-disk-a",
            candidate_bytes=600, completed_bytes=0, size_limit_bytes=1000,
            disk_available_bytes=1000,
        ),
        reconcile_download_size(
            download_id=second["id"], expected_gid="gid-disk-b",
            candidate_bytes=600, completed_bytes=0, size_limit_bytes=1000,
            disk_available_bytes=1000,
        ),
    )
    assert sorted(result["outcome"] for result in results) == [
        "admitted", "disk_budget"
    ]
    rows = [await _global(first["id"]), await _global(second["id"])]
    assert sum(int(row["disk_reserved_bytes"]) for row in rows) == 600


@pytest.mark.asyncio
async def test_active_pack_reservation_counts_against_disk_budget(temp_db: str) -> None:
    pack_user = await create_user_v0(username="pack_disk", quota_bytes=2000)
    download_user = await create_user_v0(username="download_disk", quota_bytes=2000)
    await create_pack_task_v0(
        user_id=pack_user["id"], source_user_file_ids=[],
        reserved_bytes=500, status="packing",
    )
    download = await _unknown_shared_download(
        users=[download_user], resource_key="disk:pack", gid="gid-disk-pack"
    )

    result = await reconcile_download_size(
        download_id=download["id"], expected_gid="gid-disk-pack",
        candidate_bytes=600, completed_bytes=0, size_limit_bytes=1000,
        disk_available_bytes=1000,
    )

    assert result["outcome"] == "disk_budget"
    assert (await _global(download["id"]))["disk_reserved_bytes"] == 0
    usage = await get_usage(download_user["id"], download_user["quota_bytes"])
    assert usage["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_pack_and_download_contend_on_one_physical_budget(
    temp_db: str,
) -> None:
    pack_user = await create_user_v0(username="pack_disk_race", quota_bytes=2000)
    download_user = await create_user_v0(
        username="download_pack_disk_race", quota_bytes=2000
    )
    source = Path(settings.download_dir) / "store" / "pack-race.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x")
    source_ref = await create_user_file_v0(
        user_id=pack_user["id"], real_path=source,
        content_hash="pack_disk_race_source",
        display_name=source.name, size_bytes=1,
    )
    download = await _unknown_shared_download(
        users=[download_user], resource_key="disk:pack-race", gid="gid-pack-race"
    )

    outcomes = await asyncio.gather(
        create_pending_pack_with_reservation(
            user_id=pack_user["id"],
            source_user_file_ids_json=json.dumps([source_ref["id"]]),
            source_size_bytes=1, reserved_bytes=600,
            output_name="race", delete_source=False, disk_available_bytes=1000,
        ),
        reconcile_download_size(
            download_id=download["id"], expected_gid="gid-pack-race",
            candidate_bytes=600, completed_bytes=0, size_limit_bytes=1000,
            disk_available_bytes=1000,
        ),
        return_exceptions=True,
    )
    pack_result = cast(dict[str, Any] | BaseException, outcomes[0])
    download_result = cast(SizeReconcileResult | BaseException, outcomes[1])

    assert not isinstance(download_result, BaseException)
    if isinstance(pack_result, BaseException):
        assert isinstance(pack_result, PackAdmissionError)
        assert pack_result.reason == "disk"
        assert download_result["outcome"] == "admitted"
    else:
        assert download_result["outcome"] == "disk_budget"
    assert await get_active_physical_commitment_bytes() == 600


@pytest.mark.asyncio
async def test_reported_growth_pauses_then_reserves_and_shrink_releases(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="reported_resize", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-resize")
    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri="https://example.com/resize.bin", resource_key="quota:resize",
        resource_kind="http", display_name="resize.bin", total_bytes=100,
        size_known=True, aria2_client=client,
    )
    download = await _required_download("quota:resize")
    growth = {
        "status": "active", "totalLength": "200", "completedLength": "20",
        "files": [{"length": "200", "selected": "true"}],
    }

    result = await coordinate_reported_size(
        client=client, download=download, expected_gid="gid-resize",
        control_gid="gid-resize", status=growth,
    )
    assert result["outcome"] == "admitted"
    client.pause.assert_awaited_once_with("gid-resize")
    client.unpause.assert_awaited_once_with("gid-resize")
    resized_task = await _required_task(user["id"], task["global_download_id"])
    assert resized_task["reserved_bytes"] == 200

    client.pause.reset_mock()
    shrink = {
        "status": "active", "totalLength": "50", "completedLength": "20",
        "files": [{"length": "50", "selected": "true"}],
    }
    await coordinate_reported_size(
        client=client, download=await _global(download["id"]),
        expected_gid="gid-resize", control_gid="gid-resize", status=shrink,
    )
    usage = await get_usage(user["id"], user["quota_bytes"])
    client.pause.assert_not_awaited()
    assert usage["reserved_bytes"] == 50
    assert (await _global(download["id"]))["disk_reserved_bytes"] == 50


@pytest.mark.asyncio
async def test_actual_size_growth_is_admitted_before_completion(temp_db: str) -> None:
    user = await create_user_v0(username="actual_growth", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-actual")
    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri="https://example.com/actual.bin", resource_key="quota:actual",
        resource_kind="http", display_name="actual.bin", total_bytes=100,
        size_known=True, aria2_client=client,
    )
    task_dir = get_task_download_dir(task["global_download_id"])
    source = task_dir / "actual.bin"
    source.write_bytes(b"x" * 150)

    result = await complete_global_download(
        global_download_id=task["global_download_id"],
        expected_gid="gid-actual", source_path=source, original_name="actual.bin",
    )
    assert result is not None

    usage = await get_usage(user["id"], user["quota_bytes"])
    completed = await _required_task(user["id"], task["global_download_id"])
    assert result["status"] == "completed"
    assert usage["used_bytes"] == 150
    assert usage["reserved_bytes"] == 0
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_actual_over_snapshot_limit_rejects_before_hash(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="actual_max", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-actual-max")
    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri="https://example.com/max.bin", resource_key="quota:actual-max",
        resource_kind="http", display_name="max.bin", total_bytes=80,
        size_known=True, size_limit_bytes=100, aria2_client=client,
    )
    source = get_task_download_dir(task["global_download_id"]) / "max.bin"
    source.write_bytes(b"x" * 120)
    result = await complete_global_download(
        global_download_id=task["global_download_id"],
        expected_gid="gid-actual-max", source_path=source, original_name="max.bin",
    )
    assert result is not None
    assert result["status"] == "rejected"
    assert result["reason"] == "max_task_size"
    assert (await _global(task["global_download_id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_terminal_release_is_idempotent_and_old_gid_is_noop(temp_db: str) -> None:
    user = await create_user_v0(username="terminal_once", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-current")
    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri="https://example.com/terminal.bin", resource_key="quota:terminal",
        resource_kind="http", display_name="terminal.bin", total_bytes=100,
        size_known=True, aria2_client=client,
    )
    stale = await reconcile_download_size(
        download_id=task["global_download_id"], expected_gid="gid-old",
        candidate_bytes=200, completed_bytes=0, size_limit_bytes=1000,
        disk_available_bytes=1000,
    )
    first = await mark_global_download_failed(
        task["global_download_id"], expected_gid="gid-current", message="失败"
    )
    second = await mark_global_download_failed(
        task["global_download_id"], expected_gid="gid-current", message="重复"
    )
    usage = await get_usage(user["id"], user["quota_bytes"])
    assert stale["outcome"] == "stale"
    assert first is not None and second is None
    assert usage["reserved_bytes"] == 0
    assert (await _global(task["global_download_id"]))["disk_reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_terminal_residual_gid_does_not_count_against_disk_budget(
    temp_db: str,
) -> None:
    """Terminal residual gids may remain briefly for cleanup/retry safety,
    but they must not continue locking global disk budget.
    """
    first_user = await create_user_v0(username="residual_disk_a", quota_bytes=2000)
    second_user = await create_user_v0(username="residual_disk_b", quota_bytes=2000)
    client = make_aria2_client(add_uri="gid-residual-disk")
    first = await create_user_download(
        user_id=first_user["id"],
        quota_bytes=first_user["quota_bytes"],
        uri="https://example.com/residual.bin",
        resource_key="disk:residual",
        resource_kind="http",
        display_name="residual.bin",
        total_bytes=100,
        size_known=True,
        aria2_client=client,
    )
    await mark_global_download_failed(
        first["global_download_id"],
        expected_gid="gid-residual-disk",
        message="failed before cleanup",
    )
    second = await _unknown_shared_download(
        users=[second_user],
        resource_key="disk:after-residual",
        gid="gid-after-residual",
    )

    result = await reconcile_download_size(
        download_id=second["id"],
        expected_gid="gid-after-residual",
        candidate_bytes=950,
        completed_bytes=0,
        size_limit_bytes=2000,
        disk_available_bytes=1000,
    )

    residual = await _global(first["global_download_id"])
    assert residual["status"] == "failed"
    assert residual["aria2_gid"] == "gid-residual-disk"
    assert residual["disk_reserved_bytes"] == 0
    assert result["outcome"] == "admitted"


@pytest.mark.asyncio
async def test_startup_rebuild_uses_authoritative_files_tasks_and_packs(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="startup_usage", quota_bytes=2000)
    stored_path = Path(temp_db).parent / "stored.bin"
    stored_path.write_bytes(b"x" * 40)
    await create_user_file_v0(
        user_id=user["id"], real_path=stored_path,
        content_hash="startup-authoritative-hash", display_name="stored.bin",
        size_bytes=40,
    )
    await create_pack_task_v0(
        user_id=user["id"], source_user_file_ids=[], reserved_bytes=50,
        status="pending",
    )
    download = await create_global_download_v0(
        resource_key="startup:active", resource_kind="http", status="active",
        aria2_gid="gid-startup", total_bytes=300, size_known=True,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"],
        status="active", reserved_bytes=0,
    )
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user["id"])
            .values(used_bytes=999, reserved_bytes=999)
        )
    client = make_aria2_client(
        tell_status={
            "status": "paused", "totalLength": "300", "completedLength": "20",
            "files": [{"length": "300", "selected": "true"}],
        },
    )

    result = await rebuild_active_download_accounting(client)

    usage = await get_usage(user["id"], user["quota_bytes"])
    rebuilt_task = await _required_task(user["id"], download["id"])
    rebuilt_download = await _global(download["id"])
    # M3: rebuild delegates to reconcile_attempt_signal; a paused status is
    # projected as paused (not force-unpaused), so disk_reserved_bytes
    # reflects completed_bytes from snapshot, not the full total.
    assert result == {"rebuilt": 1, "failed": 0}
    assert usage["used_bytes"] == 40
    assert usage["reserved_bytes"] == 350
    assert rebuilt_task["id"] == task["id"]
    assert rebuilt_task["reserved_bytes"] == 300
    client.unpause.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_cannot_lower_quota_below_used_and_reserved(temp_db: str) -> None:
    admin = await create_user_v0(
        username="quota_admin", quota_bytes=1000, is_admin=True
    )
    user = await create_user_v0(username="quota_target", quota_bytes=1000)
    await reserve_bytes(user["id"], 300, quota_bytes=user["quota_bytes"])

    with pytest.raises(auth_repo.QuotaBelowUsageError):
        await auth_repo.update_user_as_admin(
            actor_id=admin["id"], user_id=user["id"],
            expected_username=user["username"], quota_bytes=299,
        )

    updated = await auth_repo.update_user_as_admin(
        actor_id=admin["id"], user_id=user["id"],
        expected_username=user["username"], quota_bytes=300,
    )
    assert updated is not None
    assert updated["quota_bytes"] == 300


@pytest.mark.asyncio
async def test_known_join_resizes_all_unknown_shared_subscribers(temp_db: str) -> None:
    existing = await create_user_v0(username="known_join_existing", quota_bytes=1000)
    joining = await create_user_v0(username="known_join_low", quota_bytes=100)
    download = await _unknown_shared_download(
        users=[existing], resource_key="quota:known-join", gid="gid-known-join"
    )
    client = make_aria2_client()

    with pytest.raises(DownloadAdmissionError, match="quota exceeded"):
        await create_user_download(
            user_id=joining["id"], quota_bytes=joining["quota_bytes"],
            uri="https://example.com/known-join.bin",
            resource_key="quota:known-join", resource_kind="http",
            display_name="known-join.bin", total_bytes=500, size_known=True,
            aria2_client=client,
        )

    existing_task = await _required_task(existing["id"], download["id"])
    joining_task = await _required_task(joining["id"], download["id"])
    stored = await _global(download["id"])
    assert existing_task["reserved_bytes"] == 500
    assert joining_task["status"] == "failed"
    assert joining_task["reserved_bytes"] == 0
    assert stored["size_known"] == 1
    assert stored["disk_reserved_bytes"] == 500
    assert (await get_usage(existing["id"], 1000))["reserved_bytes"] == 500
    assert (await get_usage(joining["id"], 100))["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_equal_candidate_repairs_subscriber_reservation(temp_db: str) -> None:
    user = await create_user_v0(username="equal_repair", quota_bytes=1000)
    download = await _unknown_shared_download(
        users=[user], resource_key="quota:equal-repair", gid="gid-equal-repair"
    )
    await reconcile_download_size(
        download_id=download["id"], expected_gid="gid-equal-repair",
        candidate_bytes=400, completed_bytes=0, size_limit_bytes=1000,
        disk_available_bytes=1000,
    )
    task = await _required_task(user["id"], download["id"])
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks).where(user_tasks.c.id == task["id"])
            .values(reserved_bytes=0)
        )
        await conn.execute(
            update(user_storage_usage)
            .where(user_storage_usage.c.user_id == user["id"])
            .values(reserved_bytes=0)
        )

    result = await coordinate_reported_size(
        client=make_aria2_client(), download=await _global(download["id"]),
        expected_gid="gid-equal-repair", control_gid="gid-equal-repair",
        status={"status": "active", "totalLength": "400", "completedLength": "0"},
    )

    repaired = await _required_task(user["id"], download["id"])
    assert result["outcome"] == "admitted"
    assert repaired["reserved_bytes"] == 400
    assert (await get_usage(user["id"], 1000))["reserved_bytes"] == 400


@pytest.mark.asyncio
async def test_explicit_known_zero_entry_is_allowed(temp_db: str) -> None:
    user = await create_user_v0(username="known_zero", quota_bytes=1000)
    client = make_aria2_client(add_uri="gid-known-zero")

    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri="https://example.com/empty.bin", resource_key="quota:known-zero",
        resource_kind="http", display_name="empty.bin", total_bytes=0,
        size_known=True, aria2_client=client,
    )

    stored = await _global(task["global_download_id"])
    assert stored["size_known"] == 1
    assert stored["total_bytes"] == 0
    assert task["reserved_bytes"] == 0
    assert "pause" not in client.add_uri.call_args.args[1]
    client.tell_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_growth_unpause_failure_terminalizes_generation(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="growth_unpause_fail", quota_bytes=1000)
    client = AsyncMock()
    client.add_uri.return_value = "gid-growth-unpause"
    task = await create_user_download(
        user_id=user["id"], quota_bytes=user["quota_bytes"],
        uri="https://example.com/grow.bin", resource_key="quota:growth-unpause",
        resource_kind="http", display_name="grow.bin", total_bytes=100,
        size_known=True, aria2_client=client,
    )
    client.unpause.side_effect = OSError("unpause failed")

    result = await coordinate_reported_size(
        client=client, download=await _global(task["global_download_id"]),
        expected_gid="gid-growth-unpause", control_gid="gid-growth-unpause",
        status={"status": "active", "totalLength": "200", "completedLength": "0"},
    )

    stored = await _global(task["global_download_id"])
    failed_task = await _required_task(user["id"], task["global_download_id"])
    # M3: unpause failure terminalizes with outcome="terminalized",
    # error_code remains growth_unpause_failed.
    assert result["outcome"] == "terminalized"
    assert stored["status"] == "failed"
    assert stored["error_code"] == "growth_unpause_failed"
    assert stored["aria2_gid"] is None
    assert failed_task["status"] == "failed"
    assert failed_task["reserved_bytes"] == 0
    client.pause.assert_awaited_once_with("gid-growth-unpause")
    assert client.force_remove.await_count >= 1
    assert client.force_remove.await_args.args[-1] == "gid-growth-unpause"


@pytest.mark.asyncio
async def test_startup_quiescent_statuses_are_not_paused(temp_db: str) -> None:
    complete_user = await create_user_v0(username="startup_complete", quota_bytes=1000)
    error_user = await create_user_v0(username="startup_error", quota_bytes=1000)
    removed_user = await create_user_v0(username="startup_removed", quota_bytes=1000)
    complete = await create_global_download_v0(
        resource_key="startup:complete-live", resource_kind="http",
        status="active", aria2_gid="gid-startup-complete", total_bytes=100,
        size_known=True,
    )
    error = await create_global_download_v0(
        resource_key="startup:error-live", resource_kind="http",
        status="active", aria2_gid="gid-startup-error", total_bytes=100,
        size_known=True,
    )
    removed = await create_global_download_v0(
        resource_key="startup:removed-live", resource_kind="http",
        status="active", aria2_gid="gid-startup-removed", total_bytes=100,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=complete_user["id"], global_download_id=complete["id"],
        status="active", reserved_bytes=0,
    )
    await create_user_task_v0(
        user_id=error_user["id"], global_download_id=error["id"],
        status="active", reserved_bytes=0,
    )
    await create_user_task_v0(
        user_id=removed_user["id"], global_download_id=removed["id"],
        status="active", reserved_bytes=0,
    )
    client = make_aria2_client()
    statuses = {
        "gid-startup-complete": {
            "status": "complete", "totalLength": "100", "completedLength": "100"
        },
        "gid-startup-error": {
            "status": "error", "totalLength": "100", "completedLength": "10"
        },
        "gid-startup-removed": {
            "status": "removed", "totalLength": "100", "completedLength": "10"
        },
    }
    client.tell_status.side_effect = lambda gid: statuses[gid]

    result = await rebuild_active_download_accounting(client)

    assert result == {"rebuilt": 1, "failed": 2}
    client.pause.assert_not_awaited()
    client.unpause.assert_not_awaited()
    assert (await _global(complete["id"]))["status"] == "active"
    failed = await _global(error["id"])
    removed_failed = await _global(removed["id"])
    assert failed["status"] == "failed"
    assert failed["aria2_gid"] is None
    assert removed_failed["status"] == "failed"
    assert removed_failed["aria2_gid"] is None


@pytest.mark.asyncio
async def test_startup_pause_provenance_survives_second_start(temp_db: str) -> None:
    user = await create_user_v0(username="startup_twice", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="startup:twice", resource_kind="http", status="active",
        aria2_gid="gid-startup-twice", total_bytes=100, size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"],
        status="active", reserved_bytes=0,
    )
    client = make_aria2_client(
        tell_status=[
            {"status": "active", "totalLength": "100", "completedLength": "0"},
            {"status": "paused", "totalLength": "100", "completedLength": "0"},
        ],
    )

    first = await rebuild_active_download_accounting(client)
    second = await rebuild_active_download_accounting(client)

    # M3: rebuild delegates to reconcile_attempt_signal; it projects status
    # without manual pause/unpause. Both passes rebuild successfully.
    assert first == second == {"rebuilt": 1, "failed": 0}
    client.pause.assert_not_awaited()
    client.unpause.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_unpause_failure_terminalizes_generation(temp_db: str) -> None:
    user = await create_user_v0(username="startup_unpause_fail", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="startup:unpause-fail", resource_kind="http", status="active",
        aria2_gid="gid-startup-unpause-fail", total_bytes=100, size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"],
        status="active", reserved_bytes=0,
    )
    client = make_aria2_client(
        tell_status={
            "status": "paused", "totalLength": "100", "completedLength": "0"
        },
        unpause=OSError("cannot resume"),
    )

    result = await rebuild_active_download_accounting(client)

    stored = await _global(download["id"])
    task = await _required_task(user["id"], download["id"])
    # M3: rebuild no longer force-unpauses on startup. A paused aria2 status
    # is projected as paused without attempting unpause, so the unpause
    # side_effect never triggers.
    assert result == {"rebuilt": 1, "failed": 0}
    assert stored["status"] == "paused"
    assert stored["aria2_gid"] == "gid-startup-unpause-fail"
    assert task["status"] == "paused"
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_progress_only_size_never_resumes(temp_db: str) -> None:
    user = await create_user_v0(username="startup_progress_only", quota_bytes=1000)
    download = await create_global_download_v0(
        resource_key="startup:progress-only", resource_kind="http",
        status="active", aria2_gid="gid-startup-progress-only",
        total_bytes=0, size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="active"
    )
    client = make_aria2_client(
        tell_status={
            "status": "paused", "totalLength": "0",
            "completedLength": "123", "files": [],
        },
    )

    result = await rebuild_active_download_accounting(client)

    updated = await _global(download["id"])
    updated_task = await _required_task(user["id"], download["id"])
    assert result == {"rebuilt": 0, "failed": 1}
    assert updated["status"] == "failed"
    assert updated["error_code"] == "unknown_size"
    assert updated["aria2_gid"] is None
    assert updated_task["status"] == "failed"
    assert updated_task["reserved_bytes"] == 0
    client.unpause.assert_not_awaited()
    assert client.force_remove.await_count >= 1
    assert client.force_remove.await_args.args[-1] == "gid-startup-progress-only"


@pytest.mark.asyncio
async def test_disk_snapshot_is_taken_after_sqlite_writer_lock(temp_db: str) -> None:
    user = await create_user_v0(username="disk_snapshot_lock", quota_bytes=1000)
    download = await _unknown_shared_download(
        users=[user], resource_key="disk:snapshot-lock", gid="gid-snapshot-lock"
    )
    samples: list[str] = []

    def sample_available() -> int:
        samples.append("sampled")
        return 1000

    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == download["id"])
            .values(updated_at_ms=global_downloads.c.updated_at_ms)
        )
        pending = asyncio.create_task(
            reconcile_download_size(
                download_id=download["id"], expected_gid="gid-snapshot-lock",
                candidate_bytes=100, completed_bytes=0, size_limit_bytes=1000,
                disk_available_bytes=sample_available,
            )
        )
        await asyncio.sleep(0.05)
        assert samples == []

    result = await pending
    assert result["outcome"] == "admitted"
    assert samples == ["sampled"]
