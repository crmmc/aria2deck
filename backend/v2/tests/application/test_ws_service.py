from __future__ import annotations

import asyncio
import json

from aria2deck.application.downloads.broadcast import BroadcastMessage, TaskBroadcast
from aria2deck.application.downloads.ws_service import (
    ConnectionRegistry,
    WebSocketTaskService,
)
from aria2deck.domain.accounts import CredentialKind, User
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings

SESSION = "session-raw-id"
PRINCIPAL_USER_ID = 11


def _user() -> User:
    return User(
        id=PRINCIPAL_USER_ID,
        username="ws-user",
        username_key="ws-user",
        password_hash="x",  # noqa: S106  # 测试假数据，非真实凭证
        is_admin=False,
        quota_bytes=100,
        is_initial_password=False,
        pending_delete=False,
        credential_generation=1,
        created_at_ms=1,
        updated_at_ms=1,
    )


class FakeAuthenticator:
    def __init__(self) -> None:
        self.valid = True
        self.calls = 0

    async def authenticate_session(self, raw_session_id: str) -> object:
        self.calls += 1
        if not self.valid or raw_session_id != SESSION:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "会话无效")
        from aria2deck.application.accounts.models import Principal

        return Principal(_user(), CredentialKind.SESSION, session_id="digest")


class FakeSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed: int | None = None
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []
        self.send_error: Exception | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        return await self.inbox.get()

    async def send_text(self, message: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def push(self, text: str) -> None:
        await self.inbox.put(text)


def _service(
    *,
    broadcast: TaskBroadcast | None = None,
    authenticator: FakeAuthenticator | None = None,
    allowed_origins: frozenset[str] = frozenset(),
    registry: ConnectionRegistry | None = None,
    sleep=None,
) -> tuple[WebSocketTaskService, TaskBroadcast, FakeAuthenticator]:
    bus = broadcast or TaskBroadcast()
    auth = authenticator or FakeAuthenticator()
    service = WebSocketTaskService(
        authenticator=auth,
        broadcast=bus,
        registry=registry,
        allowed_origins=allowed_origins,
        sleep=sleep or asyncio.sleep,
        idle_timeout_seconds=0.2,
        revalidate_interval_seconds=0.05,
    )
    return service, bus, auth


def _handle(service, socket, *, session=SESSION, origin=None, ip="1.2.3.4"):
    return service.handle(socket, session_id=session, origin=origin, ip=ip)


async def test_unauthenticated_and_bad_origin_close_before_accept() -> None:
    service, _, _ = _service()
    no_cookie = FakeSocket()
    await _handle(service, no_cookie, session=None)
    assert no_cookie.closed == 4401 and not no_cookie.accepted

    service, _, _ = _service(allowed_origins=frozenset({"https://app.example"}))
    bad_origin = FakeSocket()
    await _handle(service, bad_origin, origin="https://evil.example")
    assert bad_origin.closed == 4403 and not bad_origin.accepted

    good_origin = FakeSocket()
    handle = asyncio.create_task(
        _handle(service, good_origin, origin="https://app.example")
    )
    await asyncio.wait_for(good_origin.inbox.put("ping"), timeout=1)
    await asyncio.wait_for(handle, timeout=1)
    assert good_origin.accepted
    assert good_origin.sent[0] == "pong"


async def test_connection_quotas_per_user_and_ip() -> None:
    registry = ConnectionRegistry()
    sockets = []
    for _ in range(5):
        service, _, _ = _service(registry=registry)
        socket = FakeSocket()
        task = asyncio.create_task(_handle(service, socket))
        for _ in range(100):
            if socket.accepted:
                break
            await asyncio.sleep(0.01)
        assert socket.accepted, "连接必须先完成接受"
        sockets.append((socket, task))
    # 第 6 条同用户连接被拒。
    service, _, _ = _service(registry=registry)
    sixth = FakeSocket()
    await _handle(service, sixth)
    assert sixth.closed == 4429 and not sixth.accepted

    for _socket, task in sockets:
        await asyncio.sleep(0.25)  # 空闲超时自然关闭。
        await asyncio.wait_for(task, timeout=2)
    assert registry._per_user == {}

    # 每 IP 50 条限额（不同用户）。
    for index in range(50):
        assert registry.try_acquire(1000 + index, "9.9.9.9") is True
    assert registry.try_acquire(2000, "9.9.9.9") is False
    registry.release(1000, "9.9.9.9")
    assert registry.try_acquire(2000, "9.9.9.9") is True


async def test_heartbeat_bad_messages_and_idle_timeout() -> None:
    service, _, _ = _service()
    socket = FakeSocket()
    handle = asyncio.create_task(_handle(service, socket))
    await asyncio.sleep(0.02)
    await socket.inbox.put("garbage-not-ping")  # 坏消息忽略。
    await socket.inbox.put("  ping  ")
    await asyncio.sleep(0.05)
    assert "pong" in socket.sent
    # 空闲超过 idle_timeout → 连接关闭。
    await asyncio.wait_for(handle, timeout=1)
    assert socket.closed is not None


async def test_send_failure_closes_1011() -> None:
    service, bus, _ = _service()
    socket = FakeSocket()
    socket.send_error = RuntimeError("connection reset")
    handle = asyncio.create_task(_handle(service, socket))
    await asyncio.sleep(0.02)
    bus.publish([PRINCIPAL_USER_ID], BroadcastMessage("task_update", 1, "active", {"id": 1}))
    await asyncio.wait_for(handle, timeout=1)
    assert socket.closed == 1011


async def test_push_reaches_subscriber_and_filters_others() -> None:
    service, bus, _ = _service()
    socket = FakeSocket()
    handle = asyncio.create_task(_handle(service, socket))
    await asyncio.sleep(0.02)
    bus.publish([PRINCIPAL_USER_ID], BroadcastMessage("task_update", 5, "active", {"id": 5, "name": "n"}))
    bus.publish([999], BroadcastMessage("task_update", 6, "active", {"id": 6}))
    await asyncio.sleep(0.05)
    frames = [json.loads(item) for item in socket.sent if item.startswith("{")]
    assert len(frames) == 1
    assert frames[0]["type"] == "task_update"
    assert frames[0]["task"]["id"] == 5
    await socket.inbox.put("ping")
    await asyncio.wait_for(handle, timeout=1)


async def test_revalidation_closes_on_revocation() -> None:
    service, _, auth = _service()
    socket = FakeSocket()
    handle = asyncio.create_task(_handle(service, socket))
    await asyncio.sleep(0.02)
    auth.valid = False  # 登出/改密/删用户都会让会话失效。
    await asyncio.wait_for(handle, timeout=1)
    assert socket.closed == 4401
    assert auth.calls >= 2, "连接后仍周期性复验"


async def test_origin_same_host_allowed_and_proxy_header() -> None:
    """P1-2：同源默认放行 + 代理协议头推导 + 归一化比对（容忍尾斜杠/默认端口）。"""
    from aria2deck.application.downloads.ws_service import origin_allowed

    # 同源：Host 与 Origin 推导一致。
    assert origin_allowed(
        "http://localhost:8001", host="localhost:8001", forwarded_proto=None,
        allowed_origins=frozenset(),
    )
    # https 反代：x-forwarded-proto=https，Host 不带端口 → 默认 443。
    assert origin_allowed(
        "https://app.example", host="app.example", forwarded_proto="https",
        allowed_origins=frozenset(),
    )
    assert origin_allowed(
        "https://app.example:443", host="app.example", forwarded_proto="https",
        allowed_origins=frozenset(),
    ) is True or origin_allowed(
        "https://app.example", host="app.example:443", forwarded_proto="https",
        allowed_origins=frozenset(),
    )
    # 尾斜杠容忍。
    assert origin_allowed(
        "http://localhost:8001/", host="localhost:8001", forwarded_proto=None,
        allowed_origins=frozenset(),
    )
    # 异源拒绝。
    assert not origin_allowed(
        "https://evil.example", host="localhost:8001", forwarded_proto=None,
        allowed_origins=frozenset(),
    )
    # 无 Origin（非浏览器客户端）放行。
    assert origin_allowed(
        None, host=None, forwarded_proto=None, allowed_origins=frozenset(),
    )
    # 白名单精确匹配仍有效。
    assert origin_allowed(
        "https://ariang.example", host="localhost:8001", forwarded_proto=None,
        allowed_origins=frozenset({"https://ariang.example"}),
    )
    assert not origin_allowed(
        "https://ariang.evil", host="localhost:8001", forwarded_proto=None,
        allowed_origins=frozenset({"https://ariang.example"}),
    )


async def test_same_origin_connection_accepted() -> None:
    service, _, _ = _service()
    socket = FakeSocket()
    handle = asyncio.create_task(
        service.handle(
            socket, session_id=SESSION, origin="http://1.2.3.4", ip="1.2.3.4", host="1.2.3.4"
        )
    )
    await asyncio.wait_for(socket.inbox.put("ping"), timeout=1)
    await asyncio.wait_for(handle, timeout=1)
    assert socket.accepted
    assert "pong" in socket.sent


async def test_revocation_closes_connection_immediately() -> None:
    """P2-7：撤销到达立即关闭（不等 30s 复验）。"""
    from aria2deck.application.ports import SessionRevocation

    service, _, _ = _service()
    socket = FakeSocket()
    handle = asyncio.create_task(_handle(service, socket))
    await asyncio.sleep(0.05)
    service.on_revocation(SessionRevocation(user_id=PRINCIPAL_USER_ID))
    await asyncio.wait_for(handle, timeout=1)
    assert socket.closed == 4401


async def test_shutdown_closes_all_connections() -> None:
    service, _, _ = _service()
    sockets = []
    for _ in range(2):
        socket = FakeSocket()
        task = asyncio.create_task(_handle(service, socket))
        for _ in range(100):
            if socket.accepted:
                break
            await asyncio.sleep(0.01)
        sockets.append((socket, task))
    await service.shutdown()
    for socket, task in sockets:
        await asyncio.wait_for(task, timeout=2)
        assert socket.closed is not None


async def test_lifecycle_broadcast_to_ws_end_to_end(tmp_path):
    """P2-8：真实 lifecycle→broadcast→订阅者 端到端推送（状态转换即帧到达）。"""

    from aria2deck.application.downloads.broadcast import TaskBroadcast
    from aria2deck.application.downloads.lifecycle import DownloadLifecycleService
    from aria2deck.application.downloads.projection import TaskProjector
    from aria2deck.application.downloads.queries import DownloadQueryService
    from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
    from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
    from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository

    from .test_download_lifecycle import GID, observation, runtime

    async with runtime(tmp_path, total=3) as rt:
        bus = TaskBroadcast()
        sink = bus.subscribe(rt.user_id)

        from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

        def uow_factory() -> SQLiteUnitOfWork:
            return SQLiteUnitOfWork(rt.database)


        projector = TaskProjector(observations=rt.observations, clock_ms=lambda: 100)
        queries = DownloadQueryService(
            uow_factory=uow_factory,
            repository_factory=lambda uow: DownloadRepository(uow.connection),
            account_repository_factory=lambda uow: AccountRepository(uow.connection),
            projector=projector,
            machine_space=rt.store,
            settings=lambda: SystemSettings(min_free_disk=0),
        )
        lifecycle = DownloadLifecycleService(
            uow_factory=uow_factory,
            repository_factory=lambda uow: DownloadRepository(uow.connection),
            content_repository_factory=lambda uow: ContentRepository(uow.connection),
            backend=rt.backend,
            content_store=rt.store,
            observations=rt.observations,
            connectivity=rt.connectivity,
            clock_ms=lambda: 100,
            broadcast=bus,
            queries=queries,
        )
        await lifecycle.reconcile(observation(gid=GID, status="error"))
        message = await asyncio.wait_for(sink.next(), timeout=2)
        assert message.type == "task_update"
        assert message.task_id == rt.task_id
        # 终态帧：error 快照 → 订阅 failed（RPC 投影映射为 error，REST 状态为 failed）。
        assert message.status in {"failed", "error"}
