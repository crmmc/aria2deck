"""Tests for backend/app/aria2/download_ops.py."""

from __future__ import annotations

import pytest

from app.aria2.download_ops import (
    bt_info_hash_from_status,
    extract_display_name,
    is_metadata_handoff_pending,
    map_aria2_status,
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
        assert "display_name" not in result
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


class TestMapAria2Status:
    def test_waiting_maps_to_waiting(self) -> None:
        assert map_aria2_status({"status": "waiting"}) == "waiting"

    def test_paused_maps_to_paused(self) -> None:
        assert map_aria2_status({"status": "paused"}) == "paused"

    def test_unknown_status_uses_default(self) -> None:
        assert map_aria2_status({"status": "unknown"}, default="active") == "active"

    def test_missing_status_uses_default(self) -> None:
        assert map_aria2_status({}, default="active") == "active"


class TestIsMetadataHandoffPending:
    def test_magnet_source_uri_is_pending_even_when_kind_is_http(self) -> None:
        download = {
            "resource_kind": "http",
            "source_uri": "magnet:?xt=urn:btih:" + "a" * 40,
            "display_name": "magnet:?xt=urn:btih:" + "a" * 40,
        }
        status = {
            "status": "complete",
            "bittorrent": {"info": {"name": "[METADATA]abc"}},
            "files": [{"path": "/downloads/metadata"}],
        }

        assert is_metadata_handoff_pending(download, status) is True

    def test_real_bittorrent_name_is_not_pending(self) -> None:
        download = {
            "resource_kind": "magnet",
            "source_uri": "magnet:?xt=urn:btih:" + "b" * 40,
            "display_name": "magnet:?xt=urn:btih:" + "b" * 40,
        }
        status = {
            "status": "complete",
            "bittorrent": {"info": {"name": "Real Torrent"}},
            "files": [],
        }

        assert is_metadata_handoff_pending(download, status) is False

    def test_payload_file_path_is_not_pending(self) -> None:
        download = {
            "resource_kind": "magnet",
            "source_uri": "magnet:?xt=urn:btih:" + "c" * 40,
            "display_name": "magnet:?xt=urn:btih:" + "c" * 40,
        }
        status = {
            "status": "complete",
            "bittorrent": {"info": {"name": "[METADATA]abc"}},
            "files": [{"path": "/downloads/movie.mkv"}],
        }

        assert is_metadata_handoff_pending(download, status) is False


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


from unittest.mock import AsyncMock

from app.aria2.download_ops import switch_to_followed_download


@pytest.mark.asyncio
async def test_switch_to_followed_http_to_torrent(temp_db: str) -> None:
    """HTTP torrent link switches to BT: resource_kind upgraded, hash written, name forced."""
    user = await create_user_v0(username="switcher")
    dl = await create_global_download_v0(
        resource_key="http-torrent-key",
        resource_kind="http",
        source_uri="http://example.com/file.torrent",
        status="active",
        aria2_gid="meta-gid-1",
        display_name="file.torrent",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=dl["id"],
        status="active",
        display_name="file.torrent",
    )

    mock_client = AsyncMock()
    mock_client.tell_status.return_value = {
        "status": "active",
        "bittorrent": {"info": {"name": "Ubuntu 24.04 LTS"}},
        "infoHash": "ab" * 20,
        "totalLength": "4000000000",
        "completedLength": "100000",
        "files": [{"path": "/downloads/Ubuntu 24.04 LTS/ubuntu.iso"}],
    }
    mock_client.remove_download_result = AsyncMock()

    result = await switch_to_followed_download(
        client=mock_client,
        download=dl,
        metadata_gid="meta-gid-1",
        followed_gid="real-gid-1",
        display_name_fallback="file.torrent",
        log_prefix="[Test]",
    )

    assert result is True

    async with transaction() as conn:
        g_row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == dl["id"])
            )
        ).mappings().one()
    assert g_row["aria2_gid"] == "real-gid-1"
    assert g_row["resource_kind"] == "torrent"
    assert g_row["bt_info_hash"] == "ab" * 20
    assert g_row["display_name"] == "Ubuntu 24.04 LTS"

    async with transaction() as conn:
        t_row = (
            await conn.execute(
                select(user_tasks).where(user_tasks.c.id == task["id"])
            )
        ).mappings().one()
    assert t_row["display_name"] == "Ubuntu 24.04 LTS"
    assert t_row["status"] == "active"

    mock_client.remove_download_result.assert_called_once_with("meta-gid-1")


@pytest.mark.asyncio
async def test_switch_to_followed_uses_real_status(temp_db: str) -> None:
    """followedBy switching preserves the real aria2 task status."""
    user = await create_user_v0(username="waiting-switcher")
    dl = await create_global_download_v0(
        resource_key="waiting-followed-key",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:" + "ef" * 20,
        status="active",
        aria2_gid="meta-gid-waiting",
        display_name="magnet:?xt=urn:btih:" + "ef" * 20,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=dl["id"],
        status="active",
        display_name="magnet:?xt=urn:btih:" + "ef" * 20,
    )

    mock_client = AsyncMock()
    mock_client.tell_status.return_value = {
        "status": "waiting",
        "bittorrent": {"info": {"name": "Queued Torrent"}},
        "infoHash": "ef" * 20,
        "totalLength": "1234",
        "completedLength": "0",
        "files": [{"path": "/downloads/Queued Torrent/file.bin"}],
    }
    mock_client.remove_download_result = AsyncMock()

    result = await switch_to_followed_download(
        client=mock_client,
        download=dl,
        metadata_gid="meta-gid-waiting",
        followed_gid="real-gid-waiting",
        display_name_fallback=dl["display_name"],
        log_prefix="[Test]",
    )

    assert result is True

    async with transaction() as conn:
        g_row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == dl["id"])
            )
        ).mappings().one()
        t_row = (
            await conn.execute(select(user_tasks).where(user_tasks.c.id == task["id"]))
        ).mappings().one()

    assert g_row["status"] == "waiting"
    assert t_row["status"] == "waiting"


@pytest.mark.asyncio
async def test_switch_to_followed_magnet_no_kind_upgrade(temp_db: str) -> None:
    """Magnet→torrent: resource_kind stays as-is (already BT type)."""
    user = await create_user_v0(username="magnet-user")
    dl = await create_global_download_v0(
        resource_key="magnet-key",
        resource_kind="magnet",
        source_uri="magnet:?xt=urn:btih:" + "cc" * 20,
        status="active",
        aria2_gid="meta-gid-2",
        display_name="magnet:?xt=urn:btih:" + "cc" * 20,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=dl["id"],
        status="active",
        display_name="magnet:?xt=urn:btih:" + "cc" * 20,
    )

    mock_client = AsyncMock()
    mock_client.tell_status.return_value = {
        "status": "active",
        "bittorrent": {"info": {"name": "Debian ISO"}},
        "infoHash": "dd" * 20,
        "totalLength": "3000000000",
        "completedLength": "0",
        "files": [{"path": "/downloads/debian.iso"}],
    }
    mock_client.remove_download_result = AsyncMock()

    await switch_to_followed_download(
        client=mock_client,
        download=dl,
        metadata_gid="meta-gid-2",
        followed_gid="real-gid-2",
        display_name_fallback=None,
        log_prefix="[Test]",
    )

    async with transaction() as conn:
        g_row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == dl["id"])
            )
        ).mappings().one()
    # magnet is already BT kind, should NOT be overwritten to "torrent"
    assert g_row["resource_kind"] == "magnet"


@pytest.mark.asyncio
async def test_switch_to_followed_tell_status_fails(temp_db: str) -> None:
    """tell_status failure: still updates GID and resource_kind, no crash."""
    user = await create_user_v0(username="fail-user")
    dl = await create_global_download_v0(
        resource_key="fail-key",
        resource_kind="http",
        source_uri="http://example.com/big.torrent",
        status="active",
        aria2_gid="meta-gid-3",
        display_name="big.torrent",
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=dl["id"],
        status="active",
        display_name="big.torrent",
    )

    mock_client = AsyncMock()
    mock_client.tell_status.side_effect = Exception("connection refused")
    mock_client.remove_download_result = AsyncMock()

    result = await switch_to_followed_download(
        client=mock_client,
        download=dl,
        metadata_gid="meta-gid-3",
        followed_gid="real-gid-3",
        display_name_fallback="big.torrent",
        log_prefix="[Test]",
    )

    assert result is True

    async with transaction() as conn:
        g_row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == dl["id"])
            )
        ).mappings().one()
    assert g_row["aria2_gid"] == "real-gid-3"
    assert g_row["resource_kind"] == "torrent"
