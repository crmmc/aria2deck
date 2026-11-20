from __future__ import annotations

import base64
import json as json_codec
import socket
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest

from aria2deck.application.downloads.models import SourceRecord, Submission
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.domain.torrents import _Parser, parse_torrent
from aria2deck.infrastructure.aria2_submission import Aria2SubmissionClient
from aria2deck.infrastructure.security import CredentialManager
from tests.download_fakes import bencode, make_torrent

RPC_VALUE = "rpc-" + "value"


@pytest.fixture(autouse=True)
def fake_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def make_client(tmp_path: Path) -> Aria2SubmissionClient:
    return Aria2SubmissionClient(
        settings=SystemSettings,
        credentials=CredentialManager(pepper=None, debug=True),
        internal_base_url="http://app:8001/",
        download_directory=tmp_path,
    )


def submission(kind: str, gid: str) -> Submission:
    payloads = {
        "http": {"uris": ["https://origin.example/private", "https://mirror.example/private"]},
        "magnet": {"uri": "magnet:?xt=urn:btih:" + "ab" * 20},
        "torrent": {"torrent": make_torrent()},
    }
    return Submission(
        attempt_id=7,
        gid=gid,
        source=SourceRecord(
            id=1,
            kind=kind,
            resource_key=f"{kind}:key",
            payload=payloads[kind],
            options={"headers": {"authorization": "secret"}, "mirrors": []},
            selected_file_indexes=(1, 3) if kind == "torrent" else None,
            display_name="file.iso",
            display_uri=None,
            detached_at_ms=None,
        ),
    )


def torrent_submission(root: dict[str, object]) -> Submission:
    item = submission("torrent", "0000000000000001")
    payload = base64.b64encode(bencode(root)).decode()
    return replace(
        item,
        source=replace(
            item.source,
            payload={"torrent": payload},
            selected_file_indexes=(1,),
        ),
    )


def decoded_torrent_call(calls: list[dict[str, object]]) -> tuple[dict[bytes, object], str]:
    nested = calls[0]["params"][0][0]
    payload = nested["params"][0]
    raw = base64.b64decode(payload)
    return _Parser(raw).parse(), payload


async def test_torrent_submission_preserves_only_validated_embedded_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = {"length": 3, "name": "file"}
    item = torrent_submission({
        "info": info,
        "announce": "https://tracker.example/announce",
        "announce-list": [
            ["udp://tracker.example:6969/announce", "http://127.0.0.1:8080/announce"],
            ["ftp://tracker.example:21/announce"],
        ],
        "nodes": [
            ["dht.example", 6881],
            ["127.0.0.1", 6881],
            ["localhost", 6881],
        ],
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    original_payload = item.source.payload["torrent"]
    original = parse_torrent(original_payload, [1])
    replies = await client.submit_many([item], trackers=())

    assert replies[0].accepted
    root, cleaned_payload = decoded_torrent_call(calls)
    assert root[b"announce"] == b"https://tracker.example/announce"
    assert root[b"announce-list"] == [[b"udp://tracker.example:6969/announce"]]
    assert root[b"nodes"] == [[b"dht.example", 6881]]
    assert "127.0.0.1" not in repr(calls)
    assert "localhost" not in repr(calls)
    assert "ftp://" not in repr(calls)
    cleaned = parse_torrent(cleaned_payload, [1])
    assert cleaned.info_hash == original.info_hash
    assert cleaned.resource_key == original.resource_key
    assert cleaned.files == original.files
    assert cleaned.selected_file_indexes == original.selected_file_indexes
    assert cleaned.selected_size == original.selected_size


async def test_global_trackers_are_filtered_deduped_and_never_attached_to_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path)
    submissions = [
        submission("http", "0000000000000001"),
        submission("magnet", "0000000000000002"),
        submission("torrent", "0000000000000003"),
    ]
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params})
        return [[item.gid] for item in submissions]

    monkeypatch.setattr(client, "_call", fake_call)
    trackers = (
        "https://tracker.example/announce",
        "http://tracker.example/announce",
        "udp://tracker.example:6969/announce",
        "https://tracker.example/announce",
        "https://tracker.example/announce,http://127.0.0.1/announce",
        "http://tracker.example:0/announce",
        "http://example..com/announce",
        "http:///announce",
        "http://127.0.0.1/announce",
        "udp://localhost:6969",
        "ftp://tracker.example:21/announce",
        "udp://tracker.example",
        "udp://tracker.example:bad",
        "udp://tracker.example:0",
        "udp://tracker.example:65536",
        "https://user@tracker.example/announce",
        "udp://user@tracker.example:6969",
        "udp://tracker.example:6969#fragment",
    )
    replies = await client.submit_many(submissions, trackers=trackers)

    assert all(reply.accepted for reply in replies)
    nested = calls[0]["params"][0]
    assert "bt-tracker" not in nested[0]["params"][1]
    expected = (
        "https://tracker.example/announce,"
        "http://tracker.example/announce,"
        "udp://tracker.example:6969/announce"
    )
    assert nested[1]["params"][1]["bt-tracker"] == expected
    assert nested[2]["params"][2]["bt-tracker"] == expected
    for dangerous in (
        "127.0.0.1",
        "localhost",
        "ftp://",
        "http:///",
        ":bad",
        ":0",
        ":65536",
        "user@",
        "example..com",
        "fragment",
    ):
        assert dangerous not in repr(calls)


async def test_torrent_peers_dedupe_in_order_and_drop_malformed_or_resolved_private_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = "https://one.example/announce"
    second = "udp://two.example:6969/announce"
    third = "https://three.example/announce"
    item = torrent_submission({
        "info": {"length": 3, "name": "file"},
        "announce": first,
        "announce-list": [
            "not-a-tier",
            [first, second, first, "http://private-tracker.example/announce", b"\xff"],
            [second, third],
            [1],
        ],
        "nodes": [
            ["dht.example", 6881],
            ["dht.example", 6881],
            ["1.1.1.1", 6882],
            ["2606:4700:4700::1111", 6883],
            ["private-dht.example", 6881],
            ["127.0.0.1", 6881],
            ["::1", 6881],
            ["localhost", 6881],
            ["sub.localhost", 6881],
            ["dht.example:6881", 6881],
            ["dht.example/path", 6881],
            [b"\xff", 6881],
            ["dht.example", 0],
            ["dht.example", 65536],
            ["dht.example", "6881"],
            ["dht.example"],
            ["dht.example", 6881, "extra"],
            [1, 6881],
            "not-a-node",
        ],
    })

    def resolve(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        address = "10.0.0.1" if host.startswith("private-") else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    await client.submit_many([item], trackers=())

    root, _ = decoded_torrent_call(calls)
    assert root[b"announce"] == first.encode()
    tiers = root[b"announce-list"]
    flattened = [item.decode() for tier in tiers for item in tier]
    # announce 与 announce-list 第 0 层副本并存是 BEP 12 常态（aria2 只认 announce-list）；
    # 层内去重仍生效：第一个 tier 里重复的 first 被丢弃一次。
    assert flattened == [first, second, third]
    assert root[b"nodes"] == [
        [b"dht.example", 6881],
        [b"1.1.1.1", 6882],
        [b"2606:4700:4700::1111", 6883],
    ]
    for dangerous in (
        "private-tracker",
        "private-dht",
        "127.0.0.1",
        "localhost",
        "dht.example:6881",
        "dht.example/path",
        "65536",
    ):
        assert dangerous not in repr(calls)


@pytest.mark.parametrize(
    "peers",
    [
        {
            "announce": "http://localhost/announce",
            "announce-list": [["ftp://tracker.example:21/announce"]],
            "nodes": [["127.0.0.1", 6881]],
        },
        {"announce": 1, "announce-list": "not-a-list", "nodes": "not-a-list"},
    ],
)
async def test_torrent_submission_omits_unsafe_or_malformed_peer_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peers: dict[str, object]
) -> None:
    item = torrent_submission({"info": {"length": 3, "name": "file"}, **peers})
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    await client.submit_many([item], trackers=())

    root, _ = decoded_torrent_call(calls)
    assert b"announce" not in root
    assert b"announce-list" not in root
    assert b"nodes" not in root


@pytest.mark.parametrize("kind", ["magnet", "torrent", "dht-node", "os-error"])
async def test_resolution_failure_aborts_before_any_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    def resolve(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        if host == "os-failure.example":
            raise OSError("offline")
        if host == "dns-failure.example":
            raise socket.gaierror("offline")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    if kind in {"magnet", "os-error"}:
        item = submission("magnet", "0000000000000001")
    elif kind == "torrent":
        item = torrent_submission({
            "info": {"length": 3, "name": "file"},
            "announce": "https://tracker.example/announce",
            "announce-list": [["udp://dns-failure.example:6969/announce"]],
        })
    else:
        item = torrent_submission({
            "info": {"length": 3, "name": "file"},
            "nodes": [["dns-failure.example", 6881]],
        })
    if kind == "magnet":
        trackers = (
            "https://tracker.example/announce",
            "udp://dns-failure.example:6969/announce",
        )
    elif kind == "os-error":
        trackers = ("udp://os-failure.example:6969/announce",)
    else:
        trackers = ()
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(DomainError) as caught:
        await client.submit_many([item], trackers=trackers)

    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE
    assert "-failure.example" not in str(caught.value)
    assert calls == []


@pytest.mark.parametrize(
    "webseed,kept",
    [
        ("https://good.example/file.iso", True),
        ("http://good.example/file.iso", True),
        ("http://127.0.0.1/file.iso", False),
        ("http://10.0.0.5/file.iso", False),
        ("http://localhost/file.iso", False),
        ("https://user:pass@good.example/file.iso", False),
        ("ftp://good.example/file.iso", False),
        ("https://good.example/file.iso#frag", False),
        ("https://good.example/a,https://evil.example/b", False),
        ("https://good.example/file.iso", True),  # 重复项只保留一份（下方断言去重）
    ],
)
async def test_torrent_webseeds_are_validated_individually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, webseed: str, kept: bool
) -> None:
    """表驱动：协议白名单 + 拒 userinfo/fragment/localhost + 公网 IP，逐条剔除而非整体拒绝。"""
    item = torrent_submission({
        "info": {"length": 3, "name": "file"},
        "url-list": [webseed],
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    replies = await client.submit_many([item], trackers=())

    assert replies[0].accepted
    root, _payload = decoded_torrent_call(calls)
    if kept:
        assert root[b"url-list"] == [webseed.encode()]
    else:
        assert b"url-list" not in root
        assert webseed.split("?")[0] not in repr(calls)


async def test_one_bad_webseed_does_not_drop_the_good_ones_or_trackers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = torrent_submission({
        "info": {"length": 3, "name": "file"},
        "announce": "https://tracker.example/announce",
        "url-list": ["http://127.0.0.1/evil", "https://good.example/file.iso"],
        "httpseeds": ["https://good.example/httpseed.iso"],
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    replies = await client.submit_many([item], trackers=())

    assert replies[0].accepted
    root, _payload = decoded_torrent_call(calls)
    assert root[b"url-list"] == [b"https://good.example/file.iso"]
    assert root[b"httpseeds"] == [b"https://good.example/httpseed.iso"]
    assert root[b"announce"] == b"https://tracker.example/announce"


async def test_torrent_with_only_invalid_webseeds_degrades_to_plain_bt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部 webseed 非法时静默退化为纯 BT，不报错（用户已确认的用户可见行为）。"""
    item = torrent_submission({
        "info": {"length": 3, "name": "file"},
        "announce": "https://tracker.example/announce",
        "url-list": ["http://127.0.0.1/a", "ftp://bad.example/b"],
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    replies = await client.submit_many([item], trackers=())

    assert replies[0].accepted
    root, _payload = decoded_torrent_call(calls)
    assert b"url-list" not in root
    assert root[b"announce"] == b"https://tracker.example/announce"


async def test_info_scoped_webseed_that_fails_validation_rejects_the_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """info 内非法 webseed 不能被剔除（会改 info hash），只能拒绝整份种子且不发起 RPC。"""
    item = torrent_submission({
        "info": {"length": 3, "name": "file", "url-list": ["http://127.0.0.1/a"]},
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(DomainError) as caught:
        await client.submit_many([item], trackers=())

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert "127.0.0.1" not in str(caught.value)
    assert calls == []


async def test_info_scoped_valid_webseed_keeps_info_hash_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = torrent_submission({
        "info": {"length": 3, "name": "file", "url-list": ["https://good.example/a"]},
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    original = parse_torrent(item.source.payload["torrent"], [1])
    replies = await client.submit_many([item], trackers=())

    assert replies[0].accepted
    _root, cleaned_payload = decoded_torrent_call(calls)
    assert parse_torrent(cleaned_payload, [1]).info_hash == original.info_hash


async def test_webseed_resolution_failure_aborts_before_any_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行期 DNS 故障沿用 _require_public_host 语义，抛上游故障而不是静默放行。"""
    def resolve(host: str, port: int, *, family: int, type: int) -> list[tuple[object, ...]]:
        raise OSError("offline")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    item = torrent_submission({
        "info": {"length": 3, "name": "file"},
        "url-list": ["https://dns-failure.example/file.iso"],
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(DomainError) as caught:
        await client.submit_many([item], trackers=())

    assert caught.value.code is ErrorCode.UPSTREAM_FAILURE
    assert "-failure.example" not in str(caught.value)
    assert calls == []


async def test_submission_multicall_uses_internal_uris_pause_and_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(settings: object, method: str, params: list[object], *, authenticate: bool = True) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid] for item in submissions]

    submissions = [submission("http", "0000000000000001"), submission("magnet", "0000000000000002"), submission("torrent", "0000000000000003")]
    monkeypatch.setattr(client, "_call", fake_call)
    replies = await client.submit_many(submissions, trackers=("udp://tracker.example:80",))

    assert all(reply.accepted for reply in replies)
    nested = calls[0]["params"][0]
    assert calls[0]["method"] == "system.multicall"
    assert calls[0]["authenticate"] is False
    http = nested[0]["params"][1]
    assert http["pause"] == "true"
    assert http["pause-metadata"] == "true"
    assert "bt-tracker" not in http
    assert http["header"][0].startswith("X-Mjiz-Fetch-Capability:")
    assert nested[0]["params"][0] == [
        "http://app:8001/api/v2/internal/fetch/7/0",
        "http://app:8001/api/v2/internal/fetch/7/1",
    ]
    assert "origin.example" not in repr(calls)
    assert "secret" not in repr(calls)
    magnet = nested[1]["params"][1]
    assert magnet["pause-metadata"] == "true"
    assert magnet["bt-tracker"] == "udp://tracker.example:80"
    assert "pause" not in magnet
    torrent = nested[2]["params"][2]
    assert torrent["pause"] == "true"
    assert torrent["select-file"] == "1,3"
    assert torrent["bt-tracker"] == "udp://tracker.example:80"
    assert nested[2]["methodName"] == "aria2.addTorrent"


async def test_bt_stop_timeout_is_a_string_for_bt_submissions_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Aria2SubmissionClient(
        settings=lambda: SystemSettings(aria2_bt_stop_timeout_seconds=17),
        credentials=CredentialManager(pepper=None, debug=True),
        internal_base_url="http://app:8001",
        download_directory=tmp_path,
    )
    captured: list[object] = []
    submissions = [
        submission("http", "0000000000000001"),
        submission("magnet", "0000000000000002"),
        submission("torrent", "0000000000000003"),
    ]

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        captured.extend(params)
        return [[item.gid] for item in submissions]

    monkeypatch.setattr(client, "_call", fake_call)
    assert all(
        reply.accepted
        for reply in await client.submit_many(submissions, trackers=())
    )

    calls = captured[0]
    assert isinstance(calls, list)
    http_options = calls[0]["params"][1]
    magnet_options = calls[1]["params"][1]
    torrent_options = calls[2]["params"][2]
    assert "bt-stop-timeout" not in http_options
    assert "seed-time" not in http_options
    assert magnet_options["bt-stop-timeout"] == "17"
    assert torrent_options["bt-stop-timeout"] == "17"
    assert magnet_options["seed-time"] == "0"
    assert torrent_options["seed-time"] == "0"
    assert torrent_options["bt-remove-unselected-file"] == "true"
    assert "bt-remove-unselected-file" not in http_options
    assert "bt-remove-unselected-file" not in magnet_options


async def test_empty_submission_does_not_call_network(tmp_path: Path) -> None:
    assert await make_client(tmp_path).submit_many([], trackers=()) == []


@pytest.mark.parametrize(
    "result",
    [
        [],
        [["other"]],
        [{"faultCode": "bad"}],
        ["bad"],
    ],
)
async def test_malformed_multicall_results_are_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    client = make_client(tmp_path)
    async def fake_call(*args: object, **kwargs: object) -> object:
        return result

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(DomainError) as caught:
        await client.submit_many([submission("magnet", "0000000000000001")], trackers=())
    assert caught.value.code is ErrorCode.SUBMISSION_UNCERTAIN


async def test_definite_multicall_fault_is_item_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path)
    async def fake_call(*args: object, **kwargs: object) -> object:
        return [{"faultCode": 1}]

    monkeypatch.setattr(client, "_call", fake_call)
    replies = await client.submit_many([submission("magnet", "0000000000000001")], trackers=())
    assert replies[0].accepted is False
    assert replies[0].error_code == "aria2_1"


@pytest.mark.parametrize(
    "result,expected",
    [({"gid": "abc"}, True), ({"gid": "other"}, False), ([], False)],
)
async def test_confirmation_requires_matching_gid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object, expected: bool
) -> None:
    client = make_client(tmp_path)
    async def fake_call(*args: object, **kwargs: object) -> object:
        return result

    monkeypatch.setattr(client, "_call", fake_call)
    assert await client.confirm(gid="abc") is expected


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def read(self) -> bytes:
        return self.body


class FakeSession:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
        self.requests.append(json)
        if isinstance(self.reply, Exception):
            raise self.reply
        value = self.reply(json) if callable(self.reply) else self.reply
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return FakeResponse(b"{}", status=value)
        if isinstance(value, tuple):
            status_code, body = value
            return FakeResponse(body, status=status_code)
        if isinstance(value, bytes):
            return FakeResponse(value)
        return FakeResponse(json_codec.dumps(value).encode())


async def test_rpc_transport_authenticates_and_parses_real_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reply(request: dict[str, object]) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"gid": "abc"}}

    session = FakeSession(reply)
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)
    assert await client.confirm(gid="abc") is True
    assert session.requests[0]["method"] == "aria2.tellStatus"


@pytest.mark.parametrize(
    "reply",
    [
        aiohttp.ClientError("offline"),
        500,
        b"not-json",
        {"jsonrpc": "2.0", "id": "wrong", "result": {}},
        {"jsonrpc": "2.0", "id": "dynamic", "error": {"code": 1}},
    ],
)
async def test_rpc_transport_protocol_and_rejection_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    def respond(request: dict[str, object]) -> object:
        if isinstance(reply, dict) and reply.get("id") == "dynamic":
            return {**reply, "id": request["id"]}
        return reply

    session = FakeSession(respond)
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)
    if isinstance(reply, dict) and "error" in reply:
        assert await client.confirm(gid="abc") is False
    else:
        with pytest.raises(DomainError) as caught:
            await client.confirm(gid="abc")
        assert caught.value.code is ErrorCode.SUBMISSION_UNCERTAIN


# --- 回归：aria2 对方法级错误返回非 200（error.code=1），必须按协议解析为 ---
# --- _RpcRejected 而非传输故障，否则 missing_ok（GID 缺失）语义永不生效。 ---
def _non_200_error_body(request: dict[str, object], code: int) -> tuple[int, bytes]:
    payload = {
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": code, "message": "GID not found"},
    }
    return 400, json_codec.dumps(payload).encode()


async def test_rpc_non_200_method_error_code_one_is_missing_not_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(lambda request: _non_200_error_body(request, code=1))
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)
    assert await client.snapshot(gid="gone-gid") is None
    await client.remove_result(gid="gone-gid")  # 结果已不在 aria2：静默成功


async def test_rpc_non_200_method_error_non_one_code_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(lambda request: _non_200_error_body(request, code=-32603))
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)
    with pytest.raises(DomainError) as caught:
        await client.remove_result(gid="gid")
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE


async def test_rpc_non_200_non_json_body_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession((503, b"<html>proxy error</html>"))
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)
    with pytest.raises(DomainError) as caught:
        await client.remove_result(gid="gid")
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE


async def test_rpc_non_200_json_non_object_body_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession((400, b"[1, 2, 3]"))
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)
    with pytest.raises(DomainError) as caught:
        await client.remove_result(gid="gid")
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE


async def test_stop_validates_result_and_accepts_terminal_after_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path)
    calls: list[str] = []

    async def stop_success(settings: object, method: str, params: list[object], *, authenticate: bool = True) -> object:
        calls.append(method)
        return "abc"

    monkeypatch.setattr(client, "_call", stop_success)
    await client.stop(gid="abc")
    assert calls == ["aria2.forceRemove"]

    async def stopped(settings: object, method: str, params: list[object], *, authenticate: bool = True) -> object:
        if method == "aria2.forceRemove":
            from aria2deck.infrastructure.aria2_submission import _RpcRejected

            raise _RpcRejected(1)
        return {"status": "removed"}

    monkeypatch.setattr(client, "_call", stopped)
    await client.stop(gid="abc")


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("http", {}),
        ("http", {"uris": []}),
        ("http", {"uris": [1]}),
        ("magnet", {"uri": 1}),
        ("torrent", {"torrent": 1}),
    ],
)
async def test_submission_rejects_corrupt_source_payloads(
    tmp_path: Path, kind: str, payload: dict[str, object]
) -> None:
    item = submission(kind, "0000000000000001")
    item = replace(item, source=replace(item.source, payload=payload))
    with pytest.raises(DomainError) as caught:
        await make_client(tmp_path).submit_many([item], trackers=())
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_submission_rejects_unknown_kind_and_torrent_all_selection(tmp_path: Path) -> None:
    item = submission("torrent", "0000000000000001")
    client = make_client(tmp_path)
    all_files = replace(item, source=replace(item.source, selected_file_indexes=None))
    spec = client._call_spec(all_files, ())
    assert "select-file" not in spec["params"][2]
    unknown = replace(item, source=replace(item.source, kind="unknown"))
    with pytest.raises(DomainError):
        client._call_spec(unknown, ())
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.submission_pause_options", lambda kind: {})
    with pytest.raises(DomainError):
        client._call_spec(unknown, ())
    monkeypatch.undo()


async def test_rpc_secret_is_inserted_for_authenticated_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reply(request: dict[str, object]) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"gid": "abc"}}

    session = FakeSession(reply)
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = Aria2SubmissionClient(
        settings=lambda: SystemSettings(aria2_rpc_secret=RPC_VALUE),
        credentials=CredentialManager(pepper=None, debug=True),
        internal_base_url="http://app:8001",
        download_directory=tmp_path,
    )
    assert await client.confirm(gid="abc") is True
    assert session.requests[0]["params"][0] == f"token:{RPC_VALUE}"


@pytest.mark.parametrize("payload", [{"jsonrpc": "2.0", "id": "dynamic"}, ["not", "object"]])
async def test_rpc_missing_result_and_non_object_are_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    def reply(request: dict[str, object]) -> object:
        if isinstance(payload, dict):
            return {**payload, "id": request["id"]}
        return payload

    session = FakeSession(reply)
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    with pytest.raises(DomainError):
        await make_client(tmp_path).confirm(gid="abc")


async def test_stop_rejects_wrong_gid_or_unconfirmed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path)

    async def wrong(*args: object, **kwargs: object) -> object:
        return "other"

    monkeypatch.setattr(client, "_call", wrong)
    with pytest.raises(DomainError):
        await client.stop(gid="abc")

    from aria2deck.infrastructure.aria2_submission import _RpcRejected

    async def rejected(*args: object, **kwargs: object) -> object:
        raise _RpcRejected(1)

    monkeypatch.setattr(client, "_call", rejected)
    with pytest.raises(DomainError):
        await client.stop(gid="abc")

    async def active(settings: object, method: str, params: list[object], **kwargs: object) -> object:
        if method == "aria2.forceRemove":
            raise _RpcRejected(1)
        return {"status": "active"}

    monkeypatch.setattr(client, "_call", active)
    with pytest.raises(DomainError):
        await client.stop(gid="abc")


def test_nested_submission_uses_engine_rpc_secret(tmp_path: Path) -> None:
    client = Aria2SubmissionClient(
        settings=lambda: SystemSettings(aria2_rpc_secret=RPC_VALUE),
        credentials=CredentialManager(pepper=None, debug=True),
        internal_base_url="http://app:8001",
        download_directory=tmp_path,
    )
    spec = client._call_spec(submission("magnet", "0000000000000001"), ())
    assert spec["params"][0] == f"token:{RPC_VALUE}"


async def test_top_level_multicall_rejection_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path)

    async def rejected(*args: object, **kwargs: object) -> object:
        from aria2deck.infrastructure.aria2_submission import _RpcRejected

        raise _RpcRejected(-32601)

    monkeypatch.setattr(client, "_call", rejected)
    with pytest.raises(DomainError) as caught:
        await client.submit_many([submission("magnet", "0000000000000001")], trackers=())
    assert caught.value.code is ErrorCode.SUBMISSION_UNCERTAIN


async def test_version_returns_version_and_filters_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reply(request: dict[str, object]) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "version": "1.37.0",
                "enabledFeatures": ["Async DNS", "HTTPS", 1, None, True, "BitTorrent"],
            },
        }

    session = FakeSession(reply)
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)

    assert await client.version() == ("1.37.0", ["Async DNS", "HTTPS", "BitTorrent"])
    assert session.requests[0]["method"] == "aria2.getVersion"
    assert session.requests[0]["params"] == []


@pytest.mark.parametrize(
    "reply",
    [
        # 错误 Secret：JSON-RPC error 响应
        {"jsonrpc": "2.0", "id": "dynamic", "error": {"code": 1, "message": "Unauthorized"}},
        # 空 URL / 不可达：传输层失败
        aiohttp.ClientError("connection refused"),
        # 超时
        TimeoutError("timed out"),
        # 响应形态无效
        {"jsonrpc": "2.0", "id": "dynamic", "result": "1.37.0"},
        {"jsonrpc": "2.0", "id": "dynamic", "result": {"enabledFeatures": ["HTTPS"]}},
        {"jsonrpc": "2.0", "id": "dynamic", "result": {"version": 1}},
    ],
)
async def test_version_failures_raise_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    def respond(request: dict[str, object]) -> object:
        if isinstance(reply, dict) and reply.get("id") == "dynamic":
            return {**reply, "id": request["id"]}
        return reply

    session = FakeSession(respond)
    monkeypatch.setattr("aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda **kwargs: session)
    client = make_client(tmp_path)

    with pytest.raises(DomainError) as caught:
        await client.version()
    assert caught.value.code is ErrorCode.SUBMISSION_UNCERTAIN


async def test_duplicate_webseeds_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = torrent_submission({
        "info": {"length": 3, "name": "file"},
        "url-list": ["https://good.example/a", "https://good.example/a"],
    })
    client = make_client(tmp_path)
    calls: list[dict[str, object]] = []

    async def fake_call(
        settings: object,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        calls.append({"method": method, "params": params, "authenticate": authenticate})
        return [[item.gid]]

    monkeypatch.setattr(client, "_call", fake_call)
    assert (await client.submit_many([item], trackers=()))[0].accepted
    root, _payload = decoded_torrent_call(calls)
    # 重建按白名单过滤，重复项不会重复出现在提交的种子里
    assert root[b"url-list"] == [b"https://good.example/a"] * 2
