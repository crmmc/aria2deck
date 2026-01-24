"""aria2 RPC 方法处理器

为外部 aria2 兼容客户端（如 AriaNg、Motrix）提供 RPC 方法实现。
实现用户隔离、数据脱敏、配额检查等安全机制。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.aria2.client import Aria2Client
from app.core.config import settings
from app.db import execute, fetch_all, fetch_one, utc_now


# JSON-RPC 2.0 错误码
class RpcErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # 自定义错误码
    TASK_NOT_FOUND = 1
    PERMISSION_DENIED = 2
    QUOTA_EXCEEDED = 3


class RpcError(Exception):
    """JSON-RPC 错误"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


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
        "aria2.changeOption",
        "aria2.getGlobalOption",
        "aria2.changeGlobalOption",
        "aria2.shutdown",
        "aria2.forceShutdown",
        "aria2.saveSession",
        "aria2.purgeDownloadResult",
        "aria2.removeDownloadResult",
        "aria2.pauseAll",
        "aria2.forcePauseAll",
        "aria2.unpauseAll",
        "aria2.getSessionInfo",
        "system.listMethods",
        "system.multicall",
    ]

    def __init__(self, user_id: int, aria2_client: Aria2Client):
        self.user_id = user_id
        self.client = aria2_client
        self._user_dir: str | None = None
        self._user_incomplete_dir: str | None = None

    async def handle(self, method: str, params: list) -> Any:
        """路由到具体方法处理

        Args:
            method: RPC 方法名（如 aria2.addUri）
            params: 参数列表（已去除 token 前缀）

        Returns:
            方法执行结果

        Raises:
            RpcError: 方法不存在或执行失败
        """
        # 移除方法名中的前缀得到处理器名
        handler_name = self._get_handler_name(method)
        handler = getattr(self, handler_name, None)

        if handler is None:
            raise RpcError(
                RpcErrorCode.METHOD_NOT_FOUND,
                f"Method not found: {method}"
            )

        return await handler(params)

    def _get_handler_name(self, method: str) -> str:
        """将 RPC 方法名转换为处理器方法名

        aria2.addUri -> _handle_add_uri
        system.listMethods -> _handle_system_list_methods
        """
        # 移除前缀（aria2. 或 system.）
        if method.startswith("aria2."):
            name = method[6:]  # 移除 "aria2."
        elif method.startswith("system."):
            name = "system_" + method[7:]  # system.multicall -> system_multicall
        else:
            name = method

        # 驼峰转下划线
        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append("_")
            result.append(char.lower())

        return "_handle_" + "".join(result)

    # ========== 辅助方法 ==========

    def _get_user_download_dir(self) -> str:
        """获取用户下载目录"""
        if self._user_dir is None:
            base = Path(settings.download_dir).resolve()
            user_dir = base / str(self.user_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            self._user_dir = str(user_dir)
        return self._user_dir

    def _get_user_incomplete_dir(self) -> str:
        """获取用户的 .incomplete 目录（下载中文件存放位置）"""
        if self._user_incomplete_dir is None:
            base = Path(settings.download_dir).resolve()
            incomplete_dir = base / str(self.user_id) / ".incomplete"
            incomplete_dir.mkdir(parents=True, exist_ok=True)
            self._user_incomplete_dir = str(incomplete_dir)
        return self._user_incomplete_dir

    def _verify_task_owner(self, gid: str) -> dict | None:
        """检查 gid 对应的任务是否属于当前用户

        Args:
            gid: 任务 GID

        Returns:
            任务信息字典，如果不属于当前用户或不存在则返回 None
        """
        return fetch_one(
            "SELECT * FROM tasks WHERE gid = ? AND owner_id = ?",
            [gid, self.user_id]
        )

    def _sanitize_path(self, path: str) -> str:
        """将服务器绝对路径转为用户相对路径

        /downloads/123/movie/file.mp4 -> movie/file.mp4
        /downloads/123/.incomplete/movie/file.mp4 -> .incomplete/movie/file.mp4
        """
        if not path:
            return path

        user_dir = Path(self._get_user_download_dir())
        try:
            abs_path = Path(path)
            if abs_path.is_absolute() and str(abs_path).startswith(str(user_dir)):
                return str(abs_path.relative_to(user_dir))
        except (ValueError, RuntimeError):
            pass

        return path

    def _sanitize_status(self, status: dict) -> dict:
        """对 tellStatus 返回的数据进行脱敏处理

        - dir 字段转为相对路径
        - files[].path 转为相对路径
        """
        result = dict(status)

        # 脱敏 dir 字段
        if "dir" in result:
            result["dir"] = self._sanitize_path(result["dir"])

        # 脱敏 files 列表中的 path
        if "files" in result and isinstance(result["files"], list):
            sanitized_files = []
            for f in result["files"]:
                sanitized_file = dict(f)
                if "path" in sanitized_file:
                    sanitized_file["path"] = self._sanitize_path(sanitized_file["path"])
                sanitized_files.append(sanitized_file)
            result["files"] = sanitized_files

        return result

    def _get_user_available_space(self) -> int:
        """获取用户实际可用空间（考虑配额和机器空间限制）"""
        # 获取用户配额
        user = fetch_one("SELECT quota FROM users WHERE id = ?", [self.user_id])
        if not user:
            return 0
        user_quota = user.get("quota", 100 * 1024 * 1024 * 1024)  # 默认 100GB

        # 计算用户已使用的空间
        user_dir = Path(settings.download_dir) / str(self.user_id)
        used_space = 0
        if user_dir.exists():
            for file_path in user_dir.rglob("*"):
                if file_path.is_file():
                    try:
                        used_space += file_path.stat().st_size
                    except Exception:
                        pass

        # 获取机器实际剩余空间
        download_path = Path(settings.download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(download_path)
        machine_free = disk.free

        # 用户理论可用空间（基于配额）
        user_free_by_quota = max(0, user_quota - used_space)

        # 实际可用空间 = min(用户配额剩余, 机器剩余空间)
        return min(user_free_by_quota, machine_free)

    def _check_disk_space(self) -> tuple[bool, int]:
        """检查磁盘空间是否足够"""
        download_path = Path(settings.download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(download_path)

        # 从配置获取最小空闲空间
        config = fetch_one("SELECT value FROM config WHERE key = 'min_free_disk'")
        min_free = int(config["value"]) if config else 1073741824  # 默认 1GB

        return disk.free > min_free, disk.free

    # ========== 完整实现的方法 ==========

    async def _handle_add_uri(self, params: list) -> str:
        """添加 HTTP/FTP/磁力链接任务

        aria2.addUri(uris[, options[, position]])

        Returns:
            新任务的 GID
        """
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris is required")

        uris = params[0]
        options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
        position = params[2] if len(params) > 2 else None

        # 检查磁盘空间
        disk_ok, disk_free = self._check_disk_space()
        if not disk_ok:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                f"Disk space not enough, free: {disk_free / 1024 / 1024 / 1024:.2f} GB"
            )

        # 检查用户配额
        user_available = self._get_user_available_space()
        if user_available <= 0:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                "Your quota has been exceeded"
            )

        # 强制设置下载目录为用户的 .incomplete 目录，忽略客户端传入的 dir
        options["dir"] = self._get_user_incomplete_dir()

        # 创建数据库任务记录
        uri = uris[0] if uris else ""
        task_id = execute(
            """
            INSERT INTO tasks (owner_id, uri, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [self.user_id, uri, "queued", utc_now(), utc_now()],
        )

        # 调用 aria2 添加任务
        try:
            call_params = [uris, options]
            if position is not None:
                call_params.append(position)
            gid = await self.client.add_uri(uris, options)

            # 更新数据库记录
            execute(
                "UPDATE tasks SET gid = ?, status = ?, updated_at = ? WHERE id = ?",
                [gid, "active", utc_now(), task_id]
            )
            return gid

        except Exception as exc:
            # 更新数据库状态为错误
            execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                ["error", str(exc), utc_now(), task_id],
            )
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_add_torrent(self, params: list) -> str:
        """添加种子任务

        aria2.addTorrent(torrent[, uris[, options[, position]]])

        Args:
            torrent: Base64 编码的种子文件内容

        Returns:
            新任务的 GID
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "torrent is required")

        torrent = params[0]
        uris = params[1] if len(params) > 1 and isinstance(params[1], list) else []
        options = params[2] if len(params) > 2 and isinstance(params[2], dict) else {}
        position = params[3] if len(params) > 3 else None

        # 校验 Base64 大小（约 10MB 限制）
        max_base64_length = 14 * 1024 * 1024
        if len(torrent) > max_base64_length:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                "Torrent file too large, max 10MB"
            )

        # 检查磁盘空间
        disk_ok, disk_free = self._check_disk_space()
        if not disk_ok:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                f"Disk space not enough, free: {disk_free / 1024 / 1024 / 1024:.2f} GB"
            )

        # 检查用户配额
        user_available = self._get_user_available_space()
        if user_available <= 0:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                "Your quota has been exceeded"
            )

        # 强制设置下载目录
        options["dir"] = self._get_user_incomplete_dir()

        # 创建数据库任务记录
        task_id = execute(
            """
            INSERT INTO tasks (owner_id, uri, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [self.user_id, "[torrent]", "queued", utc_now(), utc_now()],
        )

        # 调用 aria2 添加任务
        try:
            gid = await self.client.add_torrent(torrent, uris, options)

            # 更新数据库记录
            execute(
                "UPDATE tasks SET gid = ?, status = ?, updated_at = ? WHERE id = ?",
                [gid, "active", utc_now(), task_id]
            )
            return gid

        except Exception as exc:
            execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                ["error", str(exc), utc_now(), task_id],
            )
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_remove(self, params: list) -> str:
        """删除任务

        aria2.remove(gid)
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            result = await self.client.remove(gid)
            execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ["removed", utc_now(), task["id"]]
            )
            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_force_remove(self, params: list) -> str:
        """强制删除任务

        aria2.forceRemove(gid)
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            result = await self.client.force_remove(gid)
            execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ["removed", utc_now(), task["id"]]
            )
            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_pause(self, params: list) -> str:
        """暂停任务

        aria2.pause(gid)
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            result = await self.client.pause(gid)
            execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ["paused", utc_now(), task["id"]]
            )
            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_force_pause(self, params: list) -> str:
        """强制暂停任务（等同 pause）

        aria2.forcePause(gid)
        """
        # 复用 pause 逻辑
        return await self._handle_pause(params)

    async def _handle_unpause(self, params: list) -> str:
        """恢复任务

        aria2.unpause(gid)
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            result = await self.client.unpause(gid)
            execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ["active", utc_now(), task["id"]]
            )
            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_tell_status(self, params: list) -> dict:
        """查询任务状态

        aria2.tellStatus(gid[, keys])
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        keys = params[1] if len(params) > 1 and isinstance(params[1], list) else None

        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            status = await self.client.tell_status(gid)
            sanitized = self._sanitize_status(status)

            # 如果指定了 keys，只返回指定字段
            if keys:
                return {k: sanitized[k] for k in keys if k in sanitized}
            return sanitized
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_tell_active(self, params: list) -> list:
        """查询活动任务（仅用户自己的）

        aria2.tellActive([keys])
        """
        keys = params[0] if params and isinstance(params[0], list) else None

        # 获取用户的所有活动任务 GID
        user_tasks = fetch_all(
            "SELECT gid FROM tasks WHERE owner_id = ? AND status = 'active' AND gid IS NOT NULL",
            [self.user_id]
        )
        user_gids = {task["gid"] for task in user_tasks}

        if not user_gids:
            return []

        try:
            # 从 aria2 获取所有活动任务
            all_active = await self.client.tell_active()

            # 过滤出用户的任务并脱敏
            result = []
            for status in all_active:
                if status.get("gid") in user_gids:
                    sanitized = self._sanitize_status(status)
                    if keys:
                        sanitized = {k: sanitized[k] for k in keys if k in sanitized}
                    result.append(sanitized)

            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_tell_waiting(self, params: list) -> list:
        """查询等待任务

        aria2.tellWaiting(offset, num[, keys])
        """
        if len(params) < 2:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "offset and num are required")

        offset = params[0]
        num = params[1]
        keys = params[2] if len(params) > 2 and isinstance(params[2], list) else None

        # 获取用户的等待/暂停任务 GID
        user_tasks = fetch_all(
            "SELECT gid FROM tasks WHERE owner_id = ? AND status IN ('waiting', 'paused', 'queued') AND gid IS NOT NULL",
            [self.user_id]
        )
        user_gids = {task["gid"] for task in user_tasks}

        if not user_gids:
            return []

        try:
            all_waiting = await self.client.tell_waiting(offset, num)

            result = []
            for status in all_waiting:
                if status.get("gid") in user_gids:
                    sanitized = self._sanitize_status(status)
                    if keys:
                        sanitized = {k: sanitized[k] for k in keys if k in sanitized}
                    result.append(sanitized)

            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_tell_stopped(self, params: list) -> list:
        """查询已停止任务

        aria2.tellStopped(offset, num[, keys])
        """
        if len(params) < 2:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "offset and num are required")

        offset = params[0]
        num = params[1]
        keys = params[2] if len(params) > 2 and isinstance(params[2], list) else None

        # 获取用户的已停止任务 GID
        user_tasks = fetch_all(
            "SELECT gid FROM tasks WHERE owner_id = ? AND status IN ('complete', 'error', 'stopped', 'removed') AND gid IS NOT NULL",
            [self.user_id]
        )
        user_gids = {task["gid"] for task in user_tasks}

        if not user_gids:
            return []

        try:
            all_stopped = await self.client.tell_stopped(offset, num)

            result = []
            for status in all_stopped:
                if status.get("gid") in user_gids:
                    sanitized = self._sanitize_status(status)
                    if keys:
                        sanitized = {k: sanitized[k] for k in keys if k in sanitized}
                    result.append(sanitized)

            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_get_files(self, params: list) -> list:
        """获取文件列表

        aria2.getFiles(gid)
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            files = await self.client.get_files(gid)

            # 脱敏文件路径
            result = []
            for f in files:
                sanitized = dict(f)
                if "path" in sanitized:
                    sanitized["path"] = self._sanitize_path(sanitized["path"])
                result.append(sanitized)

            return result
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_get_uris(self, params: list) -> list:
        """获取 URI 列表

        aria2.getUris(gid)
        """
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")

        gid = params[0]
        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            # aria2 client 没有 get_uris 方法，需要通过 _call 直接调用
            return await self.client._call("aria2.getUris", [gid])
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_get_global_stat(self, params: list) -> dict:
        """返回用户任务统计（伪全局）

        aria2.getGlobalStat()

        注意：这里返回的是用户自己的任务统计，而非真正的全局统计
        """
        # 从数据库获取用户任务统计
        active_count = fetch_one(
            "SELECT COUNT(*) as cnt FROM tasks WHERE owner_id = ? AND status = 'active'",
            [self.user_id]
        )
        waiting_count = fetch_one(
            "SELECT COUNT(*) as cnt FROM tasks WHERE owner_id = ? AND status IN ('waiting', 'paused', 'queued')",
            [self.user_id]
        )
        stopped_count = fetch_one(
            "SELECT COUNT(*) as cnt FROM tasks WHERE owner_id = ? AND status IN ('complete', 'error', 'stopped', 'removed')",
            [self.user_id]
        )

        # 获取用户活动任务的实时速度
        user_tasks = fetch_all(
            "SELECT gid FROM tasks WHERE owner_id = ? AND status = 'active' AND gid IS NOT NULL",
            [self.user_id]
        )

        download_speed = 0
        upload_speed = 0

        if user_tasks:
            try:
                all_active = await self.client.tell_active()
                user_gids = {task["gid"] for task in user_tasks}

                for status in all_active:
                    if status.get("gid") in user_gids:
                        download_speed += int(status.get("downloadSpeed", 0))
                        upload_speed += int(status.get("uploadSpeed", 0))
            except Exception:
                pass

        return {
            "downloadSpeed": str(download_speed),
            "uploadSpeed": str(upload_speed),
            "numActive": str(active_count["cnt"] if active_count else 0),
            "numWaiting": str(waiting_count["cnt"] if waiting_count else 0),
            "numStopped": str(stopped_count["cnt"] if stopped_count else 0),
            "numStoppedTotal": str(stopped_count["cnt"] if stopped_count else 0),
        }

    async def _handle_get_version(self, params: list) -> dict:
        """返回版本信息

        aria2.getVersion()
        """
        try:
            return await self.client.get_version()
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_change_position(self, params: list) -> int:
        """调整位置

        aria2.changePosition(gid, pos, how)
        """
        if len(params) < 3:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid, pos and how are required")

        gid = params[0]
        pos = params[1]
        how = params[2]

        task = self._verify_task_owner(gid)
        if not task:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")

        try:
            return await self.client.change_position(gid, pos, how)
        except Exception as exc:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

    async def _handle_system_list_methods(self, params: list) -> list:
        """返回支持的方法列表

        system.listMethods()
        """
        return self.SUPPORTED_METHODS

    async def _handle_system_multicall(self, params: list) -> list:
        """批量调用

        system.multicall(methods)

        methods: [{"methodName": "...", "params": [...]}]
        """
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "methods is required")

        methods = params[0]
        results = []

        for method_call in methods:
            if not isinstance(method_call, dict):
                results.append({"faultCode": RpcErrorCode.INVALID_PARAMS, "faultString": "Invalid method call"})
                continue

            method_name = method_call.get("methodName")
            method_params = method_call.get("params", [])

            if not method_name:
                results.append({"faultCode": RpcErrorCode.INVALID_PARAMS, "faultString": "methodName is required"})
                continue

            try:
                result = await self.handle(method_name, method_params)
                results.append([result])  # 成功时包装在数组中
            except RpcError as exc:
                results.append({"faultCode": exc.code, "faultString": exc.message})
            except Exception as exc:
                results.append({"faultCode": RpcErrorCode.INTERNAL_ERROR, "faultString": str(exc)})

        return results

    # ========== 静默处理的方法 ==========

    async def _handle_get_option(self, params: list) -> dict:
        """获取任务选项（静默返回空对象）

        aria2.getOption(gid)
        """
        # 验证任务归属
        if params and isinstance(params[0], str):
            gid = params[0]
            task = self._verify_task_owner(gid)
            if not task:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        return {}

    async def _handle_change_option(self, params: list) -> str:
        """修改任务选项（静默返回 OK）

        aria2.changeOption(gid, options)
        """
        # 验证任务归属
        if params and isinstance(params[0], str):
            gid = params[0]
            task = self._verify_task_owner(gid)
            if not task:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        return "OK"

    async def _handle_get_global_option(self, params: list) -> dict:
        """获取全局选项（静默返回空对象）

        aria2.getGlobalOption()
        """
        return {}

    async def _handle_change_global_option(self, params: list) -> str:
        """修改全局选项（静默返回 OK）

        aria2.changeGlobalOption(options)
        """
        return "OK"

    async def _handle_shutdown(self, params: list) -> str:
        """关闭 aria2（静默返回 OK，不实际执行）

        aria2.shutdown()
        """
        return "OK"

    async def _handle_force_shutdown(self, params: list) -> str:
        """强制关闭 aria2（静默返回 OK，不实际执行）

        aria2.forceShutdown()
        """
        return "OK"

    async def _handle_save_session(self, params: list) -> str:
        """保存会话（静默返回 OK）

        aria2.saveSession()
        """
        return "OK"

    async def _handle_purge_download_result(self, params: list) -> str:
        """清理下载结果（静默返回 OK）

        aria2.purgeDownloadResult()
        """
        return "OK"

    async def _handle_remove_download_result(self, params: list) -> str:
        """移除下载结果（静默返回 OK）

        aria2.removeDownloadResult(gid)
        """
        return "OK"

    async def _handle_pause_all(self, params: list) -> str:
        """暂停所有任务（静默返回 OK）

        aria2.pauseAll()
        """
        return "OK"

    async def _handle_force_pause_all(self, params: list) -> str:
        """强制暂停所有任务（静默返回 OK）

        aria2.forcePauseAll()
        """
        return "OK"

    async def _handle_unpause_all(self, params: list) -> str:
        """恢复所有任务（静默返回 OK）

        aria2.unpauseAll()
        """
        return "OK"

    async def _handle_get_session_info(self, params: list) -> dict:
        """获取会话信息（返回固定值）

        aria2.getSessionInfo()
        """
        return {"sessionId": "proxy"}
