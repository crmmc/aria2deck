from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from app.repositories.downloads import get_user_task, list_user_tasks
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode
from app.services.download_service import create_user_download
from app.services.storage import get_store_path_for_hash
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
)


MAGNET_INFO_HASH = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.asyncio
async def test_rpc_tell_active_uses_user_tasks(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_active")
    client = AsyncMock()
    client.add_uri.return_value = "gid-rpc-active"
    client.tell_active.return_value = [
        {"gid": "gid-rpc-active", "status": "active", "downloadSpeed": "10"}
    ]
    await create_user_download(
        user_id=user["id"],
        quota_bytes=user["quota_bytes"],
        uri="https://example.com/rpc.bin",
        resource_key="http:rpc",
        resource_kind="http",
        display_name="rpc.bin",
        total_bytes=10,
        aria2_client=client,
    )

    handler = Aria2RpcHandler(user["id"], client)
    rows = await handler.handle("aria2.tellActive", [])
    owned = await list_user_tasks(user["id"])

    assert len(rows) == 1
    assert rows[0]["gid"] == f"task-{owned[0]['id']}"
    assert rows[0]["downloadSpeed"] == "10"


@pytest.mark.asyncio
async def test_rpc_purge_download_result_deletes_terminal_user_task(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="rpc_stopped")
    client = AsyncMock()
    handler = Aria2RpcHandler(user["id"], client)

    result = await handler.handle("aria2.purgeDownloadResult", [])

    assert result == "OK"


@pytest.mark.asyncio
async def test_rpc_add_uri_creates_v0_task_and_returns_gid(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_add_uri")
    client = AsyncMock()
    client.add_uri.return_value = "gid-rpc-add-uri"
    handler = Aria2RpcHandler(user["id"], client)

    result = await handler.handle(
        "aria2.addUri",
        [
            [
                "https://example.com/add.bin",
                "https://mirror.example.com/add.bin",
            ],
            {"out": "add.bin"},
        ],
    )

    rows = await list_user_tasks(user["id"])
    assert result == f"task-{rows[0]['id']}"
    assert len(rows) == 1
    assert rows[0]["aria2_gid"] == "gid-rpc-add-uri"
    assert rows[0]["status"] == "active"
    assert rows[0]["source_uri"] == "https://example.com/add.bin"
    client.add_uri.assert_awaited_once()
    call_args = client.add_uri.call_args
    assert call_args[0][0] == [
        "https://example.com/add.bin",
        "https://mirror.example.com/add.bin",
    ]
    opts = call_args[0][1]
    assert opts["out"] == "add.bin"
    assert opts["seed-time"] == "0"
    assert "dir" in opts


@pytest.mark.asyncio
async def test_rpc_add_uri_rejects_path_like_out_option(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_add_uri_bad_out")
    client = AsyncMock()
    client.add_uri.return_value = "gid-rpc-bad-out"
    handler = Aria2RpcHandler(user["id"], client)

    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addUri",
            [["https://example.com/bad.bin"], {"out": "../evil"}],
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert "invalid out option" in exc_info.value.message
    client.add_uri.assert_not_awaited()
    assert await list_user_tasks(user["id"]) == []


@pytest.mark.asyncio
async def test_rpc_add_uri_rejects_duplicate_completed_magnet_without_renaming(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="rpc_duplicate_magnet", quota_bytes=1000)
    store_path = get_store_path_for_hash("rpc_duplicate_magnet_hash")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"magnet")
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_path,
        content_hash="rpc_duplicate_magnet_hash",
        display_name="real-magnet-name",
        size_bytes=6,
    )
    global_download = await create_global_download_v0(
        resource_key=MAGNET_INFO_HASH,
        resource_kind="magnet",
        source_uri=f"magnet:?xt=urn:btih:{MAGNET_INFO_HASH}&dn=real-magnet-name",
        status="completed",
        display_name="real-magnet-name",
        total_bytes=6,
        completed_bytes=6,
        completed_file_id=user_file["stored_file_id"],
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=global_download["id"],
        status="completed",
        display_name="real-magnet-name",
    )
    client = AsyncMock()
    handler = Aria2RpcHandler(user["id"], client)

    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addUri",
            [[f"magnet:?xt=urn:btih:{MAGNET_INFO_HASH}&dn=wrong-name"]],
        )

    task = await get_user_task(user["id"], global_download["id"])
    assert exc_info.value.code == RpcErrorCode.TASK_EXISTS
    assert exc_info.value.message == "任务已存在"
    assert task is not None
    assert task["display_name"] == "real-magnet-name"
    client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_rpc_add_torrent_creates_v0_task_and_returns_gid(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_add_torrent")
    client = AsyncMock()
    client.add_torrent.return_value = "gid-rpc-add-torrent"
    handler = Aria2RpcHandler(user["id"], client)
    torrent_data = base64.b64encode(b"d4:infod4:name4:testee").decode()

    result = await handler.handle(
        "aria2.addTorrent",
        [torrent_data, ["https://example.com/seed"], {"out": "seed.torrent"}],
    )

    rows = await list_user_tasks(user["id"])
    assert result == f"task-{rows[0]['id']}"
    assert len(rows) == 1
    assert rows[0]["aria2_gid"] == "gid-rpc-add-torrent"
    assert rows[0]["status"] == "active"
    assert rows[0]["resource_kind"] == "torrent"
    assert str(rows[0]["source_uri"]).startswith("magnet:?xt=urn:btih:")
    client.add_torrent.assert_awaited_once()
    call_args = client.add_torrent.call_args
    assert call_args[0][0] == torrent_data
    assert call_args[0][1] == ["https://example.com/seed"]
    opts = call_args[0][2]
    assert opts["out"] == "seed.torrent"
    assert opts["seed-time"] == "0"
    assert "dir" in opts


@pytest.mark.asyncio
async def test_rpc_add_torrent_rejects_duplicate_torrent(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_duplicate_torrent")
    client = AsyncMock()
    client.add_torrent.return_value = "gid-rpc-duplicate-torrent"
    handler = Aria2RpcHandler(user["id"], client)
    torrent_data = base64.b64encode(b"d4:infod4:name9:duplicatee").decode()

    first_gid = await handler.handle("aria2.addTorrent", [torrent_data])
    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addTorrent", [torrent_data])

    assert first_gid.startswith("task-")
    assert exc_info.value.code == RpcErrorCode.TASK_EXISTS
    assert exc_info.value.message == "任务已存在"
    client.add_torrent.assert_awaited_once()
