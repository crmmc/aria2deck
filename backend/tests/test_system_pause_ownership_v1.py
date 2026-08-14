"""M7: system pause ownership — constants, call-site gate, policy clear."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.lifecycle import ReconcileResult
from app.modules.task_core.policy import QuotaContext, decide_on_snapshot
from app.modules.task_core.states import (
    ACTIVE_CLEAR_ERROR_CODES,
    ERROR_ADMISSION_PAUSED,
    ERROR_EXTERNAL_PAUSED,
    ERROR_GROWTH_PAUSE_FAILED,
    ERROR_GROWTH_UNPAUSE_FAILED,
    ERROR_METADATA_ADMISSION_PAUSED,
    ERROR_UNPAUSE_FAILED,
    PROJECTION_PROTECTED_ERROR_CODES,
    SYSTEM_OWNED_PAUSE_CODES,
)
from app.services.lifecycle._shared import system_pause_gid
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
    assert row["error_code"] == ERROR_UNPAUSE_FAILED
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
    """Growth pause+unpause success must not leave sticky ownership code."""
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
