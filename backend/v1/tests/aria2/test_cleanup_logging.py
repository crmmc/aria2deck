"""Tests for cleanup logging observability via cleanup_with_claim."""

import logging
from unittest.mock import patch

import pytest

from app.domain.lifecycle import make_terminalization_claim
from app.services.failed_task_cleanup import (
    CleanupErrorType,
    cleanup_with_claim,
    get_representative_owner_id,
)
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_global_download_v0, create_user_task_v0


def _claim(task_id: int, gid: str | None = "abc123"):
    return make_terminalization_claim(
        attempt_id=task_id,
        expected_current_gid=gid,
        writer_gids=(gid,) if gid else (),
        result_gids=(gid,) if gid else (),
        terminal_status="failed",
        claim_timestamp=0,
        error_code="test",
        error_message="test",
    )


@pytest.fixture
def mock_client():
    return make_aria2_client()


@pytest.mark.asyncio
async def test_cleanup_logs_success_with_all_fields(mock_client, caplog):
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.clear_terminal_download_gid") as mock_clear,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"
        mock_clear.return_value = True

        with caplog.at_level(logging.INFO):
            result = await cleanup_with_claim(
                mock_client,
                _claim(123),
                log_prefix="[Test]",
            )

        assert result.safe_to_reuse is True
        assert result.result_removed is True
        assert "[CLEANUP]" in caplog.text
        assert "attempt_id=123" in caplog.text
        assert "result=success" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_logs_rpc_failure(mock_client, caplog):
    mock_client.force_remove.side_effect = Exception("RPC error")

    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
    ):
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"

        with caplog.at_level(logging.WARNING):
            result = await cleanup_with_claim(
                mock_client,
                _claim(123),
                log_prefix="[Test]",
            )

        assert result.writer_stopped is False
        assert result.directory_cleaned is False
        assert result.safe_to_reuse is False
        assert "[CLEANUP]" in caplog.text
        assert CleanupErrorType.RPC_FAILURE.value in caplog.text
        assert "op=force_remove" in caplog.text
        mock_cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_remove_result_failure_is_non_blocking_after_safe_cleanup(
    mock_client, caplog
):
    mock_client.remove_download_result.side_effect = OSError("history unavailable")
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir"),
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.clear_terminal_download_gid") as mock_clear,
    ):
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"
        mock_clear.return_value = True
        with caplog.at_level(logging.WARNING):
            result = await cleanup_with_claim(
                mock_client,
                _claim(123),
                log_prefix="[Test]",
            )

    assert result.writer_stopped is True
    assert result.directory_cleaned is True
    assert result.result_removed is False
    assert result.safe_to_reuse is True


@pytest.mark.asyncio
async def test_cleanup_logs_fs_failure(mock_client, caplog):
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.clear_terminal_download_gid") as mock_clear,
    ):
        mock_cleanup.side_effect = RuntimeError("Path boundary violation")
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"
        mock_clear.return_value = True

        with caplog.at_level(logging.WARNING):
            result = await cleanup_with_claim(
                mock_client,
                _claim(123),
                log_prefix="[Test]",
            )

        assert result.writer_stopped is True
        assert result.directory_cleaned is False
        assert result.safe_to_reuse is False
        assert "[CLEANUP]" in caplog.text
        assert CleanupErrorType.FS_FAILURE.value in caplog.text


@pytest.mark.asyncio
async def test_cleanup_error_type_enum_values():
    assert CleanupErrorType.RPC_FAILURE.value == "RPC_FAILURE"
    assert CleanupErrorType.FS_FAILURE.value == "FS_FAILURE"
    assert CleanupErrorType.STATUS_CONFLICT.value == "STATUS_CONFLICT"
    assert CleanupErrorType.NONE.value == "NONE"


@pytest.mark.asyncio
async def test_cleanup_writer_already_stopped(mock_client, caplog):
    mock_client.force_remove.side_effect = RuntimeError("gid abc123 is not found")
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.clear_terminal_download_gid") as mock_clear,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"
        mock_clear.return_value = True

        result = await cleanup_with_claim(
            mock_client,
            _claim(123),
            log_prefix="[Test]",
        )

        assert result.writer_stopped is True
        assert result.directory_cleaned is True
        mock_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_without_gid_skips_rpc(mock_client):
    with (
        patch("app.services.failed_task_cleanup.cleanup_task_download_dir") as mock_cleanup,
        patch("app.services.failed_task_cleanup.get_downloading_dir") as mock_dir,
        patch("app.services.failed_task_cleanup.clear_terminal_download_gid") as mock_clear,
    ):
        mock_cleanup.return_value = None
        mock_dir.return_value.__truediv__ = lambda self, x: f"/downloads/{x}"
        mock_clear.return_value = True

        result = await cleanup_with_claim(
            mock_client,
            _claim(123, gid=None),
            log_prefix="[Test]",
        )

        assert result.writer_stopped is True
        mock_client.force_remove.assert_not_called()
        mock_client.remove_download_result.assert_not_called()
        mock_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_get_representative_owner_id_returns_active_owner(test_user, temp_db):
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
