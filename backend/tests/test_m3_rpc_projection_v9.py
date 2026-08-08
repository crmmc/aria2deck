"""M3 T11: RPC handler 读路径全部走投影（task_backend_snapshots）。

验证 ``Aria2RpcHandler`` 的读方法（tellStatus/tellActive/getGlobalStat/
getFiles/getUris/getPeers/getServers/getVersion）在 aria2 client 完全
不可达（所有读方法抛异常）时仍能从 DB 投影返回数据，且不再触发任何
client 读调用。
"""

from __future__ import annotations

import json

import pytest

from app.repositories.backend_snapshots import upsert_snapshot
from app.services.aria2_rpc_handler import Aria2RpcHandler
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
)


async def _make_task(
    username: str,
    resource_key: str,
    *,
    status: str = "active",
    display_name: str = "file.bin",
    total_bytes: int = 1000,
    completed_bytes: int = 0,
) -> tuple[dict, dict, dict]:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=f"http://example.com/{display_name}",
        resource_kind="http",
        status=status,
        aria2_gid=f"gid-{resource_key[-6:]}",
        total_bytes=total_bytes,
        completed_bytes=completed_bytes,
        display_name=display_name,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status=status,
        display_name=display_name,
    )
    return user, gd, task


async def _snapshot(
    gd: dict,
    raw: dict,
    files: list[dict] | None = None,
) -> None:
    snapshot_files = files if files is not None else raw.get("files") or []
    await upsert_snapshot(
        global_download_id=int(gd["id"]),
        download_speed=int(str(raw.get("downloadSpeed") or 0)),
        upload_speed=int(str(raw.get("uploadSpeed") or 0)),
        total_length=int(str(raw.get("totalLength") or 0)),
        completed_length=int(str(raw.get("completedLength") or 0)),
        status=str(raw.get("status") or "active"),
        files_json=json.dumps(snapshot_files),
        raw_json=json.dumps(raw),
        updated_at_ms=now_ms(),
    )


def _broken_client():
    """aria2 client whose every read method raises."""
    error = RuntimeError("aria2 unavailable")
    return make_aria2_client(
        tell_status=error,
        tell_active=error,
        tell_waiting=error,
        tell_stopped=error,
        get_files=error,
        get_uris=error,
        get_peers=error,
        get_servers=error,
        get_version=error,
        get_global_stat=error,
    )


@pytest.mark.asyncio
async def test_tell_status_reads_projection_when_aria2_down(temp_db: str) -> None:
    user, gd, task = await _make_task(
        "t11_status", "http://example.com/status.bin", completed_bytes=400
    )
    await _snapshot(
        gd,
        {
            "gid": gd["aria2_gid"],
            "status": "active",
            "totalLength": "1000",
            "completedLength": "700",
            "downloadSpeed": "123",
        },
    )
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "aria2.tellStatus",
        [f"task-{task['id']}", ["gid", "completedLength", "downloadSpeed"]],
    )

    assert result == {
        "gid": f"task-{task['id']}",
        "completedLength": "700",
        "downloadSpeed": "123",
    }
    client.tell_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_tell_active_reads_projection_when_aria2_down(temp_db: str) -> None:
    user, gd, task = await _make_task("t11_active", "http://example.com/active.bin")
    await _snapshot(
        gd,
        {"gid": gd["aria2_gid"], "status": "active", "downloadSpeed": "42"},
    )
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.tellActive", [["gid", "downloadSpeed"]])

    assert result == [{"gid": f"task-{task['id']}", "downloadSpeed": "42"}]
    client.tell_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_global_stat_aggregates_snapshot_speeds_when_aria2_down(
    temp_db: str,
) -> None:
    user, gd, _task = await _make_task("t11_stat", "http://example.com/stat.bin")
    await _snapshot(
        gd,
        {
            "gid": gd["aria2_gid"],
            "status": "active",
            "downloadSpeed": "100",
            "uploadSpeed": "5",
        },
    )
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.getGlobalStat", [])

    assert result["downloadSpeed"] == "100"
    assert result["uploadSpeed"] == "5"
    assert result["numActive"] == "1"
    client.tell_active.assert_not_awaited()
    client.get_global_stat.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_files_reads_snapshot_files_when_aria2_down(temp_db: str) -> None:
    user, gd, _task = await _make_task("t11_files", "http://example.com/files.bin")
    await _snapshot(
        gd,
        {"gid": gd["aria2_gid"], "status": "active"},
        files=[{"index": "1", "path": "movie.mkv", "length": "1000"}],
    )
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.getFiles", [f"task-{_task['id']}"])

    assert result[0]["path"] == "movie.mkv"
    client.get_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_peers_and_servers_return_empty_without_aria2(temp_db: str) -> None:
    user, _gd, task = await _make_task("t11_peers", "http://example.com/peers.bin")
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    peers = await handler.handle("aria2.getPeers", [f"task-{task['id']}"])
    servers = await handler.handle("aria2.getServers", [f"task-{task['id']}"])

    assert peers == []
    assert servers == []
    client.get_peers.assert_not_awaited()
    client.get_servers.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_version_is_static_proxy_version(temp_db: str) -> None:
    user, _gd, _task = await _make_task("t11_version", "http://example.com/v.bin")
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.getVersion", [])

    assert result == {"version": "aria2deck-proxy", "enabledFeatures": []}
    client.get_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_uris_masked_shape_without_aria2(temp_db: str) -> None:
    user, _gd, task = await _make_task("t11_uris", "http://example.com/uris.bin")
    client = _broken_client()
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.getUris", [f"task-{task['id']}"])

    assert result == [{"uri": "", "status": "used"}]
    client.get_uris.assert_not_awaited()
