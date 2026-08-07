from __future__ import annotations

import base64
import logging
from unittest.mock import AsyncMock

import pytest

from app.core.config import get_internal_base_url
from app.repositories.downloads import get_user_task, list_user_tasks
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode
from app.services.download_service import (
    DOWNLOAD_SUBMISSION_FAILED_MESSAGE,
    create_user_download,
)
from app.services.internal_fetch import CAPABILITY_HEADER, verify_capability
from app.services.storage import get_store_path_for_hash
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
)


MAGNET_INFO_HASH = "0123456789abcdef0123456789abcdef01234567"


def _bencode_bytes(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def _valid_rpc_torrent(*, extra: tuple[tuple[bytes, bytes], ...] = ()) -> str:
    info = b"d6:lengthi4e4:name4:teste"
    entries = ((b"info", info), *extra)
    raw = b"d" + b"".join(_bencode_bytes(key) + value for key, value in entries) + b"e"
    return base64.b64encode(raw).decode("ascii")


@pytest.mark.asyncio
async def test_rpc_tell_active_uses_user_tasks(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_active")
    client = make_aria2_client(
        add_uri="gid-rpc-active",
        tell_active=[{"gid": "gid-rpc-active", "status": "active", "downloadSpeed": "10"}],
    )
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
    client = make_aria2_client()
    handler = Aria2RpcHandler(user["id"], client)

    result = await handler.handle("aria2.purgeDownloadResult", [])

    assert result == "OK"


@pytest.mark.asyncio
async def test_rpc_add_uri_creates_v0_task_and_returns_gid(
    temp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    user = await create_user_v0(username="rpc_add_uri")
    client = make_aria2_client(
        add_uri="gid-rpc-add-uri",
        tell_status={
            "gid": "gid-rpc-add-uri",
            "status": "paused",
            "totalLength": "128",
            "completedLength": "0",
            "files": [{"length": "128", "selected": "true"}],
        },
    )
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
    uris, opts = client.add_uri.await_args.args
    assert uris == [
        f"{get_internal_base_url()}/_internal/fetch/{rows[0]['global_download_id']}/0",
        f"{get_internal_base_url()}/_internal/fetch/{rows[0]['global_download_id']}/1",
    ]
    assert all("example.com" not in uri for uri in uris)
    assert opts["out"] == "payload"
    assert opts["seed-time"] == "0"
    assert opts["pause"] == "true"
    assert opts["split"] == "1"
    assert opts["max-connection-per-server"] == "1"
    header_name, capability = opts["header"][0].split(": ", 1)
    assert header_name == CAPABILITY_HEADER
    verified = verify_capability(
        capability,
        int(rows[0]["global_download_id"]),
        "https://example.com/add.bin",
    )
    assert verified.headers == ()
    assert verified.mirrors == ("https://mirror.example.com/add.bin",)
    assert "dir" in opts
    client.unpause.assert_awaited_once_with("gid-rpc-add-uri")


@pytest.mark.asyncio
async def test_rpc_add_uri_canonicalizes_magnet_before_submit(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_magnet_canonical")
    client = make_aria2_client(add_uri="gid-rpc-magnet")
    handler = Aria2RpcHandler(user["id"], client)
    canonical_uri = f"magnet:?xt=urn:btih:{MAGNET_INFO_HASH}"
    markers = (
        "rpc-tracker-secret.example",
        "rpc-webseed-secret.example",
        "rpc-acceptable-secret.example",
        "rpc-source-secret.example",
    )
    magnet_uri = (
        f"magnet:?xt=urn:btih:{MAGNET_INFO_HASH.upper()}"
        f"&tr=https://{markers[0]}/announce&ws=https://{markers[1]}/payload"
        f"&as=https://{markers[2]}/payload&xs=https://{markers[3]}/metadata"
    )

    await handler.handle("aria2.addUri", [[magnet_uri]])

    rows = await list_user_tasks(user["id"])
    submitted_uris = client.add_uri.await_args.args[0]
    assert submitted_uris == [canonical_uri]
    assert rows[0]["source_uri"] == canonical_uri
    assert all(marker not in repr(client.add_uri.await_args) for marker in markers)


@pytest.mark.asyncio
async def test_rpc_http_auth_contexts_do_not_share_global_download(
    temp_db: str,
) -> None:
    user_a = await create_user_v0(username="rpc_auth_a")
    user_b = await create_user_v0(username="rpc_auth_b")
    client = make_aria2_client(
        add_uri=["gid-rpc-auth-a", "gid-rpc-auth-b"],
        tell_status=[
            {
                "status": "paused",
                "totalLength": "8",
                "completedLength": "0",
                "files": [{"selected": "true", "length": "8"}],
            },
            {
                "status": "paused",
                "totalLength": "8",
                "completedLength": "0",
                "files": [{"selected": "true", "length": "8"}],
            },
        ],
    )
    uri = "https://example.com/protected.bin"

    await Aria2RpcHandler(user_a["id"], client).handle(
        "aria2.addUri",
        [[uri], {"header": "X-Api-Key: rpc-user-a-secret"}],
    )
    await Aria2RpcHandler(user_b["id"], client).handle(
        "aria2.addUri",
        [[uri], {}],
    )

    rows_a = await list_user_tasks(user_a["id"])
    rows_b = await list_user_tasks(user_b["id"])
    assert len(rows_a) == len(rows_b) == 1
    assert rows_a[0]["global_download_id"] != rows_b[0]["global_download_id"]
    assert rows_a[0]["resource_key"] != rows_b[0]["resource_key"]
    assert "rpc-user-a-secret" not in rows_a[0]["resource_key"]
    assert client.add_uri.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "http://127.0.0.1/private",
        "https://user:password@example.com/file",
        "ftp://example.com/file",
        "custom:data",
    ],
)
async def test_rpc_add_uri_rejects_unsafe_primary_before_database_write(
    temp_db: str,
    uri: str,
) -> None:
    user = await create_user_v0(username=f"rpc_unsafe_{len(uri)}")
    client = make_aria2_client()
    handler = Aria2RpcHandler(user["id"], client)

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addUri", [[uri], {}])

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert await list_user_tasks(user["id"]) == []
    client.add_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_multicall_add_uri_failure_redacts_response_task_and_logs(
    temp_db: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "fake-capability.multicall-add-uri-secret"
    user = await create_user_v0(username="rpc_multicall_submit_failure")
    client = make_aria2_client(add_uri=RuntimeError(secret))
    handler = Aria2RpcHandler(user["id"], client)

    with caplog.at_level(logging.WARNING):
        result = await handler.handle(
            "system.multicall",
            [
                [
                    {
                        "methodName": "aria2.addUri",
                        "params": [["https://example.com/multicall.bin"]],
                    }
                ]
            ],
        )

    rows = await list_user_tasks(user["id"])
    assert result == [
        {
            "faultCode": RpcErrorCode.INTERNAL_ERROR,
            "faultString": "Internal error",
        }
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_message"] == DOWNLOAD_SUBMISSION_FAILED_MESSAGE
    assert secret not in repr(result)
    assert secret not in repr(rows[0])
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_rpc_add_uri_rejects_path_like_out_option(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_add_uri_bad_out")
    client = make_aria2_client(add_uri="gid-rpc-bad-out")
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
    client = make_aria2_client()
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
    client = make_aria2_client(add_torrent="gid-rpc-add-torrent")
    handler = Aria2RpcHandler(user["id"], client)
    torrent_data = _valid_rpc_torrent()

    result = await handler.handle(
        "aria2.addTorrent",
        [torrent_data, [], {"out": "seed.torrent"}],
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
    assert call_args[0][1] == []
    opts = call_args[0][2]
    assert opts["out"] == "seed.torrent"
    assert opts["seed-time"] == "0"
    assert "dir" in opts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "webseed",
    ["http://127.0.0.1/private", "https://seed.example.com/payload"],
)
async def test_rpc_add_torrent_rejects_caller_webseed_before_submit(
    temp_db: str,
    webseed: str,
) -> None:
    user = await create_user_v0(username=f"rpc_webseed_{len(webseed)}")
    client = make_aria2_client()
    handler = Aria2RpcHandler(user["id"], client)

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addTorrent", [_valid_rpc_torrent(), [webseed]])

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    client.add_torrent.assert_not_awaited()
    assert await list_user_tasks(user["id"]) == []


@pytest.mark.asyncio
async def test_rpc_add_torrent_rejects_bt_tracker_before_submit(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_bt_tracker")
    client = make_aria2_client()
    handler = Aria2RpcHandler(user["id"], client)

    with pytest.raises(RpcError) as exc_info:
        await handler.handle(
            "aria2.addTorrent",
            [_valid_rpc_torrent(), [], {"bt-tracker": "http://127.0.0.1/announce"}],
        )

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    client.add_torrent.assert_not_awaited()
    assert await list_user_tasks(user["id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [b"url-list", b"httpseeds"])
async def test_rpc_add_torrent_rejects_embedded_webseed_before_submit(
    temp_db: str,
    key: bytes,
) -> None:
    user = await create_user_v0(username=f"rpc_embedded_{key.decode()}")
    client = make_aria2_client()
    handler = Aria2RpcHandler(user["id"], client)
    torrent = _valid_rpc_torrent(
        extra=((key, _bencode_bytes(b"http://127.0.0.1/private")),)
    )

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addTorrent", [torrent])

    assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS
    assert "webseeds" in exc_info.value.message
    client.add_torrent.assert_not_awaited()
    assert await list_user_tasks(user["id"]) == []


@pytest.mark.asyncio
async def test_rpc_add_torrent_rejects_duplicate_torrent(temp_db: str) -> None:
    user = await create_user_v0(username="rpc_duplicate_torrent")
    client = make_aria2_client(add_torrent="gid-rpc-duplicate-torrent")
    handler = Aria2RpcHandler(user["id"], client)
    torrent_data = _valid_rpc_torrent()

    first_gid = await handler.handle("aria2.addTorrent", [torrent_data])
    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addTorrent", [torrent_data])

    assert first_gid.startswith("task-")
    assert exc_info.value.code == RpcErrorCode.TASK_EXISTS
    assert exc_info.value.message == "任务已存在"
    client.add_torrent.assert_awaited_once()
