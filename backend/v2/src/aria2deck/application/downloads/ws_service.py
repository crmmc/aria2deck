from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urlsplit

from ...domain.errors import DomainError, ErrorCode
from ..accounts.models import Principal
from ..ports import SessionRevocation
from .broadcast import BroadcastMessage, BroadcastSink, TaskBroadcast

logger = logging.getLogger(__name__)

MAX_CONNECTIONS_PER_USER = 5
MAX_CONNECTIONS_PER_IP = 50
IDLE_TIMEOUT_SECONDS = 90.0
REVALIDATE_INTERVAL_SECONDS = 30.0
WS_UNAUTHENTICATED = 4401
WS_FORBIDDEN_ORIGIN = 4403
WS_NOT_FOUND = 4404
WS_RESYNC_REQUIRED = 4408
WS_TOO_MANY_CONNECTIONS = 4429
WS_INTERNAL_ERROR = 1011
WS_GOING_AWAY = 1001

class SessionAuthenticator(Protocol):
    async def authenticate_session(self, raw_session_id: str) -> Principal: ...


class WebSocketLike(Protocol):
    async def accept(self) -> None: ...

    async def receive_text(self) -> str: ...

    async def send_text(self, message: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class _FirstDone(Exception):
    def __init__(self, tasks: set[asyncio.Task[int]]) -> None:
        super().__init__()
        self.tasks = tasks


def _origin_key(origin: str) -> tuple[str, str, int] | None:
    """Normalize an origin URL to (scheme, host, port); None when unparseable."""
    try:
        parsed = urlsplit(origin.strip().rstrip("/"))
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def origin_allowed(
    origin: str | None,
    *,
    host: str | None,
    forwarded_proto: str | None,
    allowed_origins: frozenset[str],
) -> bool:
    """无 Origin（非浏览器客户端）放行；同源（按 Host + 代理协议头推导）放行；白名单精确匹配。"""
    if origin is None:
        return True
    origin_key = _origin_key(origin)
    if origin_key is None:
        return False
    if host:
        scheme = (forwarded_proto or "http").strip().lower()
        host_key = _origin_key(f"{scheme}://{host}")
        if host_key is not None and host_key[:2] == origin_key[:2] and (
            host_key[2] == origin_key[2]
            or _is_default_port(origin_key)
            and _is_default_port(host_key)
        ):
            return True
    for allowed in allowed_origins:
        key = _origin_key(allowed)
        if key == origin_key:
            return True
    return False


def _is_default_port(key: tuple[str, str, int]) -> bool:
    return (key[0] == "https" and key[2] == 443) or (key[0] == "http" and key[2] == 80)


def _dump(message: BroadcastMessage) -> str:
    import json

    return json.dumps(
        {"type": message.type, "task": message.payload}, ensure_ascii=False
    )


class ConnectionRegistry:
    """进程内连接限额：每用户 5、每 IP 50（单实例内存权威）。

    shortcut: 多实例部署时需外移为共享计数，与读租约同一升级点。
    """

    def __init__(
        self,
        *,
        max_per_user: int = MAX_CONNECTIONS_PER_USER,
        max_per_ip: int = MAX_CONNECTIONS_PER_IP,
    ) -> None:
        self.max_per_user = max_per_user
        self.max_per_ip = max_per_ip
        self._per_user: dict[int, int] = {}
        self._per_ip: dict[str, int] = {}

    def try_acquire(self, user_id: int, ip: str) -> bool:
        if self._per_user.get(user_id, 0) >= self.max_per_user:
            return False
        if self._per_ip.get(ip, 0) >= self.max_per_ip:
            return False
        self._per_user[user_id] = self._per_user.get(user_id, 0) + 1
        self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        return True

    def release(self, user_id: int, ip: str) -> None:
        if user_id in self._per_user:
            self._per_user[user_id] -= 1
            if self._per_user[user_id] <= 0:
                del self._per_user[user_id]
        if ip in self._per_ip:
            self._per_ip[ip] -= 1
            if self._per_ip[ip] <= 0:
                del self._per_ip[ip]


class WebSocketTaskService:
    """Cookie-authenticated task stream: heartbeat, quota, revalidation, push."""

    def __init__(
        self,
        *,
        authenticator: SessionAuthenticator,
        broadcast: TaskBroadcast,
        registry: ConnectionRegistry | None = None,
        allowed_origins: frozenset[str] = frozenset(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS,
        revalidate_interval_seconds: float = REVALIDATE_INTERVAL_SECONDS,
    ) -> None:
        self.authenticator = authenticator
        self.broadcast = broadcast
        self.registry = registry or ConnectionRegistry()
        self.allowed_origins = allowed_origins
        self.sleep = sleep
        self.idle_timeout_seconds = idle_timeout_seconds
        self.revalidate_interval_seconds = revalidate_interval_seconds
        # 会话撤销：每用户一组事件（同用户多连接各自一个），撤销到达即全部置位。
        self._revocation_events: dict[int, set[asyncio.Event]] = {}

    def on_revocation(self, revocation: SessionRevocation) -> None:
        """SessionRevocationBus 订阅回调：user 级或精确会话撤销立即关闭对应连接。"""
        for event in list(self._revocation_events.get(revocation.user_id, ())):
            event.set()

    async def shutdown(self) -> None:
        """supervisor 停止时关闭全部连接（正常离开）。"""
        for events in list(self._revocation_events.values()):
            for event in list(events):
                event.set()

    async def handle(
        self,
        socket: WebSocketLike,
        *,
        session_id: str | None,
        origin: str | None,
        ip: str,
        host: str | None = None,
        forwarded_proto: str | None = None,
    ) -> None:
        principal = await self._authenticate(session_id)
        if principal is None:
            await socket.close(WS_UNAUTHENTICATED)
            return
        if not origin_allowed(
            origin,
            host=host,
            forwarded_proto=forwarded_proto,
            allowed_origins=self.allowed_origins,
        ):
            await socket.close(WS_FORBIDDEN_ORIGIN)
            return
        user_id = int(principal.user.id)
        if not self.registry.try_acquire(user_id, ip):
            await socket.close(WS_TOO_MANY_CONNECTIONS)
            return

        sink: BroadcastSink | None = None
        revoked = asyncio.Event()
        self._revocation_events.setdefault(user_id, set()).add(revoked)
        try:
            await socket.accept()
            sink = self.broadcast.subscribe(user_id)
            await self._pump(socket, sink, session_id or "", revoked)
        except Exception as error:  # noqa: BLE001  # 发送异常按 1011 关闭
            logger.warning("WS 连接异常 error_type=%s", type(error).__name__)
            try:
                await socket.close(WS_INTERNAL_ERROR)
            except Exception:  # noqa: BLE001, S110  # 关闭失败无需处理
                pass
        finally:
            if sink is not None:
                self.broadcast.unsubscribe(user_id, sink)
            self.registry.release(user_id, ip)
            events = self._revocation_events.get(user_id)
            if events is not None:
                events.discard(revoked)
                if not events:
                    self._revocation_events.pop(user_id, None)

    async def _pump(
        self,
        socket: WebSocketLike,
        sink: BroadcastSink,
        session_id: str,
        revoked: asyncio.Event,
    ) -> None:
        tasks = {
            asyncio.create_task(self._send_loop(socket, sink)),
            asyncio.create_task(self._receive_loop(socket)),
            asyncio.create_task(self._revalidate_loop(socket, session_id)),
            asyncio.create_task(self._revocation_loop(socket, revoked)),
        }
        try:
            _done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            raise _FirstDone(tasks)
        except _FirstDone as finished:
            code = WS_GOING_AWAY
            for task in finished.tasks:
                if task.done() and not task.cancelled() and task.exception() is None:
                    result = task.result()
                    if isinstance(result, int):
                        code = result
                        break
            if code is WS_GOING_AWAY:
                # 抛异常的任务（发送失败/复验异常）按 1011 关闭，不能静默吞成正常离开。
                for task in finished.tasks:
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        logger.warning(
                            "WS 泵任务异常 error_type=%s", type(task.exception()).__name__
                        )
                        code = WS_INTERNAL_ERROR
                        break
            await self._safe_close(socket, code)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _safe_close(socket: WebSocketLike, code: int) -> None:
        try:
            await socket.close(code)
        except Exception:  # noqa: BLE001, S110  # 对端已断开时关闭失败可忽略
            pass

    async def _send_loop(self, socket: WebSocketLike, sink: BroadcastSink) -> int:
        while True:
            message = await sink.next()
            if sink.dirty:
                return WS_RESYNC_REQUIRED
            if message is None:
                return WS_GOING_AWAY
            await socket.send_text(_dump(message))

    async def _receive_loop(self, socket: WebSocketLike) -> int:
        while True:
            try:
                text = await asyncio.wait_for(
                    socket.receive_text(), timeout=self.idle_timeout_seconds
                )
            except TimeoutError:
                return WS_GOING_AWAY  # 空闲超时：关闭连接。
            except Exception:  # noqa: BLE001  # 对端断开等接收异常按正常结束处理
                return WS_GOING_AWAY
            if text.strip() == "ping":
                await socket.send_text("pong")
            # 坏消息忽略：不关闭、不回错误。

    async def _revalidate_loop(self, socket: WebSocketLike, session_id: str) -> int:
        while True:
            await self.sleep(self.revalidate_interval_seconds)
            principal = await self._authenticate(session_id)
            if principal is None:
                return WS_UNAUTHENTICATED

    async def _revocation_loop(self, socket: WebSocketLike, revoked: asyncio.Event) -> int:
        await revoked.wait()
        return WS_UNAUTHENTICATED  # 会话撤销/服务关闭：立即断开。

    async def _authenticate(self, session_id: str | None) -> Principal | None:
        if not session_id:
            return None
        try:
            return await self.authenticator.authenticate_session(session_id)
        except DomainError as error:
            if error.code in {ErrorCode.UNAUTHENTICATED, ErrorCode.PERMISSION_DENIED}:
                return None
            raise
