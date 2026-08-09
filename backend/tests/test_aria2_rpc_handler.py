"""Tests for the v0 aria2 RPC handler."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, insert, update

from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    user_storage_usage,
    user_tasks,
    users,
)
from app.repositories.backend_snapshots import upsert_snapshot
from app.repositories.task.user_tasks import (
    get_user_task_by_id,
    list_user_tasks,
)
from app.domain.torrent_metadata import (
    TorrentFile,
    TorrentMetadata,
    parse_torrent_base64_async,
)
from app.services.rpc import Aria2RpcHandler, RpcError, RpcErrorCode
from app.services.task_projection import BT_TRACKER_PLACEHOLDER
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_user_v0, now_ms


def _torrent_with_network_field(field: bytes, url: bytes) -> str:
    def bstr(value: bytes) -> bytes:
        return str(len(value)).encode("ascii") + b":" + value

    info = b"d6:lengthi1e4:name4:teste"
    torrent = b"d" + bstr(field) + bstr(url) + b"4:info" + info + b"e"
    return base64.b64encode(torrent).decode("ascii")


async def create_rpc_task(
    *,
    user_id: int,
    gid: str | None,
    status: str,
    name: str,
    global_status: str | None = None,
    uri: str | None = None,
    resource_kind: str = "http",
    total_bytes: int = 100,
    completed_bytes: int = 0,
    error_message: str | None = None,
    updated_at_ms: int | None = None,
) -> dict[str, Any]:
    timestamp = updated_at_ms or now_ms()
    source_uri = uri or f"https://example.com/{name}"
    effective_global_status = global_status or status
    async with transaction() as conn:
        download = (
            (
                await conn.execute(
                    insert(global_downloads)
                    .values(
                        resource_key=f"rpc:{user_id}:{gid or name}:{timestamp}",
                        resource_kind=resource_kind,
                        source_uri=source_uri,
                        display_name=name,
                        aria2_gid=gid,
                        status=effective_global_status,
                        total_bytes=total_bytes,
                        completed_bytes=completed_bytes,
                        error_message=error_message,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                        completed_at_ms=timestamp
                        if effective_global_status
                        in {"completed", "failed", "cancelled"}
                        else None,
                    )
                    .returning(global_downloads)
                )
            )
            .mappings()
            .one()
        )
        task = (
            (
                await conn.execute(
                    insert(user_tasks)
                    .values(
                        user_id=user_id,
                        global_download_id=download["id"],
                        status=status,
                        reserved_bytes=total_bytes
                        if status in {"queued", "active", "waiting", "paused"}
                        else 0,
                        display_name=name,
                        error_message=error_message,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                        finished_at_ms=timestamp
                        if status in {"completed", "failed", "cancelled"}
                        else None,
                    )
                    .returning(user_tasks)
                )
            )
            .mappings()
            .one()
        )
        reservation = (
            total_bytes
            if status in {"queued", "active", "waiting", "paused"}
            else 0
        )
        if reservation:
            await conn.execute(
                update(user_storage_usage)
                .where(user_storage_usage.c.user_id == user_id)
                .values(
                    reserved_bytes=(
                        user_storage_usage.c.reserved_bytes + reservation
                    ),
                    updated_at_ms=timestamp,
                )
            )
    row = await get_user_task_by_id(user_id, int(task["id"]))
    assert row is not None
    return row


@pytest.fixture
def mock_aria2_client() -> AsyncMock:
    return make_aria2_client(
        get_version={"version": "1.36.0", "enabledFeatures": ["BitTorrent"]},
        get_global_stat={"downloadSpeed": "1000", "uploadSpeed": "500"},
        tell_status={},
    )


@pytest.fixture
def handler(test_user: dict, mock_aria2_client: AsyncMock) -> Aria2RpcHandler:
    from app.services import task_service
    from app.modules.backend.aria2_adapter import Aria2BackendAdapter

    task_service.set_task_backend_override(Aria2BackendAdapter(mock_aria2_client))
    yield Aria2RpcHandler(test_user["id"])
    task_service.set_task_backend_override(None)


def _mock_client(handler: Aria2RpcHandler) -> AsyncMock:
    from app.services import task_service

    backend = task_service._get_backend()
    return cast(AsyncMock, backend._client)


async def _upsert_rpc_snapshot(
    task: dict[str, Any],
    raw: dict[str, Any],
    *,
    files: list[dict[str, Any]] | None = None,
) -> None:
    """Seed the projection snapshot that the RPC read path now consumes."""
    snapshot_files = files if files is not None else raw.get("files") or []
    await upsert_snapshot(
        global_download_id=int(task["global_download_id"]),
        download_speed=int(str(raw.get("downloadSpeed") or 0)),
        upload_speed=int(str(raw.get("uploadSpeed") or 0)),
        total_length=int(str(raw.get("totalLength") or 0)),
        completed_length=int(str(raw.get("completedLength") or 0)),
        status=str(raw.get("status") or "active"),
        files_json=json.dumps(snapshot_files),
        raw_json=json.dumps(raw),
        updated_at_ms=now_ms(),
    )


def test_rpc_error_to_dict() -> None:
    error = RpcError(RpcErrorCode.INVALID_PARAMS, "Invalid", {"key": "val"})
    assert error.to_dict() == {
        "code": RpcErrorCode.INVALID_PARAMS,
        "message": "Invalid",
        "data": {"key": "val"},
    }


def test_error_codes() -> None:
    assert RpcErrorCode.PARSE_ERROR == -32700
    assert RpcErrorCode.INVALID_REQUEST == -32600
    assert RpcErrorCode.METHOD_NOT_FOUND == -32601
    assert RpcErrorCode.INVALID_PARAMS == -32602
    assert RpcErrorCode.INTERNAL_ERROR == -32603
    assert RpcErrorCode.TASK_NOT_FOUND == 1
    assert RpcErrorCode.PERMISSION_DENIED == 2
    assert RpcErrorCode.QUOTA_EXCEEDED == 3


@pytest.mark.asyncio
async def test_handle_method_not_found(handler: Aria2RpcHandler) -> None:
    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.unknownMethod", [])
    assert exc_info.value.code == RpcErrorCode.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_get_version_and_system_methods(handler: Aria2RpcHandler) -> None:
    version = await handler.handle("aria2.getVersion", [])
    methods = await handler.handle("system.listMethods", [])

    assert version["version"] == "aria2deck-proxy"
    assert "aria2.addUri" in methods
    assert "aria2.getVersion" in methods


@pytest.mark.asyncio
async def test_add_uri_rejects_private_mirror(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
) -> None:
    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addUri",
            [["https://8.8.8.8/file", "http://127.0.0.1/private"]],
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert "uris[1]" in exc_info.value.message
    assert "本机地址" in exc_info.value.message
    mock_aria2_client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_uri_rejects_credentialed_mirror(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
) -> None:
    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addUri",
            [["https://8.8.8.8/file", "https://user:secret@8.8.4.4/file"]],
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert "用户名或密码" in exc_info.value.message
    mock_aria2_client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_torrent_rejects_private_webseed(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
) -> None:
    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addTorrent",
            ["dGVzdA==", ["http://10.0.0.1/webseed"]],
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert "内网地址" in exc_info.value.message
    mock_aria2_client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [b"announce", b"url-list"])
async def test_add_torrent_rejects_private_embedded_endpoint(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
    field: bytes,
) -> None:
    torrent = _torrent_with_network_field(
        field,
        b"http://100.64.0.5/private",
    )

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addTorrent", [torrent])

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    expected = "内网地址" if field == b"announce" else "webseeds"
    assert expected in exc_info.value.message
    mock_aria2_client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_torrent_parses_private_endpoint_once_before_aria2(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
) -> None:
    torrent = _torrent_with_network_field(b"announce", b"http://100.64.0.5/private")

    with patch(
        "app.services.rpc.write.parse_torrent_base64_async",
        new_callable=AsyncMock,
        wraps=parse_torrent_base64_async,
    ) as parse_mock:
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addTorrent", [torrent])

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert parse_mock.await_count == 1
    mock_aria2_client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_uri_rejects_private_bt_tracker_option(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
) -> None:
    magnet = "magnet:?xt=urn:btih:" + "d" * 40
    trackers = (
        "udp://8.8.8.8:6969/announce,"
        "udp://100.64.0.6:6969/announce"
    )

    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addUri",
            [[magnet], {"bt-tracker": trackers}],
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert exc_info.value.message == "bt-tracker option is not allowed"
    mock_aria2_client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uris",
    [
        ["https://8.8.8.8/file", 42],
        [f"https://8.8.8.8/{index}" for index in range(17)],
    ],
)
async def test_add_uri_rejects_malformed_uri_lists(
    handler: Aria2RpcHandler,
    mock_aria2_client: AsyncMock,
    uris: list[object],
) -> None:
    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addUri", [uris])

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    mock_aria2_client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_global_stat_counts_v0_tasks(
    handler: Aria2RpcHandler,
) -> None:
    active = await create_rpc_task(
        user_id=handler.user_id, gid="gid-active", status="active", name="active.bin"
    )
    await create_rpc_task(
        user_id=handler.user_id, gid="gid-waiting", status="queued", name="waiting.bin"
    )
    await create_rpc_task(
        user_id=handler.user_id, gid="gid-done", status="completed", name="done.bin"
    )
    await _upsert_rpc_snapshot(
        active,
        {
            "gid": "gid-active",
            "status": "active",
            "downloadSpeed": "1000",
            "uploadSpeed": "500",
        },
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.getGlobalStat", [])

    assert result["downloadSpeed"] == "1000"
    assert result["uploadSpeed"] == "500"
    assert result["numActive"] == "1"
    assert result["numWaiting"] == "1"
    assert result["numStopped"] == "1"


@pytest.mark.asyncio
async def test_get_global_stat_uses_owned_snapshot_speeds_only(
    handler: Aria2RpcHandler,
) -> None:
    other = await create_user_v0(username="rpc_global_stat_other")
    owned_active = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-owned-active",
        status="active",
        name="owned-active.bin",
    )
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-owned-complete",
        status="completed",
        name="owned-complete.bin",
    )
    foreign_active = await create_rpc_task(
        user_id=other["id"],
        gid="gid-foreign-active",
        status="active",
        name="foreign-active.bin",
    )
    await _upsert_rpc_snapshot(
        owned_active,
        {
            "gid": "gid-owned-active",
            "status": "active",
            "downloadSpeed": "40",
            "uploadSpeed": "4",
        },
    )
    await _upsert_rpc_snapshot(
        foreign_active,
        {
            "gid": "gid-foreign-active",
            "status": "active",
            "downloadSpeed": "900",
            "uploadSpeed": "90",
        },
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.getGlobalStat", [])

    assert result["downloadSpeed"] == "40"
    assert result["uploadSpeed"] == "4"
    assert result["numActive"] == "1"
    assert result["numWaiting"] == "0"
    assert result["numStopped"] == "1"


@pytest.mark.asyncio
async def test_tell_active_uses_v0_tasks_and_snapshot_speed(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-active",
        status="active",
        name="active.bin",
        total_bytes=500,
        completed_bytes=100,
    )
    await _upsert_rpc_snapshot(
        task,
        {"gid": "gid-active", "status": "active", "downloadSpeed": "42"},
        files=[],
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellActive", [["gid", "downloadSpeed", "files"]]
    )

    assert result == [
        {
            "gid": f"task-{task['id']}",
            "downloadSpeed": "42",
            "files": [
                {
                    "index": "1",
                    "path": "active.bin",
                    "length": "500",
                    "completedLength": "100",
                    "selected": "true",
                    "uris": [],
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_tell_active_renders_snapshot_bt_metadata(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-stale-bt",
        status="active",
        name="magnet:?xt=urn:btih:stale",
        uri="magnet:?xt=urn:btih:stale",
        resource_kind="magnet",
        total_bytes=868_289_498,
        completed_bytes=866_552_794,
    )
    snapshot_files = [
        {
            "index": "1",
            "path": "real-file.mkv",
            "length": "868289498",
            "completedLength": "868289498",
        }
    ]
    await _upsert_rpc_snapshot(
        task,
        {
            "gid": "gid-stale-bt",
            "status": "active",
            "totalLength": "868289498",
            "completedLength": "868289498",
            "infoHash": "145c59fb37d713ad1c1b84caa64ac4d9c6f78fe1",
            "bittorrent": {"info": {"name": "real-file.mkv"}},
            "files": [
                {
                    "index": "1",
                    "path": "real-file.mkv",
                    "length": "868289498",
                    "completedLength": "868289498",
                    "selected": "true",
                    "uris": [],
                }
            ],
        },
        files=[
            {
                "index": "1",
                "path": "real-file.mkv",
                "length": "868289498",
                "completedLength": "868289498",
                "selected": "true",
                "uris": [],
            }
        ],
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellActive",
        [["gid", "completedLength", "infoHash", "bittorrent", "files"]],
    )

    assert result == [
        {
            "gid": f"task-{task['id']}",
            "completedLength": "868289498",
            "infoHash": "145c59fb37d713ad1c1b84caa64ac4d9c6f78fe1",
            "bittorrent": {
                "announceList": [[BT_TRACKER_PLACEHOLDER]],
                "comment": "",
                "creationDate": 0,
                "mode": "single",
                "info": {"name": "real-file.mkv"},
            },
            "files": [
                {
                    "index": "1",
                    "path": "real-file.mkv",
                    "length": "868289498",
                    "completedLength": "868289498",
                    "selected": "true",
                    "uris": [],
                }
            ],
        }
    ]
    _mock_client(handler).tell_active.assert_not_awaited()
    _mock_client(handler).tell_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_tell_active_falls_back_to_row_name_without_snapshot(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-refresh-fails",
        status="active",
        name="fallback-name.bin",
        total_bytes=100,
        completed_bytes=10,
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellActive",
        [["gid", "status", "downloadSpeed", "files"]],
    )

    assert result == [
        {
            "gid": f"task-{task['id']}",
            "status": "active",
            "downloadSpeed": "0",
            "files": [
                {
                    "index": "1",
                    "path": "fallback-name.bin",
                    "length": "100",
                    "completedLength": "10",
                    "selected": "true",
                    "uris": [],
                }
            ],
        }
    ]
    _mock_client(handler).tell_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_tell_status_prefers_snapshot_over_stale_db(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-live-status",
        status="active",
        name="stale-name.bin",
        resource_kind="magnet",
        total_bytes=100,
        completed_bytes=90,
    )
    snapshot_files = [
        {
            "index": "1",
            "path": "real-name.bin",
            "length": "100",
            "completedLength": "100",
        }
    ]
    await _upsert_rpc_snapshot(
        task,
        {
            "gid": "gid-live-status",
            "status": "complete",
            "totalLength": "100",
            "completedLength": "100",
            "infoHash": "abc",
            "bittorrent": {"info": {"name": "real-name.bin"}},
            "files": snapshot_files,
        },
    )
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellStatus",
        ["gid-live-status", ["status", "completedLength", "bittorrent", "files"]],
    )

    assert result["status"] == "complete"
    assert result["completedLength"] == "100"
    assert result["bittorrent"]["info"]["name"] == "real-name.bin"
    assert result["files"][0]["path"] == "real-name.bin"


@pytest.mark.asyncio
async def test_tell_active_lists_db_active_rows_without_aria2(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-db-stale-active",
        status="active",
        name="stale-active.bin",
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.tellActive", [["gid"]])

    assert result == [{"gid": f"task-{task['id']}"}]


@pytest.mark.asyncio
async def test_tell_waiting_reads_projection_and_filters_by_user(
    handler: Aria2RpcHandler,
) -> None:
    other = await create_user_v0(username="rpc_waiting_other")
    owned = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-waiting-owned",
        status="waiting",
        name="owned.bin",
        total_bytes=55,
        completed_bytes=5,
    )
    await create_rpc_task(
        user_id=other["id"],
        gid="gid-waiting-other",
        status="waiting",
        name="other.bin",
    )
    await _upsert_rpc_snapshot(
        owned,
        {
            "gid": "gid-waiting-owned",
            "status": "waiting",
            "totalLength": "55",
            "completedLength": "5",
        },
        files=[
            {
                "index": "1",
                "path": "owned.bin",
                "length": "55",
                "completedLength": "5",
                "selected": "true",
                "uris": [],
            }
        ],
    )
    _mock_client(handler).tell_waiting.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellWaiting",
        [0, 10, ["gid", "status", "totalLength", "files"]],
    )

    assert result == [
        {
            "gid": f"task-{owned['id']}",
            "status": "waiting",
            "totalLength": "55",
            "files": [
                {
                    "index": "1",
                    "path": "owned.bin",
                    "length": "55",
                    "completedLength": "5",
                    "selected": "true",
                    "uris": [],
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_tell_waiting_paginates_v0_rows(handler: Aria2RpcHandler) -> None:
    base = now_ms()
    old = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-old",
        status="queued",
        name="old.bin",
        updated_at_ms=base,
    )
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-new",
        status="waiting",
        name="new.bin",
        updated_at_ms=base + 1,
    )
    _mock_client(handler).tell_waiting.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.tellWaiting", [1, 1, ["gid"]])

    assert result == [{"gid": f"task-{old['id']}"}]


@pytest.mark.asyncio
async def test_tell_stopped_maps_terminal_tasks(handler: Aria2RpcHandler) -> None:
    ok = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-ok",
        status="completed",
        name="ok.bin",
        total_bytes=9,
    )
    fail = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-fail",
        status="failed",
        name="fail.bin",
        error_message="network",
    )

    result = await handler.handle(
        "aria2.tellStopped", [0, 10, ["gid", "status", "errorMessage"]]
    )

    assert result[0] == {
        "gid": f"task-{fail['id']}",
        "status": "error",
        "errorMessage": "network",
    }
    assert result[1] == {
        "gid": f"task-{ok['id']}",
        "status": "complete",
        "errorMessage": "",
    }


@pytest.mark.asyncio
async def test_tell_status_falls_back_to_v0_row(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-status",
        status="active",
        name="status.bin",
        total_bytes=20,
        completed_bytes=3,
    )
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.tellStatus", ["gid-status"])

    assert result["gid"] == f"task-{task['id']}"
    assert result["status"] == "active"
    assert result["files"][0]["path"] == "status.bin"


@pytest.mark.asyncio
async def test_tell_status_accepts_task_fallback_gid(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid=None,
        status="completed",
        name="done.bin",
        total_bytes=7,
    )

    result = await handler.handle("aria2.tellStatus", [f"task-{task['id']}"])

    assert result["gid"] == f"task-{task['id']}"
    assert result["status"] == "complete"
    assert result["completedLength"] == "7"


@pytest.mark.asyncio
async def test_tell_status_keeps_aria2_shape_with_bt_placeholders(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-bt-shape",
        status="active",
        name="fallback.torrent",
        resource_kind="torrent",
        total_bytes=10,
        completed_bytes=1,
    )
    snapshot_files = [
        {
            "index": "1",
            "path": "file.bin",
            "length": "10",
            "completedLength": "1",
            "selected": "true",
            "uris": [{"uri": "", "status": "used"}],
        }
    ]
    await _upsert_rpc_snapshot(
        task,
        {
            "gid": "gid-bt-shape",
            "status": "active",
            "totalLength": "10",
            "completedLength": "1",
            "downloadSpeed": "7",
            "uploadSpeed": "3",
            "connections": "2",
            "bittorrent": {"mode": "multi", "info": {"name": "public display name"}},
            "files": snapshot_files,
        },
    )
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.tellStatus", ["gid-bt-shape"])

    assert result["gid"] == f"task-{task['id']}"
    assert result["downloadSpeed"] == "7"
    assert result["uploadSpeed"] == "3"
    assert result["connections"] == "2"
    assert result["files"][0]["uris"] == [{"uri": "", "status": "used"}]
    assert result["bittorrent"] == {
        "announceList": [[BT_TRACKER_PLACEHOLDER]],
        "comment": "",
        "creationDate": 0,
        "mode": "multi",
        "info": {"name": "public display name"},
    }


@pytest.mark.asyncio
async def test_http_tell_status_omits_bt_fields_for_http_task(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-http-bt-noise",
        status="active",
        name="plain.bin",
        resource_kind="http",
    )
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellStatus",
        [
            "gid-http-bt-noise",
            ["gid", "bittorrent", "infoHash", "numSeeders", "seeder"],
        ],
    )

    assert result == {"gid": f"task-{task['id']}"}


@pytest.mark.asyncio
async def test_http_torrent_conversion_tell_status_projects_bt_shape(
    handler: Aria2RpcHandler,
) -> None:
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-http-torrent-converted",
        status="active",
        name="payload.torrent",
        resource_kind="http",
    )
    snapshot_files = [
        {"index": "1", "path": "a.bin"},
        {"index": "2", "path": "b.bin"},
    ]
    await _upsert_rpc_snapshot(
        task,
        {
            "gid": "gid-http-torrent-converted",
            "status": "active",
            "totalLength": "4096",
            "completedLength": "1024",
            "infoHash": info_hash,
            "bittorrent": {"mode": "multi", "info": {"name": "Real Torrent"}},
            "files": snapshot_files,
        },
    )
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellStatus",
        ["gid-http-torrent-converted", ["gid", "infoHash", "bittorrent", "files"]],
    )

    assert result["gid"] == f"task-{task['id']}"
    assert result["infoHash"] == info_hash
    assert result["bittorrent"] == {
        "announceList": [[BT_TRACKER_PLACEHOLDER]],
        "comment": "",
        "creationDate": 0,
        "mode": "multi",
        "info": {"name": "Real Torrent"},
    }
    assert [item["path"] for item in result["files"]] == ["a.bin", "b.bin"]


@pytest.mark.asyncio
async def test_get_files_reads_snapshot_and_falls_back_to_v0_name(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id, gid="gid-files", status="active", name="fallback.bin"
    )
    await _upsert_rpc_snapshot(
        task,
        {"gid": "gid-files", "status": "active"},
        files=[
            {
                "index": "1",
                "path": "file.bin",
                "length": "10",
                "uris": [{"uri": "", "status": "waiting"}],
            }
        ],
    )
    _mock_client(handler).get_files.side_effect = RuntimeError("aria2 unavailable")
    snapshot_files = await handler.handle("aria2.getFiles", ["gid-files"])

    await _upsert_rpc_snapshot(
        task,
        {"gid": "gid-files", "status": "active"},
        files=[],
    )
    fallback_files = await handler.handle("aria2.getFiles", ["gid-files"])

    assert snapshot_files[0]["path"] == "file.bin"
    assert snapshot_files[0]["uris"] == [{"uri": "", "status": "waiting"}]
    assert fallback_files[0]["path"] == "fallback.bin"
    _mock_client(handler).get_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_uris_returns_masked_source_uri_shape(
    handler: Aria2RpcHandler,
) -> None:
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-uris",
        status="active",
        name="secret.bin",
        uri="https://user:pass@example.com/secret.bin",
    )
    _mock_client(handler).get_uris.side_effect = RuntimeError("aria2 unavailable")
    live = await handler.handle("aria2.getUris", ["gid-uris"])

    assert live == [{"uri": "", "status": "used"}]
    _mock_client(handler).get_uris.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_peers_and_servers_return_empty_without_aria2(
    handler: Aria2RpcHandler,
) -> None:
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-peer",
        status="active",
        name="magnet:?xt=urn:btih:abc",
        uri="magnet:?xt=urn:btih:abc",
        resource_kind="magnet",
    )
    _mock_client(handler).get_peers.side_effect = RuntimeError("aria2 unavailable")
    _mock_client(handler).get_servers.side_effect = RuntimeError("aria2 unavailable")

    peers = await handler.handle("aria2.getPeers", ["gid-peer"])
    servers = await handler.handle("aria2.getServers", ["gid-peer"])

    assert peers == []
    assert servers == []
    _mock_client(handler).get_peers.assert_not_awaited()
    _mock_client(handler).get_servers.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_cancels_active_v0_task(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id, gid="gid-remove", status="active", name="remove.bin"
    )

    result = await handler.handle("aria2.remove", ["gid-remove"])
    latest = await get_user_task_by_id(handler.user_id, task["id"])

    assert result == "gid-remove"
    assert latest is not None
    assert latest["status"] == "cancelled"
    _mock_client(handler).remove.assert_awaited_once_with("gid-remove")


@pytest.mark.asyncio
async def test_purge_download_result_deletes_terminal_only(
    handler: Aria2RpcHandler,
) -> None:
    active = await create_rpc_task(
        user_id=handler.user_id, gid="gid-active", status="active", name="active.bin"
    )
    terminal = await create_rpc_task(
        user_id=handler.user_id, gid="gid-terminal", status="failed", name="failed.bin"
    )

    result = await handler.handle("aria2.purgeDownloadResult", [])

    assert result == "OK"
    assert await get_user_task_by_id(handler.user_id, terminal["id"]) is None
    assert await get_user_task_by_id(handler.user_id, active["id"]) is not None


@pytest.mark.asyncio
async def test_remove_download_result_accepts_gid_and_task_fallback(
    handler: Aria2RpcHandler,
) -> None:
    by_gid = await create_rpc_task(
        user_id=handler.user_id, gid="gid-terminal", status="failed", name="failed.bin"
    )
    by_task = await create_rpc_task(
        user_id=handler.user_id, gid=None, status="completed", name="done.bin"
    )

    assert await handler.handle("aria2.removeDownloadResult", ["gid-terminal"]) == "OK"
    assert (
        await handler.handle("aria2.removeDownloadResult", [f"task-{by_task['id']}"])
        == "OK"
    )
    assert await get_user_task_by_id(handler.user_id, by_gid["id"]) is None
    assert await get_user_task_by_id(handler.user_id, by_task["id"]) is None


@pytest.mark.asyncio
async def test_remove_download_result_rejects_other_user_terminal_gid(
    handler: Aria2RpcHandler,
) -> None:
    other = await create_user_v0(username="rpc_remove_other")
    await create_rpc_task(
        user_id=other["id"],
        gid="gid-other-terminal",
        status="completed",
        name="other-terminal.bin",
    )

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.removeDownloadResult", ["gid-other-terminal"])

    assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_remove_download_result_deletes_effective_terminal_gid(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-effective-complete",
        status="active",
        global_status="completed",
        name="effective-complete.bin",
        total_bytes=11,
        completed_bytes=11,
    )

    stopped = await handler.handle("aria2.tellStopped", [0, 10, ["gid", "status"]])
    result = await handler.handle("aria2.removeDownloadResult", ["gid-effective-complete"])

    assert stopped == [{"gid": f"task-{task['id']}", "status": "complete"}]
    assert result == "OK"
    assert await get_user_task_by_id(handler.user_id, task["id"]) is None


@pytest.mark.asyncio
async def test_purge_download_result_deletes_effective_terminal_rows(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-effective-failed",
        status="active",
        global_status="failed",
        name="effective-failed.bin",
        error_message="network",
    )

    result = await handler.handle("aria2.purgeDownloadResult", [])

    assert result == "OK"
    assert await get_user_task_by_id(handler.user_id, task["id"]) is None


@pytest.mark.asyncio
async def test_invalid_params_raise_rpc_errors(handler: Aria2RpcHandler) -> None:
    for method in (
        "aria2.remove",
        "aria2.tellStatus",
        "aria2.getFiles",
        "aria2.getUris",
    ):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle(method, [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.tellStatus", ["missing"])
    assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_static_compat_methods(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-options",
        status="active",
        name="options.bin",
    )
    task_gid = f"task-{task['id']}"
    for method in (
        "aria2.pause",
        "aria2.forcePause",
        "aria2.unpause",
        "aria2.pauseAll",
        "aria2.forcePauseAll",
        "aria2.unpauseAll",
    ):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle(method, [task_gid])
        assert exc_info.value.code == 1
    assert await handler.handle("aria2.getOption", [task_gid]) == {}
    assert await handler.handle("aria2.getGlobalOption", []) == {}
    assert await handler.handle(
        "aria2.changePosition", [task_gid, 0, "POS_SET"]
    ) == 0
    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.changeUri",
            [task_gid, 1, [], ["https://attacker.example/payload"]],
        )
    assert exc_info.value.code == RpcErrorCode.PERMISSION_DENIED
    assert "not supported" in exc_info.value.message
    assert await handler.handle("aria2.getSessionInfo", []) == {
        "sessionId": "aria2deck-proxy-session"
    }
    # Removed dangerous methods should raise METHOD_NOT_FOUND
    for method in ("aria2.changeOption", "aria2.changeGlobalOption",
                   "aria2.shutdown", "aria2.forceShutdown"):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle(method, [])
        assert exc_info.value.code == RpcErrorCode.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_gateway_capability_is_omitted_from_rpc_views_and_logs(
    temp_db: str,
    mock_aria2_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = await create_user_v0(username="rpc_capability_owner")
    other = await create_user_v0(username="rpc_capability_other")
    task = await create_rpc_task(
        user_id=owner["id"],
        gid="gid-capability",
        status="active",
        name="capability.bin",
    )
    task_gid = f"task-{task['id']}"
    capability = "eyJwIjoic291cmNlLXNlY3JldCJ9.raw-capability-signature"
    await _upsert_rpc_snapshot(
        task,
        {
            "gid": "gid-capability",
            "status": "active",
            "errorMessage": capability,
            "files": [
                {
                    "index": "1",
                    "path": "payload",
                    "length": "1",
                    "completedLength": "0",
                    "selected": "true",
                    "uris": [{"uri": "", "status": "used"}],
                }
            ],
        },
    )
    mock_aria2_client.tell_status.side_effect = RuntimeError(capability)
    mock_aria2_client.get_uris.side_effect = RuntimeError(capability)
    mock_aria2_client.change_uri = AsyncMock()
    owner_handler = Aria2RpcHandler(owner["id"])
    other_handler = Aria2RpcHandler(other["id"])

    with caplog.at_level(logging.DEBUG):
        status = await owner_handler.handle("aria2.tellStatus", [task_gid])
        uris = await owner_handler.handle("aria2.getUris", [task_gid])
        options = await owner_handler.handle("aria2.getOption", [task_gid])
        with pytest.raises(RpcError) as change_error:
            await owner_handler.handle(
                "aria2.changeUri",
                [task_gid, 1, [], ["https://attacker.example/payload"]],
            )
        with pytest.raises(RpcError) as cross_user_error:
            await other_handler.handle("aria2.getOption", [task_gid])

    stored = await get_user_task_by_id(owner["id"], int(task["id"]))
    assert capability not in repr(status)
    assert capability not in repr(uris)
    assert capability not in repr(options)
    assert capability not in caplog.text
    assert status["errorMessage"] == ""
    assert status["files"][0]["uris"] == [{"uri": "", "status": "used"}]
    assert uris == [{"uri": "", "status": "used"}]
    assert options == {}
    assert change_error.value.code == RpcErrorCode.PERMISSION_DENIED
    assert cross_user_error.value.code == RpcErrorCode.TASK_NOT_FOUND
    mock_aria2_client.change_uri.assert_not_awaited()
    assert stored is not None
    assert stored["source_uri"] == task["source_uri"]
    assert stored["aria2_gid"] == "gid-capability"


@pytest.mark.asyncio
async def test_system_multicall_strips_inner_token(handler: Aria2RpcHandler) -> None:
    result = await handler.handle(
        "system.multicall",
        [[{"methodName": "aria2.getVersion", "params": ["token:inner"]}]],
    )

    assert result == [[{"version": "aria2deck-proxy", "enabledFeatures": []}]]


@pytest.mark.asyncio
async def test_system_multicall_rejects_nested_calls(handler: Aria2RpcHandler) -> None:
    result = await handler.handle(
        "system.multicall",
        [
            [
                {
                    "methodName": "system.multicall",
                    "params": [[{"methodName": "aria2.getVersion", "params": []}]],
                }
            ]
        ],
    )

    assert result == [
        {
            "faultCode": RpcErrorCode.INVALID_REQUEST,
            "faultString": "Nested multicall is not allowed",
        }
    ]


@pytest.mark.asyncio
async def test_system_multicall_generic_error_does_not_disclose_exception(
    handler: Aria2RpcHandler,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "fake-capability.secret-payload"
    monkeypatch.setattr(
        handler,
        "_handle_get_version",
        AsyncMock(side_effect=RuntimeError(secret)),
    )

    with caplog.at_level(logging.WARNING):
        result = await handler.handle(
            "system.multicall",
            [[{"methodName": "aria2.getVersion", "params": []}]],
        )

    assert result == [
        {
            "faultCode": RpcErrorCode.INTERNAL_ERROR,
            "faultString": "Internal error",
        }
    ]
    assert secret not in str(result)
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_system_multicall_redacts_explicit_internal_rpc_error(
    handler: Aria2RpcHandler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "explicit-internal-rpc-secret"
    monkeypatch.setattr(
        handler,
        "_handle_get_version",
        AsyncMock(
            side_effect=RpcError(RpcErrorCode.INTERNAL_ERROR, secret)
        ),
    )

    result = await handler.handle(
        "system.multicall",
        [[{"methodName": "aria2.getVersion", "params": []}]],
    )

    assert result == [
        {
            "faultCode": RpcErrorCode.INTERNAL_ERROR,
            "faultString": "Internal error",
        }
    ]
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_system_multicall_rejects_oversized_batches(
    handler: Aria2RpcHandler,
) -> None:
    calls = [{"methodName": "aria2.getVersion", "params": []} for _ in range(21)]

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("system.multicall", [calls])

    assert exc_info.value.code == RpcErrorCode.INVALID_REQUEST
    assert exc_info.value.message == "Too many methods in multicall, max 20"


@pytest.mark.asyncio
async def test_get_user_available_space_uses_v0_usage(handler: Aria2RpcHandler) -> None:
    available = await handler._get_user_available_space()

    assert available > 0


@pytest.mark.asyncio
async def test_get_user_available_space_no_user(handler: Aria2RpcHandler) -> None:
    async with transaction() as conn:
        await conn.execute(delete(users).where(users.c.id == handler.user_id))

    assert await handler._get_user_available_space() == 0


@pytest.mark.asyncio
async def test_other_user_tasks_are_hidden(handler: Aria2RpcHandler) -> None:
    other = await create_user_v0(username="other-rpc")
    await create_rpc_task(
        user_id=other["id"], gid="gid-other", status="active", name="other.bin"
    )

    assert await handler.handle("aria2.tellActive", []) == []
    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.tellStatus", ["gid-other"])
    assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_list_user_tasks_helper_still_orders_by_updated_at(
    handler: Aria2RpcHandler,
) -> None:
    base = now_ms()
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-a",
        status="active",
        name="a.bin",
        updated_at_ms=base,
    )
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-b",
        status="active",
        name="b.bin",
        updated_at_ms=base + 1,
    )

    rows = await list_user_tasks(handler.user_id, ["active"])

    assert [row["aria2_gid"] for row in rows] == ["gid-b", "gid-a"]


# ---------------------------------------------------------------------------
# Regression: the backend aria2 gid must never be exposed to RPC clients.
# The external identity is always task-{user_task_id}; live data is still
# fetched from aria2 via the real backend gid behind the scenes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tell_active_masks_backend_gid(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="backend-gid-active",
        status="active",
        name="active.bin",
    )
    await _upsert_rpc_snapshot(
        task,
        {"gid": "backend-gid-active", "status": "active", "downloadSpeed": "42"},
    )
    _mock_client(handler).tell_active.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.tellActive", [["gid", "downloadSpeed"]])

    assert result == [{"gid": f"task-{task['id']}", "downloadSpeed": "42"}]
    _mock_client(handler).tell_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_tell_status_by_task_gid_reads_projection(
    handler: Aria2RpcHandler,
) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="backend-gid-status",
        status="active",
        name="stale-name.bin",
        total_bytes=100,
        completed_bytes=10,
    )
    await _upsert_rpc_snapshot(
        task,
        {
            "gid": "backend-gid-status",
            "status": "active",
            "totalLength": "100",
            "completedLength": "80",
            "downloadSpeed": "999",
        },
    )
    _mock_client(handler).tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle(
        "aria2.tellStatus",
        [f"task-{task['id']}", ["gid", "completedLength", "downloadSpeed"]],
    )

    # Output identity is masked; the numbers come from the backend snapshot.
    assert result["gid"] == f"task-{task['id']}"
    assert result["completedLength"] == "80"
    assert result["downloadSpeed"] == "999"
    _mock_client(handler).tell_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_accepts_task_gid(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(
        user_id=handler.user_id,
        gid="backend-gid-remove",
        status="active",
        name="remove.bin",
    )

    result = await handler.handle("aria2.remove", [f"task-{task['id']}"])
    latest = await get_user_task_by_id(handler.user_id, task["id"])

    assert result == f"task-{task['id']}"
    assert latest is not None
    assert latest["status"] == "cancelled"
    _mock_client(handler).remove.assert_awaited_once_with("backend-gid-remove")


@pytest.mark.asyncio
async def test_http_task_omits_bittorrent_and_infohash(
    handler: Aria2RpcHandler,
) -> None:
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-http",
        status="active",
        name="file.bin",
        resource_kind="http",
    )
    _mock_client(handler).tell_active.return_value = [
        {"gid": "gid-http", "status": "active", "infoHash": "deadbeef"}
    ]

    result = await handler.handle("aria2.tellActive", [])

    assert len(result) == 1
    status = result[0]
    for key in ("bittorrent", "infoHash", "numSeeders", "seeder"):
        assert key not in status
    # 通用字段（所有下载都应有）需保留，否则第三方客户端会报错
    for key in ("pieceLength", "numPieces", "connections"):
        assert key in status


@pytest.mark.asyncio
async def test_magnet_metadata_phase_name_falls_back_to_magnet(
    handler: Aria2RpcHandler,
) -> None:
    magnet = "magnet:?xt=urn:btih:abc123"
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-magnet",
        status="active",
        name=magnet,
        uri=magnet,
        resource_kind="magnet",
    )
    # During metadata resolution aria2 reports a [METADATA] placeholder name.
    _mock_client(handler).tell_active.return_value = [
        {
            "gid": "gid-magnet",
            "status": "active",
            "bittorrent": {"info": {"name": "[METADATA]abc123"}},
        }
    ]

    result = await handler.handle("aria2.tellActive", [["gid", "bittorrent"]])

    assert result[0]["bittorrent"]["info"]["name"] == magnet


def test_select_file_rejects_huge_range_before_expansion() -> None:
    metadata = TorrentMetadata(
        info_hash="a" * 40,
        name="one.bin",
        files=(TorrentFile(index=1, path=("one.bin",), size=1),),
        tree=[],
        tracker_urls=(),
        webseed_urls=(),
    )

    with pytest.raises(RpcError) as exc_info:
        Aria2RpcHandler._selected_torrent_indexes(metadata, "1-1000000000")

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert exc_info.value.message == "select-file 参数无效"
