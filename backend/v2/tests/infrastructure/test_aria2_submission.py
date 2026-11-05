from __future__ import annotations

import json as json_codec
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest

from aria2deck.application.downloads.models import SourceRecord, Submission
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.aria2_submission import Aria2SubmissionClient
from aria2deck.infrastructure.security import CredentialManager

RPC_VALUE = "rpc-" + "value"


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
        "torrent": {"torrent": "cGF5bG9hZA=="},
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
