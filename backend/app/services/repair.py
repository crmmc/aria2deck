"""Startup repair: scan orphan files, create StoredFile records, repair task associations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

from app.aria2.protocol import Aria2Gateway
from app.repositories.downloads import (
    list_active_global_downloads,
    list_completed_downloads_without_file,
    list_terminal_downloads_with_residual_gid,
    mark_global_download_failed,
    reconcile_download_size,
    reset_active_accounting_for_startup,
)
from app.repositories.files import (
    create_stored_file_with_entries,
    get_stored_file_by_content_hash,
    list_stored_file_content_hashes,
)
from app.services.failed_task_cleanup import cleanup_terminal_download_generation
from app.services.download_service import (
    candidate_size_from_status,
    get_disk_available_bytes,
)
from app.services.settings_service import get_max_task_size
from app.services.task_projection import is_metadata_phase_status
from app.services.storage import get_store_dir, get_store_path_for_hash
from app.services.storage_index import (
    CONTENT_HASH_V1,
    calculate_legacy_content_hash,
    content_identity_from_content_hash,
    scan_storage_path_async,
)

logger = logging.getLogger(__name__)


class StoredFileScanResult(TypedDict):
    found: int
    created: int
    unresolved: int
    errors: list[str]


class StartupRepairResult(TypedDict):
    orphan_files_found: int
    stored_files_created: int
    unresolved_files: int
    tasks_repaired: int
    errors: list[str]
    safe_for_cleanup: bool


class TaskAssociationRepairResult(TypedDict):
    repaired: int
    unresolved: int
    errors: list[str]


FileRepairStatus = Literal["created", "resolved", "unresolved"]


async def purge_terminal_residual_gids(client: Aria2Gateway) -> dict[str, int]:
    """Clear terminal downloads that still hold an aria2 gid.

    These residuals must never continue to claim disk budget after cancel/fail.
    """
    residuals = await list_terminal_downloads_with_residual_gid()
    purged = 0
    failed = 0
    for download in residuals:
        download_id = int(download["id"])
        gid = str(download.get("aria2_gid") or "")
        if not gid:
            continue
        try:
            result = await cleanup_terminal_download_generation(
                client=client,
                task_id=download_id,
                gid=gid,
                owner_id=None,
                log_prefix="[Residual]",
                skip_status_check=True,
            )
            if result.writer_stopped:
                purged += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "Failed to purge residual gid download_id=%s gid=%s error=%s",
                download_id,
                gid,
                exc,
            )
    return {"found": len(residuals), "purged": purged, "failed": failed}


async def rebuild_active_download_accounting(
    client: Aria2Gateway,
) -> dict[str, int]:
    downloads = await list_active_global_downloads()
    snapshots: dict[int, tuple[dict[str, Any], bool]] = {}
    for download in downloads:
        gid = str(download.get("aria2_gid") or "")
        if not gid:
            continue
        status = await client.tell_status(gid)
        raw_status = str(status.get("status") or "")
        quiescent = raw_status in {"complete", "error", "removed"}
        if raw_status != "paused" and not quiescent:
            await client.pause(gid)
        should_resume = (
            str(download.get("status") or "") != "paused"
            or not bool(download.get("size_known"))
        )
        snapshots[int(download["id"])] = (status, should_resume)

    await reset_active_accounting_for_startup()
    rebuilt = 0
    failed = 0
    for download in downloads:
        download_id = int(download["id"])
        gid = str(download.get("aria2_gid") or "")
        snapshot = snapshots.get(download_id)
        status = snapshot[0] if snapshot else {}
        should_resume = snapshot[1] if snapshot else False
        raw_status = str(status.get("status") or "")
        if raw_status in {"error", "removed"}:
            await mark_global_download_failed(
                download_id,
                expected_gid=gid,
                message="启动恢复发现下载已终止",
                error_code=f"startup_{raw_status}",
            )
            await cleanup_terminal_download_generation(
                client=client,
                task_id=download_id,
                gid=gid,
                owner_id=None,
                log_prefix="[Startup]",
                skip_status_check=True,
            )
            failed += 1
            continue

        is_metadata = (
            str(download.get("resource_kind") or "") == "magnet"
            and bool(status)
            and is_metadata_phase_status(status)
        )
        if is_metadata:
            try:
                await client.change_option(gid, {"pause-metadata": "true"})
                if should_resume and raw_status not in {"complete", "error", "removed"}:
                    await client.unpause(gid)
            except Exception:
                await mark_global_download_failed(
                    download_id,
                    expected_gid=gid,
                    message="启动恢复无法安全配置磁力元数据暂停",
                    error_code="startup_metadata_pause_failed",
                )
                await cleanup_terminal_download_generation(
                    client=client,
                    task_id=download_id,
                    gid=gid,
                    owner_id=None,
                    log_prefix="[Startup]",
                    skip_status_check=True,
                )
                failed += 1
            continue

        candidate = candidate_size_from_status(
            status, require_trusted_total=True
        )
        if candidate is None and bool(download.get("size_known")):
            known_total = max(0, int(download.get("total_bytes") or 0))
            try:
                reported_completed = max(0, int(status.get("completedLength") or 0))
            except (TypeError, ValueError):
                reported_completed = 0
            if reported_completed <= known_total:
                candidate = (
                    known_total,
                    max(
                        reported_completed,
                        max(0, int(download.get("completed_bytes") or 0)),
                    ),
                )
        if candidate is None:
            await mark_global_download_failed(
                download_id,
                expected_gid=gid or None,
                message="启动恢复无法确认任务大小",
                error_code="startup_unknown_size",
            )
            if gid:
                await cleanup_terminal_download_generation(
                    client=client,
                    task_id=download_id,
                    gid=gid,
                    owner_id=None,
                    log_prefix="[Startup]",
                    skip_status_check=True,
                )
            failed += 1
            continue

        result = await reconcile_download_size(
            download_id=download_id,
            expected_gid=gid or None,
            candidate_bytes=candidate[0],
            completed_bytes=candidate[1],
            size_limit_bytes=int(
                download.get("size_limit_bytes") or get_max_task_size()
            ),
            disk_available_bytes=get_disk_available_bytes,
        )
        if result.admitted:
            if (
                gid
                and should_resume
                and raw_status not in {"complete", "error", "removed"}
            ):
                try:
                    await client.unpause(gid)
                except Exception:
                    await mark_global_download_failed(
                        download_id,
                        expected_gid=gid,
                        message="启动恢复后无法恢复下载",
                        error_code="startup_unpause_failed",
                    )
                    await cleanup_terminal_download_generation(
                        client=client,
                        task_id=download_id,
                        gid=gid,
                        owner_id=None,
                        log_prefix="[Startup]",
                        skip_status_check=True,
                    )
                    failed += 1
                    continue
            rebuilt += 1
        else:
            failed += 1
            if gid:
                await cleanup_terminal_download_generation(
                    client=client,
                    task_id=download_id,
                    gid=gid,
                    owner_id=None,
                    log_prefix="[Startup]",
                    skip_status_check=True,
                )
    return {"rebuilt": rebuilt, "failed": failed}


async def run_startup_repair() -> StartupRepairResult:
    logger.info("Starting startup repair scan...")

    results: StartupRepairResult = {
        "orphan_files_found": 0,
        "stored_files_created": 0,
        "unresolved_files": 0,
        "tasks_repaired": 0,
        "errors": [],
        "safe_for_cleanup": False,
    }

    scan_results = await scan_and_create_stored_files()
    results["orphan_files_found"] = scan_results["found"]
    results["stored_files_created"] = scan_results["created"]
    results["unresolved_files"] = scan_results["unresolved"]
    results["errors"].extend(scan_results["errors"])

    association_results = await repair_task_associations()
    results["tasks_repaired"] = association_results["repaired"]
    results["unresolved_files"] += association_results["unresolved"]
    results["errors"].extend(association_results["errors"])
    results["safe_for_cleanup"] = (
        results["unresolved_files"] == 0 and not results["errors"]
    )

    logger.info(
        "Startup repair complete: orphans=%d, created=%d, unresolved=%d, "
        "tasks_repaired=%d, safe_for_cleanup=%s",
        results["orphan_files_found"],
        results["stored_files_created"],
        results["unresolved_files"],
        results["tasks_repaired"],
        results["safe_for_cleanup"],
    )

    return results


async def scan_and_create_stored_files() -> StoredFileScanResult:
    results: StoredFileScanResult = {
        "found": 0, "created": 0, "unresolved": 0, "errors": [],
    }
    store_dir = get_store_dir()
    if not store_dir.exists():
        return results
    existing_hashes = await _get_existing_content_hashes()
    candidates: list[tuple[Path, str]] = []
    for top_level in store_dir.iterdir():
        if not top_level.is_dir():
            continue
        if top_level.name != "v2":
            for item_path in top_level.iterdir():
                candidates.append((item_path, item_path.name))
            continue
        for object_kind in top_level.iterdir():
            if not object_kind.is_dir() or object_kind.name not in {"file", "directory"}:
                continue
            for prefix_dir in object_kind.iterdir():
                if not prefix_dir.is_dir():
                    continue
                for item_path in prefix_dir.iterdir():
                    candidates.append((item_path, f"v2:{object_kind.name}:{item_path.name}"))
    for item_path, content_hash in candidates:
        if content_hash in existing_hashes:
            continue
        results["found"] += 1
        try:
            if get_store_path_for_hash(content_hash).resolve(strict=False) != item_path.resolve(strict=False):
                raise ValueError("store path is not canonical")
            status = await _create_stored_file_for_path(item_path, content_hash)
        except Exception as exc:
            error = f"Failed to create StoredFile for {content_hash}: {exc}"
            logger.error(error)
            results["unresolved"] += 1
            results["errors"].append(error)
            continue
        if status == "created":
            results["created"] += 1
        elif status == "unresolved":
            error = f"Unresolved stored file candidate: {item_path}"
            results["unresolved"] += 1
            results["errors"].append(error)
            continue
        existing_hashes.add(content_hash)
    return results


async def _get_existing_content_hashes() -> set[str]:
    content_hashes: set[str] = await list_stored_file_content_hashes()
    return content_hashes


def _stored_file_path_matches(row: dict[str, Any], path: Path) -> bool:
    registered_path = Path(str(row["real_path"])).resolve(strict=False)
    return registered_path == path.resolve(strict=False)


async def _create_stored_file_for_path(
    path: Path, content_hash: str
) -> FileRepairStatus:
    try:
        scan = await scan_storage_path_async(path)
        identity = content_identity_from_content_hash(content_hash)
        actual_hash = (
            scan.content_hash
            if identity.version == CONTENT_HASH_V1
            and scan.content_hash_version == CONTENT_HASH_V1
            else calculate_legacy_content_hash(path)
            if identity.version == CONTENT_HASH_V1
            else scan.content_hash
        )
        if actual_hash != content_hash:
            logger.warning(
                "Content hash mismatch for %s: expected=%s, actual=%s",
                path,
                content_hash,
                actual_hash,
            )
            return "unresolved"
    except Exception as exc:
        logger.warning("Could not verify content hash for %s: %s", path, exc)
        return "unresolved"

    is_directory = scan.is_directory
    size = scan.size_bytes

    existing = await get_stored_file_by_content_hash(content_hash)
    if existing is not None:
        if _stored_file_path_matches(existing, path):
            logger.debug("StoredFile already exists for %s", content_hash)
            return "resolved"
        logger.warning(
            "StoredFile hash exists at a different path: hash=%s path=%s",
            content_hash,
            existing["real_path"],
        )
        return "unresolved"

    try:
        await create_stored_file_with_entries(
            {
                "content_hash": content_hash,
                "content_hash_version": identity.version,
                "content_object_kind": identity.object_kind,
                "content_digest": identity.digest,
                "real_path": str(path),
                "size_bytes": size,
                "is_directory": 1 if is_directory else 0,
                "original_name": path.name,
            },
            scan.entry_templates,
        )
        logger.info("Created StoredFile for orphan: %s", content_hash)
        return "created"
    except Exception as exc:
        try:
            concurrent = await get_stored_file_by_content_hash(content_hash)
        except Exception as confirmation_exc:
            logger.warning(
                "Failed to create StoredFile: %s; confirmation failed: %s",
                exc,
                confirmation_exc,
            )
            return "unresolved"
        if concurrent is not None and _stored_file_path_matches(concurrent, path):
            logger.info("StoredFile concurrently registered for %s", content_hash)
            return "resolved"
        logger.warning("Failed to create StoredFile (race condition?): %s", exc)
        return "unresolved"


async def repair_task_associations() -> TaskAssociationRepairResult:
    orphan_tasks = await list_completed_downloads_without_file()
    if not orphan_tasks:
        logger.info("No orphan tasks to repair")
        return {"repaired": 0, "unresolved": 0, "errors": []}

    logger.warning(
        "Found %d completed tasks without a verifiable content identity",
        len(orphan_tasks),
    )
    errors: list[str] = []
    for task in orphan_tasks:
        error = (
            f"Download {task['id']} has no verifiable stored-file content identity; "
            "association left unresolved"
        )
        logger.warning(error)
        errors.append(error)

    return {
        "repaired": 0,
        "unresolved": len(orphan_tasks),
        "errors": errors,
    }


def _is_valid_sha256_hash(s: str) -> bool:
    if len(s) != 64:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False
