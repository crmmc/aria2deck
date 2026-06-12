"""Tests for cleanup logging observability (Phase 3)."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.services.failed_task_cleanup import (
    CleanupErrorType,
    cleanup_failed_task_artifacts,
    get_representative_owner_id,
)
from tests.helpers_v0 import create_global_download_v0, create_user_task_v0


@pytest.fixture
def mock_client():
    """Create a mock Aria2Client."""
    client = AsyncMock()
    client.force_remove = AsyncMock()
    client.remove_download_result = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_cleanup_logs_success_with_all_fields(mock_client, caplog):
    """Verify successful cleanup logs all required fields."""
    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.joinpath = lambda x: f"/downloads/{x}"
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.INFO):
            result = await cleanup_failed_task_artifacts(
                client=mock_client,
                task_id=123,
                gid="abc123",
                owner_id=456,
                log_prefix="[Test]",
                skip_status_check=True,
            )

        assert result is True
        assert "[CLEANUP]" in caplog.text
        assert "task_id=123" in caplog.text
        assert "owner_id=456" in caplog.text
        assert "gid=abc123" in caplog.text
        assert "result=success" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_logs_rpc_failure(mock_client, caplog):
    """Verify RPC failures are logged with error_type."""
    mock_client.force_remove.side_effect = Exception("RPC error")

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.WARNING):
            result = await cleanup_failed_task_artifacts(
                client=mock_client,
                task_id=123,
                gid="abc123",
                owner_id=456,
                log_prefix="[Test]",
                skip_status_check=True,
            )

        assert result is True  # RPC failure doesn't fail cleanup
        assert "[CLEANUP]" in caplog.text
        assert CleanupErrorType.RPC_FAILURE.value in caplog.text
        assert "op=force_remove" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_logs_fs_failure(mock_client, caplog):
    """Verify filesystem failures are logged with error_type."""
    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.side_effect = RuntimeError("Path boundary violation")
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.WARNING):
            result = await cleanup_failed_task_artifacts(
                client=mock_client,
                task_id=123,
                gid="abc123",
                owner_id=456,
                log_prefix="[Test]",
                skip_status_check=True,
            )

        assert result is False  # FS failure fails cleanup
        assert "[CLEANUP]" in caplog.text
        assert CleanupErrorType.FS_FAILURE.value in caplog.text


@pytest.mark.asyncio
async def test_cleanup_error_type_enum_values():
    """Verify CleanupErrorType enum has expected values."""
    assert CleanupErrorType.RPC_FAILURE.value == "RPC_FAILURE"
    assert CleanupErrorType.FS_FAILURE.value == "FS_FAILURE"
    assert CleanupErrorType.STATUS_CONFLICT.value == "STATUS_CONFLICT"
    assert CleanupErrorType.NONE.value == "NONE"


# Phase 4: Core unit tests for cleanup behavior


@pytest.mark.asyncio
async def test_cleanup_idempotent_repeated_calls(mock_client):
    """Verify repeated cleanup calls are idempotent."""
    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        # First call
        result1 = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=123,
            gid="abc123",
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=True,
        )

        # Second call (simulating repeated cleanup)
        result2 = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=123,
            gid="abc123",
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=True,
        )

        assert result1 is True
        assert result2 is True
        assert mock_cleanup.call_count == 2


@pytest.mark.asyncio
async def test_cleanup_skips_non_failed_status(mock_client, caplog, temp_db):
    """Verify cleanup skips tasks not in failed state."""
    task = await create_global_download_v0(
        resource_key="http:active-cleanup-skip",
        status="active",
    )

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.DEBUG):
            result = await cleanup_failed_task_artifacts(
                client=mock_client,
                task_id=task["id"],
                gid="abc123",
                owner_id=456,
                log_prefix="[Test]",
                skip_status_check=False,  # Enable status check
            )

        assert result is True  # Returns True (no cleanup needed)
        assert "[CLEANUP] skipped" in caplog.text
        assert "STATUS_CONFLICT" in caplog.text
        mock_cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_handles_missing_task(mock_client, caplog, temp_db):
    """Verify cleanup handles task not found gracefully."""
    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.DEBUG):
            result = await cleanup_failed_task_artifacts(
                client=mock_client,
                task_id=999,
                gid="nonexistent",
                owner_id=456,
                log_prefix="[Test]",
                skip_status_check=False,
            )

        assert result is True  # Returns True (already clean)
        assert "[CLEANUP] skipped" in caplog.text
        assert "task_not_found" in caplog.text
        mock_cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_proceeds_for_failed_status(mock_client, temp_db):
    """Verify cleanup proceeds for tasks in failed state."""
    task = await create_global_download_v0(
        resource_key="http:failed-cleanup",
        status="failed",
    )

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        result = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=task["id"],
            gid="abc123",
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=False,
        )

        assert result is True
        mock_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_proceeds_for_cancelled_status(mock_client, temp_db):
    """Verify cleanup proceeds for tasks in cancelled state."""
    task = await create_global_download_v0(
        resource_key="http:cancelled-cleanup",
        status="cancelled",
    )

    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        result = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=task["id"],
            gid="abc123",
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=False,
        )

        assert result is True
        mock_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_get_representative_owner_id_returns_active_owner(test_user, temp_db):
    """Verify representative owner uses active v0 user task rows."""
    task = await create_global_download_v0(
        resource_key="http:representative-owner-active",
        status="active",
    )
    await create_user_task_v0(
        user_id=test_user["id"],
        global_download_id=task["id"],
        status="paused",
    )

    assert await get_representative_owner_id(task["id"]) == test_user["id"]


@pytest.mark.asyncio
async def test_get_representative_owner_id_ignores_terminal_tasks(test_user, temp_db):
    """Verify terminal v0 user task rows are ignored for representative owner."""
    task = await create_global_download_v0(
        resource_key="http:representative-owner-terminal",
        status="failed",
    )
    await create_user_task_v0(
        user_id=test_user["id"],
        global_download_id=task["id"],
        status="failed",
    )

    assert await get_representative_owner_id(task["id"]) is None


@pytest.mark.asyncio
async def test_cleanup_without_gid_skips_rpc(mock_client):
    """Verify cleanup without GID skips RPC calls."""
    with (
        patch(
            "app.services.failed_task_cleanup.cleanup_task_download_dir"
        ) as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        result = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=123,
            gid=None,  # No GID
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=True,
        )

        assert result is True
        mock_client.force_remove.assert_not_called()
        mock_client.remove_download_result.assert_not_called()
        mock_cleanup.assert_called_once()
