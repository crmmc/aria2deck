"""aria2 RPC read methods (M4 T15).

Implementations of the read-only RPC methods, extracted from
``aria2_rpc_handler.py``. Each function takes ``user_id`` explicitly;
``Aria2RpcHandler`` (services/rpc/system.py) keeps thin delegates that
forward ``self.user_id``.

Behaviour is unchanged from the legacy handler.
"""

from __future__ import annotations

from typing import Any

from app.domain.task_policy import stat_counts
from app.repositories.task.user_tasks import list_user_tasks
from app.services import aria2_snapshot_sanitize, rpc_view_service
from app.services.rpc._shared import (
    RpcError,
    RpcErrorCode,
    _apply_status_keys,
    _apply_status_keys_to_list,
    _extract_status_keys,
    _normalize_pagination,
    _resolve_owned_row,
    _slice_with_offset,
)
from app.services.task_projection import has_real_file_path, speed_totals
from app.services.task_projection_rows import attach_snapshots_to_rows


async def _get_projection_row(row: dict[str, Any]) -> dict[str, Any]:
    """Attach the backend snapshot/files projection to a task row."""
    return (await attach_snapshots_to_rows([row]))[0]


async def _handle_tell_status(user_id: int, params: list) -> dict:
    """aria2.tellStatus(gid[, keys])"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    keys = _extract_status_keys(params, 1)

    row = await _resolve_owned_row(user_id, gid)
    if row is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

    projected = await _get_projection_row(row)
    response = rpc_view_service.status_from_task(projected)
    return _apply_status_keys(response, keys)


async def _handle_tell_active(user_id: int, params: list) -> list:
    """aria2.tellActive([keys])"""
    keys = _extract_status_keys(params, 0)
    statuses = await rpc_view_service.list_active_statuses(user_id)
    return _apply_status_keys_to_list(statuses, keys)


async def _handle_tell_waiting(user_id: int, params: list) -> list:
    """aria2.tellWaiting(offset, num[, keys])"""
    offset, num = _normalize_pagination(params)
    keys = _extract_status_keys(params, 2)
    waiting_statuses = await rpc_view_service.list_waiting_statuses(user_id)
    sliced = _slice_with_offset(waiting_statuses, offset, num)
    return _apply_status_keys_to_list(sliced, keys)


async def _handle_tell_stopped(user_id: int, params: list) -> list:
    """aria2.tellStopped(offset, num[, keys])"""
    offset, num = _normalize_pagination(params)
    keys = _extract_status_keys(params, 2)
    stopped_statuses = await rpc_view_service.list_stopped_statuses(user_id)
    sliced = _slice_with_offset(stopped_statuses, offset, num)
    return _apply_status_keys_to_list(sliced, keys)


async def _handle_get_global_stat(user_id: int, params: list) -> dict:
    """aria2.getGlobalStat()"""
    rows = await attach_snapshots_to_rows(await list_user_tasks(user_id))
    counts = stat_counts(rows)
    num_active = counts["active"]
    num_waiting = counts["waiting"]
    num_stopped = counts["stopped"]

    speeds = speed_totals(rows)
    return {
        "downloadSpeed": str(speeds["download_speed"]),
        "uploadSpeed": str(speeds["upload_speed"]),
        "numActive": str(num_active),
        "numWaiting": str(num_waiting),
        "numStopped": str(num_stopped),
        "numStoppedTotal": str(num_stopped),
    }


async def _handle_get_files(user_id: int, params: list) -> list:
    """aria2.getFiles(gid)"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    row = await _resolve_owned_row(user_id, gid)
    if row is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
    projected = await _get_projection_row(row)
    snapshot_files = projected.get("backend_files") or []
    if snapshot_files and has_real_file_path({"files": snapshot_files}):
        return snapshot_files
    return aria2_snapshot_sanitize.sanitize_files(
        rpc_view_service.status_from_task(projected).get("files")
    )


async def _handle_get_uris(user_id: int, params: list) -> list:
    """aria2.getUris(gid)"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    row = await _resolve_owned_row(user_id, gid)
    if row is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
    source_uri = row.get("source_uri")
    return (
        aria2_snapshot_sanitize.sanitize_uris(
            [{"uri": source_uri, "status": "used"}]
        )
        if source_uri
        else []
    )


async def _handle_get_peers(user_id: int, params: list) -> list:
    """aria2.getPeers(gid)"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    row = await _resolve_owned_row(user_id, gid)
    if row is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
    return []


async def _handle_get_servers(user_id: int, params: list) -> list:
    """aria2.getServers(gid)"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    row = await _resolve_owned_row(user_id, gid)
    if row is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
    return []


async def _handle_get_version(user_id: int, params: list) -> dict:
    """aria2.getVersion()"""
    return {"version": "aria2deck-proxy", "enabledFeatures": []}
