from __future__ import annotations

import asyncio
import logging
from typing import Any


from app.repositories.downloads import list_user_tasks_for_download
from app.services.task_projection import build_rest_task_response
from app.services.task_projection_rows import attach_snapshots_to_rows


logger = logging.getLogger(__name__)
MAX_WS_CONNECTIONS_PER_USER = 5
MAX_WS_CONNECTIONS_PER_IP = 50

_ws_connections: dict[int, set[Any]] = {}
_ws_connections_by_ip: dict[str, set[Any]] = {}
_ws_connections_by_session: dict[str, set[Any]] = {}
_ws_connection_info: dict[Any, tuple[int, str, str]] = {}
_ws_reservations_by_user: dict[int, set[object]] = {}
_ws_reservations_by_ip: dict[str, set[object]] = {}
_ws_reservations_by_session: dict[str, set[object]] = {}
_ws_reservation_info: dict[object, tuple[int, str, str]] = {}
_ws_lock = asyncio.Lock()


def _discard_from_index(index: dict[Any, set[Any]], key: Any, ws: Any) -> None:
    sockets = index.get(key)
    if sockets is None:
        return
    sockets.discard(ws)
    if not sockets:
        index.pop(key, None)


def _detach_ws_locked(ws: Any, user_id_hint: int | None = None) -> None:
    info = _ws_connection_info.pop(ws, None)
    if info is None:
        if user_id_hint is not None:
            _discard_from_index(_ws_connections, user_id_hint, ws)
        return
    user_id, session_id, client_ip = info
    _discard_from_index(_ws_connections, user_id, ws)
    _discard_from_index(_ws_connections_by_session, session_id, ws)
    _discard_from_index(_ws_connections_by_ip, client_ip, ws)


def _release_ws_reservation_locked(reservation: object) -> None:
    info = _ws_reservation_info.pop(reservation, None)
    if info is None:
        return
    user_id, session_id, client_ip = info
    _discard_from_index(_ws_reservations_by_user, user_id, reservation)
    _discard_from_index(_ws_reservations_by_session, session_id, reservation)
    _discard_from_index(_ws_reservations_by_ip, client_ip, reservation)


async def reserve_ws_slot(
    user_id: int,
    session_id: str,
    client_ip: str,
) -> object | None:
    async with _ws_lock:
        active_user = len(_ws_connections.get(user_id, set()))
        pending_user = len(_ws_reservations_by_user.get(user_id, set()))
        active_ip = len(_ws_connections_by_ip.get(client_ip, set()))
        pending_ip = len(_ws_reservations_by_ip.get(client_ip, set()))
        if (
            active_user + pending_user >= MAX_WS_CONNECTIONS_PER_USER
            or active_ip + pending_ip >= MAX_WS_CONNECTIONS_PER_IP
        ):
            return None

        reservation = object()
        _ws_reservations_by_user.setdefault(user_id, set()).add(reservation)
        _ws_reservations_by_session.setdefault(session_id, set()).add(reservation)
        _ws_reservations_by_ip.setdefault(client_ip, set()).add(reservation)
        _ws_reservation_info[reservation] = (user_id, session_id, client_ip)
        return reservation


async def release_ws_slot(reservation: object) -> None:
    async with _ws_lock:
        _release_ws_reservation_locked(reservation)


async def activate_ws_slot(
    reservation: object,
    ws: Any,
) -> bool:
    async with _ws_lock:
        info = _ws_reservation_info.get(reservation)
        if info is None:
            return False
        if ws in _ws_connection_info:
            _release_ws_reservation_locked(reservation)
            return False

        user_id, session_id, client_ip = info
        _release_ws_reservation_locked(reservation)
        _ws_connections.setdefault(user_id, set()).add(ws)
        _ws_connections_by_ip.setdefault(client_ip, set()).add(ws)
        _ws_connections_by_session.setdefault(session_id, set()).add(ws)
        _ws_connection_info[ws] = (user_id, session_id, client_ip)
        return True


class WsReservation:
    __slots__ = ("_token",)

    def __init__(self, token: object) -> None:
        self._token = token

    async def activate(self, ws: Any) -> bool:
        return await activate_ws_slot(self._token, ws)

    async def release(self) -> None:
        await release_ws_slot(self._token)


async def register_ws(
    user_id: int,
    session_id: str,
    client_ip: str,
) -> WsReservation | None:
    token = await reserve_ws_slot(user_id, session_id, client_ip)
    return WsReservation(token) if token is not None else None


async def unregister_ws(user_id: int, ws: Any) -> None:
    async with _ws_lock:
        _detach_ws_locked(ws, user_id)


async def _registered_sockets(user_id: int) -> list[Any]:
    async with _ws_lock:
        return list(_ws_connections.get(user_id, set()))


async def _close_sockets(sockets: list[Any], code: int) -> None:
    for ws in sockets:
        try:
            await ws.close(code=code)
        except Exception as exc:
            logger.debug("WebSocket close failed: %s", exc)


async def _detach_and_close(
    sockets: list[Any],
    code: int,
    user_id_hint: int | None = None,
) -> None:
    async with _ws_lock:
        for ws in sockets:
            _detach_ws_locked(ws, user_id_hint)
    await _close_sockets(sockets, code)


async def broadcast_task_update_to_subscribers(task_id: int) -> None:
    # M3 T10: 广播只读快照投影，不再回退实时 aria2 RPC。
    rows = await attach_snapshots_to_rows(
        await list_user_tasks_for_download(task_id)
    )

    for row in rows:
        owner_id = int(row["user_id"])
        payload = build_rest_task_response(row)

        sockets = await _registered_sockets(owner_id)

        failed_sockets = []
        for ws in sockets:
            try:
                await ws.send_json({"type": "task_update", "task": payload})
            except Exception as exc:
                logger.debug("WebSocket send failed for user %s: %s", owner_id, exc)
                failed_sockets.append(ws)

        await _detach_and_close(failed_sockets, 1011, owner_id)


async def broadcast_notification(
    user_id: int,
    message: str,
    level: str = "info",
) -> None:
    sockets = await _registered_sockets(user_id)

    notification = {"type": "notification", "message": message, "level": level}
    failed_sockets = []

    for ws in sockets:
        try:
            await ws.send_json(notification)
        except Exception as exc:
            logger.debug("Notification send failed user_id=%s error=%s", user_id, exc)
            failed_sockets.append(ws)

    await _detach_and_close(failed_sockets, 1011, user_id)


async def clear_connections() -> None:
    async with _ws_lock:
        sockets = list({ws for group in _ws_connections.values() for ws in group})
        _ws_connections.clear()
        _ws_connections_by_ip.clear()
        _ws_connections_by_session.clear()
        _ws_connection_info.clear()
        _ws_reservations_by_user.clear()
        _ws_reservations_by_ip.clear()
        _ws_reservations_by_session.clear()
        _ws_reservation_info.clear()
    await _close_sockets(sockets, 1001)


async def set_connections_for_user(user_id: int, sockets: set[Any]) -> None:
    async with _ws_lock:
        previous = list(_ws_connections.get(user_id, set()))
        for ws in previous:
            _detach_ws_locked(ws, user_id)
        if sockets:
            _ws_connections[user_id] = set(sockets)
        else:
            _ws_connections.pop(user_id, None)
    await _close_sockets(previous, 1001)


async def remove_connections_for_user(user_id: int, code: int = 4401) -> None:
    async with _ws_lock:
        reservations = list(_ws_reservations_by_user.get(user_id, set()))
        for reservation in reservations:
            _release_ws_reservation_locked(reservation)
        sockets = list(_ws_connections.get(user_id, set()))
        for ws in sockets:
            _detach_ws_locked(ws, user_id)
    await _close_sockets(sockets, code)


async def remove_connections_for_session(
    session_id: str,
    code: int = 4401,
) -> None:
    async with _ws_lock:
        reservations = list(_ws_reservations_by_session.get(session_id, set()))
        for reservation in reservations:
            _release_ws_reservation_locked(reservation)
        sockets = list(_ws_connections_by_session.get(session_id, set()))
        for ws in sockets:
            _detach_ws_locked(ws)
    await _close_sockets(sockets, code)
