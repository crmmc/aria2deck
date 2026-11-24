from __future__ import annotations

import logging
from pathlib import Path

from app.core.time_utils import ms_to_iso
from app.domain.errors import NotFoundError
from app.repositories import storage as storage_repo
from app.services.storage_locks import get_content_hash_lock
from app.services.task_broadcast import broadcast_task_update_to_subscribers

logger = logging.getLogger(__name__)


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


async def list_stored_files(
    search: str,
    orphan_only: bool,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total, rows = await storage_repo.list_stored_files(
        search,
        orphan_only,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    files = [
        {
            "id": int(row["id"]),
            "content_hash": row["content_hash"],
            "original_name": row["original_name"],
            "size": int(row["size_bytes"]),
            "is_directory": bool(row["is_directory"]),
            "ref_count": int(row["ref_count"] or 0),
            "created_at": ms_to_iso(row["created_at_ms"]) or "",
            "real_path": row["real_path"],
            "exists_on_disk": Path(row["real_path"]).exists(),
            "cleanup_state": "pending" if row.get("pending_delete") else "active",
            "cleanup_attempts": int(row.get("delete_attempts") or 0),
            "cleanup_next_retry_at": ms_to_iso(row.get("delete_next_retry_at_ms")),
            "cleanup_error": row.get("delete_error"),
        }
        for row in rows
    ]
    return {
        "files": files,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def get_file_users(file_id: int) -> dict:
    if not await storage_repo.stored_file_exists(file_id):
        raise NotFoundError(f"存储文件不存在: {file_id}")
    rows = await storage_repo.list_file_users(file_id)
    return {
        "file_id": file_id,
        "users": [
            {
                "user_id": int(row["user_id"]),
                "username": row["username"],
                "display_name": row["display_name"],
            }
            for row in rows
        ],
    }


async def bulk_delete_files(file_ids: list[int]) -> dict:
    accepted_count = 0
    failed_ids: list[int] = []
    errors: list[str] = []
    results: list[dict] = []

    for file_id in file_ids:
        try:
            stored = await storage_repo.get_stored_file(file_id)
            if stored is None:
                failed_ids.append(file_id)
                message = f"文件不存在: {file_id}"
                errors.append(message)
                results.append(
                    {"file_id": file_id, "ok": False, "state": "not_found", "accepted": False, "error": message}
                )
                continue
            content_lock = await get_content_hash_lock(str(stored["content_hash"]))
            async with content_lock:
                result = await storage_repo.delete_orphan_stored_file(
                    file_id,
                    expected_content_hash=str(stored["content_hash"]),
                )
            if result is None:
                failed_ids.append(file_id)
                message = f"文件状态已变化: {file_id}"
                errors.append(message)
                results.append(
                    {"file_id": file_id, "ok": False, "state": "conflict", "accepted": False, "error": message}
                )
                continue
            content_hash, _real_path, affected_download_ids = result
            for download_id in affected_download_ids:
                await broadcast_task_update_to_subscribers(download_id)
            accepted_count += 1
            results.append(
                {"file_id": file_id, "ok": True, "state": "pending", "accepted": True, "error": None}
            )
            logger.info("管理员已提交存储文件清理: %s", content_hash)
        except ValueError as exc:
            failed_ids.append(file_id)
            message = str(exc)
            errors.append(message)
            results.append(
                {"file_id": file_id, "ok": False, "state": "referenced", "accepted": False, "error": message}
            )
        except Exception:
            failed_ids.append(file_id)
            message = f"删除受理失败: {file_id}"
            errors.append(message)
            results.append(
                {"file_id": file_id, "ok": False, "state": "failed", "accepted": False, "error": message}
            )
            logger.exception("删除存储文件受理失败: %s", file_id)

    if accepted_count:
        from app.services.deletion_cleanup import DeletionCleanupManager

        DeletionCleanupManager.wake()
    return {
        "deleted_count": 0,
        "accepted_count": accepted_count,
        "failed_ids": failed_ids,
        "errors": errors,
        "results": results,
    }
