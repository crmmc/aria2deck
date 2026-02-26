"""aria2 RPC 方法处理器

为外部 aria2 兼容客户端（如 AriaNg、Motrix）提供 RPC 方法实现。
实现用户隔离、数据脱敏、配额检查等安全机制。

基于共享下载架构（DownloadTask + UserTaskSubscription）。
"""
from __future__ import annotations
import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.exc import IntegrityError
from sqlmodel import select, func, col, update

from app.aria2.client import Aria2Client
from app.core.config import settings
from app.core.state import AppState
from app.database import get_session
from app.models import DownloadTask, TaskHistory, User, UserTaskSubscription, utc_now_str
from app.routers.config import get_min_free_disk
from app.services.hash import extract_info_hash_from_torrent_base64, get_uri_hash
from app.services.storage import get_user_space_info


logger = logging.getLogger(__name__)


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
        "aria2.getPeers",
        "aria2.getServers",
        "aria2.changeUri",
        "system.listMethods",
        "system.multicall",
    ]

    def __init__(self, user_id: int, aria2_client: Aria2Client, app_state: AppState):
        self.user_id = user_id
        self.client = aria2_client
        if app_state is None:
            raise RuntimeError("AppState is required for Aria2RpcHandler")
        self.app_state: AppState = app_state
        self._user_dir: str | None = None
        self._user_incomplete_dir: str | None = None

    async def handle(self, method: str, params: list) -> Any:
        """路由到具体方法处理"""
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
    async def _verify_task_owner(self, gid: str) -> tuple[DownloadTask, UserTaskSubscription] | None:
        """检查 gid 对应的任务是否属于当前用户
        Returns: (DownloadTask, UserTaskSubscription) 或 None
        """
        async with get_session() as db:
            result = await db.exec(
                select(DownloadTask, UserTaskSubscription)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    DownloadTask.gid == gid,
                    UserTaskSubscription.owner_id == self.user_id,
                )
            )
            return result.first()
    async def _get_user_gids(self, sub_statuses: list[str] | None = None, task_statuses: list[str] | None = None) -> set[str]:
        """获取用户指定状态的任务 gid 集合"""
        async with get_session() as db:
            stmt = (
                select(DownloadTask.gid)
                .join(UserTaskSubscription, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    col(DownloadTask.gid).is_not(None),
                )
            )
            if sub_statuses:
                stmt = stmt.where(col(UserTaskSubscription.status).in_(sub_statuses))
            if task_statuses:
                stmt = stmt.where(col(DownloadTask.status).in_(task_statuses))
            result = await db.exec(stmt)
            return self._normalize_gid_collection(result.all())

    @staticmethod
    def _extract_scalar_value(value: Any) -> Any:
        scalar: Any = value
        if isinstance(scalar, (tuple, list)):
            if not scalar:
                return None
            return scalar[0]

        mapping = getattr(scalar, "_mapping", None)
        if mapping:
            try:
                return next(iter(mapping.values()))
            except StopIteration:
                return None
        return scalar

    @classmethod
    def _to_int_scalar(cls, value: Any, default: int = 0) -> int:
        scalar = cls._extract_scalar_value(value)
        if scalar is None:
            return default
        try:
            return int(scalar)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_gid_collection(values: Any) -> set[str]:
        gids: set[str] = set()
        for raw in values:
            value = Aria2RpcHandler._extract_scalar_value(raw)

            if value is None:
                continue

            gid = str(value)
            if gid:
                gids.add(gid)
        return gids
    def _sanitize_path(self, path: str) -> str:
        """将服务器绝对路径转为用户相对路径"""
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

    def _sanitize_file_path(self, path: str) -> str:
        safe_path = self._sanitize_path(path)
        if not safe_path:
            return safe_path
        return Path(safe_path).name

    def _sanitize_status(self, status: dict) -> dict:
        """对 tellStatus 返回的数据进行脱敏处理"""
        result: dict[str, Any] = {}

        allowed_top_level = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "uploadLength",
            "downloadSpeed",
            "uploadSpeed",
            "errorCode",
            "errorMessage",
            "files",
        ]

        for key in allowed_top_level:
            if key in status:
                result[key] = status[key]

        if "uploadLength" not in result:
            result["uploadLength"] = "0"

        if "files" in result and isinstance(result["files"], list):
            sanitized_files = []
            for f in result["files"]:
                if not isinstance(f, dict):
                    continue
                sanitized_file = {}
                if "index" in f:
                    sanitized_file["index"] = f["index"]
                if "length" in f:
                    sanitized_file["length"] = f["length"]
                if "completedLength" in f:
                    sanitized_file["completedLength"] = f["completedLength"]
                if "selected" in f:
                    sanitized_file["selected"] = f["selected"]
                if "path" in f:
                    sanitized_file["path"] = self._sanitize_file_path(f["path"])
                sanitized_file["uris"] = []
                sanitized_files.append(sanitized_file)
            result["files"] = sanitized_files
        else:
            result["files"] = []

        bt_info = status.get("bittorrent")
        if isinstance(bt_info, dict):
            sanitized_bt: dict[str, Any] = {"announceList": []}
            info_dict = bt_info.get("info")
            if isinstance(info_dict, dict):
                name = info_dict.get("name")
                if isinstance(name, str) and name:
                    sanitized_bt["info"] = {"name": name}
            result["bittorrent"] = sanitized_bt

        return result

    @staticmethod
    def _build_history_gid(task: DownloadTask) -> str:
        if task.gid:
            return task.gid
        if task.id is not None:
            return f"task-{task.id}"
        return ""

    @staticmethod
    def _parse_history_gid(gid: str) -> tuple[str | None, int | None, int | None]:
        if gid.startswith("hist-"):
            suffix = gid[5:]
            if suffix.isdigit():
                return None, None, int(suffix)
        if gid.startswith("task-"):
            suffix = gid[5:]
            if suffix.isdigit():
                return None, int(suffix), None
        return gid, None, None

    @staticmethod
    def _build_status_from_history(history: TaskHistory) -> dict:
        if history.result == "completed":
            status = "complete"
            error_message = ""
        else:
            status = "error"
            error_message = history.reason or ""

        gid = f"hist-{history.id}" if history.id is not None else ""
        return {
            "gid": gid,
            "status": status,
            "totalLength": str(history.total_length or 0),
            "completedLength": str(history.total_length or 0),
            "uploadLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "errorCode": "0",
            "errorMessage": error_message,
            "files": [],
        }

    async def _get_user_available_space(self) -> int:
        """获取用户实际可用空间"""
        async with get_session() as db:
            result = await db.exec(select(User.quota).where(User.id == self.user_id))
            user_quota = self._to_int_scalar(result.first(), default=0)
        if user_quota <= 0:
            return 0
        space_info = await get_user_space_info(self.user_id, user_quota)
        return space_info["available"]
    def _check_disk_space(self) -> tuple[bool, int]:
        """检查磁盘空间是否足够"""
        download_path = Path(settings.download_dir)
        download_path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(download_path)
        min_free = get_min_free_disk()
        return disk.free > min_free, disk.free

    async def _get_task_submit_lock(self, task_id: int) -> asyncio.Lock:
        async with self.app_state.lock:
            lock = self.app_state.task_submit_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self.app_state.task_submit_locks[task_id] = lock
            return lock

    @staticmethod
    def _normalize_pagination(params: list, default_num: int = 1000) -> tuple[int, int]:
        offset = params[0] if params else 0
        num = params[1] if len(params) > 1 else default_num
        if type(offset) is not int or type(num) is not int:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "offset and num must be integers")
        if num < 0:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "num must be non-negative")
        return offset, num

    @staticmethod
    def _slice_with_offset(items: Sequence[Any], offset: int, num: int) -> list[Any]:
        items_list = list(items)
        if num == 0:
            return []
        if offset >= 0:
            return items_list[offset: offset + num]

        start = len(items_list) + offset
        if start < 0:
            return []

        result: list[Any] = []
        idx = start
        while idx >= 0 and len(result) < num:
            result.append(items_list[idx])
            idx -= 1
        return result

    async def _fetch_waiting_tasks(self, max_items: int = 10000, page_size: int = 1000) -> list[dict]:
        offset = 0
        all_waiting: list[dict] = []
        while offset < max_items:
            batch_limit = min(page_size, max_items - offset)
            batch = await self.client.tell_waiting(offset, batch_limit)
            if not batch:
                break
            all_waiting.extend(batch)
            if len(batch) < batch_limit:
                break
            offset += len(batch)
        return all_waiting

    @staticmethod
    def _strip_rpc_token(params: Any) -> list:
        if not isinstance(params, list):
            return []
        if params and isinstance(params[0], str) and params[0].startswith("token:"):
            return params[1:]
        return params

    async def _find_or_create_task(
        self,
        uri_hash: str,
        uri: str,
        name: str,
    ) -> tuple[DownloadTask, bool]:
        async with get_session() as db:
            result = await db.exec(select(DownloadTask).where(DownloadTask.uri_hash == uri_hash))
            existing = result.first()
            if existing:
                return existing, False

            task = DownloadTask(
                uri_hash=uri_hash,
                uri=uri,
                name=name,
                status="queued",
            )
            db.add(task)
            try:
                await db.commit()
                await db.refresh(task)
                return task, True
            except IntegrityError:
                await db.rollback()
                result = await db.exec(select(DownloadTask).where(DownloadTask.uri_hash == uri_hash))
                existing = result.first()
                if existing:
                    return existing, False
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Failed to create task")

    async def _create_or_get_subscription(self, task_id: int) -> UserTaskSubscription:
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.task_id == task_id,
                )
            )
            existing = result.first()
            if existing:
                if existing.status != "pending":
                    existing.status = "pending"
                    existing.error_display = None
                    db.add(existing)
                    await db.commit()
                    await db.refresh(existing)
                return existing

            sub = UserTaskSubscription(owner_id=self.user_id, task_id=task_id, status="pending")
            db.add(sub)
            try:
                await db.commit()
                await db.refresh(sub)
                return sub
            except IntegrityError:
                await db.rollback()
                result = await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.owner_id == self.user_id,
                        UserTaskSubscription.task_id == task_id,
                    )
                )
                existing = result.first()
                if existing:
                    if existing.status != "pending":
                        existing.status = "pending"
                        existing.error_display = None
                        db.add(existing)
                        await db.commit()
                        await db.refresh(existing)
                    return existing
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Failed to create subscription")

    async def _cleanup_aria2_gid(self, gid: str) -> None:
        try:
            await self.client.force_remove(gid)
        except Exception:
            logger.warning("Failed to force remove aria2 task %s during compensation", gid)
        try:
            await self.client.remove_download_result(gid)
        except Exception:
            logger.warning("Failed to remove aria2 result %s during compensation", gid)

    async def _mark_submit_failed(self, task_id: int, message: str) -> None:
        async with get_session() as db:
            task = await db.get(DownloadTask, task_id)
            if task and task.status in ("queued", "active", "waiting"):
                task.status = "error"
                task.error_display = message
                task.updated_at = utc_now_str()
                db.add(task)

            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.task_id == task_id,
                    UserTaskSubscription.status == "pending",
                )
            )
            sub = result.first()
            if sub:
                sub.status = "failed"
                sub.error_display = message
                db.add(sub)
    async def _check_quota_and_disk(self) -> None:
        """检查配额和磁盘空间，不足则抛异常"""
        disk_ok, disk_free = self._check_disk_space()
        if not disk_ok:
            raise RpcError(
                RpcErrorCode.QUOTA_EXCEEDED,
                f"Disk space not enough, free: {disk_free / 1024 / 1024 / 1024:.2f} GB"
            )
        user_available = await self._get_user_available_space()
        if user_available <= 0:
            raise RpcError(RpcErrorCode.QUOTA_EXCEEDED, "Your quota has been exceeded")
    # ========== 完整实现的方法 ==========
    async def _handle_add_uri(self, params: list) -> str:
        """aria2.addUri(uris[, options[, position]])"""
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris is required")
        uris = params[0]
        if not uris:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris list is empty")
        options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
        # 获取用户空间锁
        from app.core.state import get_user_space_lock
        user_lock = await get_user_space_lock(self.app_state, self.user_id)
        async with user_lock:
            await self._check_quota_and_disk()
            options["dir"] = self._get_user_incomplete_dir()
            uri = uris[0] if uris else ""
            uri_hash = get_uri_hash(uri) or hashlib.sha256(uri.encode()).hexdigest()
            task_name = uri.split("/")[-1] or uri

            task, _ = await self._find_or_create_task(uri_hash=uri_hash, uri=uri, name=task_name)
            task_id = task.id
            if task_id is None:
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task id missing")

            await self._create_or_get_subscription(task_id)

            if task.gid and task.status in ("active", "queued", "waiting"):
                return task.gid

            task_lock = await self._get_task_submit_lock(task_id)
            async with task_lock:
                async with get_session() as db:
                    db_task = await db.get(DownloadTask, task_id)
                    if not db_task:
                        raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                    if db_task.gid and db_task.status in ("active", "queued", "waiting"):
                        return db_task.gid

                try:
                    gid = await self.client.add_uri(uris, options)
                except Exception as exc:
                    await self._mark_submit_failed(task_id, str(exc))
                    raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

                try:
                    async with get_session() as db:
                        db_task = await db.get(DownloadTask, task_id)
                        if not db_task:
                            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                        db_task.gid = gid
                        db_task.uri = uri
                        db_task.name = task_name
                        db_task.status = "active"
                        db.add(db_task)
                except Exception:
                    await self._cleanup_aria2_gid(gid)
                    raise

                return gid
    async def _handle_add_torrent(self, params: list) -> str:
        """aria2.addTorrent(torrent[, uris[, options[, position]]])"""
        if not params or not isinstance(params[0], str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "torrent data is required")
        torrent_data = params[0]
        # 限制 torrent 文件大小（10MB）
        if len(torrent_data) > 10 * 1024 * 1024:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "Torrent data too large")
        uris = params[1] if len(params) > 1 and isinstance(params[1], list) else []
        options = params[2] if len(params) > 2 and isinstance(params[2], dict) else {}
        from app.core.state import get_user_space_lock
        user_lock = await get_user_space_lock(self.app_state, self.user_id)
        async with user_lock:
            await self._check_quota_and_disk()
            options["dir"] = self._get_user_incomplete_dir()
            info_hash = extract_info_hash_from_torrent_base64(torrent_data)
            uri_hash = info_hash or hashlib.sha256(torrent_data.encode()).hexdigest()
            task_uri = f"torrent:{uri_hash}"
            task_name = f"torrent-{uri_hash[:12]}"

            task, _ = await self._find_or_create_task(uri_hash=uri_hash, uri=task_uri, name=task_name)
            task_id = task.id
            if task_id is None:
                raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task id missing")

            await self._create_or_get_subscription(task_id)

            if task.gid and task.status in ("active", "queued", "waiting"):
                return task.gid

            task_lock = await self._get_task_submit_lock(task_id)
            async with task_lock:
                async with get_session() as db:
                    db_task = await db.get(DownloadTask, task_id)
                    if not db_task:
                        raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                    if db_task.gid and db_task.status in ("active", "queued", "waiting"):
                        return db_task.gid

                try:
                    gid = await self.client.add_torrent(torrent_data, uris, options)
                except Exception as exc:
                    await self._mark_submit_failed(task_id, str(exc))
                    raise RpcError(RpcErrorCode.INTERNAL_ERROR, str(exc))

                try:
                    name = task_name
                    try:
                        status = await self.client.tell_status(gid)
                        bt_info = status.get("bittorrent", {})
                        name = bt_info.get("info", {}).get("name", "") or name
                    except Exception as exc:
                        logger.debug(
                            "Failed to fetch bittorrent metadata for gid=%s user_id=%s",
                            gid,
                            self.user_id,
                            exc_info=exc,
                        )

                    async with get_session() as db:
                        db_task = await db.get(DownloadTask, task_id)
                        if not db_task:
                            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task not found after add")
                        db_task.gid = gid
                        db_task.uri = task_uri
                        db_task.name = name
                        db_task.status = "active"
                        db.add(db_task)
                except Exception:
                    await self._cleanup_aria2_gid(gid)
                    raise

                return gid
    async def _handle_remove(self, params: list) -> str:
        """aria2.remove(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = params[0]
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        task, sub = pair
        if sub.status != "pending":
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        is_active_cancel = task.status in ("queued", "active")
        if sub.id is None:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Subscription id missing")
        if task.id is None:
            raise RpcError(RpcErrorCode.INTERNAL_ERROR, "Task id missing")

        task_id = task.id
        remaining_count = 0
        async with get_session() as db:
            db_sub = await db.get(UserTaskSubscription, sub.id)
            if db_sub:
                await db.delete(db_sub)

            result = await db.exec(
                select(func.count()).select_from(UserTaskSubscription).where(
                    UserTaskSubscription.task_id == task_id,
                    UserTaskSubscription.status == "pending",
                )
            )
            remaining_count = self._to_int_scalar(result.one(), default=0)

            if remaining_count == 0:
                await db.execute(
                    update(DownloadTask)
                    .where(
                        col(DownloadTask.id) == task_id,
                        col(DownloadTask.status).in_(["queued", "active", "error"]),
                    )
                    .values(status="error", error_display="已取消")
                )

        # 写历史记录
        if is_active_cancel:
            from app.services.history import add_task_history
            await add_task_history(
                owner_id=self.user_id,
                task_name=task.name or task.uri or "",
                result="cancelled",
                reason="用户取消",
                uri=task.uri,
                total_length=task.total_length,
                created_at=sub.created_at,
            )

        if remaining_count == 0:
            lock = await self._get_task_submit_lock(task_id)
            async with lock:
                async with get_session() as db:
                    result = await db.exec(
                        select(func.count()).select_from(UserTaskSubscription).where(
                            UserTaskSubscription.task_id == task_id,
                            UserTaskSubscription.status == "pending",
                        )
                    )
                    still_pending = self._to_int_scalar(result.one(), default=0)
                    if still_pending != 0:
                        return gid

                    db_task = await db.get(DownloadTask, task_id)

                if db_task and db_task.gid and db_task.status in ("queued", "active", "error"):
                    await self._cleanup_aria2_gid(db_task.gid)

        return gid
    async def _handle_force_remove(self, params: list) -> str:
        """aria2.forceRemove(gid) - 同 remove"""
        return await self._handle_remove(params)
    async def _handle_tell_status(self, params: list) -> dict:
        """aria2.tellStatus(gid[, keys])"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = params[0]
        _ = params[1] if len(params) > 1 and isinstance(params[1], list) else None  # keys ignored
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        task, sub = pair
        # 尝试从 aria2 获取实时数据
        try:
            status = await self.client.tell_status(gid)
            return self._sanitize_status(status)
        except Exception as exc:
            # aria2 中已不存在（已完成/失败），从 DB 构造
            logger.debug(
                "Fallback to DB status for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return self._build_status_from_db(task, sub)
    def _build_status_from_db(self, task: DownloadTask, sub: UserTaskSubscription) -> dict:
        """从数据库记录构造 aria2 tellStatus 格式的响应"""
        # 状态映射
        status_map = {
            "success": "complete",
            "failed": "error",
            "active": "active",
            "queued": "waiting",
            "waiting": "waiting",
        }
        aria2_status = status_map.get(sub.status, status_map.get(task.status, "error"))
        return {
            "gid": self._build_history_gid(task),
            "status": aria2_status,
            "totalLength": str(task.total_length or 0),
            "completedLength": str(task.completed_length or 0),
            "uploadLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "errorCode": "0",
            "errorMessage": task.error or task.error_display or "",
            "files": [],
        }
    async def _handle_tell_active(self, params: list) -> list:
        """aria2.tellActive([keys])"""
        _ = params[0] if params and isinstance(params[0], list) else None  # keys ignored
        user_gids = await self._get_user_gids(sub_statuses=["pending"])
        if not user_gids:
            return []
        try:
            all_active = await self.client.tell_active()
        except Exception as exc:
            logger.warning(
                "aria2.tellActive failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
            return []
        return [self._sanitize_status(t) for t in all_active if t.get("gid") in user_gids]
    async def _handle_tell_waiting(self, params: list) -> list:
        """aria2.tellWaiting(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)
        _ = params[2] if len(params) > 2 and isinstance(params[2], list) else None  # keys ignored
        user_gids = await self._get_user_gids(sub_statuses=["pending"])
        if not user_gids:
            return []
        try:
            all_waiting = await self._fetch_waiting_tasks()
        except Exception as exc:
            logger.warning(
                "aria2.tellWaiting failed for user_id=%s",
                self.user_id,
                exc_info=exc,
            )
            return []

        filtered = [self._sanitize_status(t) for t in all_waiting if t.get("gid") in user_gids]
        return self._slice_with_offset(filtered, offset, num)
    async def _handle_tell_stopped(self, params: list) -> list:
        """aria2.tellStopped(offset, num[, keys])"""
        offset, num = self._normalize_pagination(params)

        async with get_session() as db:
            stmt = (
                select(TaskHistory)
                .where(
                    TaskHistory.owner_id == self.user_id,
                )
                .order_by(col(TaskHistory.id).asc())
            )
            result = await db.exec(stmt)
            rows = result.all()

        sliced_rows = self._slice_with_offset(rows, offset, num)
        return [self._build_status_from_history(history) for history in sliced_rows]
    async def _handle_get_global_stat(self, params: list) -> dict:
        """aria2.getGlobalStat()"""
        # 用户级别统计
        async with get_session() as db:
            # active: subscription pending + task active
            r_active = await db.exec(
                select(func.count()).select_from(UserTaskSubscription)
                .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.status == "pending",
                    DownloadTask.status == "active",
                )
            )
            num_active = self._to_int_scalar(r_active.one(), default=0)
            # waiting: subscription pending + task queued/waiting
            r_waiting = await db.exec(
                select(func.count()).select_from(UserTaskSubscription)
                .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    UserTaskSubscription.status == "pending",
                    col(DownloadTask.status).in_(["queued", "waiting"]),
                )
            )
            num_waiting = self._to_int_scalar(r_waiting.one(), default=0)
            # stopped
            r_stopped = await db.exec(
                select(func.count()).select_from(TaskHistory).where(
                    TaskHistory.owner_id == self.user_id,
                )
            )
            num_stopped = self._to_int_scalar(r_stopped.one(), default=0)
        # 获取全局速度
        try:
            global_stat = await self.client.get_global_stat()
            download_speed = global_stat.get("downloadSpeed", "0")
            upload_speed = global_stat.get("uploadSpeed", "0")
        except Exception as exc:
            logger.warning(
                "aria2.getGlobalStat failed for user_id=%s, fallback to zero speed",
                self.user_id,
                exc_info=exc,
            )
            download_speed = "0"
            upload_speed = "0"
        return {
            "downloadSpeed": download_speed,
            "uploadSpeed": upload_speed,
            "numActive": str(num_active),
            "numWaiting": str(num_waiting),
            "numStopped": str(num_stopped),
            "numStoppedTotal": str(num_stopped),
        }
    async def _handle_get_files(self, params: list) -> list:
        """aria2.getFiles(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = params[0]
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        try:
            files = await self.client.get_files(gid)
            for f in files:
                if "path" in f:
                    f["path"] = self._sanitize_file_path(f["path"])
                f["uris"] = []
            return files
        except Exception as exc:
            logger.warning(
                "aria2.getFiles failed for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return []
    async def _handle_get_uris(self, params: list) -> list:
        """aria2.getUris(gid)"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid = params[0]
        pair = await self._verify_task_owner(gid)
        if not pair:
            raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
        try:
            return await self.client.get_uris(gid)
        except Exception as exc:
            logger.warning(
                "aria2.getUris failed for gid=%s user_id=%s",
                gid,
                self.user_id,
                exc_info=exc,
            )
            return []
    async def _handle_get_version(self, params: list) -> dict:
        """aria2.getVersion()"""
        return await self.client.get_version()
    async def _handle_remove_download_result(self, params: list) -> str:
        """aria2.removeDownloadResult(gid) - 删除用户的 stopped 订阅（历史记录）"""
        if not params:
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
        gid_param = params[0]
        if not isinstance(gid_param, str):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid must be a string")
        gid, task_id, history_id = self._parse_history_gid(gid_param)
        async with get_session() as db:
            if history_id is not None:
                history = await db.get(TaskHistory, history_id)
                if history is None or history.owner_id != self.user_id:
                    raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")
                await db.delete(history)
                await db.commit()
                return "OK"

            stmt = (
                select(UserTaskSubscription)
                .join(DownloadTask, UserTaskSubscription.task_id == DownloadTask.id)  # type: ignore[arg-type]
                .where(
                    UserTaskSubscription.owner_id == self.user_id,
                    col(UserTaskSubscription.status).in_(["success", "failed"]),
                    col(DownloadTask.status).in_(["complete", "error"]),
                )
            )
            if task_id is not None:
                stmt = stmt.where(DownloadTask.id == task_id)
            else:
                stmt = stmt.where(DownloadTask.gid == gid)

            result = await db.exec(stmt)
            sub = result.first()
            if not sub:
                raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid_param}")

            task = await db.get(DownloadTask, sub.task_id)
            await db.delete(sub)

            if task is not None:
                history_stmt = (
                    select(TaskHistory)
                    .where(
                        TaskHistory.owner_id == self.user_id,
                        TaskHistory.uri == task.uri,
                    )
                    .order_by(col(TaskHistory.id).desc())
                )
                history = (await db.exec(history_stmt)).first()
                if history is not None:
                    await db.delete(history)

            await db.commit()
        return "OK"
    async def _handle_purge_download_result(self, params: list) -> str:
        """aria2.purgeDownloadResult() - 删除用户所有 stopped 订阅"""
        async with get_session() as db:
            result = await db.exec(
                select(UserTaskSubscription).where(
                    UserTaskSubscription.owner_id == self.user_id,
                    col(UserTaskSubscription.status).in_(["success", "failed"]),
                )
            )
            subs = result.all()
            for sub in subs:
                await db.delete(sub)

            history_result = await db.exec(
                select(TaskHistory).where(TaskHistory.owner_id == self.user_id)
            )
            for history in history_result.all():
                await db.delete(history)

            await db.commit()
        return "OK"
    # ========== 静默返回的方法（不支持但不报错） ==========
    async def _handle_pause(self, params: list) -> str:
        """aria2.pause - 不支持，静默返回 gid"""
        return params[0] if params else "0"
    async def _handle_force_pause(self, params: list) -> str:
        return params[0] if params else "0"
    async def _handle_unpause(self, params: list) -> str:
        return params[0] if params else "0"
    async def _handle_pause_all(self, params: list) -> str:
        return "OK"
    async def _handle_force_pause_all(self, params: list) -> str:
        return "OK"
    async def _handle_unpause_all(self, params: list) -> str:
        return "OK"
    async def _handle_get_option(self, params: list) -> dict:
        return {}
    async def _handle_change_option(self, params: list) -> str:
        return "OK"
    async def _handle_get_global_option(self, params: list) -> dict:
        return {}
    async def _handle_change_global_option(self, params: list) -> str:
        return "OK"
    async def _handle_change_position(self, params: list) -> int:
        return 0
    async def _handle_change_uri(self, params: list) -> list:
        return [0, 0]
    async def _handle_get_peers(self, params: list) -> list:
        return []
    async def _handle_get_servers(self, params: list) -> list:
        return []
    async def _handle_shutdown(self, params: list) -> str:
        return "OK"
    async def _handle_force_shutdown(self, params: list) -> str:
        return "OK"
    async def _handle_save_session(self, params: list) -> str:
        return "OK"
    async def _handle_get_session_info(self, params: list) -> dict:
        return {"sessionId": "aria2deck-proxy-session"}
    # ========== system 方法 ==========
    async def _handle_system_list_methods(self, params: list) -> list:
        """system.listMethods()"""
        return self.SUPPORTED_METHODS
    async def _handle_system_multicall(self, params: list) -> list:
        """system.multicall(methods)"""
        if not params or not isinstance(params[0], list):
            raise RpcError(RpcErrorCode.INVALID_PARAMS, "methods is required")
        methods = params[0]
        results = []
        for call in methods:
            if not isinstance(call, dict):
                results.append({"faultCode": RpcErrorCode.INVALID_PARAMS, "faultString": "Invalid method call"})
                continue
            method_name = call.get("methodName", "")
            method_params = self._strip_rpc_token(call.get("params", []))
            try:
                result = await self.handle(method_name, method_params)
                results.append([result])
            except RpcError as e:
                results.append({"faultCode": e.code, "faultString": e.message})
            except Exception as e:
                results.append({"faultCode": RpcErrorCode.INTERNAL_ERROR, "faultString": str(e)})
        return results
