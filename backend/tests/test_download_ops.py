"""Tests for backend/app/aria2/download_ops.py."""

from __future__ import annotations

import pytest

from app.aria2.download_ops import (
    bt_info_hash_from_status,
    extract_display_name,
    map_progress_values,
    safe_int,
)


class TestSafeInt:
    def test_string_number(self) -> None:
        assert safe_int("42") == 42

    def test_none_returns_default(self) -> None:
        assert safe_int(None, 7) == 7

    def test_invalid_string(self) -> None:
        assert safe_int("abc") == 0

    def test_int_passthrough(self) -> None:
        assert safe_int(100) == 100

    def test_empty_string(self) -> None:
        assert safe_int("") == 0


class TestBtInfoHashFromStatus:
    def test_valid_hash(self) -> None:
        status = {"infoHash": "a" * 40}
        assert bt_info_hash_from_status(status) == "a" * 40

    def test_uppercase_normalized(self) -> None:
        status = {"infoHash": "A" * 40}
        assert bt_info_hash_from_status(status) == "a" * 40

    def test_invalid_hash(self) -> None:
        assert bt_info_hash_from_status({"infoHash": "short"}) is None

    def test_none_status(self) -> None:
        assert bt_info_hash_from_status(None) is None

    def test_missing_key(self) -> None:
        assert bt_info_hash_from_status({}) is None

    def test_hash_with_spaces(self) -> None:
        status = {"infoHash": "  " + "b" * 40 + "  "}
        assert bt_info_hash_from_status(status) == "b" * 40


class TestExtractDisplayName:
    def test_bittorrent_name_preferred(self) -> None:
        status = {
            "bittorrent": {"info": {"name": "My Torrent"}},
            "files": [{"path": "/downloads/file.bin"}],
        }
        assert extract_display_name(status, "fallback") == "My Torrent"

    def test_file_path_fallback(self) -> None:
        status = {"files": [{"path": "/downloads/movie.mkv"}]}
        assert extract_display_name(status, "fallback") == "movie.mkv"

    def test_metadata_prefix_returns_fallback(self) -> None:
        status = {"bittorrent": {"info": {"name": "[METADATA]abc"}}}
        assert extract_display_name(status, "fb") == "fb"

    def test_empty_returns_fallback(self) -> None:
        assert extract_display_name({}, "fb") == "fb"

    def test_sanitizes_control_chars(self) -> None:
        status = {"bittorrent": {"info": {"name": "bad\x1b[31mname"}}}
        result = extract_display_name(status, None)
        assert "\x1b" not in (result or "")


class TestMapProgressValues:
    def test_normal_progress(self) -> None:
        status = {
            "bittorrent": {"info": {"name": "Movie"}},
            "totalLength": "1000",
            "completedLength": "500",
        }
        result = map_progress_values(status, None)
        assert result == {
            "display_name": "Movie",
            "total_bytes": 1000,
            "completed_bytes": 500,
        }

    def test_metadata_phase_skips_total(self) -> None:
        status = {
            "bittorrent": {"info": {"name": "[METADATA]hash"}},
            "totalLength": "99",
            "completedLength": "10",
        }
        result = map_progress_values(status, "fallback")
        assert "total_bytes" not in result
        assert result["completed_bytes"] == 10

    def test_empty_status_returns_empty(self) -> None:
        assert map_progress_values({}, None) == {}

    def test_skip_total_disabled(self) -> None:
        status = {
            "bittorrent": {"info": {"name": "[METADATA]hash"}},
            "totalLength": "99",
            "completedLength": "10",
        }
        result = map_progress_values(status, "fallback", skip_total_on_metadata=False)
        assert result["total_bytes"] == 99


# --- DB operation tests ---

from sqlalchemy import select

from app.aria2.download_ops import (
    guarded_update_global_download,
    update_active_user_tasks,
)
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


@pytest.mark.asyncio
async def test_guarded_update_returns_bool(temp_db: str) -> None:
    dl = await create_global_download_v0(
        resource_key="test-key-1",
        status="active",
        aria2_gid="aaa111",
    )
    result = await guarded_update_global_download(
        dl["id"], {"status": "active", "total_bytes": 999}
    )
    assert result is True


@pytest.mark.asyncio
async def test_guarded_update_returns_row(temp_db: str) -> None:
    dl = await create_global_download_v0(
        resource_key="test-key-2",
        status="active",
        aria2_gid="bbb222",
    )
    result = await guarded_update_global_download(
        dl["id"], {"aria2_gid": "ccc333"}, return_row=True
    )
    assert isinstance(result, dict)
    assert result["aria2_gid"] == "ccc333"


@pytest.mark.asyncio
async def test_guarded_update_skips_completed(temp_db: str) -> None:
    dl = await create_global_download_v0(
        resource_key="test-key-3",
        status="completed",
        aria2_gid="ddd444",
    )
    result = await guarded_update_global_download(
        dl["id"], {"total_bytes": 100}
    )
    assert result is False


@pytest.mark.asyncio
async def test_update_active_user_tasks_force_display_name(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="tester")
    dl = await create_global_download_v0(
        resource_key="test-key-4",
        status="active",
        aria2_gid="eee555",
        display_name="old.torrent",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=dl["id"],
        status="active",
        display_name="old.torrent",
    )
    await update_active_user_tasks(
        dl["id"],
        display_name="Real BT Name",
        force_display_name=True,
    )
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    assert row["display_name"] == "Real BT Name"


@pytest.mark.asyncio
async def test_update_active_user_tasks_respects_refreshable(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="tester2")
    dl = await create_global_download_v0(
        resource_key="test-key-5",
        resource_kind="http",
        source_uri="http://example.com/file.bin",
        status="active",
        aria2_gid="fff666",
        display_name="file.bin",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=dl["id"],
        status="active",
        display_name="file.bin",
    )
    await update_active_user_tasks(
        dl["id"],
        display_name="New Name",
        force_display_name=False,
    )
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    # "file.bin" does not match refreshable condition, so unchanged
    assert row["display_name"] == "file.bin"
