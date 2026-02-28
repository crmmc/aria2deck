"""Storage management service for shared download architecture.

Handles:
1. Moving completed files to /store/{content_hash}/
2. Managing reference counts
3. Cleaning up unreferenced files
4. Directory structure management
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, update, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.config import settings
from app.database import get_session
from app.models import DownloadTask, StoredFile, UserFile, UserTaskSubscription, utc_now_str
from app.services.hash import calculate_content_hash

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
        task_id: The DownloadTask ID

    Returns:
        Path to the task's download directory
    """
    task_dir = get_downloading_dir() / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


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


async def move_to_store(
    source_path: Path,
    original_name: str,
) -> StoredFile:
    """Move a completed download to the store.

    Calculates content hash, moves file to store location,
    and creates or returns existing StoredFile record.

    Args:
        source_path: Path to the completed file/directory
        original_name: Original filename for display

    Returns:
        StoredFile record (new or existing)
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source_path}")

    # Calculate content hash
    content_hash = calculate_content_hash(source_path)

    # Check if already stored
    async with get_session() as db:
        result = await db.exec(
            select(StoredFile).where(StoredFile.content_hash == content_hash)
        )
        existing = result.first()

        if existing:
            # File already exists in store, delete the duplicate
            logger.info(
                f"File already in store: {content_hash}, deleting duplicate at {source_path}"
            )
            if source_path.is_dir():
                shutil.rmtree(source_path)
            else:
                source_path.unlink()
            return existing

    # Calculate size
    if source_path.is_dir():
        size = sum(f.stat().st_size for f in source_path.rglob("*") if f.is_file())
        is_directory = True
    else:
        size = source_path.stat().st_size
        is_directory = False

    # Determine store path
    store_path = get_store_path_for_hash(content_hash)
    store_path.parent.mkdir(parents=True, exist_ok=True)

    # Move to store with race condition handling
    try:
        if store_path.exists():
            # Race condition: another process created it
            logger.warning(f"Store path already exists: {store_path}")
            if source_path.is_dir():
                shutil.rmtree(source_path)
            else:
                source_path.unlink()
        else:
            shutil.move(str(source_path), str(store_path))
            logger.info(f"Moved {source_path} to {store_path}")
    except shutil.Error as e:
        # Handle race condition where path was created between check and move
        if "already exists" in str(e).lower() or store_path.exists():
            logger.warning(f"Race condition during move: {e}")
            if source_path.exists():
                if source_path.is_dir():
                    shutil.rmtree(source_path)
                else:
                    source_path.unlink()
        else:
            raise
    except FileExistsError:
        # Another process created the destination
        logger.warning(f"FileExistsError during move to {store_path}")
        if source_path.exists():
            if source_path.is_dir():
                shutil.rmtree(source_path)
            else:
                source_path.unlink()

    # Create StoredFile record with race condition handling
    async with get_session() as db:
        # Double-check for race condition
        result = await db.exec(
            select(StoredFile).where(StoredFile.content_hash == content_hash)
        )
        existing = result.first()
        if existing:
            return existing

        stored_file = StoredFile(
            content_hash=content_hash,
            real_path=str(store_path),
            size=size,
            is_directory=is_directory,
            original_name=original_name,
            ref_count=0,
            created_at=utc_now_str(),
        )
        db.add(stored_file)

        try:
            await db.commit()
            await db.refresh(stored_file)
            return stored_file
        except Exception as e:
            # UNIQUE constraint violation - another process created it
            await db.rollback()
            logger.info(f"Race condition on StoredFile creation: {content_hash}, fetching existing")

            # Fetch the record created by the other process
            result = await db.exec(
                select(StoredFile).where(StoredFile.content_hash == content_hash)
            )
            existing = result.first()
            if existing:
                return existing

            # If still not found, re-raise the original error
            raise RuntimeError(f"Failed to create or find StoredFile: {content_hash}") from e


async def register_pack_output(
    output_path: Path,
    original_name: str,
    user_id: int,
) -> tuple[StoredFile, UserFile | None]:
    stored_file = await move_to_store(output_path, original_name)
    user_file = await create_user_file_reference(
        user_id=user_id,
        stored_file_id=stored_file.id,
        display_name=original_name,
    )
    return stored_file, user_file


async def create_user_file_reference(
    user_id: int,
    stored_file_id: int,
    display_name: str | None = None,
) -> UserFile | None:
    """Create a user file reference to a stored file.

    Increments the reference count on the StoredFile.
    Uses atomic ref_count increment to prevent race with deletion.

    Args:
        user_id: The user ID
        stored_file_id: The StoredFile ID
        display_name: Optional custom display name

    Returns:
        UserFile record or None if already exists or StoredFile was deleted
    """
    async with get_session() as db:
        result = await db.exec(
            select(UserFile).where(
                UserFile.owner_id == user_id,
                UserFile.stored_file_id == stored_file_id,
            )
        )
        existing = result.first()
        if existing:
            logger.debug(
                f"User {user_id} already has reference to stored file {stored_file_id}"
            )
            return None

        result = await db.execute(
            update(StoredFile)
            .where(StoredFile.id == stored_file_id)
            .values(ref_count=StoredFile.ref_count + 1)
            .returning(StoredFile)
        )
        stored_file = result.scalar_one_or_none()

        if not stored_file:
            logger.warning(
                f"StoredFile {stored_file_id} not found or deleted during reference creation"
            )
            return None

        user_file = UserFile(
            owner_id=user_id,
            stored_file_id=stored_file_id,
            display_name=display_name or stored_file.original_name,
            created_at=utc_now_str(),
        )
        db.add(user_file)

        try:
            await db.commit()
            await db.refresh(user_file)
            logger.info(
                f"Created user file reference: user={user_id}, "
                f"stored_file={stored_file_id}"
            )
            return user_file
        except IntegrityError:
            await db.rollback()
            logger.debug(
                f"Race condition: user {user_id} already has reference to stored file {stored_file_id}"
            )
            return None


async def _sync_task_state_for_deleted_stored_file(db, stored_file_id: int) -> None:
    result = await db.exec(
        select(DownloadTask.id).where(DownloadTask.stored_file_id == stored_file_id)
    )
    task_ids = [task_id for task_id in result.all() if task_id is not None]
    if not task_ids:
        return

    await db.execute(
        update(DownloadTask)
        .where(DownloadTask.id.in_(task_ids))
        .values(
            status="queued",
            stored_file_id=None,
            gid=None,
            completed_at=None,
            completed_length=0,
            download_speed=0,
            upload_speed=0,
            error=None,
            error_display=None,
            updated_at=utc_now_str(),
        )
    )

    await db.execute(
        update(UserTaskSubscription)
        .where(
            UserTaskSubscription.task_id.in_(task_ids),
            UserTaskSubscription.status.in_(["success", "pending", "active"]),
        )
        .values(
            status="failed",
            frozen_space=0,
            error_display="文件已删除，请重新添加任务下载",
        )
    )


async def delete_user_file_reference(user_file_id: int) -> bool:
    """Delete a user file reference.

    Decrements the reference count and deletes the physical file
    if no more references exist.

    Uses conditional delete to prevent race with create_user_file_reference:
    only deletes StoredFile if ref_count is still <= 0 at delete time.

    Args:
        user_file_id: The UserFile ID to delete

    Returns:
        True if deleted successfully
    """
    store_path_to_delete: str | None = None
    stored_file_id_to_delete: int | None = None

    async with get_session() as db:
        result = await db.exec(select(UserFile).where(UserFile.id == user_file_id))
        user_file = result.first()
        if not user_file:
            return False

        stored_file_id = user_file.stored_file_id
        
        if stored_file_id is None:
            return False

        # Delete the user reference atomically, avoid double-decrement on races
        delete_result = await db.execute(
            delete(UserFile).where(UserFile.id == user_file_id)
        )
        if delete_result.rowcount == 0:
            return False

        stmt = (
            update(StoredFile)
            .where(
                StoredFile.id == stored_file_id,
                StoredFile.ref_count > 0,
            )
            .values(ref_count=StoredFile.ref_count - 1)
            .returning(StoredFile)
        )
        result = await db.execute(stmt)
        stored_file = result.scalar_one_or_none()
        
        if stored_file is None:
            logger.warning(f"StoredFile {stored_file_id} not found or ref_count already 0")
            return True
        
        if stored_file.ref_count <= 0:
            await _sync_task_state_for_deleted_stored_file(db, stored_file_id)
            await db.execute(
                delete(StoredFile).where(StoredFile.id == stored_file_id)
            )
            store_path_to_delete = stored_file.real_path
            stored_file_id_to_delete = stored_file_id
            logger.info(f"StoredFile {stored_file_id} ref_count reached 0, deleted")
        else:
            logger.info(
                f"Deleted user file reference {user_file_id}, "
                f"ref_count now {stored_file.ref_count}"
            )
        # Transaction commits here

    # Delete physical file AFTER transaction commit (avoid I/O in transaction)
    if store_path_to_delete:
        await _delete_stored_file_by_path(store_path_to_delete)
        logger.info(f"Deleted StoredFile {stored_file_id_to_delete} and physical file")

    return True


async def _delete_stored_file_by_path(real_path: str) -> None:
    """Delete physical file by path."""
    path = Path(real_path)
    if path.exists():
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            logger.info(f"Deleted physical file: {path}")
        except Exception as e:
            logger.error(f"Failed to delete physical file {path}: {e}")


async def cleanup_task_download_dir(task_id: int) -> None:
    """Clean up the download directory for a task.

    Called after task completion or failure.

    Args:
        task_id: The DownloadTask ID
    """
    task_dir = get_downloading_dir() / str(task_id)
    if task_dir.exists():
        try:
            shutil.rmtree(task_dir)
            logger.info(f"Cleaned up task download directory: {task_dir}")
        except FileNotFoundError:
            logger.debug("Task directory already cleaned: %s", task_dir)
        except Exception as e:
            logger.error(f"Failed to clean up task directory {task_dir}: {e}")
            raise RuntimeError(f"Failed to clean up task directory: {task_dir}") from e


async def get_user_used_space_async(user_id: int) -> int:
    """Calculate user's used space from UserFile references (async version).

    Args:
        user_id: The user ID

    Returns:
        Total bytes used by user's files
    """
    async with get_session() as db:
        result = await db.exec(
            select(UserFile, StoredFile)
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
            .where(UserFile.owner_id == user_id)
        )
        rows = result.all()
        return sum(stored_file.size for _, stored_file in rows)


async def get_user_frozen_space(user_id: int) -> int:
    """Calculate user's frozen space from active subscriptions.

    Args:
        user_id: The user ID

    Returns:
        Total bytes frozen for pending downloads
    """
    from app.models import UserTaskSubscription

    async with get_session() as db:
        result = await db.exec(
            select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == user_id,
                UserTaskSubscription.status == "pending",
            )
        )
        subscriptions = result.all()
        return sum(sub.frozen_space for sub in subscriptions)


async def get_user_space_info(user_id: int, user_quota: int) -> dict:
    """Get comprehensive space information for a user with atomic calculation.

    Args:
        user_id: The user ID
        user_quota: User's quota in bytes

    Returns:
        Dict with used, frozen, available, and quota
    """
    from app.models import UserTaskSubscription

    async with get_session() as db:
        # Single query for used space (from UserFile + StoredFile)
        used_result = await db.exec(
            select(func.coalesce(func.sum(StoredFile.size), 0))
            .select_from(UserFile)
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
            .where(UserFile.owner_id == user_id)
        )
        used_space = used_result.one()

        # Single query for frozen space (from pending subscriptions)
        frozen_result = await db.exec(
            select(func.coalesce(func.sum(UserTaskSubscription.frozen_space), 0))
            .where(
                UserTaskSubscription.owner_id == user_id,
                UserTaskSubscription.status == "pending",
            )
        )
        frozen_space = frozen_result.one()

    # Get machine free space
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free

    # Available = min(quota - used - frozen, machine_free)
    quota_available = max(0, user_quota - used_space - frozen_space)
    available = min(quota_available, machine_free)

    return {
        "quota": user_quota,
        "used": used_space,
        "frozen": frozen_space,
        "available": available,
    }
