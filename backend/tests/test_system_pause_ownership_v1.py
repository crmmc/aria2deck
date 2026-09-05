"""M7: system pause ownership — constants, call-site gate, policy clear."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.lifecycle import ReconcileResult
from app.modules.task_core.policy import QuotaContext, decide_on_snapshot
from app.modules.task_core.states import (
    ACTIVE_CLEAR_ERROR_CODES,
    ERROR_ADMISSION_PAUSED,
    ERROR_DISK_QUEUED,
    ERROR_EXTERNAL_PAUSED,
    ERROR_GROWTH_PAUSE_FAILED,
    ERROR_GROWTH_UNPAUSE_FAILED,
    ERROR_METADATA_ADMISSION_PAUSED,
    ERROR_QUOTA_QUEUED,
    ERROR_UNPAUSE_FAILED,
    PENDING_RELEASE_CODES,
    PROJECTION_PROTECTED_ERROR_CODES,
    SYSTEM_OWNED_PAUSE_CODES,
)
from app.services.lifecycle._shared import system_pause_gid, system_unpause_gid
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


def test_system_owned_codes_include_all_pause_ownership_constants() -> None:
    required = {
        ERROR_ADMISSION_PAUSED,
        ERROR_METADATA_ADMISSION_PAUSED,
        ERROR_UNPAUSE_FAILED,
        ERROR_GROWTH_UNPAUSE_FAILED,
        ERROR_GROWTH_PAUSE_FAILED,
        "quota_queued",
        "disk_queued",
    }
    assert required <= SYSTEM_OWNED_PAUSE_CODES
    assert ERROR_EXTERNAL_PAUSED not in SYSTEM_OWNED_PAUSE_CODES
    assert SYSTEM_OWNED_PAUSE_CODES <= PROJECTION_PROTECTED_ERROR_CODES
    assert SYSTEM_OWNED_PAUSE_CODES <= ACTIVE_CLEAR_ERROR_CODES
    assert ERROR_EXTERNAL_PAUSED in ACTIVE_CLEAR_ERROR_CODES


def test_pending_release_codes_contract() -> None:
    """M9: PENDING is the auto-resume subset; external is never pending/owned."""
    expected_pending = frozenset(
        {
            ERROR_ADMISSION_PAUSED,
            ERROR_METADATA_ADMISSION_PAUSED,
            ERROR_UNPAUSE_FAILED,
            ERROR_GROWTH_UNPAUSE_FAILED,
        }
    )
    assert PENDING_RELEASE_CODES == expected_pending
    # growth_pause_failed is system-owned but not auto-resumed.
    assert ERROR_GROWTH_PAUSE_FAILED not in PENDING_RELEASE_CODES
    assert ERROR_GROWTH_PAUSE_FAILED in SYSTEM_OWNED_PAUSE_CODES
    # SYSTEM_OWNED = PENDING ∪ queued ∪ growth_pause_failed
    assert SYSTEM_OWNED_PAUSE_CODES == (
        PENDING_RELEASE_CODES
        | frozenset(
            {
                ERROR_QUOTA_QUEUED,
                ERROR_DISK_QUEUED,
                ERROR_GROWTH_PAUSE_FAILED,
            }
        )
    )
    assert ERROR_EXTERNAL_PAUSED not in PENDING_RELEASE_CODES
    assert ERROR_EXTERNAL_PAUSED not in SYSTEM_OWNED_PAUSE_CODES
    assert PENDING_RELEASE_CODES <= SYSTEM_OWNED_PAUSE_CODES
    assert PENDING_RELEASE_CODES <= PROJECTION_PROTECTED_ERROR_CODES
    assert PENDING_RELEASE_CODES <= ACTIVE_CLEAR_ERROR_CODES


def test_lifecycle_pause_gid_only_in_system_helpers() -> None:
    """AC-8: no direct backend.pause_gid/unpause_gid outside _shared helpers."""
    root = Path(__file__).resolve().parents[1] / "app" / "services" / "lifecycle"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            # Only flag real RPC call forms, not comments/docstrings about them.
            if (
                "await backend.pause_gid(" in stripped
                or "await backend.unpause_gid(" in stripped
            ):
                if path.name != "_shared.py":
                    offenders.append(f"{path.name}:{i}:{stripped}")
            elif (
                ".pause_gid(" in stripped or ".unpause_gid(" in stripped
            ) and path.name != "_shared.py":
                if "system_pause_gid" in stripped or "system_unpause_gid" in stripped:
                    continue
                # Import/type lines without call paren already excluded by '('
                offenders.append(f"{path.name}:{i}:{stripped}")
    assert offenders == []


def test_policy_clears_owned_code_when_active() -> None:
    decision = decide_on_snapshot(
        {"error_code": ERROR_GROWTH_UNPAUSE_FAILED, "size_known": 1, "total_bytes": 100},
        "active",
    )
    assert decision.action == "clear_error_code"
    assert decision.clear_error_code is True


def test_policy_metadata_admission_paused_active_no_generic_clear() -> None:
    """T9c: metadata_admission_paused + active/waiting must not generic clear."""
    for status in ("active", "waiting"):
        decision = decide_on_snapshot(
            {
                "error_code": ERROR_METADATA_ADMISSION_PAUSED,
                "size_known": 0,
                "total_bytes": 0,
            },
            status,
        )
        assert decision.action != "clear_error_code"
        assert decision.clear_error_code is False


def test_policy_resumes_unpause_failed_when_paused_size_known() -> None:
    decision = decide_on_snapshot(
        {"error_code": ERROR_UNPAUSE_FAILED, "size_known": 1, "total_bytes": 50},
        "paused",
    )
    assert decision.action == "resume"
    assert decision.clear_error_code is True


def test_policy_never_resumes_external() -> None:
    decision = decide_on_snapshot(
        {"error_code": ERROR_EXTERNAL_PAUSED, "size_known": 1, "total_bytes": 50},
        "paused",
    )
    assert decision.action == "keep"


def test_policy_marks_bare_paused_external() -> None:
    decision = decide_on_snapshot(
        {"error_code": None, "size_known": 1, "total_bytes": 50},
        "paused",
        quota=QuotaContext(),
    )
    assert decision.action == "mark_external_paused"
    assert decision.error_code == ERROR_EXTERNAL_PAUSED


def test_policy_growth_pause_failed_paused_keeps_not_resume() -> None:
    """AC-6: growth_pause_failed is owned but not auto-resumed while paused."""
    decision = decide_on_snapshot(
        {
            "error_code": ERROR_GROWTH_PAUSE_FAILED,
            "size_known": 1,
            "total_bytes": 200,
        },
        "paused",
    )
    assert decision.action == "keep"
    assert decision.error_code == ERROR_GROWTH_PAUSE_FAILED


def test_policy_growth_pause_failed_active_clears() -> None:
    decision = decide_on_snapshot(
        {
            "error_code": ERROR_GROWTH_PAUSE_FAILED,
            "size_known": 1,
            "total_bytes": 200,
        },
        "active",
    )
    assert decision.action == "clear_error_code"
    assert decision.clear_error_code is True


@pytest.mark.asyncio
async def test_system_pause_success_stamps_ownership(temp_db: str) -> None:
    """Successful system pause must stamp ownership_error_code (no bare pause)."""
    download = await create_global_download_v0(
        resource_key="http:m7-pause-stamp",
        source_uri="https://example.com/a.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_pause_stamp",
        total_bytes=100,
        size_known=True,
    )
    client = make_aria2_client(pause="OK")
    result = await system_pause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_pause_stamp",
        expected_gid="gid_pause_stamp",
        failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
        failure_message="任务大小增长时无法安全暂停",
        ownership_error_code=ERROR_METADATA_ADMISSION_PAUSED,
        acquire_lifecycle_lock=False,
    )
    assert result == "success"
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED


@pytest.mark.asyncio
async def test_soft_mark_does_not_default_paused_when_requery_unknown(
    temp_db: str,
) -> None:
    """AC soft re-query: unknown re_raw must not force status=paused."""
    download = await create_global_download_v0(
        resource_key="http:m7-soft-status",
        source_uri="https://example.com/b.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_soft_status",
        total_bytes=100,
        size_known=True,
    )
    # pause fails; re-query returns an unexpected status (not in success set).
    client = make_aria2_client(
        pause=Exception("pause boom"),
        tell_status={
            "status": "unknown",
            "totalLength": "100",
            "completedLength": "0",
        },
    )
    result = await system_pause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_soft_status",
        expected_gid="gid_soft_status",
        failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
        failure_message="任务大小增长时无法安全暂停",
        acquire_lifecycle_lock=False,
    )
    assert result == "soft_failed"
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] == ERROR_GROWTH_PAUSE_FAILED
    # Must keep live DB status when re-query does not map to a known live status.
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_system_unpause_rpc_ok_still_paused_soft_failed(temp_db: str) -> None:
    """AC-3 / T10: unpause RPC ok but tell_status still paused → soft_failed."""
    download = await create_global_download_v0(
        resource_key="http:m9-unpause-still-paused",
        source_uri="https://example.com/still-paused.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_unpause_paused",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_UNPAUSE_FAILED,
        error_message="磁力任务准入后恢复下载失败",
    )
    client = make_aria2_client(
        unpause="OK",
        tell_status={
            "status": "paused",
            "totalLength": "100",
            "completedLength": "0",
        },
    )
    result = await system_unpause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_unpause_paused",
        expected_gid="gid_unpause_paused",
        failure_error_code=ERROR_UNPAUSE_FAILED,
        failure_message="磁力任务准入后恢复下载失败",
        acquire_lifecycle_lock=False,
    )
    assert result == "soft_failed"
    client.unpause.assert_awaited_once_with("gid_unpause_paused")
    client.tell_status.assert_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    # Soft path must stamp/keep failure code; never treat RPC-ok as success.
    assert row["error_code"] == ERROR_UNPAUSE_FAILED
    assert row["status"] == "paused"


@pytest.mark.asyncio
async def test_system_unpause_rpc_ok_active_success(temp_db: str) -> None:
    """AC-3: unpause RPC ok + re-query active → success."""
    download = await create_global_download_v0(
        resource_key="http:m9-unpause-active",
        source_uri="https://example.com/unpause-active.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_unpause_active",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_UNPAUSE_FAILED,
    )
    client = make_aria2_client(
        unpause="OK",
        tell_status={
            "status": "active",
            "totalLength": "100",
            "completedLength": "0",
        },
    )
    result = await system_unpause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_unpause_active",
        expected_gid="gid_unpause_active",
        failure_error_code=ERROR_UNPAUSE_FAILED,
        failure_message="磁力任务准入后恢复下载失败",
        acquire_lifecycle_lock=False,
    )
    assert result == "success"
    client.unpause.assert_awaited_once_with("gid_unpause_active")
    client.tell_status.assert_awaited()


@pytest.mark.asyncio
async def test_system_unpause_rpc_ok_waiting_success(temp_db: str) -> None:
    """AC-3 / T13: unpause RPC ok + re-query waiting → success."""
    download = await create_global_download_v0(
        resource_key="http:m9-unpause-waiting",
        source_uri="https://example.com/unpause-waiting.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_unpause_waiting",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_UNPAUSE_FAILED,
    )
    client = make_aria2_client(
        unpause="OK",
        tell_status={
            "status": "waiting",
            "totalLength": "100",
            "completedLength": "0",
        },
    )
    result = await system_unpause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_unpause_waiting",
        expected_gid="gid_unpause_waiting",
        failure_error_code=ERROR_UNPAUSE_FAILED,
        failure_message="磁力任务准入后恢复下载失败",
        acquire_lifecycle_lock=False,
    )
    assert result == "success"
    client.unpause.assert_awaited_once_with("gid_unpause_waiting")
    client.tell_status.assert_awaited()


@pytest.mark.asyncio
async def test_handoff_unpause_soft_keeps_live(temp_db: str) -> None:
    """AC-1: handoff unpause fail + re-query paused → live + unpause_failed."""
    from app.services.lifecycle.coordinator import reconcile_attempt_signal

    user = await create_user_v0(username="m7_handoff_soft", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:m7-handoff-soft",
        source_uri="magnet:?xt=urn:btih:m7handoffsoft",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_src_m7",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload = "gid_payload_m7"
    observed = {
        "status": "complete",
        "followedBy": [payload],
        "totalLength": "0",
        "completedLength": "0",
    }
    client = make_aria2_client(
        unpause=Exception("unpause boom"),
        tell_status={
            "status": "paused",
            "totalLength": "4096",
            "completedLength": "0",
            "files": [
                {"path": "/dl/a.bin", "length": "4096", "selected": "true"},
            ],
            "bittorrent": {"info": {"name": "a.bin"}},
        },
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"
        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_src_m7",
            event="complete",
            observed_status=observed,
            log_prefix="[M7]",
        )

    assert result != ReconcileResult.TERMINALIZED
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["status"] != "failed"
    # Soft unpause failure with re-query still paused keeps system ownership.
    assert row["status"] == "paused"
    assert row["error_code"] in {
        ERROR_UNPAUSE_FAILED,
        ERROR_METADATA_ADMISSION_PAUSED,
        ERROR_ADMISSION_PAUSED,
    }
    assert row["aria2_gid"] == payload
    mock_dir.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_unpause_fail_requery_active_clears_ownership(
    temp_db: str,
) -> None:
    """AC-2: unpause throws but re-query active → success path, clear ownership."""
    from app.services.lifecycle.coordinator import reconcile_attempt_signal

    user = await create_user_v0(username="m7_handoff_active", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:m7-handoff-active",
        source_uri="magnet:?xt=urn:btih:m7handoffactive",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_src_m7a",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload = "gid_payload_m7a"
    # pause-metadata style: payload already paused with trusted size; unpause
    # RPC fails but re-query shows active (race: unpause actually took effect).
    tell_calls = {"n": 0}

    async def _tell(gid: str) -> dict:
        tell_calls["n"] += 1
        if gid == payload and tell_calls["n"] >= 2:
            # post-unpause re-query: already active
            return {
                "status": "active",
                "totalLength": "4096",
                "completedLength": "0",
                "files": [
                    {"path": "/dl/b.bin", "length": "4096", "selected": "true"},
                ],
                "bittorrent": {"info": {"name": "b.bin"}},
            }
        return {
            "status": "paused",
            "totalLength": "4096",
            "completedLength": "0",
            "files": [
                {"path": "/dl/b.bin", "length": "4096", "selected": "true"},
            ],
            "bittorrent": {"info": {"name": "b.bin"}},
        }

    client = make_aria2_client(unpause=Exception("unpause boom"))
    client.tell_status.side_effect = _tell

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"
        result = await reconcile_attempt_signal(
            backend=client,
            observed_gid="gid_src_m7a",
            event="complete",
            observed_status={
                "status": "complete",
                "followedBy": [payload],
                "totalLength": "0",
                "completedLength": "0",
            },
            log_prefix="[M7]",
        )

    assert result != ReconcileResult.TERMINALIZED
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["status"] != "failed"
    assert row["aria2_gid"] == payload
    # Re-query active is UNPAUSE success → clear ownership and project active.
    assert row["status"] == "active"
    assert row["error_code"] is None
    mock_dir.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_size_pause_stamps_metadata_code(temp_db: str) -> None:
    """Unknown-size handoff pause success must not leave bare paused/waiting."""
    from app.services.lifecycle.handoff import _handoff_locked

    user = await create_user_v0(username="m7_unknown_size", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="magnet:m7-unknown",
        source_uri="magnet:?xt=urn:btih:m7unknown",
        resource_kind="magnet",
        status="active",
        aria2_gid="gid_src_unk",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
    )

    payload = "gid_payload_unk"
    # Pre-fetched payload is active/unknown-size; post-pause re-query is paused.
    client = make_aria2_client(
        pause="OK",
        tell_status={
            "status": "paused",
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
    )

    result, _ = await _handoff_locked(
        backend=client,
        attempt_id=int(download["id"]),
        source_gid="gid_src_unk",
        payload_gid=payload,
        snapshot={
            "aria2_gid": "gid_src_unk",
            "status": "active",
        },
        download=dict(download),
        log_prefix="[M7]",
        _payload_status={
            "status": "active",
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        },
    )
    assert result == ReconcileResult.WAITING
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    # Ownership must be stamped on successful system pause for unknown-size.
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED


@pytest.mark.asyncio
async def test_system_pause_preserves_existing_owned_code(temp_db: str) -> None:
    """Successful pause must not overwrite an existing SYSTEM_OWNED code."""
    download = await create_global_download_v0(
        resource_key="http:m7-preserve-owned",
        source_uri="https://example.com/c.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_preserve",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_METADATA_ADMISSION_PAUSED,
    )
    client = make_aria2_client(pause="OK")
    result = await system_pause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_preserve",
        expected_gid="gid_preserve",
        failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
        failure_message="任务大小增长时无法安全暂停",
        ownership_error_code=ERROR_ADMISSION_PAUSED,
        acquire_lifecycle_lock=False,
    )
    assert result == "success"
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED


@pytest.mark.asyncio
async def test_growth_pause_success_stamps_ownership_when_not_resuming(
    temp_db: str,
) -> None:
    """Growth pause with resume_after_admission=False must leave ownership code."""
    from app.services.lifecycle.handoff import coordinate_reported_size

    user = await create_user_v0(username="m7_growth_own", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:m7-growth-own",
        source_uri="https://example.com/grow-own.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_growth_own",
        total_bytes=1024,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=1024,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    client = make_aria2_client(pause="OK")
    result = await coordinate_reported_size(
        backend=client,
        download=dict(download),
        expected_gid="gid_growth_own",
        control_gid="gid_growth_own",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "0",
        },
        resume_after_admission=False,
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True
    client.pause.assert_awaited()
    client.unpause.assert_not_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
    assert int(row["total_bytes"]) == 2048


@pytest.mark.asyncio
async def test_growth_pause_unpause_success_clears_ownership(temp_db: str) -> None:
    """Growth pause+unpause: re-query active success clears ownership (not RPC-only)."""
    from app.services.lifecycle.handoff import coordinate_reported_size

    user = await create_user_v0(username="m7_growth_clear", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:m7-growth-clear",
        source_uri="https://example.com/grow-clear.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_growth_clear",
        total_bytes=1024,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=1024,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    # Default tell_status is active → unpause re-query success path.
    client = make_aria2_client(pause="OK", unpause="OK")
    result = await coordinate_reported_size(
        backend=client,
        download=dict(download),
        expected_gid="gid_growth_clear",
        control_gid="gid_growth_clear",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "0",
        },
        resume_after_admission=True,
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True
    client.pause.assert_awaited()
    client.unpause.assert_awaited()
    client.tell_status.assert_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] is None
    assert int(row["total_bytes"]) == 2048


@pytest.mark.asyncio
async def test_apply_clear_error_code_does_not_unpause(temp_db: str) -> None:
    """clear_error_code only clears DB ownership; no backend unpause."""
    from unittest.mock import AsyncMock

    from app.modules.backend.port import BackendPort
    from app.modules.task_core.policy import Decision, apply_decision

    download = await create_global_download_v0(
        resource_key="http:m7-clear-apply",
        source_uri="https://example.com/clear.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_clear_apply",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_GROWTH_UNPAUSE_FAILED,
        error_message="任务大小调整后恢复下载失败",
    )
    backend = AsyncMock(spec=BackendPort)
    await apply_decision(
        backend,
        int(download["id"]),
        Decision("clear_error_code", clear_error_code=True),
    )
    backend.unpause.assert_not_awaited()
    backend.pause.assert_not_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] is None
    assert row["error_message"] is None
    assert row["status"] == "active"


# ---------------------------------------------------------------------------
# M9 Task 4 — create-time stamp + pause-start DB status (T1/T2/T11/T14/T14b)
# ---------------------------------------------------------------------------


async def _fetch_download(download_id: int) -> dict:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == download_id)
            )
        ).mappings().one()
    return dict(row)


@pytest.mark.asyncio
async def test_assign_submitted_gid_stamps_error_code_same_txn(temp_db: str) -> None:
    """§3.2.0: assign can write error_code in the same UPDATE as gid+status."""
    from app.repositories.task.downloads import assign_submitted_gid

    download = await create_global_download_v0(
        resource_key="http:assign-stamp",
        source_uri="https://example.com/stamp.bin",
        resource_kind="http",
        status="queued",
        aria2_gid=None,
        total_bytes=0,
        size_known=False,
    )
    row = await assign_submitted_gid(
        download_id=download["id"],
        gid="gid-stamp",
        status="paused",
        error_code=ERROR_ADMISSION_PAUSED,
        error_message=None,
    )
    assert row is not None
    assert row["aria2_gid"] == "gid-stamp"
    assert row["status"] == "paused"
    assert row["error_code"] == ERROR_ADMISSION_PAUSED

    stored = await _fetch_download(download["id"])
    assert stored["error_code"] == ERROR_ADMISSION_PAUSED
    assert stored["status"] == "paused"


@pytest.mark.asyncio
async def test_assign_submitted_gid_default_no_error_code(temp_db: str) -> None:
    """Old callers without error_code remain compatible (no forced stamp)."""
    from app.repositories.task.downloads import assign_submitted_gid

    download = await create_global_download_v0(
        resource_key="http:assign-nostamp",
        status="queued",
        aria2_gid=None,
        total_bytes=100,
        size_known=True,
    )
    row = await assign_submitted_gid(
        download_id=download["id"], gid="gid-nostamp", status="active"
    )
    assert row is not None
    assert row["error_code"] is None
    stored = await _fetch_download(download["id"])
    assert stored["error_code"] is None
    assert stored["status"] == "active"


@pytest.mark.asyncio
async def test_t1_http_unknown_submit_stamps_admission_paused(temp_db: str) -> None:
    """T1 / AC-1: HTTP unknown-size submit uses pause and stamps admission_paused."""
    from app.modules.backend.aria2_adapter import Aria2BackendAdapter

    download = await create_global_download_v0(
        resource_key="http:t1-unknown",
        source_uri="https://example.com/t1.bin",
        resource_kind="http",
        status="queued",
        total_bytes=0,
        size_known=False,
    )
    client = make_aria2_client(add_uri="gid-t1")
    adapter = Aria2BackendAdapter(client)
    gid = await adapter.submit(
        tid=download["id"],
        uri="https://example.com/t1.bin",
        options={},
    )
    assert gid == "gid-t1"
    _, opts = client.add_uri.await_args.args
    assert opts.get("pause") == "true"
    row = await _fetch_download(download["id"])
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED
    assert row["status"] != "active"
    assert row["status"] == "paused"
    assert row["aria2_gid"] == "gid-t1"


@pytest.mark.asyncio
async def test_t2_magnet_submit_stamps_metadata_admission_paused(temp_db: str) -> None:
    """T2 / AC-1: magnet submit uses pause-metadata and stamps metadata_admission_paused."""
    from app.modules.backend.aria2_adapter import Aria2BackendAdapter

    magnet = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"
    download = await create_global_download_v0(
        resource_key=magnet,
        source_uri=magnet,
        resource_kind="magnet",
        status="queued",
        total_bytes=0,
        size_known=False,
    )
    client = make_aria2_client(add_uri="gid-t2")
    adapter = Aria2BackendAdapter(client)
    gid = await adapter.submit(tid=download["id"], uri=magnet, options={})
    assert gid == "gid-t2"
    _, opts = client.add_uri.await_args.args
    assert opts.get("pause-metadata") == "true"
    row = await _fetch_download(download["id"])
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED
    assert row["status"] != "active"
    assert row["status"] == "paused"
    assert row["aria2_gid"] == "gid-t2"


@pytest.mark.asyncio
async def test_t14_torrent_partial_select_pause_and_stamp(temp_db: str) -> None:
    """T14 / AC-9: torrent partial select-file with pause=true and admission_paused."""
    from app.modules.backend.aria2_adapter import Aria2BackendAdapter

    download = await create_global_download_v0(
        resource_key="torrent:t14-partial",
        source_uri="base64:AAAA",
        resource_kind="torrent",
        status="queued",
        total_bytes=400,
        size_known=True,
    )
    client = make_aria2_client(add_torrent="gid-t14")
    adapter = Aria2BackendAdapter(client)
    gid = await adapter.submit(
        tid=download["id"],
        uri="base64:AAAA",
        options={"select-file": "1,3"},
    )
    assert gid == "gid-t14"
    torrent_b64, uris, opts = client.add_torrent.await_args.args
    assert torrent_b64 == "AAAA"
    assert uris == []
    assert opts.get("pause") == "true"
    assert opts.get("select-file") == "1,3"
    row = await _fetch_download(download["id"])
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED
    assert row["status"] != "active"
    assert row["status"] == "paused"


@pytest.mark.asyncio
async def test_t14b_torrent_full_select_same_pause_stamp(temp_db: str) -> None:
    """T14b / AC-9: torrent full selection uses the same pause + stamp strategy."""
    from app.modules.backend.aria2_adapter import Aria2BackendAdapter

    download = await create_global_download_v0(
        resource_key="torrent:t14b-full",
        source_uri="base64:BBBB",
        resource_kind="torrent",
        status="queued",
        total_bytes=1024,
        size_known=True,
    )
    client = make_aria2_client(add_torrent="gid-t14b")
    adapter = Aria2BackendAdapter(client)
    gid = await adapter.submit(tid=download["id"], uri="base64:BBBB", options={})
    assert gid == "gid-t14b"
    _, _, opts = client.add_torrent.await_args.args
    assert opts.get("pause") == "true"
    row = await _fetch_download(download["id"])
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
    assert row["status"] == "paused"
    assert row["status"] != "active"


@pytest.mark.asyncio
async def test_t11_http_pause_start_two_round_release(temp_db: str) -> None:
    """T11 / AC-8: pause-start keeps code while still paused; clears after active."""
    from unittest.mock import AsyncMock

    from app.modules.backend.aria2_adapter import Aria2BackendAdapter
    from app.modules.backend.port import BackendPort, Snapshot
    from app.modules.task_core.sync import sync_once

    download = await create_global_download_v0(
        resource_key="http:t11-release",
        source_uri="https://example.com/t11.bin",
        resource_kind="http",
        status="queued",
        total_bytes=0,
        size_known=False,
    )
    user = await create_user_v0(username="t11-user", quota_bytes=10**9)
    await create_user_task_v0(
        user_id=user["id"], global_download_id=download["id"], status="queued"
    )

    client = make_aria2_client(add_uri="gid-t11")
    adapter = Aria2BackendAdapter(client)
    await adapter.submit(
        tid=download["id"],
        uri="https://example.com/t11.bin",
        options={},
    )
    row = await _fetch_download(download["id"])
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
    assert row["status"] == "paused"

    # Round 1: policy resumes but re-query still paused → keep ownership code.
    backend = AsyncMock(spec=BackendPort)
    backend.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=download["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "0"},
            )
        ]
    )
    backend.tell_status = AsyncMock(return_value={"status": "paused"})
    await sync_once(backend)
    backend.unpause.assert_awaited()
    row = await _fetch_download(download["id"])
    assert row["error_code"] in {ERROR_ADMISSION_PAUSED, ERROR_UNPAUSE_FAILED}
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED

    # Round 2: re-query active → clear pending code.
    backend2 = AsyncMock(spec=BackendPort)
    backend2.tell_many = AsyncMock(
        return_value=[
            Snapshot(
                tid=download["id"],
                status="paused",
                raw={"completedLength": "0", "totalLength": "100"},
            )
        ]
    )
    backend2.tell_status = AsyncMock(return_value={"status": "active"})
    await sync_once(backend2)
    backend2.unpause.assert_awaited()
    row = await _fetch_download(download["id"])
    assert row["error_code"] is None


# ---------------------------------------------------------------------------
# Task 6 — coordinator projection: SYSTEM_OWNED/PENDING never brand external
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owned_code",
    [
        ERROR_ADMISSION_PAUSED,
        ERROR_METADATA_ADMISSION_PAUSED,
        ERROR_QUOTA_QUEUED,
        ERROR_DISK_QUEUED,
        ERROR_GROWTH_PAUSE_FAILED,
        ERROR_UNPAUSE_FAILED,
    ],
)
async def test_t7_system_owned_paused_projection_keeps_code_not_external(
    temp_db: str,
    owned_code: str,
) -> None:
    """T7 / AC-5: owned/pending code + live paused must not write external."""
    from app.domain.lifecycle import ReconcileResult
    from app.services.lifecycle.coordinator import reconcile_attempt_signal

    download = await create_global_download_v0(
        resource_key=f"http:t7-owned-{owned_code}",
        source_uri="https://example.com/t7.bin",
        resource_kind="http",
        status="paused",
        aria2_gid=f"gid_t7_{owned_code}",
        total_bytes=1000,
        size_known=True,
        completed_bytes=100,
        disk_reserved_bytes=100,
        error_code=owned_code,
        error_message="system owned",
    )
    user = await create_user_v0(
        username=f"t7_{owned_code}", quota_bytes=10_000_000
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
    )

    paused_status = {
        "gid": f"gid_t7_{owned_code}",
        "status": "paused",
        "totalLength": "1000",
        "completedLength": "100",
        "files": [
            {
                "path": "/tmp/t7.bin",
                "length": "1000",
                "selected": "true",
            }
        ],
    }
    client = make_aria2_client(tell_status=paused_status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=f"gid_t7_{owned_code}",
        event=None,
        observed_status=paused_status,
        log_prefix="[T7]",
    )
    assert result in (ReconcileResult.CHANGED, ReconcileResult.STALE)

    row = await _fetch_download(download["id"])
    assert row["status"] == "paused"
    assert row["error_code"] == owned_code
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED
    assert "外部暂停" not in (row["error_message"] or "")


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_t9c_coordinator_metadata_active_keeps_metadata_admission_paused(
    temp_db: str,
) -> None:
    """T9c lifecycle: metadata-phase active must not clear metadata_admission_paused.

    Spec §3.1.1 / review P-1: coordinator ACTIVE_CLEAR must not strip the
    create-time magnet credential while aria2 is still in [METADATA] phase.
    """
    from app.domain.lifecycle import ReconcileResult
    from app.services.lifecycle.coordinator import reconcile_attempt_signal

    gid = "gid_t9c_meta_active"
    download = await create_global_download_v0(
        resource_key="magnet:t9c-meta-active",
        source_uri="magnet:?xt=urn:btih:t9cmetaactive",
        resource_kind="magnet",
        status="paused",
        aria2_gid=gid,
        total_bytes=0,
        size_known=False,
        error_code=ERROR_METADATA_ADMISSION_PAUSED,
        error_message=None,
    )
    user = await create_user_v0(username="t9c_coord", quota_bytes=10_000_000)
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
    )

    # aria2 metadata phase: active + [METADATA] path, no bittorrent.info dict
    meta_status = {
        "gid": gid,
        "status": "active",
        "totalLength": "0",
        "completedLength": "1024",
        "bittorrent": {},  # not a full info dict → metadata phase with path
        "files": [
            {
                "path": "[METADATA]t9c",
                "length": "0",
                "selected": "true",
            }
        ],
    }
    client = make_aria2_client(tell_status=meta_status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid=gid,
        event=None,
        observed_status=meta_status,
        log_prefix="[T9c-coord]",
    )
    assert result in (
        ReconcileResult.CHANGED,
        ReconcileResult.STALE,
        ReconcileResult.WAITING,
    )

    row = await _fetch_download(download["id"])
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED
    assert "外部暂停" not in (row["error_message"] or "")


async def test_t12_waiting_unknown_size_with_pending_code_waits(
    temp_db: str,
) -> None:
    """T12 / §3.3.1: waiting + totalLength=0 + pending code → WAITING, not kill."""
    from app.domain.lifecycle import ReconcileResult
    from app.services.lifecycle.coordinator import reconcile_attempt_signal

    download = await create_global_download_v0(
        resource_key="http:t12-waiting-pending",
        source_uri="https://example.com/t12w.bin",
        resource_kind="http",
        status="waiting",
        aria2_gid="gid_t12_waiting",
        total_bytes=0,
        size_known=False,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    user = await create_user_v0(username="t12_waiting", quota_bytes=10_000_000)
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="waiting",
    )
    status = {
        "gid": "gid_t12_waiting",
        "status": "waiting",
        "totalLength": "0",
        "completedLength": "0",
        "files": [],
    }
    client = make_aria2_client(tell_status=status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_t12_waiting",
        event=None,
        observed_status=status,
        log_prefix="[T12]",
    )
    assert result == ReconcileResult.WAITING
    row = await _fetch_download(download["id"])
    assert row["status"] != "failed"
    assert row["error_code"] == ERROR_ADMISSION_PAUSED
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED
    assert row["error_code"] != "unknown_size"
    client.force_remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 7 — Path AC Contract: dual paths forbid "RPC-only clear"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_ac_t10_system_unpause_success_requires_requery(
    temp_db: str,
) -> None:
    """Path AC / T10: system_unpause_gid success only after tell_status re-query.

    Contract: RPC ok is never success by itself. tell_status must run before
    "success", and only active|waiting counts as success.
    """
    from unittest.mock import AsyncMock

    download = await create_global_download_v0(
        resource_key="http:path-ac-t10",
        source_uri="https://example.com/path-ac-t10.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_path_ac_t10",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_UNPAUSE_FAILED,
    )
    client = make_aria2_client(unpause="OK")
    client.tell_status = AsyncMock(
        return_value={
            "status": "active",
            "totalLength": "100",
            "completedLength": "0",
        }
    )

    result = await system_unpause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_path_ac_t10",
        expected_gid="gid_path_ac_t10",
        failure_error_code=ERROR_UNPAUSE_FAILED,
        failure_message="路径 AC：恢复下载失败",
        acquire_lifecycle_lock=False,
    )

    assert result == "success"
    client.unpause.assert_awaited_once_with("gid_path_ac_t10")
    # Path AC: re-query is mandatory on the success path (not RPC-only).
    client.tell_status.assert_awaited()
    client.tell_status.assert_awaited_once_with("gid_path_ac_t10")


@pytest.mark.asyncio
async def test_path_ac_t10_system_unpause_rpc_ok_still_paused_not_success(
    temp_db: str,
) -> None:
    """Path AC / T10 (negative): RPC ok + re-query paused → soft_failed, no clear."""
    download = await create_global_download_v0(
        resource_key="http:path-ac-t10-neg",
        source_uri="https://example.com/path-ac-t10-neg.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_path_ac_t10_neg",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    client = make_aria2_client(
        unpause="OK",
        tell_status={
            "status": "paused",
            "totalLength": "100",
            "completedLength": "0",
        },
    )

    result = await system_unpause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_path_ac_t10_neg",
        expected_gid="gid_path_ac_t10_neg",
        failure_error_code=ERROR_UNPAUSE_FAILED,
        failure_message="路径 AC：仍 paused",
        acquire_lifecycle_lock=False,
    )

    assert result == "soft_failed"
    client.unpause.assert_awaited_once_with("gid_path_ac_t10_neg")
    client.tell_status.assert_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    # Must not treat RPC-ok as success; ownership remains system-owned.
    assert row["error_code"] in {
        ERROR_ADMISSION_PAUSED,
        ERROR_UNPAUSE_FAILED,
    }
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED


@pytest.mark.asyncio
async def test_path_ac_t10b_apply_decision_resume_still_paused_keeps_code(
    temp_db: str,
) -> None:
    """Path AC / T10b: apply_decision(resume) still paused must not clear code.

    Contract (policy path): unpause + re-query paused keeps pending ownership;
    never clear on RPC-only success.
    """
    from unittest.mock import AsyncMock

    from app.modules.backend.port import BackendPort
    from app.modules.task_core.policy import Decision, apply_decision

    download = await create_global_download_v0(
        resource_key="http:path-ac-t10b",
        source_uri="https://example.com/path-ac-t10b.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_path_ac_t10b",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_status = AsyncMock(return_value={"status": "paused"})

    await apply_decision(
        backend,
        int(download["id"]),
        Decision("resume", clear_error_code=True),
    )

    backend.unpause.assert_awaited_once_with(download["id"])
    # Path AC: observation after unpause is required before any clear.
    backend.tell_status.assert_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] is not None
    assert row["error_code"] in {
        ERROR_ADMISSION_PAUSED,
        ERROR_UNPAUSE_FAILED,
    }
    assert row["error_code"] != ERROR_EXTERNAL_PAUSED


@pytest.mark.asyncio
async def test_path_ac_t10b_apply_decision_resume_active_clears_after_requery(
    temp_db: str,
) -> None:
    """Path AC / T10b complement: clear only when re-query shows running."""
    from unittest.mock import AsyncMock

    from app.modules.backend.port import BackendPort
    from app.modules.task_core.policy import Decision, apply_decision

    download = await create_global_download_v0(
        resource_key="http:path-ac-t10b-ok",
        source_uri="https://example.com/path-ac-t10b-ok.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid_path_ac_t10b_ok",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_status = AsyncMock(return_value={"status": "active"})

    await apply_decision(
        backend,
        int(download["id"]),
        Decision("resume", clear_error_code=True),
    )

    backend.unpause.assert_awaited_once_with(download["id"])
    backend.tell_status.assert_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] is None
    assert row["error_message"] is None


def test_path_ac_no_rpc_only_clear_production_paths() -> None:
    """Path AC optional static: unpause success paths require re-query / observe.

    Guards against reintroducing "RPC success ⇒ clear" without tell_status
    (or _observe_backend_status) on the two Contract paths.
    """
    shared = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "lifecycle"
        / "_shared.py"
    ).read_text(encoding="utf-8")
    policy = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "task_core"
        / "policy.py"
    ).read_text(encoding="utf-8")

    # system_unpause_gid must always re-query via _requery_after_control_failure
    # (which calls tell_status); never return success from RPC alone.
    unpause_fn_start = shared.index("async def system_unpause_gid")
    unpause_fn_end = shared.index("\ndef ", unpause_fn_start + 1)
    unpause_body = shared[unpause_fn_start:unpause_fn_end]
    assert "_requery_after_control_failure" in unpause_body
    assert "return \"success\"" not in unpause_body

    apply_start = policy.index("async def apply_decision")
    apply_end = policy.index("\ndef ", apply_start + 1)
    apply_body = policy[apply_start:apply_end]
    assert "_observe_backend_status" in apply_body
    # clear on resume only after observed running statuses
    assert "_RUNNING_STATUSES" in apply_body or "active" in apply_body


# ---------------------------------------------------------------------------
# 09-05 fix-pause-ownership-loss: pause ownership must survive every success
# path; a stamp that cannot land must not become silent success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_rpc_error_requery_paused_success_stamps_ownership(
    temp_db: str,
) -> None:
    """RPC throws but re-query shows paused → success must still own the pause.

    Pre-fix: the except branch returned the re-query result directly, so the
    ownership stamp was skipped and the pause could later be branded
    external_paused.
    """
    download = await create_global_download_v0(
        resource_key="http:m7-pause-requery-stamp",
        source_uri="https://example.com/requery-stamp.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_requery_stamp",
        total_bytes=100,
        size_known=True,
    )
    client = make_aria2_client(
        pause=Exception("pause rpc timed out"),
        tell_status={
            "status": "paused",
            "totalLength": "100",
            "completedLength": "0",
        },
    )
    result = await system_pause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_requery_stamp",
        expected_gid="gid_requery_stamp",
        failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
        failure_message="任务大小增长时无法安全暂停",
        ownership_error_code=ERROR_METADATA_ADMISSION_PAUSED,
        acquire_lifecycle_lock=False,
    )
    assert result == "success"
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] == ERROR_METADATA_ADMISSION_PAUSED


@pytest.mark.asyncio
async def test_pause_stamp_generation_miss_returns_stale_without_rpc(
    temp_db: str,
) -> None:
    """Stamp cannot land (row GID moved) → "stale", never silent success.

    Pre-fix: the pause RPC fired first and a missed generation read silently
    returned "success", leaving a paused gid with no ownership code.
    """
    download = await create_global_download_v0(
        resource_key="http:m7-pause-stale",
        source_uri="https://example.com/pause-stale.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_actual_current",
        total_bytes=100,
        size_known=True,
    )
    client = make_aria2_client(pause="OK")
    result = await system_pause_gid(
        backend=client,
        download_id=int(download["id"]),
        control_gid="gid_moved_on",
        expected_gid="gid_moved_on",
        failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
        failure_message="任务大小增长时无法安全暂停",
        ownership_error_code=ERROR_ADMISSION_PAUSED,
        acquire_lifecycle_lock=False,
    )
    assert result == "stale"
    client.pause.assert_not_awaited()
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(
                    global_downloads.c.id == download["id"]
                )
            )
        ).mappings().one()
    assert row["error_code"] is None


@pytest.mark.asyncio
async def test_pause_stamp_fence_race_returns_stale(temp_db: str) -> None:
    """Intent-first stamp loses the fence race → "stale", pause RPC not sent."""
    download = await create_global_download_v0(
        resource_key="http:m7-pause-fence-race",
        source_uri="https://example.com/fence-race.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_fence_race",
        total_bytes=100,
        size_known=True,
    )
    client = make_aria2_client(pause="OK")
    with patch(
        "app.repositories.task.downloads.guarded_update_global_download",
        new=AsyncMock(return_value=False),
    ):
        result = await system_pause_gid(
            backend=client,
            download_id=int(download["id"]),
            control_gid="gid_fence_race",
            expected_gid="gid_fence_race",
            failure_error_code=ERROR_GROWTH_PAUSE_FAILED,
            failure_message="任务大小增长时无法安全暂停",
            ownership_error_code=ERROR_ADMISSION_PAUSED,
            acquire_lifecycle_lock=False,
        )
    assert result == "stale"
    client.pause.assert_not_awaited()
