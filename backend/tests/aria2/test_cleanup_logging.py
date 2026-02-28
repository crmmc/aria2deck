"""Tests for cleanup logging observability (Phase 3)."""
import logging
from unittest.mock import AsyncMock, patch

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
