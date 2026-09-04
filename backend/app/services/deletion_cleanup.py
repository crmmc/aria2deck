from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.status import ACTIVE_USER_TASK_STATUSES
from app.repositories import auth as auth_repo
from app.repositories import files as files_repo
from app.repositories.task import user_tasks as downloads_repo
from app.services.storage import get_store_dir, is_path_within_base
from app.services.storage_locks import (
    get_content_hash_lock,
    wait_for_content_readers_locked,
)
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.task_service import cancel_task

logger = logging.getLogger(__name__)

_BATCH_SIZE = 16
_SWEEP_SECONDS = 1.0
_FILE_LEASE_MS = 5 * 60 * 1000
_USER_LEASE_MS = 10 * 60 * 1000
_RETRY_DELAYS_MS = (1_000, 5_000, 30_000, 120_000, 600_000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _next_retry_ms(attempts: int) -> int:
    delay = _RETRY_DELAYS_MS[min(max(attempts - 1, 0), len(_RETRY_DELAYS_MS) - 1)]
    return _now_ms() + delay


def _safe_error(prefix: str, exc: BaseException) -> str:
    return f"{prefix}：{type(exc).__name__}"[:1000]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_tree_cancellable(path: Path, cancel_event: threading.Event) -> None:
    stack: list[tuple[Path, bool]] = [(path, False)]
    while stack:
        if cancel_event.is_set():
            raise InterruptedError("清理任务已取消")
        current, visited = stack.pop()
        if not current.exists() and not current.is_symlink():
            continue
        if current.is_symlink() or not current.is_dir():
            current.unlink()
            continue
        if visited:
            current.rmdir()
            continue
        stack.append((current, True))
        with os.scandir(current) as entries:
            children = [Path(entry.path) for entry in entries]
        stack.extend((child, False) for child in children)


def _delete_stored_path(
    row: dict[str, Any], cancel_event: threading.Event
) -> None:
    store_dir = get_store_dir().resolve()
    path = Path(str(row["real_path"]))
    if not is_path_within_base(store_dir, path) or path.resolve(strict=False) == store_dir:
        raise ValueError("存储路径超出允许范围")
    tombstone = path.parent / f".{path.name}.aria2deck-delete-{int(row['id'])}"
    path_exists = path.exists() or path.is_symlink()
    tombstone_exists = tombstone.exists() or tombstone.is_symlink()
    if path_exists and tombstone_exists:
        raise FileExistsError("清理暂存路径冲突")
    if path_exists:
        os.replace(path, tombstone)
        _fsync_directory(path.parent)
        tombstone_exists = True
    if tombstone_exists:
        _remove_tree_cancellable(tombstone, cancel_event)
        _fsync_directory(tombstone.parent)
    elif path.parent.exists():
        _fsync_directory(path.parent)


class DeletionCleanupManager:
    _worker_task: asyncio.Task[None] | None = None
    _wake_event: asyncio.Event | None = None

    @classmethod
    async def recover_startup(cls) -> None:
        from app.modules.pack import PackTaskManager

        for user_id in await auth_repo.list_pending_user_ids():
            await PackTaskManager.cancel_user_jobs(user_id)

    @classmethod
    async def start(cls) -> None:
        task = cls._worker_task
        if task is not None and not task.done():
            return
        cls._wake_event = asyncio.Event()
        task = asyncio.create_task(cls._worker_loop(), name="deletion-cleanup")
        cls._worker_task = task
        task.add_done_callback(cls._consume_worker_done)

    @classmethod
    def wake(cls) -> None:
        event = cls._wake_event
        if event is not None:
            event.set()

    @classmethod
    async def shutdown(cls) -> None:
        task = cls._worker_task
        cls._worker_task = None
        if task is None:
            cls._wake_event = None
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cls._wake_event = None

    @classmethod
    def _consume_worker_done(cls, task: asyncio.Task[None]) -> None:
        if cls._worker_task is task:
            cls._worker_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("持久删除 worker 异常退出")

    @classmethod
    async def _worker_loop(cls) -> None:
        while True:
            try:
                processed = await cls.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                processed = 0
                logger.exception("持久删除扫描失败，将继续重试")
            if processed:
                await asyncio.sleep(0)
                continue
            event = cls._wake_event
            if event is None:
                return
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=_SWEEP_SECONDS)
            except TimeoutError:
                pass

    @classmethod
    async def run_once(cls) -> int:
        timestamp = _now_ms()
        user_token = uuid4().hex
        users = await auth_repo.claim_due_users(
            lease_token=user_token,
            timestamp_ms=timestamp,
            lease_expires_at_ms=timestamp + _USER_LEASE_MS,
            limit=_BATCH_SIZE,
        )
        for row in users:
            await cls._process_user(row, user_token)

        file_token = uuid4().hex
        files = await files_repo.claim_due_stored_files(
            lease_token=file_token,
            timestamp_ms=_now_ms(),
            lease_expires_at_ms=_now_ms() + _FILE_LEASE_MS,
            limit=_BATCH_SIZE,
        )
        for row in files:
            await cls._process_file(row, file_token)
        return len(users) + len(files)

    @classmethod
    async def _process_file(cls, row: dict[str, Any], lease_token: str) -> None:
        content_lock = await get_content_hash_lock(str(row["content_hash"]))
        async with content_lock:
            await wait_for_content_readers_locked(str(row["content_hash"]))
            cancel_event = threading.Event()
            operation = asyncio.create_task(
                asyncio.to_thread(_delete_stored_path, row, cancel_event)
            )
            try:
                await asyncio.shield(operation)
                finalized = await files_repo.hard_delete_claimed_stored_file(
                    int(row["id"]), lease_token
                )
                if not finalized:
                    logger.warning(
                        "物理文件已清理但 DB finalize 待重试 stored_file_id=%s",
                        row["id"],
                    )
            except asyncio.CancelledError:
                cancel_event.set()
                await asyncio.gather(operation, return_exceptions=True)
                raise
            except Exception as exc:
                error = _safe_error("物理清理失败", exc)
                await files_repo.retry_claimed_stored_file_delete(
                    stored_file_id=int(row["id"]),
                    lease_token=lease_token,
                    next_retry_at_ms=_next_retry_ms(int(row["delete_attempts"])),
                    error=error,
                )
                logger.exception(
                    "存储文件物理清理失败，将重试: stored_file_id=%s", row["id"]
                )

    @classmethod
    async def _process_user(cls, row: dict[str, Any], lease_token: str) -> None:
        user_id = int(row["id"])
        try:
            completed = await cls._cleanup_user(user_id, lease_token, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await auth_repo.retry_claimed_user_delete(
                user_id=user_id,
                lease_token=lease_token,
                next_retry_at_ms=_next_retry_ms(int(row["delete_attempts"])),
                error=_safe_error("用户清理失败", exc),
            )
            logger.exception("用户持久清理失败，将重试: user_id=%s", user_id)
            return
        if not completed:
            await auth_repo.retry_claimed_user_delete(
                user_id=user_id,
                lease_token=lease_token,
                next_retry_at_ms=_now_ms() + 250,
                error=None,
            )

    @staticmethod
    async def _renew_user_lease(user_id: int, lease_token: str) -> None:
        renewed = await auth_repo.renew_claimed_user_delete(
            user_id=user_id,
            lease_token=lease_token,
            lease_expires_at_ms=_now_ms() + _USER_LEASE_MS,
        )
        if not renewed:
            raise RuntimeError("用户删除 lease 已失效")

    @classmethod
    async def _cleanup_user(
        cls,
        user_id: int,
        lease_token: str,
        claimed_row: dict[str, Any],
    ) -> bool:
        from app.modules.pack import PackTaskManager

        await cls._renew_user_lease(user_id, lease_token)
        if not await PackTaskManager.prepare_user_deletion(user_id):
            return False
        await cls._renew_user_lease(user_id, lease_token)

        tasks = await downloads_repo.list_user_tasks(
            user_id,
            statuses=ACTIVE_USER_TASK_STATUSES,
            include_pending_user=True,
        )
        for task in tasks:
            await cancel_task(
                user_id=user_id,
                user_task_id=int(task["id"]),
                quota_bytes=int(claimed_row["quota_bytes"]),
                tolerate_backend_failure=True,
            )
            await broadcast_task_update_to_subscribers(
                int(task["global_download_id"])
            )
        if await downloads_repo.list_user_tasks(
            user_id,
            statuses=ACTIVE_USER_TASK_STATUSES,
            include_pending_user=True,
        ):
            return False
        await auth_repo.delete_terminal_user_tasks_for_cleanup(user_id)
        await cls._renew_user_lease(user_id, lease_token)

        file_ids = await files_repo.list_pending_user_file_ids(
            user_id, limit=_BATCH_SIZE
        )
        for user_file_id in file_ids:
            identity = await files_repo.get_pending_user_file_delete_identity(
                user_id, user_file_id
            )
            if identity is None:
                continue
            content_lock = await get_content_hash_lock(str(identity["content_hash"]))
            async with content_lock:
                deleted, download_ids, _cleanup_path = (
                    await files_repo.delete_user_file_reference(
                        user_id,
                        user_file_id,
                        expected_stored_file_id=int(identity["stored_file_id"]),
                        expected_created_at_ms=int(identity["created_at_ms"]),
                        cleanup_pending_user=True,
                    )
                )
            if not deleted:
                return False
            for download_id in download_ids:
                await broadcast_task_update_to_subscribers(download_id)
        if await files_repo.list_pending_user_file_ids(user_id, limit=1):
            return False

        await auth_repo.delete_terminal_user_tasks_for_cleanup(user_id)
        if not await auth_repo.hard_delete_claimed_user(user_id, lease_token):
            return False
        from app.services.task_broadcast import remove_connections_for_user

        try:
            await remove_connections_for_user(user_id)
        finally:
            await PackTaskManager.unblock_user(user_id)
        logger.info("用户持久清理完成: user_id=%s", user_id)
        return True
