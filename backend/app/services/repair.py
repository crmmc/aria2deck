"""Startup repair: scan orphan files, create StoredFile records, repair task associations."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.db.engine import transaction
from app.db.schema import global_downloads, stored_files
from app.repositories.files import create_stored_file_with_entries
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
    async with transaction() as conn:
        result = await conn.execute(select(stored_files.c.content_hash))
        return {str(row[0]) for row in result.all()}


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

    async with transaction() as conn:
        existing = (
            await conn.execute(
                select(stored_files.c.id).where(
                    stored_files.c.content_hash == content_hash
                )
            )
        ).first()
        if existing:
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
    async with transaction() as conn:
        orphan_tasks = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.status == "completed",
                        global_downloads.c.completed_file_id.is_(None),
                    )
                )
            )
            .mappings()
            .all()
        )

        if not orphan_tasks:
            logger.info("No orphan tasks to repair")
            return 0

        logger.info(f"Found {len(orphan_tasks)} orphan tasks to repair")

        all_stored_files = (await conn.execute(select(stored_files))).mappings().all()
        # 构建按 (name, size) 的精确匹配索引
        sf_by_name_size = _build_stored_file_lookup_by_name_size(all_stored_files)

        repaired_count = 0
        for task in orphan_tasks:
            task_name = task["display_name"]
            if not task_name:
                continue

            # 优先用 name + size 双重匹配，避免同名文件错绑
            lookup_key = (str(task_name).lower(), int(task["total_bytes"] or 0))
            matched_sf = sf_by_name_size.get(lookup_key)
            if not matched_sf:
                continue
            await conn.execute(
                update(global_downloads)
                .where(global_downloads.c.id == task["id"])
                .values(completed_file_id=matched_sf["id"])
            )
            repaired_count += 1
            logger.info(
                f"Repaired download {task['id']} ({task_name}) -> StoredFile {matched_sf['id']}"
            )

    return repaired_count


def _build_stored_file_lookup_by_name_size(
    stored_file_rows: Sequence[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """构建按 (name, size) 的精确匹配索引，避免同名文件错绑"""
    lookup: dict[tuple[str, int], dict[str, Any]] = {}
    for sf in stored_file_rows:
        name_key = str(sf["original_name"]).lower() if sf["original_name"] else ""
        if name_key:
            key = (name_key, int(sf["size_bytes"]))
            if key not in lookup:
                lookup[key] = sf
    return lookup


def _is_valid_sha256_hash(s: str) -> bool:
    if len(s) != 64:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False
