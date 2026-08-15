"""aria2 RPC system + misc methods (M4 T16).

Implementations of ``system.multicall`` / ``system.listMethods``, the
terminal-result cleanup methods (``aria2.removeDownloadResult`` /
``aria2.purgeDownloadResult``), the unsupported/static compatibility
methods, and the ``Aria2RpcHandler`` dispatch shell, extracted from the
legacy ``services/aria2_rpc_handler.py``.

Behaviour is unchanged from the legacy handler.
"""

from __future__ import annotations

import logging
from typing import Any

from app.repositories.task.user_tasks import (
    delete_all_terminal_user_tasks,
    delete_terminal_user_task,
    delete_terminal_user_task_by_gid,
    get_user_task_by_gid,
)
from app.services import aria2_snapshot_sanitize
from app.services.rpc import read as rpc_read
from app.services.rpc import write as rpc_write
from app.services.rpc._shared import (
    RpcError,
    RpcErrorCode,
    _parse_history_gid,
    _resolve_owned_row,
)
from app.services.task_projection_rows import attach_snapshots_to_rows

logger = logging.getLogger(__name__)


def _get_handler_name(method: str) -> str:
    """将 RPC 方法名转换为处理器方法名

    aria2.addUri -> _handle_add_uri
    system.listMethods -> _handle_system_list_methods
    """
    if method.startswith("aria2."):
        name = method[6:]
    elif method.startswith("system."):
        name = "system_" + method[7:]
    else:
        name = method

    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())

    return "_handle_" + "".join(result)


def _strip_rpc_token(params: Any) -> list:
    if not isinstance(params, list):
        return []
    if params and isinstance(params[0], str) and params[0].startswith("token:"):
        return params[1:]
    return params


# ========== misc：终态结果清理 ==========
async def _handle_remove_download_result(user_id: int, params: list) -> str:
    """aria2.removeDownloadResult(gid) - 删除用户的 stopped 订阅（历史记录）"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid_param = params[0]
    if not isinstance(gid_param, str):
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid must be a string")
    gid, task_id, history_id = _parse_history_gid(gid_param)
    if history_id is not None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")
    deleted_tid = (
        await delete_terminal_user_task(user_id, task_id)
        if task_id is not None
        else await delete_terminal_user_task_by_gid(user_id, str(gid))
    )
    if deleted_tid is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")
    from app.services.history_retention import reclaim_zero_pid_tid

    await reclaim_zero_pid_tid(deleted_tid)
    return "OK"


async def _handle_purge_download_result(user_id: int, params: list) -> str:
    """aria2.purgeDownloadResult() - 删除用户所有 stopped 订阅"""
    deleted_tids = await delete_all_terminal_user_tasks(user_id)
    from app.services.history_retention import reclaim_zero_pid_tid

    for tid in set(deleted_tids):
        await reclaim_zero_pid_tid(tid)
    return "OK"


# ========== 明确拒绝暂停（aria2deck 不支持暂停，只支持取消） ==========
async def _handle_pause(user_id: int, params: list) -> str:
    raise RpcError(1, "Pause is not supported, use aria2.remove to cancel")


async def _handle_force_pause(user_id: int, params: list) -> str:
    raise RpcError(1, "Pause is not supported, use aria2.remove to cancel")


async def _handle_unpause(user_id: int, params: list) -> str:
    raise RpcError(1, "Unpause is not supported")


async def _handle_pause_all(user_id: int, params: list) -> str:
    raise RpcError(1, "Pause is not supported")


async def _handle_force_pause_all(user_id: int, params: list) -> str:
    raise RpcError(1, "Pause is not supported")


async def _handle_unpause_all(user_id: int, params: list) -> str:
    raise RpcError(1, "Unpause is not supported")


# ========== 静态兼容方法 ==========
async def _handle_get_option(user_id: int, params: list) -> dict:
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    if await _resolve_owned_row(user_id, gid) is None:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, "Task not found")
    return {}


async def _handle_get_global_option(user_id: int, params: list) -> dict:
    return {}


async def _handle_change_position(user_id: int, params: list) -> int:
    return 0


async def _handle_change_uri(user_id: int, params: list) -> list:
    raise RpcError(
        RpcErrorCode.PERMISSION_DENIED,
        "URI mutation is not supported",
    )


async def _handle_save_session(user_id: int, params: list) -> str:
    return "OK"


async def _handle_get_session_info(user_id: int, params: list) -> dict:
    return {"sessionId": "aria2deck-proxy-session"}


class Aria2RpcHandler:
    """aria2 RPC 方法处理器

    为每个用户提供隔离的 aria2 RPC 接口。
    所有操作只能访问当前用户的任务和文件。
    """

    # 支持的 RPC 方法列表
    SUPPORTED_METHODS = [
        "aria2.addUri",
        "aria2.addTorrent",
        "aria2.remove",
        "aria2.forceRemove",
        "aria2.pause",
        "aria2.forcePause",
        "aria2.unpause",
        "aria2.tellStatus",
        "aria2.tellActive",
        "aria2.tellWaiting",
        "aria2.tellStopped",
        "aria2.getFiles",
        "aria2.getUris",
        "aria2.getGlobalStat",
        "aria2.getVersion",
        "aria2.changePosition",
        "aria2.getOption",
        "aria2.getGlobalOption",
        "aria2.saveSession",
        "aria2.purgeDownloadResult",
        "aria2.removeDownloadResult",
        "aria2.pauseAll",
        "aria2.forcePauseAll",
        "aria2.unpauseAll",
        "aria2.getSessionInfo",
        "aria2.getPeers",
        "aria2.getServers",
        "aria2.changeUri",
        "system.listMethods",
        "system.multicall",
    ]

    def __init__(self, user_id: int):
        self.user_id = user_id
        self._multicall_depth: int = 0

    async def handle(self, method: str, params: list) -> Any:
        """路由到具体方法处理"""
        handler_name = _get_handler_name(method)
        handler = getattr(self, handler_name, None)

        if handler is None:
            raise RpcError(RpcErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")

        return await handler(params)

    async def _get_projection_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Attach the backend snapshot/files projection to a task row."""
        return (await attach_snapshots_to_rows([row]))[0]

    async def _verify_task_owner(self, gid: str) -> dict[str, Any] | None:
        """Return the current user's v0 task row for an aria2 gid."""
        return await get_user_task_by_gid(self.user_id, gid)

    async def _get_user_available_space(self) -> int:
        """获取用户实际可用空间（供既有测试与配额检查复用）"""
        from app.services.rpc import _shared as _rpc_shared

        return await _rpc_shared._get_user_available_space(self.user_id)

    @staticmethod
    def _selected_torrent_indexes(metadata: Any, value: Any) -> tuple[int, ...]:
        from app.services.rpc import _shared as _rpc_shared

        return _rpc_shared._selected_torrent_indexes(metadata, value)

    def _sanitize_files(self, files: Any) -> list[dict]:
        return aria2_snapshot_sanitize.sanitize_files(files)

    def _sanitize_uris(self, uris: Any) -> list[dict]:
        return aria2_snapshot_sanitize.sanitize_uris(uris)

    @staticmethod
    def _status_has_file_name(status: dict[str, Any]) -> bool:
        from app.services.task_projection import has_real_file_path

        return has_real_file_path(status)

    @staticmethod
    def _strip_rpc_token(params: Any) -> list:
        return _strip_rpc_token(params)

    # ========== 写方法：services/rpc/write.py（M4 T14） ==========
    async def _handle_add_uri(self, params: list) -> str:
        """aria2.addUri(uris[, options[, position]])"""
        return await rpc_write._handle_add_uri(self.user_id, params)

    async def _handle_add_torrent(self, params: list) -> str:
        """aria2.addTorrent(torrent[, uris[, options[, position]]])"""
        return await rpc_write._handle_add_torrent(self.user_id, params)

    async def _handle_remove(self, params: list) -> str:
        """aria2.remove(gid)"""
        return await rpc_write._handle_remove(self.user_id, params)

    async def _handle_force_remove(self, params: list) -> str:
        """aria2.forceRemove(gid) - 同 remove"""
        return await rpc_write._handle_force_remove(self.user_id, params)

    # ========== 读方法：services/rpc/read.py（M4 T15） ==========
    async def _handle_tell_status(self, params: list) -> dict:
        """aria2.tellStatus(gid[, keys])"""
        return await rpc_read._handle_tell_status(self.user_id, params)

    async def _handle_tell_active(self, params: list) -> list:
        """aria2.tellActive([keys])"""
        return await rpc_read._handle_tell_active(self.user_id, params)

    async def _handle_tell_waiting(self, params: list) -> list:
        """aria2.tellWaiting(offset, num[, keys])"""
        return await rpc_read._handle_tell_waiting(self.user_id, params)

    async def _handle_tell_stopped(self, params: list) -> list:
        """aria2.tellStopped(offset, num[, keys])"""
        return await rpc_read._handle_tell_stopped(self.user_id, params)

    async def _handle_get_global_stat(self, params: list) -> dict:
        """aria2.getGlobalStat()"""
        return await rpc_read._handle_get_global_stat(self.user_id, params)

    async def _handle_get_files(self, params: list) -> list:
        """aria2.getFiles(gid)"""
        return await rpc_read._handle_get_files(self.user_id, params)

    async def _handle_get_uris(self, params: list) -> list:
        """aria2.getUris(gid)"""
        return await rpc_read._handle_get_uris(self.user_id, params)

    async def _handle_get_version(self, params: list) -> dict:
        """aria2.getVersion()"""
        return await rpc_read._handle_get_version(self.user_id, params)

    async def _handle_get_peers(self, params: list) -> list:
        """aria2.getPeers(gid)"""
        return await rpc_read._handle_get_peers(self.user_id, params)

    async def _handle_get_servers(self, params: list) -> list:
        """aria2.getServers(gid)"""
        return await rpc_read._handle_get_servers(self.user_id, params)

    # ========== misc：终态结果清理 ==========
    async def _handle_remove_download_result(self, params: list) -> str:
        """aria2.removeDownloadResult(gid) - 删除用户的 stopped 订阅（历史记录）"""
        return await _handle_remove_download_result(self.user_id, params)

    async def _handle_purge_download_result(self, params: list) -> str:
        """aria2.purgeDownloadResult() - 删除用户所有 stopped 订阅"""
        return await _handle_purge_download_result(self.user_id, params)

    # ========== 明确拒绝暂停（aria2deck 不支持暂停，只支持取消） ==========
    async def _handle_pause(self, params: list) -> str:
        return await _handle_pause(self.user_id, params)

    async def _handle_force_pause(self, params: list) -> str:
        return await _handle_force_pause(self.user_id, params)

    async def _handle_unpause(self, params: list) -> str:
        return await _handle_unpause(self.user_id, params)

    async def _handle_pause_all(self, params: list) -> str:
        return await _handle_pause_all(self.user_id, params)

    async def _handle_force_pause_all(self, params: list) -> str:
        return await _handle_force_pause_all(self.user_id, params)

    async def _handle_unpause_all(self, params: list) -> str:
        return await _handle_unpause_all(self.user_id, params)

    # ========== 静态兼容方法 ==========
    async def _handle_get_option(self, params: list) -> dict:
        return await _handle_get_option(self.user_id, params)

    async def _handle_get_global_option(self, params: list) -> dict:
        return await _handle_get_global_option(self.user_id, params)

    async def _handle_change_position(self, params: list) -> int:
        return await _handle_change_position(self.user_id, params)

    async def _handle_change_uri(self, params: list) -> list:
        return await _handle_change_uri(self.user_id, params)

    async def _handle_save_session(self, params: list) -> str:
        return await _handle_save_session(self.user_id, params)

    async def _handle_get_session_info(self, params: list) -> dict:
        return await _handle_get_session_info(self.user_id, params)

    # ========== system 方法 ==========
    async def _handle_system_list_methods(self, params: list) -> list:
        """system.listMethods()"""
        return self.SUPPORTED_METHODS

    async def _handle_system_multicall(self, params: list) -> list:
        """system.multicall(methods)"""
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "methods is required")
        if self._multicall_depth >= 1:
            raise RpcError(RpcErrorCode.INVALID_REQUEST, "Nested multicall is not allowed")
        methods = params[0]
        MAX_MULTICALL_SIZE = 20
        if len(methods) > MAX_MULTICALL_SIZE:
            raise RpcError(
                RpcErrorCode.INVALID_REQUEST,
                f"Too many methods in multicall, max {MAX_MULTICALL_SIZE}",
            )
        self._multicall_depth += 1
        try:
            results: list[Any] = []
            for index, call in enumerate(methods):
                if not isinstance(call, dict):
                    results.append(
                        {
                            "faultCode": RpcErrorCode.INVALID_PARAMS,
                            "faultString": "Invalid method call",
                        }
                    )
                    continue
                method_name = call.get("methodName", "")
                raw_method_params = call.get("params", [])
                method_params = _strip_rpc_token(raw_method_params)
                try:
                    result = await self.handle(method_name, method_params)
                    results.append([result])
                except RpcError as exc:
                    fault_string = (
                        "Internal error"
                        if exc.code == RpcErrorCode.INTERNAL_ERROR
                        else exc.message
                    )
                    results.append(
                        {"faultCode": exc.code, "faultString": fault_string}
                    )
                except Exception as exc:
                    logger.warning(
                        "system.multicall method failed user_id=%s index=%s error_type=%s",
                        self.user_id,
                        index,
                        type(exc).__name__,
                    )
                    results.append(
                        {
                            "faultCode": RpcErrorCode.INTERNAL_ERROR,
                            "faultString": "Internal error",
                        }
                    )
            return results
        finally:
            self._multicall_depth -= 1
