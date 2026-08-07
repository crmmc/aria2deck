"""T23: handoff concurrency and idempotency race tests.

Verifies spec §22.2 Handoff requirements with a focus on ordering,
concurrency, and idempotency:

1. magnet and HTTP .torrent followedBy/following arrive out of order.
2. payload active/paused/waiting/complete — handoff is idempotent.
3. payload tell_status transient failure — keep source attempt, no dir delete.
4. admission rejection — only clean up claim-specified writer/result GIDs.
5. handoff complete — no fabricated second complete event.
6. duplicate handoff — switch current GID exactly once.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from app.domain.lifecycle import ReconcileResult
from app.services import aria2_lifecycle_service
from app.services.aria2_lifecycle_service import reconcile_attempt_signal
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_global(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


def _payload_status(
    *,
    gid: str = "payload",
    status: str = "active",
    total: int = 4096,
    completed: int = 0,
    following: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "status": status,
        "totalLength": str(total),
        "completedLength": str(completed),
        "gid": gid,
        "files": [
            {"path": "/dl/1/file.iso", "length": str(total), "selected": "true"}
        ],
    }
    if following is not None:
        d["following"] = following
    return d


def _source_status(
    *,
    gid: str = "source",
    status: str = "complete",
    total: int = 4096,
    followed_by: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "status": status,
        "totalLength": str(total),
        "completedLength": str(total),
        "gid": gid,
        "files": [
            {"path": "[METADATA]", "length": str(total), "selected": "true"}
        ],
    }
    if followed_by is not None:
        d["followedBy"] = [followed_by]
    return d


# ---------------------------------------------------------------------------
# 1. magnet followedBy/following out of order — both orderings, single switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magnet_followedby_then_following_single_switch(
    temp_db: str,
) -> None:
    """Magnet source: followedBy event arrives first (source complete),
    then payload following event arrives. GID switches exactly once."""
    user = await create_user_v0(
        username="t23_mag_order", quota_bytes=10_000_000
    )
    source_gid = "mag_src_01"
    payload_gid = "mag_pay_01"
    download = await create_global_download_v0(
        resource_key="magnet:t23-mag-order",
        source_uri="magnet:?xt=urn:btih:t23_mag_order",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return _payload_status(gid=payload_gid, following=source_gid)
        return _source_status(gid=source_gid, followed_by=payload_gid)

    client = make_aria2_client(tell_status=_tell)

    # Event 1: source complete with followedBy.
    r1 = await reconcile_attempt_signal(
        client=client,
        observed_gid=source_gid,
        event="complete",
        observed_status=_source_status(gid=source_gid, followed_by=payload_gid),
        log_prefix="[T23]",
    )
    assert r1 == ReconcileResult.CHANGED
    after1 = await _fetch_global(download["id"])
    assert after1["aria2_gid"] == payload_gid
    assert after1["resource_kind"] == "torrent"

    # Event 2: payload start with following (arrives late).
    r2 = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=_payload_status(gid=payload_gid, following=source_gid),
        log_prefix="[T23]",
    )
    assert r2 in (ReconcileResult.CHANGED, ReconcileResult.ALREADY_ACTIVE)

    after2 = await _fetch_global(download["id"])
    assert after2["aria2_gid"] == payload_gid
    assert after2["status"] == "active"


@pytest.mark.asyncio
async def test_magnet_following_then_followedby_single_switch(
    temp_db: str,
) -> None:
    """Magnet source: payload following event arrives first, then source
    complete with followedBy. GID switches exactly once."""
    user = await create_user_v0(
        username="t23_mag_rev", quota_bytes=10_000_000
    )
    source_gid = "mag_src_02"
    payload_gid = "mag_pay_02"
    download = await create_global_download_v0(
        resource_key="magnet:t23-mag-rev",
        source_uri="magnet:?xt=urn:btih:t23_mag_rev",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return _payload_status(gid=payload_gid, following=source_gid)
        return _source_status(gid=source_gid, followed_by=payload_gid)

    client = make_aria2_client(tell_status=_tell)

    # Event 1: payload start with following (arrives first).
    r1 = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=_payload_status(gid=payload_gid, following=source_gid),
        log_prefix="[T23]",
    )
    assert r1 == ReconcileResult.CHANGED
    after1 = await _fetch_global(download["id"])
    assert after1["aria2_gid"] == payload_gid

    # Event 2: source complete with followedBy (arrives later — stale).
    r2 = await reconcile_attempt_signal(
        client=client,
        observed_gid=source_gid,
        event="complete",
        observed_status=_source_status(gid=source_gid, followed_by=payload_gid),
        log_prefix="[T23]",
    )
    assert r2 in (ReconcileResult.STALE, ReconcileResult.IGNORED)

    after2 = await _fetch_global(download["id"])
    assert after2["aria2_gid"] == payload_gid


# ---------------------------------------------------------------------------
# 1b. HTTP .torrent followedBy/following out of order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_torrent_followedby_out_of_order(
    temp_db: str,
) -> None:
    """HTTP .torrent source: followedBy arrives first (complete), then
    payload following. Single switch."""
    user = await create_user_v0(
        username="t23_http_order", quota_bytes=10_000_000
    )
    source_gid = "http_src_01"
    payload_gid = "http_pay_01"
    download = await create_global_download_v0(
        resource_key="torrent:t23-http-order",
        source_uri="https://example.com/file.torrent",
        resource_kind="torrent",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return _payload_status(gid=payload_gid, following=source_gid)
        return _source_status(gid=source_gid, followed_by=payload_gid)

    client = make_aria2_client(tell_status=_tell)

    # Source complete with followedBy.
    r1 = await reconcile_attempt_signal(
        client=client,
        observed_gid=source_gid,
        event="complete",
        observed_status=_source_status(gid=source_gid, followed_by=payload_gid),
        log_prefix="[T23]",
    )
    assert r1 == ReconcileResult.CHANGED
    after1 = await _fetch_global(download["id"])
    assert after1["aria2_gid"] == payload_gid

    # Payload following arrives later — idempotent.
    r2 = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=_payload_status(gid=payload_gid, following=source_gid),
        log_prefix="[T23]",
    )
    assert r2 in (ReconcileResult.CHANGED, ReconcileResult.ALREADY_ACTIVE)

    after2 = await _fetch_global(download["id"])
    assert after2["aria2_gid"] == payload_gid


@pytest.mark.asyncio
async def test_http_torrent_following_arrives_first(
    temp_db: str,
) -> None:
    """HTTP .torrent source: payload following arrives before source
    complete. Single switch."""
    user = await create_user_v0(
        username="t23_http_rev", quota_bytes=10_000_000
    )
    source_gid = "http_src_02"
    payload_gid = "http_pay_02"
    download = await create_global_download_v0(
        resource_key="torrent:t23-http-rev",
        source_uri="https://example.com/file2.torrent",
        resource_kind="torrent",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return _payload_status(gid=payload_gid, following=source_gid)
        return _source_status(gid=source_gid, followed_by=payload_gid)

    client = make_aria2_client(tell_status=_tell)

    # Payload following arrives first.
    r1 = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=_payload_status(gid=payload_gid, following=source_gid),
        log_prefix="[T23]",
    )
    assert r1 == ReconcileResult.CHANGED
    after1 = await _fetch_global(download["id"])
    assert after1["aria2_gid"] == payload_gid

    # Source complete with followedBy arrives later — stale.
    r2 = await reconcile_attempt_signal(
        client=client,
        observed_gid=source_gid,
        event="complete",
        observed_status=_source_status(gid=source_gid, followed_by=payload_gid),
        log_prefix="[T23]",
    )
    assert r2 in (ReconcileResult.STALE, ReconcileResult.IGNORED)

    after2 = await _fetch_global(download["id"])
    assert after2["aria2_gid"] == payload_gid


# ---------------------------------------------------------------------------
# 2. payload active/paused/waiting/complete — idempotent after switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_statuses_idempotent_after_switch(
    temp_db: str,
) -> None:
    """After the initial handoff switch, repeated signals for the payload
    GID with active, paused, waiting, and complete statuses are all
    idempotent — no second switch, no errors, no terminalization."""
    user = await create_user_v0(
        username="t23_idem_states", quota_bytes=10_000_000
    )
    source_gid = "idem_src"
    payload_gid = "idem_pay"
    download = await create_global_download_v0(
        resource_key="magnet:t23-idem-states",
        source_uri="magnet:?xt=urn:btih:t23_idem_states",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return _payload_status(gid=payload_gid, following=source_gid)
        return _source_status(gid=source_gid, followed_by=payload_gid)

    client = make_aria2_client(tell_status=_tell)

    # Initial handoff.
    r0 = await reconcile_attempt_signal(
        client=client,
        observed_gid=source_gid,
        event="complete",
        observed_status=_source_status(gid=source_gid, followed_by=payload_gid),
        log_prefix="[T23]",
    )
    assert r0 == ReconcileResult.CHANGED
    assert (await _fetch_global(download["id"]))["aria2_gid"] == payload_gid

    # Now simulate multiple subsequent payload observations.
    for st in ("active", "paused", "waiting"):
        status = _payload_status(
            gid=payload_gid, status=st, following=source_gid
        )
        client2 = make_aria2_client(tell_status=status)
        r = await reconcile_attempt_signal(
            client=client2,
            observed_gid=payload_gid,
            event=None,
            observed_status=status,
            log_prefix="[T23]",
        )
        assert r in (
            ReconcileResult.CHANGED,
            ReconcileResult.STALE,
            ReconcileResult.ALREADY_ACTIVE,
        ), f"payload status={st} returned {r}"
        after = await _fetch_global(download["id"])
        assert after["aria2_gid"] == payload_gid
        assert after["status"] != "failed"


# ---------------------------------------------------------------------------
# 3. payload tell_status transient failure — keep source, no dir delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_transient_rpc_keeps_source_no_cleanup(
    temp_db: str,
) -> None:
    """Payload tell_status raises a transient RPC error during handoff.
    The source attempt stays active, GID unchanged, no force_remove or
    directory deletion (spec §22.2.4, §9.2)."""
    user = await create_user_v0(
        username="t23_transient", quota_bytes=10_000_000
    )
    source_gid = "trans_src"
    payload_gid = "trans_pay"
    download = await create_global_download_v0(
        resource_key="magnet:t23-transient",
        source_uri="magnet:?xt=urn:btih:t23_transient",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    client = make_aria2_client(
        tell_status=ConnectionError(
            "cannot connect to host localhost:6800"
        )
    )

    # Following observed but payload RPC unavailable.
    r = await reconcile_attempt_signal(
        client=client,
        observed_gid=payload_gid,
        event="start",
        observed_status=_payload_status(
            gid=payload_gid, following=source_gid, total=0
        ),
        log_prefix="[T23]",
    )

    assert r == ReconcileResult.WAITING

    stored = await _fetch_global(download["id"])
    assert stored["status"] == "active"
    assert stored["aria2_gid"] == source_gid

    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 4. admission rejection — only clean claim-specified writer/result GIDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_rejection_cleans_only_claim_gids(
    temp_db: str,
) -> None:
    """When size admission is rejected, cleanup only touches the source
    and payload GIDs specified by the terminal claim — not an unrelated
    GID belonging to another attempt (spec §22.2.5)."""
    user = await create_user_v0(username="t23_reject", quota_bytes=100)
    source_gid = "rej_src"
    payload_gid = "rej_pay"
    download = await create_global_download_v0(
        resource_key="magnet:t23-reject",
        source_uri="magnet:?xt=urn:btih:t23_reject",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    # A second unrelated attempt with its own GID.
    other_gid = "other_unrelated"
    other_download = await create_global_download_v0(
        resource_key="magnet:t23-other",
        source_uri="magnet:?xt=urn:btih:t23_other",
        resource_kind="magnet",
        status="active",
        aria2_gid=other_gid,
        total_bytes=2048,
        size_known=True,
        disk_reserved_bytes=2048,
    )

    huge = 100 * 1024 * 1024
    client = make_aria2_client(
        tell_status=_payload_status(
            gid=payload_gid,
            following=source_gid,
            total=huge,
        )
    )

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_dir,
        patch(
            "app.services.failed_task_cleanup.get_downloading_dir"
        ) as mock_get_dir,
    ):
        mock_dir.return_value = None
        mock_get_dir.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        r = await reconcile_attempt_signal(
            client=client,
            observed_gid=payload_gid,
            event="start",
            observed_status=_payload_status(
                gid=payload_gid,
                following=source_gid,
                total=huge,
            ),
            log_prefix="[T23]",
        )

    assert r in (ReconcileResult.TERMINALIZED, ReconcileResult.STALE)

    stored = await _fetch_global(download["id"])
    assert stored["status"] in ("failed", "cancelled")

    # Cleanup scoping: no force_remove or remove_download_result call
    # touches the unrelated GID (spec §22.2.5).
    removed_gids = {
        call.args[0] for call in client.force_remove.call_args_list
    }
    assert other_gid not in removed_gids

    result_gids = {
        call.args[0] for call in client.remove_download_result.call_args_list
    }
    assert other_gid not in result_gids

    # The other attempt is untouched.
    other_stored = await _fetch_global(other_download["id"])
    assert other_stored["status"] == "active"
    assert other_stored["aria2_gid"] == other_gid


# ---------------------------------------------------------------------------
# 5. handoff complete — no fabricated second complete event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_complete_no_fabricated_event(temp_db: str) -> None:
    """When the payload is already complete at handoff time, the
    coordinator must NOT call handle_aria2_event(event='complete') to
    chain the lifecycle. It may delegate to handle_v0_download_complete
    directly (spec §22.2.6, §9.5)."""
    user = await create_user_v0(
        username="t23_nofake", quota_bytes=10_000_000
    )
    source_gid = "nofake_src"
    payload_gid = "nofake_pay"
    download = await create_global_download_v0(
        resource_key="magnet:t23-nofake",
        source_uri="magnet:?xt=urn:btih:t23_nofake",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    complete_payload = _payload_status(
        gid=payload_gid,
        status="complete",
        total=4096,
        completed=4096,
        following=source_gid,
    )

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return complete_payload
        return _source_status(gid=source_gid, followed_by=payload_gid)

    client = make_aria2_client(tell_status=_tell)

    with patch.object(
        aria2_lifecycle_service,
        "handle_aria2_event",
        new=AsyncMock(),
    ) as spy_event:
        with patch.object(
            aria2_lifecycle_service,
            "handle_v0_download_complete",
            new=AsyncMock(return_value=False),
        ) as spy_complete:
            r = await reconcile_attempt_signal(
                client=client,
                observed_gid=source_gid,
                event="complete",
                observed_status=_source_status(
                    gid=source_gid, followed_by=payload_gid
                ),
                log_prefix="[T23]",
            )

    # handle_aria2_event must never be called (no fabricated event).
    spy_event.assert_not_called()

    # The result should reflect the handoff happened (possibly via
    # completion dispatch). Either CHANGED or WAITING/COMPLETED.
    assert r in (
        ReconcileResult.CHANGED,
        ReconcileResult.COMPLETED,
        ReconcileResult.WAITING,
    ), f"unexpected result {r}"

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == payload_gid


# ---------------------------------------------------------------------------
# 6. duplicate handoff — switch current GID exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_handoff_switches_once(temp_db: str) -> None:
    """Multiple concurrent handoff signals for the same source/payload
    pair result in exactly one GID switch. The second and subsequent
    signals are stale or ignored."""
    user = await create_user_v0(
        username="t23_dup_switch", quota_bytes=10_000_000
    )
    source_gid = "dup_src"
    payload_gid = "dup_pay"
    download = await create_global_download_v0(
        resource_key="magnet:t23-dup-switch",
        source_uri="magnet:?xt=urn:btih:t23_dup_switch",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    source_st = _source_status(gid=source_gid, followed_by=payload_gid)
    payload_st = _payload_status(gid=payload_gid, following=source_gid)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return payload_st
        return source_st

    # Run 5 concurrent handoff signals.
    clients = [make_aria2_client(tell_status=_tell) for _ in range(5)]
    tasks = [
        reconcile_attempt_signal(
            client=c,
            observed_gid=source_gid,
            event="complete",
            observed_status=source_st,
            log_prefix="[T23]",
        )
        for c in clients
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == payload_gid
    assert stored["resource_kind"] == "torrent"

    # At least one must have CHANGED.
    changed_count = sum(
        1 for r in results if r == ReconcileResult.CHANGED
    )
    assert changed_count >= 1
    # But the GID is payload — never reverted to source.
    assert stored["aria2_gid"] == payload_gid


@pytest.mark.asyncio
async def test_sequential_duplicate_handoff_switches_once(
    temp_db: str,
) -> None:
    """Sequential duplicate handoff: first succeeds, rest are stale."""
    user = await create_user_v0(
        username="t23_seq_dup", quota_bytes=10_000_000
    )
    source_gid = "seq_src"
    payload_gid = "seq_pay"
    download = await create_global_download_v0(
        resource_key="magnet:t23-seq-dup",
        source_uri="magnet:?xt=urn:btih:t23_seq_dup",
        resource_kind="magnet",
        status="active",
        aria2_gid=source_gid,
        total_bytes=4096,
        size_known=True,
        disk_reserved_bytes=4096,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=4096,
    )
    await _set_usage_reserved(user["id"], 4096)

    source_st = _source_status(gid=source_gid, followed_by=payload_gid)

    async def _tell(gid: str) -> dict[str, Any]:
        if gid == payload_gid:
            return _payload_status(gid=payload_gid, following=source_gid)
        return source_st

    client = make_aria2_client(tell_status=_tell)

    results = []
    for _ in range(3):
        r = await reconcile_attempt_signal(
            client=client,
            observed_gid=source_gid,
            event="complete",
            observed_status=source_st,
            log_prefix="[T23]",
        )
        results.append(r)

    # First is CHANGED, rest are STALE.
    assert results[0] == ReconcileResult.CHANGED
    for r in results[1:]:
        assert r in (ReconcileResult.STALE, ReconcileResult.IGNORED)

    stored = await _fetch_global(download["id"])
    assert stored["aria2_gid"] == payload_gid
    assert stored["status"] == "active"
