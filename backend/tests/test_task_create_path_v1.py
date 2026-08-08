"""Task C1 — create path switches to Task Core register + submit.

Covers:
- ``task_service.create_task`` (magnet and HTTP) goes through
  ``register_and_submit`` and produces pid/tid.
- ``task_service.create_torrent_task`` submits torrent data via
  ``base64:`` source_uri so the adapter calls ``add_torrent``.
- AST guard: ``task_service`` module no longer references
  ``create_user_download`` or ``create_user_torrent_download``.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services import task_service
from app.services.hash import get_uri_hash
from app.services.http_probe import ProbeResult
from app.services.internal_fetch import (
    http_resource_identity,
    source_request_options,
)
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_user_v0


# --------------------------------------------------------------------------- #
# AST guard                                                                   #
# --------------------------------------------------------------------------- #


def test_task_service_does_not_call_legacy_create_functions() -> None:
    """task_service.py must not reference create_user_download / create_user_torrent_download."""
    source = Path(task_service.__file__).read_text()
    tree = ast.parse(source)
    forbidden = {"create_user_download", "create_user_torrent_download"}
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.add(node.attr)

    assert not found, f"task_service still references legacy functions: {found}"


# --------------------------------------------------------------------------- #
# create_task (magnet)                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_task_magnet_registers_and_submits(temp_db: str) -> None:
    user = await create_user_v0(username="magnet-user")
    client = make_aria2_client(add_uri="gid-magnet-create")

    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    canonical = f"magnet:?xt=urn:btih:{info_hash}"
    magnet_with_extras = (
        f"magnet:?xt=urn:btih:{info_hash}"
        "&tr=http://tracker.example.com/announce"
        "&dn=should-be-stripped"
    )

    with patch("app.services.task_service._get_client", return_value=client):
        payload = await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=magnet_with_extras,
            options=None,
        )

    assert payload["uri"] == canonical
    assert payload["status"] in {"active", "waiting"}
    assert payload["id"]
    assert payload["task_id"]

    client.add_uri.assert_awaited_once()
    uris, opts = client.add_uri.await_args.args
    assert uris == [canonical]
    assert opts["pause-metadata"] == "true"
    assert opts["seed-time"] == "0"
    assert "dir" in opts


# --------------------------------------------------------------------------- #
# create_task (HTTP)                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_task_http_registers_and_submits(temp_db: str) -> None:
    user = await create_user_v0(username="http-user")
    client = make_aria2_client(add_uri="gid-http-create")

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
            options=None,
        )

    assert payload["uri"] == final_url
    assert payload["name"] == "file.zip"
    assert payload["status"] == "active"
    assert payload["total_length"] == 1024
    assert payload["id"]
    assert payload["task_id"]

    client.add_uri.assert_awaited_once()
    uris, opts = client.add_uri.await_args.args
    assert uris[0].endswith(f"/_internal/fetch/{payload['task_id']}/0")
    assert opts["seed-time"] == "0"
    assert opts["out"] == "payload"


# --------------------------------------------------------------------------- #
# create_torrent_task                                                         #
# --------------------------------------------------------------------------- #


def _valid_torrent_payload() -> tuple[str, str]:
    info_dict = b"d4:name4:test6:lengthi1024e12:piece lengthi16384e6:pieces20:01234567890123456789e"
    torrent = b"d8:announce26:http://tracker.example.com4:info" + info_dict + b"e"
    return base64.b64encode(torrent).decode("ascii"), hashlib.sha1(info_dict).hexdigest()


@pytest.mark.asyncio
async def test_create_torrent_task_submits_base64_torrent(temp_db: str) -> None:
    user = await create_user_v0(username="torrent-user")
    client = make_aria2_client(add_torrent="gid-torrent-create")
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

    assert payload["uri"] == f"magnet:?xt=urn:btih:{info_hash}"
    assert payload["status"] == "active"
    assert payload["total_length"] == 1024
    assert payload["frozen_space"] == 1024
    assert payload["id"]
    assert payload["task_id"]

    client.add_torrent.assert_awaited_once()
    submitted_torrent, submitted_uris, opts = client.add_torrent.await_args.args
    assert submitted_torrent == torrent_data
    assert submitted_uris == []
    assert opts["seed-time"] == "0"
    assert "dir" in opts


@pytest.mark.asyncio
async def test_create_torrent_task_partial_selection_sets_select_file(
    temp_db: str,
) -> None:
    def bstr(value: bytes) -> bytes:
        return str(len(value)).encode("ascii") + b":" + value

    def bint(value: int) -> bytes:
        return b"i" + str(value).encode("ascii") + b"e"

    def bdict(items):
        return b"d" + b"".join(bstr(k) + v for k, v in items) + b"e"

    def blist(values):
        return b"l" + b"".join(values) + b"e"

    info = bdict([
        (b"name", bstr(b"multi")),
        (b"files", blist([
            bdict([(b"length", bint(100)), (b"path", blist([bstr(b"a.bin")]))]),
            bdict([(b"length", bint(200)), (b"path", blist([bstr(b"b.bin")]))]),
            bdict([(b"length", bint(300)), (b"path", blist([bstr(b"c.bin")]))]),
        ])),
        (b"piece length", bint(16384)),
        (b"pieces", bstr(b"1" * 20)),
    ])
    torrent = bdict([(b"announce", bstr(b"http://tracker.example.com")), (b"info", info)])
    torrent_data = base64.b64encode(torrent).decode("ascii")

    user = await create_user_v0(username="torrent-partial")
    client = make_aria2_client(add_torrent="gid-torrent-partial")

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
            options=None,
        )

    assert payload["total_length"] == 400
    assert payload["frozen_space"] == 400

    _, _, opts = client.add_torrent.await_args.args
    assert opts["select-file"] == "1,3"
