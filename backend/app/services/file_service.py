from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.time_utils import ms_to_iso
from app.domain.errors import BadRequestError, ForbiddenError, NotFoundError
from app.domain.file_name_match import rank_file_name
from app.repositories import files as files_repo
from app.services.storage_locks import (
    ContentReadLease,
    acquire_content_read_lease_locked,
    get_content_hash_lock,
)
from app.services.task_broadcast import broadcast_task_update_to_subscribers
from app.services.usage_service import get_visible_space

logger = logging.getLogger(__name__)

SEARCH_RESULT_LIMIT = 200


@dataclass(frozen=True)
class DeleteUserFileReferenceResult:
    deleted: bool
    affected_download_ids: list[int]
    state: str = "not_found"
    accepted: bool = False


def file_row_to_dict(row: dict[str, Any]) -> dict:
    return {
        "id": row["user_file_id"],
        "content_hash": row["content_hash"],
        "name": row["display_name"],
        "size": row["size_bytes"],
        "is_directory": bool(row["is_directory"]),
        "created_at": ms_to_iso(row["user_file_created_at_ms"]) or "",
    }


def normalize_entry_parent(path: str) -> str:
    candidate = (path or "").strip().replace("\\", "/").strip("/")
    if not candidate:
        return ""
    parts = [part for part in candidate.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ForbiddenError("无权访问此路径")
    return "/".join(parts)


def validate_subpath(base_path: Path, subpath: str) -> Path:
    if not subpath:
        return base_path.resolve()

    resolved_base = base_path.resolve()
    target = (resolved_base / subpath).resolve()
    try:
        target.relative_to(resolved_base)
    except ValueError:
        raise ForbiddenError("无权访问此路径") from None
    return target


async def get_user_file_by_hash(
    user_id: int, content_hash: str
) -> dict[str, Any] | None:
    return await files_repo.get_user_file_by_hash(user_id, content_hash)


async def directory_entries(
    stored_file_id: int, parent_path: str
) -> list[dict[str, Any]]:
    parent_is_dir, rows = await files_repo.directory_entries(stored_file_id, parent_path)
    if parent_is_dir is None:
        raise NotFoundError("路径不存在")
    if parent_is_dir is False:
        raise BadRequestError("路径不是文件夹")
    return [
        {
            "name": row["name"],
            "path": row["relative_path"],
            "size": row["size_bytes"],
            "is_dir": bool(row["is_dir"]),
            "is_directory": bool(row["is_dir"]),
            "modified_at": row["mtime_ms"],
        }
        for row in rows
    ]


async def get_user_space_info(user_id: int, quota_bytes: int) -> dict[str, int]:
    visible = await get_visible_space(user_id, quota_bytes)
    return {
        "quota": int(visible["quota"]),
        "used": int(visible["used"]),
        "frozen": int(visible["frozen"]),
        "available": int(visible["available"]),
        "total": int(visible["total"]),
    }


async def list_files(
    user_id: int,
    quota_bytes: int,
    *,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    if page_size not in (10, 20, 30, 50, 100):
        page_size = 10
    if page < 1:
        page = 1
    offset = (page - 1) * page_size
    total, rows = await files_repo.list_user_file_rows(
        user_id,
        offset=offset,
        limit=page_size,
    )
    space_info = await get_user_space_info(user_id, quota_bytes)
    return {
        "files": [file_row_to_dict(row) for row in rows],
        "total": total,
        "space": {
            "used": space_info["used"],
            "frozen": space_info["frozen"],
            "available": space_info["available"],
        },
    }


async def browse_file(user_id: int, file_hash: str, path: str = "") -> list[dict]:
    row = await get_user_file_by_hash(user_id, file_hash)
    if not row:
        raise NotFoundError("文件不存在")
    if not row["is_directory"]:
        raise BadRequestError("此文件不是文件夹")
    return await directory_entries(
        int(row["stored_file_id"]),
        normalize_entry_parent(path),
    )


async def search_files(
    user_id: int,
    keyword: str,
    *,
    scope_content_hash: str | None = None,
    scope_path: str = "",
) -> dict[str, Any]:
    """在当前用户文件中按名称关键词搜索，返回按相关度排序的命中列表。

    keyword 需由调用方 trim；命中数到 SEARCH_RESULT_LIMIT+1 停扫。
    """
    root_rows = await files_repo.list_all_user_file_rows(user_id)
    scan_limit = SEARCH_RESULT_LIMIT + 1
    path_prefix = scope_path if scope_content_hash else ""
    matches: list[tuple[int, int, int, dict[str, Any]]] = []

    def top_level_item(
        row: dict[str, Any], root_index: int, rank: int
    ) -> tuple[int, int, int, dict[str, Any]]:
        display_name = str(row["display_name"])
        return (
            rank,
            int(row["user_file_id"]),
            0,
            {
                "user_file_id": int(row["user_file_id"]),
                "content_hash": str(row["content_hash"]),
                "name": display_name,
                "size": int(row["size_bytes"]),
                "path": f"/{display_name}",
                "is_directory": bool(row["is_directory"]),
                "entry_path": None,
                "rank": rank,
                "root_index": root_index,
            },
        )

    def entry_item(
        row: dict[str, Any],
        root_index: int,
        rank: int,
        entry_row: dict[str, Any],
    ) -> tuple[int, int, int, dict[str, Any]]:
        display_name = str(row["display_name"])
        relative_path = str(entry_row["relative_path"])
        return (
            rank,
            int(row["user_file_id"]),
            int(entry_row["id"]),
            {
                "user_file_id": int(row["user_file_id"]),
                "content_hash": str(row["content_hash"]),
                "name": str(entry_row["name"]),
                "size": int(entry_row["size_bytes"]),
                "path": f"/{display_name}/{relative_path}",
                "is_directory": bool(entry_row["is_dir"]),
                "entry_path": relative_path,
                "rank": rank,
                "root_index": root_index,
            },
        )

    for root_index, row in enumerate(root_rows):
        if scope_content_hash and row["content_hash"] != scope_content_hash:
            continue
        # 子目录 scope 下顶层包名不属于该子树，仅 scope 为包根时可命中
        if not path_prefix:
            rank = rank_file_name(keyword, str(row["display_name"]))
            if rank is not None:
                matches.append(top_level_item(row, root_index, rank))
                if len(matches) >= scan_limit:
                    break
        if not bool(row["is_directory"]):
            continue
        entries = await files_repo.search_stored_file_entries(
            [int(row["stored_file_id"])],
            path_prefix=path_prefix,
        )
        for entry_row in entries:
            entry_rank = rank_file_name(keyword, str(entry_row["name"]))
            if entry_rank is None:
                continue
            matches.append(entry_item(row, root_index, entry_rank, entry_row))
            if len(matches) >= scan_limit:
                break
        if len(matches) >= scan_limit:
            break

    matches.sort(key=lambda match: (match[0], match[1], match[2]))
    truncated = len(matches) > SEARCH_RESULT_LIMIT
    items = [match[3] for match in matches[:SEARCH_RESULT_LIMIT]]
    return {"items": items, "total": len(items), "truncated": truncated}


async def resolve_download_target(
    user_id: int,
    file_hash: str,
    path: str = "",
) -> tuple[Path, str]:
    row = await get_user_file_by_hash(user_id, file_hash)
    if not row:
        raise NotFoundError("文件不存在")
    base_path = Path(str(row["real_path"]))
    if not base_path.exists():
        raise NotFoundError("文件不存在")
    if path:
        if not row["is_directory"]:
            raise BadRequestError("此文件不是文件夹，不支持路径参数")
        target_path = validate_subpath(base_path, path)
    else:
        target_path = base_path
    if not target_path.exists():
        raise NotFoundError("文件不存在")
    if target_path.is_dir():
        raise BadRequestError("不能直接下载文件夹，请选择具体文件")
    return target_path, target_path.name if path else str(row["display_name"])


async def resolve_download_target_with_read_lease(
    user_id: int,
    file_hash: str,
    path: str = "",
) -> tuple[Path, str, ContentReadLease]:
    content_lock = await get_content_hash_lock(file_hash)
    async with content_lock:
        target_path, download_name = await resolve_download_target(
            user_id, file_hash, path
        )
        return (
            target_path,
            download_name,
            acquire_content_read_lease_locked(file_hash),
        )


async def resolve_file_ids(
    user_id: int, file_ids: list[int]
) -> list[tuple[str, int, str]]:
    if not file_ids:
        raise BadRequestError("文件列表不能为空")
    requested_ids = list(dict.fromkeys(file_ids))
    rows = await files_repo.resolve_user_file_ids(user_id, requested_ids)
    by_id = {int(row["user_file_id"]): row for row in rows}
    if len(by_id) != len(requested_ids):
        raise NotFoundError("部分文件不存在或无权访问")
    return [
        (
            str(by_id[file_id]["real_path"]),
            int(by_id[file_id]["size_bytes"]),
            str(by_id[file_id]["display_name"] or "未命名"),
        )
        for file_id in requested_ids
    ]


async def delete_user_file_reference_v0_result(
    user_id: int, user_file_id: int
) -> DeleteUserFileReferenceResult:
    identity = await files_repo.get_user_file_delete_identity(user_id, user_file_id)
    if identity is None:
        return DeleteUserFileReferenceResult(False, [])
    content_lock = await get_content_hash_lock(str(identity["content_hash"]))
    async with content_lock:
        try:
            deleted, affected_download_ids, real_path = (
                await files_repo.delete_user_file_reference(
                    user_id,
                    user_file_id,
                    expected_stored_file_id=int(identity["stored_file_id"]),
                    expected_created_at_ms=int(identity["created_at_ms"]),
                )
            )
        except files_repo.PackSourceProtectedError:
            raise ForbiddenError(
                "文件正在被打包或等待源文件清理，暂不能删除"
            ) from None
        if not deleted:
            return DeleteUserFileReferenceResult(False, [])
    accepted = real_path is not None
    if accepted:
        from app.services.deletion_cleanup import DeletionCleanupManager

        DeletionCleanupManager.wake()
    return DeleteUserFileReferenceResult(
        True,
        affected_download_ids,
        state="pending" if accepted else "released",
        accepted=accepted,
    )


async def delete_user_file_reference_v0(user_id: int, user_file_id: int) -> bool:
    return (await delete_user_file_reference_v0_result(user_id, user_file_id)).deleted


async def delete_file_by_hash(user_id: int, file_hash: str) -> DeleteUserFileReferenceResult:
    row = await get_user_file_by_hash(user_id, file_hash)
    if not row:
        raise NotFoundError("文件不存在")
    result = await delete_user_file_reference_v0_result(
        user_id, int(row["user_file_id"])
    )
    if not result.deleted:
        raise NotFoundError("文件不存在")
    for download_id in result.affected_download_ids:
        await broadcast_task_update_to_subscribers(download_id)
    return result


def validate_display_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise BadRequestError("名称不能为空")
    if "/" in normalized or "\\" in normalized:
        raise BadRequestError("名称不能包含路径分隔符")
    if normalized in {".", ".."}:
        raise BadRequestError("名称不合法")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise BadRequestError("名称包含非法字符")
    return normalized


async def rename_file(user_id: int, file_hash: str, name: str) -> None:
    normalized = validate_display_name(name)
    if not await files_repo.rename_user_file_by_hash(user_id, file_hash, normalized):
        raise NotFoundError("文件不存在")
