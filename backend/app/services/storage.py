"""Filesystem storage helpers for shared download artifacts.

Database-backed file registration now lives in v0 services/repositories.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_store_dir() -> Path:
    """Get the store directory path."""
    store_dir = Path(settings.download_dir).resolve() / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


def get_downloading_dir() -> Path:
    """Get the downloading directory path."""
    downloading_dir = Path(settings.download_dir).resolve() / "downloading"
    downloading_dir.mkdir(parents=True, exist_ok=True)
    return downloading_dir


def get_task_download_dir(task_id: int) -> Path:
    """Get the download directory for a specific task.

    Each task gets its own directory to avoid filename conflicts.

    Args:
        task_id: The global download ID

    Returns:
        Path to the task's download directory
    """
    task_dir = get_downloading_dir() / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def is_path_within_base(base_dir: Path, target: Path) -> bool:
    """Check whether target path is within base directory."""
    base_resolved = base_dir.resolve(strict=False)
    target_abs = target if target.is_absolute() else base_resolved / target
    normalized_target = target_abs.parent.resolve(strict=False) / target_abs.name

    try:
        normalized_target.relative_to(base_resolved)
        return True
    except ValueError:
        return False


def safe_delete_path(
    base_dir: Path,
    target: Path,
    *,
    recursive: bool = False,
    allow_missing: bool = True,
    allow_delete_base: bool = False,
) -> bool:
    """Safely delete a file/directory under a whitelisted base directory."""
    target_raw = str(target).strip()
    if not target_raw:
        logger.warning(
            "Refused delete: empty target path base_dir=%s target=%s recursive=%s reason=empty_target",
            base_dir,
            target,
            recursive,
        )
        raise ValueError("Empty target path is not allowed")

    base_resolved = base_dir.resolve(strict=False)
    target_candidate = target if target.is_absolute() else base_resolved / target
    target_abs = target_candidate.parent.resolve(strict=False) / target_candidate.name
    target_resolved = target_abs.resolve(strict=False)

    if not is_path_within_base(base_resolved, target_abs):
        logger.warning(
            "Refused delete: target outside base_dir=%s target=%s resolved_target=%s recursive=%s reason=outside_base",
            base_resolved,
            target_abs,
            target_resolved,
            recursive,
        )
        raise ValueError(f"Target path outside allowed base: {target_abs}")

    fs_root = Path(target_abs.anchor).resolve(strict=False)
    if target_abs == fs_root:
        logger.warning(
            "Refused delete: filesystem root base_dir=%s target=%s resolved_target=%s recursive=%s reason=filesystem_root",
            base_resolved,
            target_abs,
            target_resolved,
            recursive,
        )
        raise ValueError("Deleting filesystem root is not allowed")

    if target_abs == base_resolved and not allow_delete_base:
        logger.warning(
            "Refused delete: base directory base_dir=%s target=%s resolved_target=%s recursive=%s reason=delete_base_forbidden",
            base_resolved,
            target_abs,
            target_resolved,
            recursive,
        )
        raise ValueError("Deleting base directory is not allowed")

    exists_or_link = target_abs.exists() or target_abs.is_symlink()
    if not exists_or_link:
        if allow_missing:
            logger.debug(
                "Skip delete: target missing base_dir=%s target=%s resolved_target=%s recursive=%s reason=missing",
                base_resolved,
                target_abs,
                target_resolved,
                recursive,
            )
            return False
        raise FileNotFoundError(target_abs)

    if target_abs.is_symlink():
        try:
            target_abs.unlink()
        except FileNotFoundError:
            if allow_missing:
                logger.debug(
                    "Skip delete: symlink already missing base_dir=%s target=%s resolved_target=%s recursive=%s reason=missing_after_check",
                    base_resolved,
                    target_abs,
                    target_resolved,
                    recursive,
                )
                return False
            raise
        logger.info(
            "Deleted symlink path base_dir=%s target=%s resolved_target=%s recursive=%s",
            base_resolved,
            target_abs,
            target_resolved,
            recursive,
        )
        return True

    try:
        target_resolved.relative_to(base_resolved)
    except ValueError as exc:
        logger.warning(
            "Refused delete: resolved target outside base_dir=%s target=%s resolved_target=%s recursive=%s reason=resolved_outside_base",
            base_resolved,
            target_abs,
            target_resolved,
            recursive,
        )
        raise ValueError(
            f"Resolved target path outside allowed base: {target_resolved}"
        ) from exc

    if target_abs.is_dir():
        try:
            if recursive:
                shutil.rmtree(target_abs)
            else:
                target_abs.rmdir()
        except FileNotFoundError:
            if allow_missing:
                logger.debug(
                    "Skip delete: directory missing during delete base_dir=%s target=%s resolved_target=%s recursive=%s reason=missing_after_check",
                    base_resolved,
                    target_abs,
                    target_resolved,
                    recursive,
                )
                return False
            raise
    else:
        try:
            target_abs.unlink()
        except FileNotFoundError:
            if allow_missing:
                logger.debug(
                    "Skip delete: file missing during delete base_dir=%s target=%s resolved_target=%s recursive=%s reason=missing_after_check",
                    base_resolved,
                    target_abs,
                    target_resolved,
                    recursive,
                )
                return False
            raise

    logger.info(
        "Deleted path base_dir=%s target=%s resolved_target=%s recursive=%s",
        base_resolved,
        target_abs,
        target_resolved,
        recursive,
    )
    return True


def get_store_path_for_hash(content_hash: str) -> Path:
    """Get the store path for a content hash.

    Uses first 2 characters as subdirectory for better filesystem distribution.

    Args:
        content_hash: The content hash (hex string)

    Returns:
        Path like /store/ab/abc123.../
    """
    prefix = content_hash[:2]
    store_dir = get_store_dir()
    return store_dir / prefix / content_hash


async def cleanup_task_download_dir(task_id: int) -> None:
    """Clean up the download directory for a task.

    Called after task completion or failure.

    Args:
        task_id: The global download ID
    """
    task_dir = get_downloading_dir() / str(task_id)
    try:
        safe_delete_path(
            base_dir=get_downloading_dir(),
            target=task_dir,
            recursive=True,
            allow_missing=True,
        )
        logger.info(f"Cleaned up task download directory: {task_dir}")
    except Exception as e:
        logger.error(f"Failed to clean up task directory {task_dir}: {e}")
        raise RuntimeError(f"Failed to clean up task directory: {task_dir}") from e
