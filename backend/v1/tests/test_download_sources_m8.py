"""M8 Task 2 — create path persists download_sources (S) and short tid.source_uri."""

from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import download_sources, global_downloads
from app.services import task_service
from app.services.http_probe import ProbeResult
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_user_v0


def _valid_torrent_payload() -> tuple[str, str]:
    info_dict = (
        b"d4:name4:test6:lengthi1024e12:piece lengthi16384e"
        b"6:pieces20:01234567890123456789e"
    )
    torrent = b"d8:announce26:http://tracker.example.com4:info" + info_dict + b"e"
    return base64.b64encode(torrent).decode("ascii"), hashlib.sha1(info_dict).hexdigest()


def _multi_file_torrent_payload() -> str:
    def bstr(value: bytes) -> bytes:
        return str(len(value)).encode("ascii") + b":" + value

    def bint(value: int) -> bytes:
        return b"i" + str(value).encode("ascii") + b"e"

    def bdict(items):
        return b"d" + b"".join(bstr(k) + v for k, v in items) + b"e"

    def blist(values):
        return b"l" + b"".join(values) + b"e"

    info = bdict(
        [
            (b"name", bstr(b"multi")),
            (
                b"files",
                blist(
                    [
                        bdict([(b"length", bint(100)), (b"path", blist([bstr(b"a.bin")]))]),
                        bdict([(b"length", bint(200)), (b"path", blist([bstr(b"b.bin")]))]),
                        bdict([(b"length", bint(300)), (b"path", blist([bstr(b"c.bin")]))]),
                    ]
                ),
            ),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"1" * 20)),
        ]
    )
    torrent = bdict(
        [(b"announce", bstr(b"http://tracker.example.com")), (b"info", info)]
    )
    return base64.b64encode(torrent).decode("ascii")


async def _fetch_source_and_tid(tid: int) -> tuple[dict, dict]:
    async with transaction() as conn:
        gd = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == tid)
                )
            )
            .mappings()
            .one()
        )
        assert gd["source_id"] is not None
        source = (
            (
                await conn.execute(
                    select(download_sources).where(
                        download_sources.c.id == gd["source_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(source), dict(gd)


@pytest.mark.asyncio
async def test_http_create_inserts_download_source(temp_db: str) -> None:
    user = await create_user_v0(username="m8-http-src")
    client = make_aria2_client(add_uri="gid-m8-http")
    final_url = "http://example.com/file.zip"
    probe = ProbeResult(
        success=True,
        final_url=final_url,
        content_length=1024,
        filename="file.zip",
    )

    with (
        patch("app.services.task_service._get_client", return_value=client),
        patch(
            "app.services.task_service.probe_url_with_get_fallback",
            new=AsyncMock(return_value=probe),
        ),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        payload = await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=final_url,
            options={
                "header": "X-Token: abc",
                "out": "renamed.zip",
                "select-file": "1,2",  # must not be persisted on S
            },
        )

    source, gd = await _fetch_source_and_tid(int(payload["task_id"]))
    assert source["resource_kind"] == "http"
    assert source["payload_text"] == final_url
    assert gd["source_uri"] == final_url
    assert gd["source_id"] == source["id"]
    assert source["selection_json"] is None
    options = json.loads(source["options_json"] or "{}")
    assert options.get("header") == "X-Token: abc"
    assert options.get("out") == "renamed.zip"
    assert "select-file" not in options


@pytest.mark.asyncio
async def test_magnet_create_inserts_download_source(temp_db: str) -> None:
    user = await create_user_v0(username="m8-magnet-src")
    client = make_aria2_client(add_uri="gid-m8-magnet")
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    canonical = f"magnet:?xt=urn:btih:{info_hash}"

    with patch("app.services.task_service._get_client", return_value=client):
        payload = await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=canonical,
            options=None,
        )

    source, gd = await _fetch_source_and_tid(int(payload["task_id"]))
    assert source["resource_kind"] == "magnet"
    assert source["payload_text"] == canonical
    assert gd["source_uri"] == canonical
    assert gd["source_id"] == source["id"]


@pytest.mark.asyncio
async def test_torrent_full_selection_uses_short_source_uri(temp_db: str) -> None:
    user = await create_user_v0(username="m8-torrent-full")
    client = make_aria2_client(add_torrent="gid-m8-torrent-full")
    torrent_data, info_hash = _valid_torrent_payload()

    with (
        patch("app.services.task_service._get_client", return_value=client),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        payload = await task_service.create_torrent_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            torrent=torrent_data,
            selected_file_indexes=None,
            options=None,
        )

    source, gd = await _fetch_source_and_tid(int(payload["task_id"]))
    assert source["resource_kind"] == "torrent"
    assert source["payload_text"] == f"base64:{torrent_data}"
    assert gd["source_uri"] == f"torrent:{info_hash}"
    assert not str(gd["source_uri"]).startswith("base64:")
    assert gd["source_id"] == source["id"]
    assert source["selection_json"] is None

    # submit still receives real torrent payload
    client.add_torrent.assert_awaited_once()
    submitted_torrent, _, _ = client.add_torrent.await_args.args
    assert submitted_torrent == torrent_data


@pytest.mark.asyncio
async def test_torrent_partial_selection_stores_indexes_not_select_file(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="m8-torrent-partial")
    client = make_aria2_client(add_torrent="gid-m8-torrent-partial")
    torrent_data = _multi_file_torrent_payload()

    with (
        patch("app.services.task_service._get_client", return_value=client),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        payload = await task_service.create_torrent_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            torrent=torrent_data,
            selected_file_indexes=[3, 1],
            options={"out": "pack.bin", "select-file": "should-not-persist"},
        )

    source, gd = await _fetch_source_and_tid(int(payload["task_id"]))
    assert source["payload_text"].startswith("base64:")
    assert str(gd["source_uri"]).startswith("torrent:")
    selection = json.loads(source["selection_json"] or "{}")
    assert selection["version"] == 1
    assert selection["selected_file_indexes"] == [1, 3]
    options = json.loads(source["options_json"] or "{}")
    assert options.get("out") == "pack.bin"
    assert "select-file" not in options

    _, _, opts = client.add_torrent.await_args.args
    assert opts["select-file"] == "1,3"
