"""Tests for cleanup logging observability (Phase 3)."""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.aria2.failed_task_cleanup import (
    CleanupErrorType,
    cleanup_failed_task_artifacts,
)


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
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
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
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
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
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
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
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
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
async def test_cleanup_skips_non_failed_status(mock_client, caplog):
    """Verify cleanup skips tasks not in failed state."""
    mock_task = MagicMock()
    mock_task.status = "downloading"  # Not a failed state

    with (
        patch("app.aria2.failed_task_cleanup.get_session") as mock_get_session,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = mock_task
        mock_db.exec = AsyncMock(return_value=mock_result)
        mock_get_session.return_value.__aenter__.return_value = mock_db
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.DEBUG):
            result = await cleanup_failed_task_artifacts(
                client=mock_client,
                task_id=123,
                gid="abc123",
                owner_id=456,
                log_prefix="[Test]",
                skip_status_check=False,  # Enable status check
            )

        assert result is True  # Returns True (no cleanup needed)
        assert "[CLEANUP] skipped" in caplog.text
        assert "STATUS_CONFLICT" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_handles_missing_task(mock_client, caplog):
    """Verify cleanup handles task not found gracefully."""
    with (
        patch("app.aria2.failed_task_cleanup.get_session") as mock_get_session,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = None  # Task not found
        mock_db.exec = AsyncMock(return_value=mock_result)
        mock_get_session.return_value.__aenter__.return_value = mock_db
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


@pytest.mark.asyncio
async def test_cleanup_proceeds_for_error_status(mock_client):
    """Verify cleanup proceeds for tasks in error state."""
    mock_task = MagicMock()
    mock_task.status = "error"  # Failed state

    with (
        patch("app.aria2.failed_task_cleanup.get_session") as mock_get_session,
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = mock_task
        mock_db.exec = AsyncMock(return_value=mock_result)
        mock_get_session.return_value.__aenter__.return_value = mock_db
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        result = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=123,
            gid="abc123",
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=False,
        )

        assert result is True
        mock_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_proceeds_for_removed_status(mock_client):
    """Verify cleanup proceeds for tasks in removed state."""
    mock_task = MagicMock()
    mock_task.status = "removed"  # Failed state

    with (
        patch("app.aria2.failed_task_cleanup.get_session") as mock_get_session,
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = mock_task
        mock_db.exec = AsyncMock(return_value=mock_result)
        mock_get_session.return_value.__aenter__.return_value = mock_db
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        result = await cleanup_failed_task_artifacts(
            client=mock_client,
            task_id=123,
            gid="abc123",
            owner_id=456,
            log_prefix="[Test]",
            skip_status_check=False,
        )

        assert result is True
        mock_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_without_gid_skips_rpc(mock_client):
    """Verify cleanup without GID skips RPC calls."""
    with (
        patch("app.aria2.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.aria2.failed_task_cleanup.get_downloading_dir") as mock_dir,
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
