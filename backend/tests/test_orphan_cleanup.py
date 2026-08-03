"""Regression tests for startup orphan cleanup."""

from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.mark.asyncio
async def test_cleanup_rechecks_database_before_each_delete(tmp_path):
    """A file registered after the initial snapshot must not be deleted."""
    from app.services.orphan_cleanup import cleanup_orphan_files

    store_dir = tmp_path / "store"
    candidate = store_dir / "ab" / ("a" * 64)
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"registered")
    equivalent_path = candidate.parent / ".." / "ab" / candidate.name

    list_paths = AsyncMock(
        side_effect=[
            set(),
            {str(equivalent_path)},
        ]
    )
    delete_path = Mock()
    with (
        patch("app.services.orphan_cleanup.get_store_dir", return_value=store_dir),
        patch(
            "app.services.orphan_cleanup.list_stored_file_real_paths", list_paths
        ),
        patch("app.services.orphan_cleanup.safe_delete_path", delete_path),
    ):
        deleted = await cleanup_orphan_files()

    assert deleted == 0
    assert candidate.read_bytes() == b"registered"
    assert list_paths.await_count == 2
    delete_path.assert_not_called()
