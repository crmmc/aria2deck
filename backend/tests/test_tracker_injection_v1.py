"""M18 Task 3 — tracker 注入链路测试。

双层验证：
- orchestration 单点注入：create_task/create_torrent_task 经 fake Aria2Client
  捕获 add_uri/add_torrent 实参（覆盖 adapter 白名单过滤，测到协议边界）。
- adapter 白名单：bt-tracker 作为服务端注入透传项在 _merge_user_and_server_options
  存活。
- 回归：用户自传 bt-tracker 仍被拒绝；HTTP 不注入；5000 条无截断冒烟。
"""

from __future__ import annotations

import base64

import pytest
from unittest.mock import AsyncMock, patch
from app.domain.errors import BadRequestError
from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from app.services import task_service, tracker_list_service
from tests.fakes import make_aria2_client
from tests.helpers_v0 import create_user_v0

TRACKERS = ["udp://t1.example:6969/announce", "http://t2.example/announce"]
EXPECTED_OPTION = ",".join(TRACKERS)


def _seed_cache(trackers: list[str]) -> None:
    tracker_list_service._merged = list(trackers)


@pytest.mark.asyncio
async def test_magnet_task_injects_bt_tracker(temp_db):
    user = await create_user_v0(username="inj-magnet")
    client = make_aria2_client(add_uri="gid-inj-magnet")
    _seed_cache(TRACKERS)

    with patch("app.services.task_service._get_client", return_value=client):
        payload = await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
            options=None,
        )

    assert payload["task_id"]
    client.add_uri.assert_awaited_once()
    _, opts = client.add_uri.await_args.args
    assert opts["bt-tracker"] == EXPECTED_OPTION


@pytest.mark.asyncio
async def test_magnet_task_empty_cache_no_injection(temp_db):
    user = await create_user_v0(username="inj-empty")
    client = make_aria2_client(add_uri="gid-inj-empty")

    with patch("app.services.task_service._get_client", return_value=client):
        await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
            options=None,
        )

    _, opts = client.add_uri.await_args.args
    assert "bt-tracker" not in opts


@pytest.mark.asyncio
async def test_http_task_no_injection(temp_db):
    from app.services.http_probe import ProbeResult

    user = await create_user_v0(username="inj-http")
    client = make_aria2_client(add_uri="gid-inj-http")
    _seed_cache(TRACKERS)

    probe = ProbeResult(
        success=True,
        final_url="http://example.com/file.zip",
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
        await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="http://example.com/file.zip",
            options=None,
        )

    _, opts = client.add_uri.await_args.args
    assert "bt-tracker" not in opts


def _valid_torrent_payload() -> str:
    info_dict = (
        b"d4:name4:test6:lengthi1024e12:piece lengthi16384e"
        b"6:pieces20:01234567890123456789e"
    )
    torrent = b"d8:announce26:http://tracker.example.com4:info" + info_dict + b"e"
    return base64.b64encode(torrent).decode("ascii")


@pytest.mark.asyncio
async def test_torrent_task_inherits_injection(temp_db):
    user = await create_user_v0(username="inj-torrent")
    client = make_aria2_client(add_torrent="gid-inj-torrent")
    _seed_cache(TRACKERS)

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
            torrent=_valid_torrent_payload(),
            selected_file_indexes=None,
            options=None,
        )

    assert payload["task_id"]
    client.add_torrent.assert_awaited_once()
    opts = client.add_torrent.await_args.args[-1]
    assert opts["bt-tracker"] == EXPECTED_OPTION


def test_adapter_whitelist_passes_bt_tracker():
    submit_options: dict = {}
    Aria2BackendAdapter._merge_user_and_server_options(
        submit_options, {"bt-tracker": "udp://whitelist.example/announce"}
    )
    assert submit_options["bt-tracker"] == "udp://whitelist.example/announce"


@pytest.mark.asyncio
async def test_max_list_no_truncation(temp_db):
    user = await create_user_v0(username="inj-max")
    client = make_aria2_client(add_uri="gid-inj-max")
    big = [f"udp://t{i}.example:6969/announce" for i in range(5000)]
    expected = ",".join(big)
    _seed_cache(big)

    with patch("app.services.task_service._get_client", return_value=client):
        await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
            options=None,
        )

    _, opts = client.add_uri.await_args.args
    assert opts["bt-tracker"] == expected
    assert len(opts["bt-tracker"].split(",")) == 5000


@pytest.mark.asyncio
async def test_user_supplied_bt_tracker_still_rejected(temp_db):
    user = await create_user_v0(username="inj-reject")
    with pytest.raises(BadRequestError):
        await task_service.create_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
            options={"bt-tracker": "udp://evil.example/announce"},
        )
