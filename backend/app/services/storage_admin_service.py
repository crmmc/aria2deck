from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.core.time_utils import ms_to_iso
from app.domain.errors import NotFoundError
from app.repositories import storage as storage_repo
from app.services.storage import get_store_dir, safe_delete_path
from app.services.task_broadcast import broadcast_task_update_to_subscribers

logger = logging.getLogger(__name__)


async def list_stored_files(search: str, orphan_only: bool) -> dict:
    rows = await storage_repo.list_stored_files(search, orphan_only)
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
        }
        for row in rows
    ]
    return {"files": files, "total": len(files)}


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
    deleted_count = 0
    failed_ids: list[int] = []
    errors: list[str] = []

    for file_id in file_ids:
        try:
            result = await storage_repo.delete_orphan_stored_file(file_id)
            if result is None:
                failed_ids.append(file_id)
                errors.append(f"文件不存在: {file_id}")
                continue
            content_hash, real_path, affected_download_ids = result
            path = Path(real_path)
            if path.exists():
                try:
                    await asyncio.to_thread(
                        safe_delete_path,
                        base_dir=get_store_dir(),
                        target=path,
                        recursive=path.is_dir(),
                        allow_missing=True,
                    )
                except Exception:
                    logger.warning(
                        "Failed to delete unreferenced stored path=%s",
                        path,
                        exc_info=True,
                    )
            for download_id in affected_download_ids:
                await broadcast_task_update_to_subscribers(download_id)
            deleted_count += 1
            logger.info("管理员删除存储文件: %s", content_hash)
        except Exception as exc:
            failed_ids.append(file_id)
            errors.append(f"删除失败 {file_id}: {exc!s}")
            logger.exception("删除存储文件失败: %s", file_id)

    return {
        "deleted_count": deleted_count,
        "failed_ids": failed_ids,
        "errors": errors,
    }
