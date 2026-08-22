"""Task 5 — State machine: resource queue / external pause / handoff.

Covers AC-5 (resource_queued resumable), AC-6 (external pause never
auto-unpaused), AC-7 (size > total quota is terminal quota_exceeded).
Also pins the invariant: size_known alone never triggers an unpause.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.modules.backend.port import BackendPort
from app.modules.task_core.policy import (
    Decision,
    QuotaContext,
    apply_decision,
    decide_on_snapshot,
    on_followed_size,
)
from app.modules.task_core.states import (
    ERROR_DISK_QUEUED,
    ERROR_EXTERNAL_PAUSED,
    ERROR_QUOTA_EXCEEDED,
    ERROR_QUOTA_QUEUED,
)
from tests.helpers_v0 import create_global_download_v0, create_user_v0


async def _get_global(tid: int) -> dict | None:
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == tid)
            )
        ).mappings().first()
    return dict(row) if row else None


def _row(**overrides) -> dict:
    base = {
        "id": 1,
        "status": "paused",
        "error_code": None,
        "total_bytes": 0,
        "size_known": False,
    }
    base.update(overrides)
    return base


# --- pure decision tests ---------------------------------------------------


def test_external_pause_without_known_size_is_marked() -> None:
    """Paused + no error_code + size not yet admitted → external pause."""
    row = _row(status="paused", error_code=None, total_bytes=0, size_known=False)
    decision = decide_on_snapshot(row, "paused", quota=QuotaContext(quota_bytes=10**9))
    assert decision.action == "mark_external_paused"
    assert decision.error_code == ERROR_EXTERNAL_PAUSED

    # Once recorded, repeated syncs keep it (still no unpause).
    row2 = _row(status="paused", error_code=ERROR_EXTERNAL_PAUSED,
                total_bytes=100, size_known=True)
    decision2 = decide_on_snapshot(row2, "paused", quota=QuotaContext(quota_bytes=10**9))
    assert decision2.action == "keep"


def test_admission_paused_resumes() -> None:
    """System-owned admission pause → resume and clear marker."""
    row = _row(status="paused", error_code="admission_paused", total_bytes=500, size_known=True)
    decision = decide_on_snapshot(row, "paused", quota=QuotaContext(quota_bytes=10**9))
    assert decision.action == "resume"
    assert decision.clear_error_code


def test_metadata_admission_paused_resumes_when_size_known() -> None:
    """Magnet pause-metadata ownership resumes only after size is admitted."""
    row = _row(
        status="paused",
        error_code="metadata_admission_paused",
        total_bytes=500,
        size_known=True,
    )
    decision = decide_on_snapshot(row, "paused", quota=QuotaContext(quota_bytes=10**9))
    assert decision.action == "resume"
    assert decision.clear_error_code


def test_metadata_admission_paused_holds_without_size() -> None:
    """Do not resume metadata admission pause before size is known."""
    row = _row(
        status="paused",
        error_code="metadata_admission_paused",
        total_bytes=0,
        size_known=False,
    )
    decision = decide_on_snapshot(row, "paused", quota=QuotaContext(quota_bytes=10**9))
    assert decision.action == "keep"
    assert decision.error_code == "metadata_admission_paused"


def test_metadata_admission_paused_active_does_not_generic_clear() -> None:
    """T9c / §3.1.1: metadata phase active must not clear via generic owned clear."""
    row = _row(
        status="active",
        error_code="metadata_admission_paused",
        total_bytes=0,
        size_known=False,
    )
    decision = decide_on_snapshot(row, "active", quota=QuotaContext(quota_bytes=10**9))
    assert decision.action != "clear_error_code"
    assert decision.clear_error_code is False
    assert decision.action in {"noop", "keep"}

    waiting = decide_on_snapshot(
        row, "waiting", quota=QuotaContext(quota_bytes=10**9)
    )
    assert waiting.action != "clear_error_code"
    assert waiting.clear_error_code is False


def test_size_known_paused_without_error_is_external() -> None:
    """Known size + paused without ownership marker → external pause."""
    row = _row(status="paused", error_code=None, total_bytes=500, size_known=True)
    for ctx in (
        None,
        QuotaContext(),
        QuotaContext(quota_bytes=10**9, quota_used_bytes=0),
    ):
        decision = decide_on_snapshot(row, "paused", quota=ctx)
        assert decision.action == "mark_external_paused"


def test_quota_queued_resumes_when_headroom_sufficient() -> None:
    """AC-5: quota_queued + enough headroom → resume, clear error_code."""
    row = _row(status="paused", error_code=ERROR_QUOTA_QUEUED,
               total_bytes=100, size_known=True)
    ctx = QuotaContext(quota_bytes=1000, quota_used_bytes=800)
    decision = decide_on_snapshot(row, "paused", quota=ctx)
    assert decision.action == "resume"
    assert decision.clear_error_code


def test_quota_queued_holds_when_headroom_insufficient() -> None:
    """AC-5: quota_queued without headroom stays queued (keep, no unpause)."""
    row = _row(status="paused", error_code=ERROR_QUOTA_QUEUED,
               total_bytes=100, size_known=True)
    ctx = QuotaContext(quota_bytes=1000, quota_used_bytes=950)
    decision = decide_on_snapshot(row, "paused", quota=ctx)
    assert decision.action == "keep"
    assert decision.error_code == ERROR_QUOTA_QUEUED


def test_disk_queued_resumes_when_disk_available() -> None:
    """AC-5: disk_queued + disk available → resume."""
    row = _row(status="paused", error_code=ERROR_DISK_QUEUED,
               total_bytes=100, size_known=True)
    decision = decide_on_snapshot(
        row, "paused", quota=QuotaContext(disk_available=True)
    )
    assert decision.action == "resume"

    held = decide_on_snapshot(
        row, "paused", quota=QuotaContext(disk_available=False)
    )
    assert held.action == "keep"
    assert held.error_code == ERROR_DISK_QUEUED


def test_system_pause_flag_is_not_misread_as_external() -> None:
    """A pause initiated by the state machine this round is not external."""
    row = _row(status="paused", error_code=None, total_bytes=100, size_known=True)
    decision = decide_on_snapshot(
        row, "paused", quota=QuotaContext(system_pause=True)
    )
    assert decision.action == "keep"
    assert decision.error_code is None


def test_size_over_total_quota_is_terminal() -> None:
    """AC-7: known size > total quota → terminal quota_exceeded."""
    row = _row(status="active", error_code=None, total_bytes=2000, size_known=True)
    decision = decide_on_snapshot(
        row, "active", quota=QuotaContext(quota_bytes=1000)
    )
    assert decision.action == "terminal_quota_exceeded"
    assert decision.error_code == ERROR_QUOTA_EXCEEDED
    assert decision.terminal


def test_size_unknown_never_terminal() -> None:
    """Size unknown → no quota verdict, even with a tiny quota."""
    row = _row(status="active", error_code=None, total_bytes=0, size_known=False)
    decision = decide_on_snapshot(row, "active", quota=QuotaContext(quota_bytes=1))
    assert decision.action == "noop"


def test_active_row_without_issues_is_noop() -> None:
    row = _row(status="active", error_code=None, total_bytes=10, size_known=True)
    decision = decide_on_snapshot(
        row, "active", quota=QuotaContext(quota_bytes=10**9)
    )
    assert decision.action == "noop"


# --- apply_decision (backend + DB effects) ---------------------------------


@pytest.mark.asyncio
async def test_apply_resume_unpauses_and_clears_error_code(temp_db: str) -> None:
    """AC-5: resume calls backend.unpause and clears error_code only when running."""
    user = await create_user_v0(username="sm1")
    gd = await create_global_download_v0(
        resource_key="http://example.com/q.bin",
        source_uri="http://example.com/q.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-q",
        total_bytes=100,
        size_known=True,
        error_code=ERROR_QUOTA_QUEUED,
        error_message="配额不足，排队中",
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_status = AsyncMock(return_value={"status": "active"})

    await apply_decision(backend, gd["id"], Decision("resume", clear_error_code=True))

    backend.unpause.assert_awaited_once_with(gd["id"])
    backend.tell_status.assert_awaited()
    backend.pause.assert_not_called()
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is None
    assert row["error_message"] is None


@pytest.mark.asyncio
async def test_apply_resume_still_paused_keeps_code(temp_db: str) -> None:
    """T10b / AC-3: apply_decision(resume) still paused must not clear ownership."""
    await create_user_v0(username="sm-t10b")
    gd = await create_global_download_v0(
        resource_key="http://example.com/t10b.bin",
        source_uri="http://example.com/t10b.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-t10b",
        total_bytes=100,
        size_known=True,
        error_code="admission_paused",
        error_message=None,
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_status = AsyncMock(return_value={"status": "paused"})

    await apply_decision(backend, gd["id"], Decision("resume", clear_error_code=True))

    backend.unpause.assert_awaited_once_with(gd["id"])
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is not None
    assert row["error_code"] in {"admission_paused", "unpause_failed"}


@pytest.mark.asyncio
async def test_apply_resume_still_paused_second_round_can_resume(temp_db: str) -> None:
    """T5 / AC-2: after failed release, pending code remains and policy still resumes."""
    await create_user_v0(username="sm-t5")
    gd = await create_global_download_v0(
        resource_key="http://example.com/t5.bin",
        source_uri="http://example.com/t5.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-t5",
        total_bytes=100,
        size_known=True,
        error_code="admission_paused",
    )
    backend = AsyncMock(spec=BackendPort)
    backend.tell_status = AsyncMock(return_value={"status": "paused"})

    await apply_decision(backend, gd["id"], Decision("resume", clear_error_code=True))
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] is not None

    second = decide_on_snapshot(
        {
            "error_code": row["error_code"],
            "size_known": True,
            "total_bytes": 100,
            "status": "paused",
        },
        "paused",
        quota=QuotaContext(quota_bytes=10**9),
    )
    assert second.action == "resume"


@pytest.mark.asyncio
async def test_apply_external_pause_marks_code_without_unpause(temp_db: str) -> None:
    """AC-6: external pause only records error_code; backend untouched."""
    user = await create_user_v0(username="sm2")
    gd = await create_global_download_v0(
        resource_key="http://example.com/e.bin",
        source_uri="http://example.com/e.bin",
        resource_kind="http",
        status="paused",
        aria2_gid="gid-e",
        total_bytes=100,
        size_known=True,
    )
    backend = AsyncMock(spec=BackendPort)

    await apply_decision(
        backend,
        gd["id"],
        Decision("mark_external_paused", error_code=ERROR_EXTERNAL_PAUSED),
    )

    backend.unpause.assert_not_called()
    backend.pause.assert_not_called()
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["error_code"] == ERROR_EXTERNAL_PAUSED
    assert row["status"] == "paused"


@pytest.mark.asyncio
async def test_apply_terminal_quota_exceeded_pauses_and_fails(temp_db: str) -> None:
    """AC-7: terminal decision pauses the backend and fails the row."""
    user = await create_user_v0(username="sm3")
    gd = await create_global_download_v0(
        resource_key="http://example.com/big.bin",
        source_uri="http://example.com/big.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-big",
        total_bytes=2000,
        size_known=True,
    )
    backend = AsyncMock(spec=BackendPort)

    await apply_decision(
        backend,
        gd["id"],
        Decision(
            "terminal_quota_exceeded", error_code=ERROR_QUOTA_EXCEEDED, terminal=True
        ),
    )

    backend.pause.assert_awaited_once_with(gd["id"])
    backend.unpause.assert_not_called()
    row = await _get_global(gd["id"])
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == ERROR_QUOTA_EXCEEDED


# --- handoff eligibility ---------------------------------------------------


def test_on_followed_size_eligibility_chain() -> None:
    """Handoff: size known → eligibility verdict, in precedence order."""
    assert on_followed_size(size_bytes=2000, quota_bytes=1000) == "exceeded"
    assert (
        on_followed_size(size_bytes=300, quota_bytes=1000, quota_used_bytes=800)
        == "quota_queued"
    )
    assert (
        on_followed_size(size_bytes=100, quota_bytes=1000, disk_ok=False)
        == "disk_queued"
    )
    assert on_followed_size(size_bytes=100, quota_bytes=1000) == "ok"
    # No quota context: only disk can queue.
    assert on_followed_size(size_bytes=10**12, quota_bytes=None) == "ok"
    assert (
        on_followed_size(size_bytes=100, quota_bytes=None, disk_ok=False)
        == "disk_queued"
    )


# --- M17 Task 1: E-4 terminal quota error carries decision inputs ----------

_GIB = 1024**3


def test_terminal_quota_decision_carries_values() -> None:
    """decide_on_snapshot fills total_bytes/quota_bytes on terminal decisions."""
    row = _row(status="active", error_code=None, total_bytes=15 * _GIB, size_known=True)
    decision = decide_on_snapshot(
        row, "active", quota=QuotaContext(quota_bytes=10 * _GIB)
    )
    assert decision.action == "terminal_quota_exceeded"
    assert decision.total_bytes == 15 * _GIB
    assert decision.quota_bytes == 10 * _GIB


@pytest.mark.asyncio
async def test_apply_terminal_quota_message_carries_values(temp_db: str) -> None:
    """E-4: persisted error_message uses the E-3a wording with actual values."""
    user = await create_user_v0(username="m17_e4")
    gd = await create_global_download_v0(
        resource_key="http://example.com/m17-e4.bin",
        source_uri="http://example.com/m17-e4.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-m17-e4",
        total_bytes=15 * _GIB,
        size_known=True,
    )
    backend = AsyncMock(spec=BackendPort)

    decision = decide_on_snapshot(
        {"total_bytes": 15 * _GIB, "size_known": True},
        "active",
        quota=QuotaContext(quota_bytes=10 * _GIB),
    )
    await apply_decision(backend, gd["id"], decision)

    row = await _get_global(gd["id"])
    assert row["error_code"] == ERROR_QUOTA_EXCEEDED
    assert row["error_message"] == "文件大小 15.00 GB 超过用户配额 10.00 GB"
