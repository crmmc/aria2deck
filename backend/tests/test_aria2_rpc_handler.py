"""Tests for the v0 aria2 RPC handler."""
from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert

from app.aria2.client import Aria2Client
from app.core.state import AppState
from app.db.engine import transaction
from app.db.schema import global_downloads, user_tasks, users
from app.repositories.downloads import get_user_task_by_id, list_user_tasks
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode
from tests.helpers_v0 import create_user_v0, now_ms


async def create_rpc_task(
    *,
    user_id: int,
    gid: str | None,
    status: str,
    name: str,
    uri: str | None = None,
    total_bytes: int = 100,
    completed_bytes: int = 0,
    error_message: str | None = None,
    updated_at_ms: int | None = None,
) -> dict[str, Any]:
    timestamp = updated_at_ms or now_ms()
    source_uri = uri or f"https://example.com/{name}"
    async with transaction() as conn:
        download = (
            await conn.execute(
                insert(global_downloads)
                .values(
                    resource_key=f"rpc:{user_id}:{gid or name}:{timestamp}",
                    resource_kind="http",
                    source_uri=source_uri,
                    display_name=name,
                    aria2_gid=gid,
                    status=status,
                    total_bytes=total_bytes,
                    completed_bytes=completed_bytes,
                    error_message=error_message,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    completed_at_ms=timestamp if status in {"completed", "failed", "cancelled"} else None,
                )
                .returning(global_downloads)
            )
        ).mappings().one()
        task = (
            await conn.execute(
                insert(user_tasks)
                .values(
                    user_id=user_id,
                    global_download_id=download["id"],
                    status=status,
                    reserved_bytes=total_bytes if status in {"queued", "active", "waiting", "paused"} else 0,
                    display_name=name,
                    error_message=error_message,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp if status in {"completed", "failed", "cancelled"} else None,
                )
                .returning(user_tasks)
            )
        ).mappings().one()
    row = await get_user_task_by_id(user_id, int(task["id"]))
    assert row is not None
    return row


@pytest.fixture
def mock_aria2_client() -> AsyncMock:
    client = AsyncMock()
    client.get_version.return_value = {"version": "1.36.0", "enabledFeatures": ["BitTorrent"]}
    client.get_global_stat.return_value = {"downloadSpeed": "1000", "uploadSpeed": "500"}
    client.tell_active.return_value = []
    client.tell_status.return_value = {}
    client.get_files.return_value = []
    client.get_uris.return_value = []
    client.get_peers.return_value = []
    client.get_servers.return_value = []
    client.force_remove.return_value = "OK"
    return client


@pytest.fixture
def handler(test_user: dict, mock_aria2_client: AsyncMock) -> Aria2RpcHandler:
    return Aria2RpcHandler(test_user["id"], mock_aria2_client, AppState())


def test_aria2_rpc_handler_requires_app_state() -> None:
    client = Aria2Client("http://localhost:6800/jsonrpc")
    with pytest.raises(RuntimeError):
        Aria2RpcHandler(user_id=1, aria2_client=client, app_state=cast(AppState, None))


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

    assert version["version"] == "1.36.0"
    assert "aria2.addUri" in methods
    assert "aria2.getVersion" in methods


@pytest.mark.asyncio
async def test_get_global_stat_counts_v0_tasks(
    handler: Aria2RpcHandler,
) -> None:
    await create_rpc_task(user_id=handler.user_id, gid="gid-active", status="active", name="active.bin")
    await create_rpc_task(user_id=handler.user_id, gid="gid-waiting", status="queued", name="waiting.bin")
    await create_rpc_task(user_id=handler.user_id, gid="gid-done", status="completed", name="done.bin")

    result = await handler.handle("aria2.getGlobalStat", [])

    assert result["downloadSpeed"] == "1000"
    assert result["uploadSpeed"] == "500"
    assert result["numActive"] == "1"
    assert result["numWaiting"] == "1"
    assert result["numStopped"] == "1"


@pytest.mark.asyncio
async def test_tell_active_uses_v0_tasks_and_live_speed(handler: Aria2RpcHandler) -> None:
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-active",
        status="active",
        name="active.bin",
        total_bytes=500,
        completed_bytes=100,
    )
    handler.client.tell_active.return_value = [
        {"gid": "gid-active", "status": "active", "downloadSpeed": "42", "files": []}
    ]

    result = await handler.handle("aria2.tellActive", [["gid", "downloadSpeed", "files"]])

    assert result == [{"gid": "gid-active", "downloadSpeed": "42", "files": [
        {
            "index": "1",
            "path": "active.bin",
            "length": "500",
            "completedLength": "100",
            "selected": "true",
            "uris": [],
        }
    ]}]


@pytest.mark.asyncio
async def test_tell_waiting_paginates_v0_rows(handler: Aria2RpcHandler) -> None:
    base = now_ms()
    await create_rpc_task(user_id=handler.user_id, gid="gid-old", status="queued", name="old.bin", updated_at_ms=base)
    await create_rpc_task(user_id=handler.user_id, gid="gid-new", status="waiting", name="new.bin", updated_at_ms=base + 1)

    result = await handler.handle("aria2.tellWaiting", [1, 1, ["gid"]])

    assert result == [{"gid": "gid-old"}]


@pytest.mark.asyncio
async def test_tell_stopped_maps_terminal_tasks(handler: Aria2RpcHandler) -> None:
    await create_rpc_task(user_id=handler.user_id, gid="gid-ok", status="completed", name="ok.bin", total_bytes=9)
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-fail",
        status="failed",
        name="fail.bin",
        error_message="network",
    )

    result = await handler.handle("aria2.tellStopped", [0, 10, ["gid", "status", "errorMessage"]])

    assert result[0] == {"gid": "gid-fail", "status": "error", "errorMessage": "network"}
    assert result[1] == {"gid": "gid-ok", "status": "complete", "errorMessage": ""}


@pytest.mark.asyncio
async def test_tell_status_falls_back_to_v0_row(handler: Aria2RpcHandler) -> None:
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-status",
        status="active",
        name="status.bin",
        total_bytes=20,
        completed_bytes=3,
    )
    handler.client.tell_status.side_effect = RuntimeError("aria2 unavailable")

    result = await handler.handle("aria2.tellStatus", ["gid-status"])

    assert result["gid"] == "gid-status"
    assert result["status"] == "active"
    assert result["files"][0]["path"] == "status.bin"


@pytest.mark.asyncio
async def test_tell_status_accepts_task_fallback_gid(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(user_id=handler.user_id, gid=None, status="completed", name="done.bin", total_bytes=7)

    result = await handler.handle("aria2.tellStatus", [f"task-{task['id']}"])

    assert result["gid"] == f"task-{task['id']}"
    assert result["status"] == "complete"
    assert result["completedLength"] == "7"


@pytest.mark.asyncio
async def test_get_files_strips_paths_and_falls_back_to_v0_name(handler: Aria2RpcHandler) -> None:
    await create_rpc_task(user_id=handler.user_id, gid="gid-files", status="active", name="fallback.bin")
    handler.client.get_files.return_value = [
        {"index": "1", "path": "/private/downloads/file.bin", "length": "10", "uris": [{"uri": "http://x"}]}
    ]
    live_files = await handler.handle("aria2.getFiles", ["gid-files"])

    handler.client.get_files.return_value = []
    fallback_files = await handler.handle("aria2.getFiles", ["gid-files"])

    assert live_files[0]["path"] == "file.bin"
    assert live_files[0]["uris"] == []
    assert fallback_files[0]["path"] == "fallback.bin"


@pytest.mark.asyncio
async def test_get_uris_masks_credentials_and_falls_back_to_source_uri(handler: Aria2RpcHandler) -> None:
    await create_rpc_task(
        user_id=handler.user_id,
        gid="gid-uris",
        status="active",
        name="secret.bin",
        uri="https://user:pass@example.com/secret.bin",
    )
    handler.client.get_uris.return_value = [
        {"uri": "https://user:pass@example.com/secret.bin", "status": "used"},
        {"uri": "https://example.com/next.bin", "status": "unknown"},
    ]
    live = await handler.handle("aria2.getUris", ["gid-uris"])

    handler.client.get_uris.side_effect = RuntimeError("aria2 unavailable")
    fallback = await handler.handle("aria2.getUris", ["gid-uris"])

    assert live[0] == {"uri": "https://***:***@example.com/secret.bin", "status": "used"}
    assert live[1] == {"uri": "https://example.com/next.bin", "status": "waiting"}
    assert fallback == [{"uri": "https://***:***@example.com/secret.bin", "status": "used"}]


@pytest.mark.asyncio
async def test_get_peers_and_servers_mask_sensitive_fields(handler: Aria2RpcHandler) -> None:
    await create_rpc_task(user_id=handler.user_id, gid="gid-peer", status="active", name="peer.bin")
    handler.client.get_peers.return_value = [{"peerId": "raw", "ip": "8.8.8.8", "downloadSpeed": "1"}]
    handler.client.get_servers.return_value = [
        {"index": "1", "servers": [{"uri": "https://x", "currentUri": "https://y", "downloadSpeed": "2"}]}
    ]

    peers = await handler.handle("aria2.getPeers", ["gid-peer"])
    servers = await handler.handle("aria2.getServers", ["gid-peer"])

    assert peers[0]["peerId"] == "masked-peer"
    assert peers[0]["ip"] == "0.0.0.0"
    assert servers[0]["servers"][0]["uri"] == ""
    assert servers[0]["servers"][0]["downloadSpeed"] == "2"


@pytest.mark.asyncio
async def test_remove_cancels_active_v0_task(handler: Aria2RpcHandler) -> None:
    task = await create_rpc_task(user_id=handler.user_id, gid="gid-remove", status="active", name="remove.bin")

    result = await handler.handle("aria2.remove", ["gid-remove"])
    latest = await get_user_task_by_id(handler.user_id, task["id"])

    assert result == "gid-remove"
    assert latest is not None
    assert latest["status"] == "cancelled"
    handler.client.force_remove.assert_awaited_once_with("gid-remove")


@pytest.mark.asyncio
async def test_purge_download_result_deletes_terminal_only(handler: Aria2RpcHandler) -> None:
    active = await create_rpc_task(user_id=handler.user_id, gid="gid-active", status="active", name="active.bin")
    terminal = await create_rpc_task(user_id=handler.user_id, gid="gid-terminal", status="failed", name="failed.bin")

    result = await handler.handle("aria2.purgeDownloadResult", [])

    assert result == "OK"
    assert await get_user_task_by_id(handler.user_id, terminal["id"]) is None
    assert await get_user_task_by_id(handler.user_id, active["id"]) is not None


@pytest.mark.asyncio
async def test_remove_download_result_accepts_gid_and_task_fallback(handler: Aria2RpcHandler) -> None:
    by_gid = await create_rpc_task(user_id=handler.user_id, gid="gid-terminal", status="failed", name="failed.bin")
    by_task = await create_rpc_task(user_id=handler.user_id, gid=None, status="completed", name="done.bin")

    assert await handler.handle("aria2.removeDownloadResult", ["gid-terminal"]) == "OK"
    assert await handler.handle("aria2.removeDownloadResult", [f"task-{by_task['id']}"]) == "OK"
    assert await get_user_task_by_id(handler.user_id, by_gid["id"]) is None
    assert await get_user_task_by_id(handler.user_id, by_task["id"]) is None


@pytest.mark.asyncio
async def test_invalid_params_raise_rpc_errors(handler: Aria2RpcHandler) -> None:
    for method in ("aria2.remove", "aria2.tellStatus", "aria2.getFiles", "aria2.getUris"):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle(method, [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.tellStatus", ["missing"])
    assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_static_compat_methods(handler: Aria2RpcHandler) -> None:
    assert await handler.handle("aria2.pause", ["gid"]) == "gid"
    assert await handler.handle("aria2.forcePause", ["gid"]) == "gid"
    assert await handler.handle("aria2.unpause", ["gid"]) == "gid"
    assert await handler.handle("aria2.pauseAll", []) == "OK"
    assert await handler.handle("aria2.forcePauseAll", []) == "OK"
    assert await handler.handle("aria2.unpauseAll", []) == "OK"
    assert await handler.handle("aria2.getOption", ["gid"]) == {}
    assert await handler.handle("aria2.changeOption", ["gid", {}]) == "OK"
    assert await handler.handle("aria2.getGlobalOption", []) == {}
    assert await handler.handle("aria2.changeGlobalOption", [{}]) == "OK"
    assert await handler.handle("aria2.changePosition", ["gid", 0, "POS_SET"]) == 0
    assert await handler.handle("aria2.getSessionInfo", []) == {"sessionId": "aria2deck-proxy-session"}


@pytest.mark.asyncio
async def test_system_multicall_strips_inner_token(handler: Aria2RpcHandler) -> None:
    result = await handler.handle(
        "system.multicall",
        [[{"methodName": "aria2.getVersion", "params": ["token:inner"]}]],
    )

    assert result == [[{"version": "1.36.0", "enabledFeatures": ["BitTorrent"]}]]


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
    await create_rpc_task(user_id=other["id"], gid="gid-other", status="active", name="other.bin")

    assert await handler.handle("aria2.tellActive", []) == []
    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.tellStatus", ["gid-other"])
    assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_list_user_tasks_helper_still_orders_by_updated_at(handler: Aria2RpcHandler) -> None:
    base = now_ms()
    await create_rpc_task(user_id=handler.user_id, gid="gid-a", status="active", name="a.bin", updated_at_ms=base)
    await create_rpc_task(user_id=handler.user_id, gid="gid-b", status="active", name="b.bin", updated_at_ms=base + 1)

    rows = await list_user_tasks(handler.user_id, ["active"])

    assert [row["aria2_gid"] for row in rows] == ["gid-b", "gid-a"]
