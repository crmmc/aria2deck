from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.state import AppState
from app.services.aria2_rpc_handler import Aria2RpcHandler
from app.services.download_service import create_user_download
from tests.helpers_v0 import create_user_v0


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

    handler = Aria2RpcHandler(user["id"], client, AppState())
    rows = await handler.handle("aria2.tellActive", [])

    assert len(rows) == 1
    assert rows[0]["gid"] == "gid-rpc-active"
    assert rows[0]["downloadSpeed"] == "10"


@pytest.mark.asyncio
async def test_rpc_purge_download_result_deletes_terminal_user_task(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="rpc_stopped")
    client = AsyncMock()
    handler = Aria2RpcHandler(user["id"], client, AppState())

    result = await handler.handle("aria2.purgeDownloadResult", [])

    assert result == "OK"
