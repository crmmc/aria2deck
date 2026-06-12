"""Startup repair: scan orphan files, create StoredFile records, repair task associations."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.repositories.downloads import (
    list_completed_downloads_without_file,
    now_ms,
    repair_completed_download_with_stored_file,
)
from app.repositories.files import (
    create_stored_file_with_entries,
    list_stored_file_content_hashes,
    list_stored_file_rows,
    stored_file_exists_by_content_hash,
)
from app.services.hash import calculate_content_hash_async
from app.services.storage import get_store_dir
from app.services.storage_index import build_entry_templates

logger = logging.getLogger(__name__)


async def run_startup_repair() -> dict:
    logger.info("Starting startup repair scan...")

    results = {
        "orphan_files_found": 0,
        "stored_files_created": 0,
        "tasks_repaired": 0,
        "errors": [],
    }

    scan_results = await scan_and_create_stored_files()
    results["orphan_files_found"] = scan_results["found"]
    results["stored_files_created"] = scan_results["created"]
    results["errors"].extend(scan_results["errors"])

    results["tasks_repaired"] = await repair_task_associations()

    logger.info(
        f"Startup repair complete: "
        f"orphans={results['orphan_files_found']}, "
        f"created={results['stored_files_created']}, "
        f"tasks_repaired={results['tasks_repaired']}"
    )

    return results


async def scan_and_create_stored_files() -> dict:
    results = {"found": 0, "created": 0, "errors": []}

    store_dir = get_store_dir()
    if not store_dir.exists():
        logger.info(f"Store directory does not exist: {store_dir}")
        return results

    existing_hashes = await _get_existing_content_hashes()
    logger.info(f"Found {len(existing_hashes)} existing StoredFile records")

    for prefix_dir in store_dir.iterdir():
        if not prefix_dir.is_dir() or len(prefix_dir.name) != 2:
            continue

        for item_path in prefix_dir.iterdir():
            content_hash = item_path.name

            if not _is_valid_sha256_hash(content_hash):
                logger.warning(f"Skipping invalid hash format: {content_hash}")
                continue

            if content_hash in existing_hashes:
                continue

            results["found"] += 1
            logger.info(f"Found orphan file: {content_hash}")

            try:
                if await _create_stored_file_for_path(item_path, content_hash):
                    results["created"] += 1
                    existing_hashes.add(content_hash)
            except Exception as e:
                error_msg = f"Failed to create StoredFile for {content_hash}: {e}"
                logger.error(error_msg)
                results["errors"].append(error_msg)

    return results


async def _get_existing_content_hashes() -> set[str]:
    return await list_stored_file_content_hashes()


async def _create_stored_file_for_path(path: Path, content_hash: str) -> bool:
    try:
        actual_hash = await calculate_content_hash_async(path)
        if actual_hash != content_hash:
            logger.warning(
                f"Content hash mismatch for {path}: "
                f"expected={content_hash}, actual={actual_hash}"
            )
            # Hash 不匹配，不创建记录以保证数据完整性
            return False
    except Exception as e:
        logger.warning(f"Could not verify content hash for {path}: {e}")
        # 无法验证 hash，不创建记录
        return False

    is_directory = path.is_dir()
    if is_directory:
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    else:
        size = path.stat().st_size
    original_name = path.name

    if await stored_file_exists_by_content_hash(content_hash):
        logger.debug(f"StoredFile already exists for {content_hash}")
        return False

    try:
        await create_stored_file_with_entries(
            {
                "content_hash": content_hash,
                "real_path": str(path),
                "size_bytes": size,
                "is_directory": 1 if is_directory else 0,
                "original_name": original_name,
            },
            build_entry_templates(path),
        )
        logger.info(f"Created StoredFile for orphan: {content_hash}")
        return True
    except Exception as e:
        logger.warning(f"Failed to create StoredFile (race condition?): {e}")
        return False


async def repair_task_associations() -> int:
    orphan_tasks = await list_completed_downloads_without_file()
    if not orphan_tasks:
        logger.info("No orphan tasks to repair")
        return 0

    logger.info(f"Found {len(orphan_tasks)} orphan tasks to repair")

    all_stored_files = await list_stored_file_rows()
    sf_by_name_size = _build_stored_file_lookup_by_name_size(all_stored_files)

    repair_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task in orphan_tasks:
        task_name = task["display_name"]
        if not task_name:
            continue

        lookup_key = (str(task_name).lower(), int(task["total_bytes"] or 0))
        matched_sf = sf_by_name_size.get(lookup_key)
        if not matched_sf:
            continue
        repair_candidates.append((dict(task), dict(matched_sf)))

    repaired_count = 0
    for task, matched_sf in repair_candidates:
        repaired = await repair_completed_download_with_stored_file(
            global_download_id=int(task["id"]),
            stored_file_id=int(matched_sf["id"]),
            size_bytes=int(matched_sf["size_bytes"] or 0),
            original_name=str(matched_sf["original_name"]),
            completed_at_ms=int(task["completed_at_ms"] or now_ms()),
        )
        if not repaired:
            continue
        repaired_count += 1
        logger.info(
            f"Repaired download {task['id']} ({task['display_name']}) -> "
            f"StoredFile {matched_sf['id']}"
        )

    return repaired_count


def _build_stored_file_lookup_by_name_size(
    stored_file_rows: Sequence[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """构建按 (name, size) 的精确匹配索引，避免同名文件错绑"""
    lookup: dict[tuple[str, int], dict[str, Any]] = {}
    ambiguous_keys: set[tuple[str, int]] = set()
    for sf in stored_file_rows:
        name_key = str(sf["original_name"]).lower() if sf["original_name"] else ""
        if name_key:
            key = (name_key, int(sf["size_bytes"]))
            if key in lookup:
                ambiguous_keys.add(key)
            elif key not in ambiguous_keys:
                lookup[key] = sf
    for key in ambiguous_keys:
        lookup.pop(key, None)
    return lookup


def _is_valid_sha256_hash(s: str) -> bool:
    if len(s) != 64:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False
