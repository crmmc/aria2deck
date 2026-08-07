"""T06: claim-only cleanup contract tests.

Verifies that ``cleanup_with_claim`` is the sole authorized entry point for
destructive cleanup and follows the spec §10.3 ordering.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.lifecycle import (
    RepairClaim,
    TerminalizationClaim,
    make_repair_claim,
    make_terminalization_claim,
)
from app.services.failed_task_cleanup import cleanup_with_claim
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_global_download_v0


async def _fetch_gid(download_id: int) -> str | None:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads.c.aria2_gid).where(
                        global_downloads.c.id == download_id
                    )
                )
            )
            .one()
        )
    return row[0]


# ---------------------------------------------------------------------------
# 1. TerminalizationClaim: writer stops -> dir deleted -> GID cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_claim_full_cleanup(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t1",
        status="failed",
        aria2_gid="gid-t1",
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-t1",
        writer_gids=("gid-t1",),
        result_gids=("gid-t1",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await cleanup_with_claim(client, claim, log_prefix="[T1]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    assert result.result_removed is True
    client.force_remove.assert_awaited_once_with("gid-t1")
    client.remove_download_result.assert_awaited_once_with("gid-t1")
    mock_dir.assert_awaited_once_with(download["id"])

    gid = await _fetch_gid(download["id"])
    assert gid is None


# ---------------------------------------------------------------------------
# 2. force_remove fails with non-not-found error -> keep dir, no GID clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_not_stopped_keeps_directory(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t2",
        status="failed",
        aria2_gid="gid-t2",
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    client.force_remove.side_effect = ConnectionError("network unreachable")
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-t2",
        writer_gids=("gid-t2",),
        result_gids=("gid-t2",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir:
        result = await cleanup_with_claim(client, claim, log_prefix="[T2]")

    assert result.writer_stopped is False
    assert result.directory_cleaned is False
    assert result.safe_to_reuse is False
    mock_dir.assert_not_called()
    client.remove_download_result.assert_not_called()

    gid = await _fetch_gid(download["id"])
    assert gid == "gid-t2"


# ---------------------------------------------------------------------------
# 3. GID not found treated as stopped -> cleanup continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gid_not_found_treated_as_stopped(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t3",
        status="failed",
        aria2_gid="gid-t3",
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    client.force_remove.side_effect = Exception("GID not found")
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-t3",
        writer_gids=("gid-t3",),
        result_gids=("gid-t3",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await cleanup_with_claim(client, claim, log_prefix="[T3]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    assert result.result_removed is True

    gid = await _fetch_gid(download["id"])
    assert gid is None


# ---------------------------------------------------------------------------
# 4. RepairClaim can clean failed residual
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_claim_cleans_residual(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t4",
        status="failed",
        aria2_gid="gid-t4",
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    claim = make_repair_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-t4",
        writer_gids=("gid-t4",),
        result_gids=("gid-t4",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await cleanup_with_claim(client, claim, log_prefix="[T4]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True

    gid = await _fetch_gid(download["id"])
    assert gid is None


# ---------------------------------------------------------------------------
# 5. Handoff dual writer_gids: only process claim GIDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_dual_writers_both_stop(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="magnet:t5",
        status="failed",
        aria2_gid="source_gid",
        total_bytes=1000,
        completed_bytes=0,
    )
    client = make_aria2_client()
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="source_gid",
        writer_gids=("source_gid", "payload_gid"),
        result_gids=("source_gid",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await cleanup_with_claim(client, claim, log_prefix="[T5]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    # force_remove called for both writers
    assert client.force_remove.await_count == 2
    client.force_remove.assert_any_await("source_gid")
    client.force_remove.assert_any_await("payload_gid")
    # remove_download_result only for result_gids (source only)
    client.remove_download_result.assert_awaited_once_with("source_gid")


@pytest.mark.asyncio
async def test_handoff_second_writer_not_stopped(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="magnet:t5b",
        status="failed",
        aria2_gid="src",
        total_bytes=1000,
        completed_bytes=0,
    )
    client = make_aria2_client()

    # First writer stops, second doesn't
    async def _force_remove(gid: str) -> str:
        if gid == "payload":
            raise OSError("permission denied")
        return "OK"

    client.force_remove.side_effect = _force_remove
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="src",
        writer_gids=("src", "payload"),
        result_gids=("src",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir:
        result = await cleanup_with_claim(client, claim, log_prefix="[T5b]")

    assert result.writer_stopped is False
    assert result.directory_cleaned is False
    mock_dir.assert_not_called()

    gid = await _fetch_gid(download["id"])
    assert gid == "src"


# ---------------------------------------------------------------------------
# 6. No claim / wrong type cannot use the new entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_string_not_accepted_as_claim(temp_db: str) -> None:
    """Passing a raw string (not a claim object) must not succeed."""
    download = await create_global_download_v0(
        resource_key="http:t6",
        status="failed",
        aria2_gid="gid-t6",
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()

    with pytest.raises((AttributeError, TypeError)):
        await cleanup_with_claim(
            client, "gid-t6", log_prefix="[T6]"  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_new_api_requires_claim_not_task_id(temp_db: str) -> None:
    """The new API signature has no task_id/gid parameters at all."""
    import inspect

    sig = inspect.signature(cleanup_with_claim)
    params = set(sig.parameters.keys())
    assert "client" in params
    assert "claim" in params
    # The old bypass parameters must not exist on the new API
    assert "task_id" not in params
    assert "gid" not in params
    assert "skip_status_check" not in params


# ---------------------------------------------------------------------------
# 7. result_removed=False when remove_download_result fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_remove_failure_non_blocking(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t7",
        status="failed",
        aria2_gid="gid-t7",
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    client.remove_download_result.side_effect = Exception("history unavailable")
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid="gid-t7",
        writer_gids=("gid-t7",),
        result_gids=("gid-t7",),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_get,
    ):
        mock_dir.return_value = None
        mock_get.return_value.__truediv__ = lambda self, x: f"/dl/{x}"

        result = await cleanup_with_claim(client, claim, log_prefix="[T7]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    assert result.result_removed is False
    assert result.safe_to_reuse is True

    # GID still cleared because cleanup is best-effort
    gid = await _fetch_gid(download["id"])
    assert gid is None


# ---------------------------------------------------------------------------
# 8. Empty writer_gids: nothing to stop, proceed to dir cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_writer_gids_proceeds_to_dir_cleanup(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t8",
        status="failed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    claim = make_terminalization_claim(
        attempt_id=download["id"],
        expected_current_gid=None,
        writer_gids=(),
        result_gids=(),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir:
        result = await cleanup_with_claim(client, claim, log_prefix="[T8]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    client.force_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 9. expected_current_gid None skips CAS clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_expected_gid_skips_cas_clear(temp_db: str) -> None:
    download = await create_global_download_v0(
        resource_key="http:t9",
        status="failed",
        aria2_gid=None,
        total_bytes=100,
        completed_bytes=0,
    )
    client = make_aria2_client()
    claim = make_repair_claim(
        attempt_id=download["id"],
        expected_current_gid=None,
        writer_gids=(),
        result_gids=(),
        terminal_status="failed",
        claim_timestamp=1,
    )

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_dir,
        patch(
            "app.services.failed_task_cleanup.clear_terminal_download_gid"
        ) as mock_clear,
    ):
        mock_dir.return_value = None
        result = await cleanup_with_claim(client, claim, log_prefix="[T9]")

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    mock_clear.assert_not_called()
