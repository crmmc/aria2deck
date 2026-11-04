from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from urllib.request import Request

import pytest

from aria2deck.application.downloads.cleanup import DownloadUserCleanup
from aria2deck.application.packs.cleanup import PackUserCleanup
from aria2deck.bootstrap import create_app
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.aria2 import Aria2DownloadControl
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.pack_writer import PackWriterControl

GID = "0123456789abcdef"
RPC_SECRET = "rpc-" + "test-" * 5


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def rpc_payload(method: str, *, result: object = None, error: bool = False) -> bytes:
    payload: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": f"account-cleanup:{method}",
    }
    if error:
        payload["error"] = {"code": 1, "message": "not available"}
    else:
        payload["result"] = result
    return json.dumps(payload).encode()


def install_replies(
    monkeypatch: pytest.MonkeyPatch, replies: list[bytes | OSError]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        assert timeout == 10.0
        calls.append(json.loads(request.data))
        reply = replies.pop(0)
        if isinstance(reply, OSError):
            raise reply
        return FakeResponse(reply)

    monkeypatch.setattr("aria2deck.infrastructure.aria2.urlopen", fake_urlopen)
    return calls


@pytest.mark.parametrize("secret", [None, RPC_SECRET])
async def test_aria2_stop_uses_authenticated_json_rpc(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    settings = SystemSettings(
        aria2_rpc_url="http://aria2:6800/jsonrpc", aria2_rpc_secret=secret
    )
    calls = install_replies(
        monkeypatch, [rpc_payload("aria2.forceRemove", result=GID)]
    )

    await Aria2DownloadControl(settings=lambda: settings).stop(gid=GID)

    expected_params = [GID] if secret is None else [f"token:{secret}", GID]
    assert calls == [
        {
            "jsonrpc": "2.0",
            "id": "account-cleanup:aria2.forceRemove",
            "method": "aria2.forceRemove",
            "params": expected_params,
        }
    ]


@pytest.mark.parametrize("status", ["removed", "complete", "error"])
async def test_aria2_stop_retry_accepts_an_already_terminal_download(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    calls = install_replies(
        monkeypatch,
        [
            rpc_payload("aria2.forceRemove", error=True),
            rpc_payload("aria2.tellStatus", result={"status": status}),
        ],
    )
    control = Aria2DownloadControl(settings=SystemSettings)

    await control.stop(gid=GID)

    assert [call["method"] for call in calls] == ["aria2.forceRemove", "aria2.tellStatus"]


async def test_aria2_stop_rejects_a_download_that_is_still_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_replies(
        monkeypatch,
        [
            rpc_payload("aria2.forceRemove", error=True),
            rpc_payload("aria2.tellStatus", result={"status": "active"}),
        ],
    )
    control = Aria2DownloadControl(settings=SystemSettings)

    with pytest.raises(DomainError) as caught:
        await control.stop(gid=GID)
    assert caught.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "reply",
    [
        OSError("offline"),
        b"not-json",
        json.dumps(["not", "an", "object"]).encode(),
        rpc_payload("aria2.forceRemove", result="different-gid"),
    ],
)
async def test_aria2_stop_fails_closed_on_transport_protocol_and_identity_errors(
    monkeypatch: pytest.MonkeyPatch, reply: bytes | OSError
) -> None:
    replies = [reply]
    if not (isinstance(reply, bytes) and reply == rpc_payload("aria2.forceRemove", result="different-gid")):
        replies.append(rpc_payload("aria2.tellStatus", error=True))
    install_replies(monkeypatch, replies)
    control = Aria2DownloadControl(settings=SystemSettings)

    with pytest.raises(DomainError):
        await control.stop(gid=GID)


async def test_pack_writer_control_cancels_and_waits_for_the_current_writer() -> None:
    control = PackWriterControl()
    cancelling = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise

    task = asyncio.create_task(writer())
    control.register(7, task)
    with pytest.raises(DomainError):
        control.register(7, asyncio.create_task(asyncio.sleep(0)))

    stopping = asyncio.create_task(control.stop_and_wait(7))
    await cancelling.wait()
    assert not stopping.done()
    release.set()
    await stopping
    assert task.cancelled()
    await control.stop_and_wait(7)


async def test_pack_writer_control_allows_replacing_a_finished_writer() -> None:
    control = PackWriterControl()
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    control.register(8, finished)
    replacement = asyncio.create_task(asyncio.sleep(0))
    control.register(8, replacement)

    await control.stop_and_wait(8)
    assert replacement.done()


def test_bootstrap_composes_real_external_cleanup_adapters(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            database_path=tmp_path / "v2.db",
            download_directory=tmp_path / "downloads",
            static_directory=tmp_path / "static",
            debug=True,
        )
    )
    cleanups = app.state.account_service.cleanup_ports

    assert isinstance(cleanups[0], DownloadUserCleanup)
    assert isinstance(cleanups[0].control, Aria2DownloadControl)
    assert isinstance(cleanups[1], PackUserCleanup)
    assert isinstance(cleanups[1].control, PackWriterControl)


async def test_pack_stop_does_not_unregister_a_replacement_writer() -> None:
    control = PackWriterControl()
    replacements: list[asyncio.Task[None]] = []
    finished = asyncio.create_task(asyncio.sleep(0))
    control.register(9, finished)

    def register_replacement(_task: asyncio.Task[None]) -> None:
        replacement = asyncio.create_task(asyncio.Event().wait())
        replacements.append(replacement)
        control.register(9, replacement)

    finished.add_done_callback(register_replacement)
    await control.stop_and_wait(9)
    await control.stop_and_wait(9)

    assert len(replacements) == 1
    assert replacements[0].cancelled()
