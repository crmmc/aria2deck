from __future__ import annotations

import asyncio
import json
import threading
import time
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
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.pack_writer import PackWriterControl
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

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


class ControlledWriter:
    """真实 asyncio 任务：只有测试释放后才结束，用来验证停止等待的是任务本身。"""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        await self.release.wait()


def _start_writer() -> tuple[ControlledWriter, asyncio.Task[None]]:
    writer = ControlledWriter()
    return writer, asyncio.create_task(writer.run())


async def test_pack_writer_control_stops_through_the_cooperative_event() -> None:
    control = PackWriterControl(stop_timeout_seconds=1.0)
    writer, task = _start_writer()
    assert control.register(7, task, writer.cancel_event) is True
    assert control.allows_resume(7) is False

    stopping = asyncio.create_task(control.stop_and_wait(7))
    await asyncio.sleep(0)
    assert writer.cancel_event.is_set()
    assert not stopping.done()

    writer.release.set()
    await asyncio.wait_for(stopping, timeout=1.0)
    assert task.done()
    assert task.cancelling() == 0
    assert not task.cancelled()
    assert control.allows_resume(7) is False

    await control.stop_and_wait(7)  # 任务已结束：直接成功
    assert control.allows_resume(7) is False  # 取消记录黏住到 clear
    control.clear(7)
    assert control.allows_resume(7) is True


async def test_pack_writer_control_applies_an_early_stop_to_a_later_writer() -> None:
    control = PackWriterControl(stop_timeout_seconds=1.0)
    await control.stop_and_wait(21)  # 尚无 writer：只记录取消请求
    await control.stop_and_wait(21)  # 重复取消幂等
    assert control.allows_resume(21) is False

    writer, task = _start_writer()
    assert control.register(21, task, writer.cancel_event) is False
    assert writer.cancel_event.is_set()

    first = asyncio.create_task(control.stop_and_wait(21))
    second = asyncio.create_task(control.stop_and_wait(21))
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    assert task.cancelling() == 0

    writer.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
    assert task.done() and not task.cancelled()
    assert control.allows_resume(21) is False
    control.clear(21)
    assert control.allows_resume(21) is True


async def test_pack_writer_control_shares_one_stop_wait_between_callers() -> None:
    control = PackWriterControl(stop_timeout_seconds=1.0)
    writer, task = _start_writer()
    control.register(22, task, writer.cancel_event)

    first = asyncio.create_task(control.stop_and_wait(22))
    await asyncio.sleep(0)
    shared = control._records[22].stop_wait
    assert shared is not None and not shared.done()

    second = asyncio.create_task(control.stop_and_wait(22))
    await asyncio.sleep(0)
    assert control._records[22].stop_wait is shared
    assert not first.done() and not second.done()
    assert writer.cancel_event.is_set()
    assert task.cancelling() == 0

    writer.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
    assert task.done() and not task.cancelled()


async def test_pack_writer_control_late_stop_callers_share_the_first_deadline() -> None:
    control = PackWriterControl(stop_timeout_seconds=0.4)
    writer, task = _start_writer()
    control.register(23, task, writer.cancel_event)

    started = time.monotonic()
    first = asyncio.create_task(control.stop_and_wait(23))
    await asyncio.sleep(0.3)  # 第二个调用晚进入，但仍在第一个调用的截止窗口内
    second = asyncio.create_task(control.stop_and_wait(23))
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    elapsed = time.monotonic() - started

    errors = [outcome for outcome in outcomes if isinstance(outcome, DomainError)]
    assert len(errors) == 2
    assert {error.code for error in errors} == {ErrorCode.RECOVERY_UNSAFE}
    # 自开新窗口会在 ~0.7s 才超时；共享窗口在 ~0.4s 结束，这里留 0.2s 宽松余量。
    assert elapsed < 0.6
    assert not task.done()
    writer.release.set()


async def test_pack_writer_control_retries_a_timed_out_stop_on_the_next_call() -> None:
    control = PackWriterControl(stop_timeout_seconds=0.05)
    writer, task = _start_writer()
    control.register(24, task, writer.cancel_event)

    with pytest.raises(DomainError) as caught:
        await control.stop_and_wait(24)
    assert caught.value.code is ErrorCode.RECOVERY_UNSAFE
    assert control.allows_resume(24) is False
    assert not task.done()
    assert task.cancelling() == 0

    retry = asyncio.create_task(control.stop_and_wait(24))
    await asyncio.sleep(0)
    assert not retry.done()  # 不缓存上一次超时，重新开启有上限等待
    writer.release.set()
    await asyncio.wait_for(retry, timeout=1.0)
    assert task.done() and not task.cancelled()

    await control.stop_and_wait(24)  # 第三次：任务已结束，直接成功
    assert control.allows_resume(24) is False
    control.clear(24)
    assert control.allows_resume(24) is True


async def test_pack_writer_control_keeps_the_shared_wait_when_a_caller_is_cancelled() -> None:
    control = PackWriterControl(stop_timeout_seconds=5.0)
    writer, task = _start_writer()
    control.register(25, task, writer.cancel_event)

    caller = asyncio.create_task(control.stop_and_wait(25))
    await asyncio.sleep(0)
    shared = control._records[25].stop_wait
    assert shared is not None and not shared.done()

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not shared.done()
    assert not shared.cancelled()
    assert not task.done()
    assert task.cancelling() == 0

    observer = asyncio.create_task(control.wait_for_writer(25, task))
    await asyncio.sleep(0)
    assert not observer.done()  # dispatcher finally 共享同一等待
    writer.release.set()
    await asyncio.wait_for(observer, timeout=1.0)
    assert task.done() and not task.cancelled()


async def test_pack_writer_control_observer_does_not_restart_after_a_timeout() -> None:
    control = PackWriterControl(stop_timeout_seconds=0.05)
    writer, task = _start_writer()
    control.register(26, task, writer.cancel_event)

    with pytest.raises(DomainError) as caught:
        await control.stop_and_wait(26)
    assert caught.value.code is ErrorCode.RECOVERY_UNSAFE
    timed_out = control._records[26].stop_wait
    assert timed_out is not None and timed_out.done()

    await control.wait_for_writer(26, task)  # 立即返回，不新开等待
    assert control._records[26].stop_wait is timed_out
    assert not task.done()
    assert control.allows_resume(26) is False

    writer.release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)
    assert control.allows_resume(26) is False  # 任务结束不等于取消已落库
    control.clear(26)
    assert control.allows_resume(26) is True


async def test_pack_writer_control_replacement_ignores_the_old_timed_out_wait() -> None:
    control = PackWriterControl(stop_timeout_seconds=0.05)
    old_writer, old_task = _start_writer()
    control.register(27, old_task, old_writer.cancel_event)
    with pytest.raises(DomainError):
        await control.stop_and_wait(27)

    old_writer.release.set()
    await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), timeout=1.0)

    new_writer, new_task = _start_writer()
    assert control.register(27, new_task, new_writer.cancel_event) is False
    assert new_writer.cancel_event.is_set()
    assert control._records[27].stop_wait is None

    await control.wait_for_writer(27, old_task)  # 旧 finally 不得污染替换状态
    assert control._records[27].writer is new_task
    assert control.allows_resume(27) is False
    assert not new_task.done()

    stopping = asyncio.create_task(control.stop_and_wait(27))
    await asyncio.sleep(0)
    assert not stopping.done()
    new_writer.release.set()
    await asyncio.wait_for(stopping, timeout=1.0)
    assert new_task.done() and not new_task.cancelled()


async def test_pack_writer_control_clear_refuses_an_active_writer() -> None:
    control = PackWriterControl(stop_timeout_seconds=1.0)
    control.clear(999)  # 无记录：幂等空操作
    writer, task = _start_writer()
    control.register(29, task, writer.cancel_event)

    with pytest.raises(DomainError) as register_error:
        control.register(29, asyncio.create_task(asyncio.sleep(0)), threading.Event())
    assert register_error.value.code is ErrorCode.INVALID_STATE

    with pytest.raises(DomainError) as clear_error:
        control.clear(29)
    assert clear_error.value.code is ErrorCode.INVALID_STATE
    assert control.allows_resume(29) is False

    writer.release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)
    assert control.allows_resume(29) is False  # 任务完成但取消未 clear
    control.clear(29)
    assert control.allows_resume(29) is True


async def test_pack_writer_control_allows_replacing_a_finished_writer() -> None:
    control = PackWriterControl(stop_timeout_seconds=1.0)
    finished_writer, finished = _start_writer()
    control.register(8, finished, finished_writer.cancel_event)
    finished_writer.release.set()
    await asyncio.wait_for(asyncio.gather(finished, return_exceptions=True), timeout=1.0)

    replacement_writer, replacement = _start_writer()
    assert control.register(8, replacement, replacement_writer.cancel_event) is True
    assert not replacement_writer.cancel_event.is_set()

    stopping = asyncio.create_task(control.stop_and_wait(8))
    await asyncio.sleep(0)
    assert replacement_writer.cancel_event.is_set()
    assert not stopping.done()
    replacement_writer.release.set()
    await asyncio.wait_for(stopping, timeout=1.0)
    assert replacement.done() and not replacement.cancelled()


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
    control = cleanups[1].control
    assert isinstance(control, PackWriterControl)
    # 账户清理必须拿到真实 prepared 存储，并与取消服务共用同一个实例。
    assert isinstance(cleanups[1].prepared_store, DownloadContentStore)
    assert app.state.pack_control.prepared_store is cleanups[1].prepared_store
    # 同一个控制实例同时接入调度、取消与账号清理，不能只装一半协议。
    assert app.state.pack_dispatcher.writer_control is control
    assert app.state.pack_control.stop_writer.__self__ is control
    assert app.state.pack_control.clear_writer.__self__ is control
    assert app.state.pack_control.clear_writer.__func__ is PackWriterControl.clear
    # 取消与账户清理必须由 bootstrap 注入同一把锁，不能各自新建。
    assert app.state.pack_control._lock is cleanups[1]._lock


async def test_pack_writer_control_old_observer_does_not_clear_a_new_record() -> None:
    control = PackWriterControl(stop_timeout_seconds=1.0)
    old_writer, old_task = _start_writer()
    control.register(9, old_task, old_writer.cancel_event)
    old_writer.release.set()
    await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), timeout=1.0)

    # 正常完成且未请求取消：自己的 finally 清掉自己的记录
    await control.wait_for_writer(9, old_task)
    assert control.allows_resume(9) is True

    new_writer, new_task = _start_writer()
    assert control.register(9, new_task, new_writer.cancel_event) is True
    assert not new_writer.cancel_event.is_set()

    await control.wait_for_writer(9, new_task)  # 未完成且未请求取消：不清记录
    assert control.allows_resume(9) is False

    await control.wait_for_writer(9, old_task)  # 迟到的旧 finally

    assert control.allows_resume(9) is False
    assert control._records[9].writer is new_task
    assert not new_writer.cancel_event.is_set()
    assert not new_task.done()
    new_writer.release.set()
    await asyncio.wait_for(asyncio.gather(new_task, return_exceptions=True), timeout=1.0)


async def _create_pack_job(database: SQLiteDatabase, owner_id: int, name: str) -> int:
    async with SQLiteUnitOfWork(database) as uow:
        return await PackRepository(uow.connection).create_job(
            owner_id=owner_id,
            output_name=name,
            pack_format="zip",
            source_size=20,
            reserved_bytes=30,
            physical_reserved_bytes=30,
            delete_source=False,
            now_ms=1,
        )


async def test_cleanup_clears_parts_and_control_before_a_reused_pack_id(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(database)
    try:
        root = tmp_path / "downloads"
        (root / "packs").mkdir(parents=True)
        control = PackWriterControl(stop_timeout_seconds=1.0)
        cleanup = PackUserCleanup(
            uow_factory=lambda: SQLiteUnitOfWork(database),
            repository_factory=lambda uow: PackRepository(uow.connection),
            control=control,
            prepared_store=DownloadContentStore(root),
            clock_ms=lambda: 2,
            lock=asyncio.Lock(),
        )
        async with SQLiteUnitOfWork(database) as uow:
            owner = await AccountRepository(uow.connection).create_user(
                username="owner",
                password_hash="ab" * 32,
                is_admin=True,
                quota_bytes=10_000,
                now_ms=1,
            )
        pack_id = await _create_pack_job(database, owner, "old.zip")
        old_writer, old_task = _start_writer()
        control.register(pack_id, old_task, old_writer.cancel_event)
        old_writer.release.set()
        await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), timeout=1.0)
        part = root / "packs" / f"prepared-{pack_id}.part"
        part.write_bytes(b"late")

        await cleanup.cleanup_user(owner)

        assert not part.exists()
        assert control.allows_resume(pack_id) is True
        async with SQLiteUnitOfWork(database) as uow:
            reused_by = await AccountRepository(uow.connection).create_user(
                username="reuser",
                password_hash="cd" * 32,
                is_admin=False,
                quota_bytes=10_000,
                now_ms=2,
            )
        reused = await _create_pack_job(database, reused_by, "new.zip")
        assert reused == pack_id, "SQLite 复用已删除的最大 rowid"

        new_writer, new_task = _start_writer()
        assert control.register(reused, new_task, new_writer.cancel_event) is True
        assert not new_writer.cancel_event.is_set(), "新任务不得继承旧取消事件"

        await control.wait_for_writer(reused, old_task)  # 旧 finally 不得清新记录
        assert control._records[reused].writer is new_task
        assert not new_writer.cancel_event.is_set()
        assert control.allows_resume(reused) is False

        stopping = asyncio.create_task(control.stop_and_wait(reused))
        await asyncio.sleep(0)
        assert new_writer.cancel_event.is_set(), "复用 ID 的取消必须作用到新事件"
        new_writer.release.set()
        await asyncio.wait_for(stopping, timeout=1.0)
        assert new_task.done() and not new_task.cancelled()
        control.clear(reused)
        assert control.allows_resume(reused) is True

        async with SQLiteUnitOfWork(database) as uow:
            await PackRepository(uow.connection).delete_job(reused)
        again = await _create_pack_job(database, reused_by, "third.zip")
        assert again == pack_id
        finished_writer, finished_task = _start_writer()
        assert control.register(again, finished_task, finished_writer.cancel_event) is True
        finished_writer.release.set()
        await asyncio.wait_for(
            asyncio.gather(finished_task, return_exceptions=True), timeout=1.0
        )
        await control.wait_for_writer(again, finished_task)  # 正常完成自行清记录
        assert control.allows_resume(again) is True
    finally:
        await database.close()
