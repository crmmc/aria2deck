"""Startup repair: scan orphan files, create StoredFile records, repair task associations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

from app.modules.backend.port import BackendPort
from app.repositories.task.downloads import (
    claim_terminal_reclaim,
    get_global_download_by_id,
    list_active_global_downloads,
    list_completed_downloads_pending_index,
    list_completed_downloads_without_file,
    list_terminal_download_ids,
    list_terminal_downloads_with_residual_gid,
    reopen_completed_download_for_index_repair,
    restore_incomplete_completed_download,
    reset_active_accounting_for_startup,
)
from app.repositories.files import (
    create_stored_file_with_entries,
    get_stored_file_by_content_hash,
    list_stored_file_content_hashes,
)
from app.services.failed_task_cleanup import cleanup_with_claim
from app.services.lifecycle.completion import handle_v0_download_complete
from app.services.storage import (
    cleanup_task_download_dir,
    get_downloading_dir,
    get_store_dir,
    get_store_path_for_hash,
)
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


async def purge_terminal_residual_gids(backend: BackendPort) -> dict[str, int]:
    """Clear terminal downloads that still hold an aria2 gid.

    These residuals must never continue to claim disk budget after cancel/fail.
    Each attempt must obtain a repair claim (spec §15.3) before physical
    reclamation; no ``skip_status_check`` bypass is used.
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
            claim = await claim_terminal_reclaim(
                attempt_id=download_id,
                expected_gid=gid,
            )
            if claim is None:
                # Attempt no longer failed/cancelled or GID changed — skip.
                continue
            result = await cleanup_with_claim(
                backend, claim, log_prefix="[Residual]"
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


async def purge_terminal_download_dirs() -> dict[str, int]:
    """Delete downloading/<id> only when reclaim is known-safe.

    Safe set:
    - failed / cancelled
    - completed with completed_file_id (payload already in store)

    Never delete completed-without-index dirs: they may hold the only copy.
    """
    terminal_ids = set(await list_terminal_download_ids())
    downloading_dir = get_downloading_dir()
    found = 0
    purged = 0
    failed = 0
    skipped = 0
    if not downloading_dir.exists():
        return {
            "found": 0,
            "purged": 0,
            "failed": 0,
            "skipped": 0,
        }

    for entry in downloading_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if not name.isdigit():
            # pack_* and other non-task dirs are out of scope for this purge.
            skipped += 1
            continue
        download_id = int(name)
        if download_id not in terminal_ids:
            skipped += 1
            continue
        found += 1
        try:
            await cleanup_task_download_dir(download_id)
            purged += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "Failed to purge terminal download dir id=%s error=%s",
                download_id,
                exc,
            )
    return {
        "found": found,
        "purged": purged,
        "failed": failed,
        "skipped": skipped,
    }


async def recover_completed_downloads_pending_index(
    backend: BackendPort,
) -> dict[str, int]:
    """Retry store indexing for completed rows that still own downloading dirs."""
    pending = await list_completed_downloads_pending_index()
    recovered = 0
    failed = 0
    skipped = 0
    for download in pending:
        download_id = int(download["id"])
        task_dir = get_downloading_dir() / str(download_id)
        if not task_dir.exists() or not task_dir.is_dir():
            skipped += 1
            continue

        original_gid = download.get("aria2_gid")
        original_gid_str = str(original_gid) if original_gid else None
        recovery_gid = original_gid_str or f"recover-{download_id}"
        reopened = await reopen_completed_download_for_index_repair(
            download_id,
            recovery_gid=None if original_gid_str else recovery_gid,
        )
        if reopened is None:
            skipped += 1
            continue

        async def _restore_incomplete() -> None:
            restored = await restore_incomplete_completed_download(
                download_id,
                aria2_gid=original_gid_str,
            )
            if restored is None:
                logger.warning(
                    "Failed to restore incomplete completed download after recovery miss id=%s",
                    download_id,
                )

        display_name = str(
            reopened.get("display_name") or reopened.get("source_uri") or task_dir.name
        )
        files: list[dict[str, Any]] = []
        try:
            for path in task_dir.rglob("*"):
                if path.is_file() and not path.name.endswith(".aria2"):
                    size = path.stat().st_size
                    files.append(
                        {
                            "path": str(path),
                            "length": str(size),
                            "completedLength": str(size),
                            "selected": "true",
                        }
                    )
        except OSError as exc:
            failed += 1
            logger.warning(
                "Failed to scan pending-index download dir id=%s error=%s",
                download_id,
                exc,
            )
            await _restore_incomplete()
            continue

        if not files:
            skipped += 1
            await _restore_incomplete()
            continue

        total_bytes = sum(int(item["length"]) for item in files)
        aria2_status = {
            "status": "complete",
            "totalLength": str(total_bytes),
            "completedLength": str(total_bytes),
            "files": files,
            "bittorrent": {"info": {"name": display_name}},
        }
        try:
            changed = await handle_v0_download_complete(
                backend=backend,
                download=reopened,
                aria2_status=aria2_status,
                completion_gid=str(reopened.get("aria2_gid") or recovery_gid),
                log_prefix="[Recover]",
                allow_metadata_handoff_defer=False,
            )
            if changed:
                snapshot = await get_global_download_by_id(download_id)
                if (
                    snapshot is not None
                    and snapshot.get("status") == "completed"
                    and snapshot.get("completed_file_id") is not None
                ):
                    recovered += 1
                else:
                    failed += 1
                    await _restore_incomplete()
            else:
                failed += 1
                await _restore_incomplete()
        except Exception as exc:
            failed += 1
            logger.warning(
                "Failed to recover pending-index download id=%s error=%s",
                download_id,
                exc,
            )
            await _restore_incomplete()
    return {
        "found": len(pending),
        "recovered": recovered,
        "failed": failed,
        "skipped": skipped,
    }


async def rebuild_active_download_accounting(
    backend: BackendPort,
) -> dict[str, int]:
    """Re-admit active attempts through the unified coordinator (spec §15.2).

    No longer does manual pause/unpause/size logic or calls
    ``fail_download_and_reclaim`` directly. Each live attempt is reconciled via ``reconcile_attempt_signal``,
    which acquires the attempt lock, rereads, queries Aria2 and handles
    size admission, handoff, completion, error/removed terminalization and
    missing-GID inside a single coordinator boundary.
    """
    from app.services.lifecycle.coordinator import reconcile_attempt_signal
    from app.domain.lifecycle import ReconcileResult

    downloads = await list_active_global_downloads()

    await reset_active_accounting_for_startup()

    rebuilt = 0
    failed = 0
    for download in downloads:
        download_id = int(download["id"])
        gid = str(download.get("aria2_gid") or "")
        if not gid:
            # Queued without GID — submission path will handle it.
            continue
        try:
            result = await reconcile_attempt_signal(
                backend=backend,
                observed_gid=gid,
                event="startup",
                observed_status=None,
                log_prefix="[Startup]",
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "Startup reconcile failed download_id=%s gid=%s error=%s",
                download_id,
                gid,
                exc,
            )
            continue

        if result in (
            ReconcileResult.CHANGED,
            ReconcileResult.ALREADY_ACTIVE,
            ReconcileResult.ALREADY_COMPLETE,
            ReconcileResult.COMPLETED,
            ReconcileResult.WAITING,
        ):
            rebuilt += 1
        elif result == ReconcileResult.TERMINALIZED:
            failed += 1
        else:
            # stale / ignored / already_terminal / recovery_pending
            rebuilt += 1
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
