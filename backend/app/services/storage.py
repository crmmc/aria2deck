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

from sqlmodel import select

from app.core.config import settings
from app.database import get_session
from app.models import StoredFile, UserFile, utc_now_str
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

    # Move to store
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

    # Create StoredFile record
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
        await db.commit()
        await db.refresh(stored_file)
        return stored_file


async def create_user_file_reference(
    user_id: int,
    stored_file_id: int,
    display_name: str | None = None,
) -> UserFile | None:
    """Create a user file reference to a stored file.

    Increments the reference count on the StoredFile.

    Args:
        user_id: The user ID
        stored_file_id: The StoredFile ID
        display_name: Optional custom display name

    Returns:
        UserFile record or None if already exists
    """
    async with get_session() as db:
        # Check if reference already exists
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

        # Get stored file for default display name
        result = await db.exec(
            select(StoredFile).where(StoredFile.id == stored_file_id)
        )
        stored_file = result.first()
        if not stored_file:
            logger.error(f"StoredFile {stored_file_id} not found")
            return None

        # Create reference
        user_file = UserFile(
            owner_id=user_id,
            stored_file_id=stored_file_id,
            display_name=display_name or stored_file.original_name,
            created_at=utc_now_str(),
        )
        db.add(user_file)

        # Increment reference count
        stored_file.ref_count += 1
        db.add(stored_file)

        await db.commit()
        await db.refresh(user_file)

        logger.info(
            f"Created user file reference: user={user_id}, "
            f"stored_file={stored_file_id}, ref_count={stored_file.ref_count}"
        )
        return user_file


async def delete_user_file_reference(user_file_id: int) -> bool:
    """Delete a user file reference.

    Decrements the reference count and deletes the physical file
    if no more references exist.

    Args:
        user_file_id: The UserFile ID to delete

    Returns:
        True if deleted successfully
    """
    async with get_session() as db:
        result = await db.exec(select(UserFile).where(UserFile.id == user_file_id))
        user_file = result.first()
        if not user_file:
            return False

        stored_file_id = user_file.stored_file_id

        # Delete the reference
        await db.delete(user_file)

        # Decrement reference count
        result = await db.exec(
            select(StoredFile).where(StoredFile.id == stored_file_id)
        )
        stored_file = result.first()
        if stored_file:
            stored_file.ref_count -= 1
            db.add(stored_file)

            # If no more references, delete the physical file
            if stored_file.ref_count <= 0:
                await db.commit()
                await _delete_stored_file(stored_file)
            else:
                await db.commit()
                logger.info(
                    f"Deleted user file reference {user_file_id}, "
                    f"ref_count now {stored_file.ref_count}"
                )
        else:
            await db.commit()

        return True


async def _delete_stored_file(stored_file: StoredFile) -> None:
    """Delete a stored file and its record.

    Args:
        stored_file: The StoredFile to delete
    """
    real_path = Path(stored_file.real_path)

    # Delete physical file
    if real_path.exists():
        try:
            if real_path.is_dir():
                shutil.rmtree(real_path)
            else:
                real_path.unlink()
            logger.info(f"Deleted physical file: {real_path}")
        except Exception as e:
            logger.error(f"Failed to delete physical file {real_path}: {e}")

    # Delete database record
    async with get_session() as db:
        result = await db.exec(
            select(StoredFile).where(StoredFile.id == stored_file.id)
        )
        db_stored_file = result.first()
        if db_stored_file:
            await db.delete(db_stored_file)
            await db.commit()
            logger.info(f"Deleted StoredFile record: {stored_file.id}")


async def cleanup_orphaned_stored_files() -> int:
    """Clean up stored files with zero references.

    Returns:
        Number of files cleaned up
    """
    count = 0
    async with get_session() as db:
        result = await db.exec(
            select(StoredFile).where(StoredFile.ref_count <= 0)
        )
        orphaned = result.all()

        for stored_file in orphaned:
            await _delete_stored_file(stored_file)
            count += 1

    if count > 0:
        logger.info(f"Cleaned up {count} orphaned stored files")
    return count


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
        except Exception as e:
            logger.error(f"Failed to clean up task directory {task_dir}: {e}")


def get_user_used_space(user_id: int) -> int:
    """Calculate user's used space from UserFile references.

    This is a synchronous function for use in space calculations.

    Args:
        user_id: The user ID

    Returns:
        Total bytes used by user's files
    """
    import asyncio

    async def _get_used():
        async with get_session() as db:
            result = await db.exec(
                select(UserFile, StoredFile)
                .join(StoredFile, UserFile.stored_file_id == StoredFile.id)
                .where(UserFile.owner_id == user_id)
            )
            rows = result.all()
            return sum(stored_file.size for _, stored_file in rows)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context, create a new task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, _get_used())
                return future.result()
        else:
            return loop.run_until_complete(_get_used())
    except RuntimeError:
        return asyncio.run(_get_used())


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
    """Get comprehensive space information for a user.

    Args:
        user_id: The user ID
        user_quota: User's quota in bytes

    Returns:
        Dict with used, frozen, available, and quota
    """
    used = await get_user_used_space_async(user_id)
    frozen = await get_user_frozen_space(user_id)

    # Get machine free space
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    machine_free = disk.free

    # Available = min(quota - used - frozen, machine_free)
    quota_available = max(0, user_quota - used - frozen)
    available = min(quota_available, machine_free)

    return {
        "quota": user_quota,
        "used": used,
        "frozen": frozen,
        "available": available,
    }
