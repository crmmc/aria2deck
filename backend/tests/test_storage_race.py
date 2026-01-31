"""Test race condition handling in storage service.

Tests for:
1. ref_count atomic operations (increment/decrement)
2. File move race conditions
3. StoredFile creation race conditions
"""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from app.database import get_session, reset_engine, init_db as init_sqlmodel_db, dispose_engine
from app.db import init_db
from app.core.config import settings
from app.models import StoredFile, UserFile, User, utc_now_str


@pytest.fixture(scope="function")
def temp_db_storage():
    """Create a fresh temporary database for storage tests."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    download_dir = os.path.join(temp_dir, "downloads")
    store_dir = os.path.join(download_dir, "store")
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(store_dir, exist_ok=True)

    original_db_path = settings.database_path
    original_download_dir = settings.download_dir
    settings.database_path = db_path
    settings.download_dir = download_dir

    reset_engine()
    init_db()
    asyncio.run(init_sqlmodel_db())

    yield {
        "db_path": db_path,
        "download_dir": download_dir,
        "store_dir": store_dir,
        "temp_dir": temp_dir,
    }

    asyncio.run(dispose_engine())
    settings.database_path = original_db_path
    settings.download_dir = original_download_dir
    reset_engine()


@pytest.fixture
def test_user_storage(temp_db_storage):
    """Create a test user for storage tests."""
    from app.db import execute
    from app.core.security import hash_password
    from datetime import datetime, timezone

    user_id = execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, quota)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["testuser", hash_password("testpass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
    )
    return {"id": user_id, "username": "testuser", "quota": 100 * 1024 * 1024 * 1024}


@pytest.fixture
def test_stored_file(temp_db_storage):
    """Create a test stored file."""
    async def _create():
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash="abc123def456",
                real_path=os.path.join(temp_db_storage["store_dir"], "ab", "abc123def456"),
                size=1024,
                is_directory=False,
                original_name="test.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            return stored_file

    return asyncio.run(_create())


class TestRefCountConcurrentIncrement:
    """Test concurrent increment of ref_count."""

    @pytest.mark.asyncio
    async def test_ref_count_concurrent_increment(self, temp_db_storage, test_user_storage, test_stored_file):
        """Simulate concurrent calls to create_user_file_reference for the same stored file.

        Multiple concurrent requests should result in correct ref_count.
        """
        from app.services.storage import create_user_file_reference

        # Create multiple users
        from app.db import execute
        from app.core.security import hash_password
        from datetime import datetime, timezone

        user_ids = [test_user_storage["id"]]
        for i in range(4):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"user{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        stored_file_id = test_stored_file.id

        # Run concurrent create_user_file_reference calls
        async def create_ref(user_id):
            return await create_user_file_reference(
                user_id=user_id,
                stored_file_id=stored_file_id,
                display_name="test.txt",
            )

        results = await asyncio.gather(*[create_ref(uid) for uid in user_ids])

        # All should succeed (different users)
        successful = [r for r in results if r is not None]
        assert len(successful) == 5, f"Expected 5 successful references, got {len(successful)}"

        # Verify ref_count is correct
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file.ref_count == 5, f"Expected ref_count=5, got {stored_file.ref_count}"

    @pytest.mark.asyncio
    async def test_ref_count_duplicate_reference_rejected(self, temp_db_storage, test_user_storage, test_stored_file):
        """Same user creating duplicate reference should be rejected."""
        from app.services.storage import create_user_file_reference

        stored_file_id = test_stored_file.id
        user_id = test_user_storage["id"]

        # First call should succeed
        result1 = await create_user_file_reference(
            user_id=user_id,
            stored_file_id=stored_file_id,
            display_name="test.txt",
        )
        assert result1 is not None

        # Second call should return None (duplicate)
        result2 = await create_user_file_reference(
            user_id=user_id,
            stored_file_id=stored_file_id,
            display_name="test.txt",
        )
        assert result2 is None

        # ref_count should be 1
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file.ref_count == 1


class TestRefCountConcurrentDecrement:
    """Test concurrent decrement of ref_count."""

    @pytest.mark.asyncio
    async def test_ref_count_concurrent_decrement(self, temp_db_storage, test_user_storage, test_stored_file):
        """Simulate concurrent calls to delete_user_file_reference."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference
        from app.db import execute
        from app.core.security import hash_password
        from datetime import datetime, timezone

        # Create multiple users and references
        user_ids = [test_user_storage["id"]]
        for i in range(4):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"deluser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        stored_file_id = test_stored_file.id

        # Create references for all users
        user_file_ids = []
        for uid in user_ids:
            user_file = await create_user_file_reference(
                user_id=uid,
                stored_file_id=stored_file_id,
                display_name="test.txt",
            )
            if user_file:
                user_file_ids.append(user_file.id)

        assert len(user_file_ids) == 5

        # Verify ref_count is 5
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file.ref_count == 5

        # Delete 3 references concurrently
        async def delete_ref(user_file_id):
            return await delete_user_file_reference(user_file_id)

        results = await asyncio.gather(*[delete_ref(ufid) for ufid in user_file_ids[:3]])
        assert all(results), "All deletions should succeed"

        # Verify ref_count is 2
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file.ref_count == 2, f"Expected ref_count=2, got {stored_file.ref_count}"

    @pytest.mark.asyncio
    async def test_concurrent_delete_same_reference(self, temp_db_storage, test_user_storage, test_stored_file):
        """Deleting the same UserFile concurrently should only decrement once."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference

        stored_file_id = test_stored_file.id
        user_id = test_user_storage["id"]

        # Create a single user file reference
        user_file = await create_user_file_reference(
            user_id=user_id,
            stored_file_id=stored_file_id,
            display_name="test.txt",
        )
        assert user_file is not None

        # Delete the same reference concurrently
        results = await asyncio.gather(
            delete_user_file_reference(user_file.id),
            delete_user_file_reference(user_file.id),
            return_exceptions=True,
        )

        # One should succeed, one should fail (already deleted)
        success_count = sum(1 for r in results if r is True)
        fail_count = sum(1 for r in results if r is False)
        assert success_count == 1
        assert fail_count == 1

        # StoredFile should be deleted (ref_count <= 0)
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file is None


class TestRefCountMixedOperations:
    """Test concurrent increment and decrement operations."""

    @pytest.mark.asyncio
    async def test_ref_count_mixed_operations(self, temp_db_storage, test_user_storage, test_stored_file):
        """Concurrent increment and decrement operations should maintain consistency."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference
        from app.db import execute
        from app.core.security import hash_password
        from datetime import datetime, timezone

        stored_file_id = test_stored_file.id

        # Create initial references
        initial_user_ids = []
        for i in range(3):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"mixuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            initial_user_ids.append(user_id)

        initial_user_file_ids = []
        for uid in initial_user_ids:
            user_file = await create_user_file_reference(
                user_id=uid,
                stored_file_id=stored_file_id,
                display_name="test.txt",
            )
            if user_file:
                initial_user_file_ids.append(user_file.id)

        # Create new users for increment
        new_user_ids = []
        for i in range(3):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"newuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            new_user_ids.append(user_id)

        # Run mixed operations concurrently
        async def increment(user_id):
            return await create_user_file_reference(
                user_id=user_id,
                stored_file_id=stored_file_id,
                display_name="test.txt",
            )

        async def decrement(user_file_id):
            return await delete_user_file_reference(user_file_id)

        # 3 increments + 2 decrements = net +1
        tasks = [
            increment(new_user_ids[0]),
            decrement(initial_user_file_ids[0]),
            increment(new_user_ids[1]),
            decrement(initial_user_file_ids[1]),
            increment(new_user_ids[2]),
        ]

        await asyncio.gather(*tasks)

        # Final ref_count should be 3 (initial) + 3 (new) - 2 (deleted) = 4
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file.ref_count == 4, f"Expected ref_count=4, got {stored_file.ref_count}"


class TestFileMoveRace:
    """Test file move race conditions."""

    @pytest.mark.asyncio
    async def test_file_move_destination_exists(self, temp_db_storage):
        """Test handling when destination is created between check and move."""
        from app.services.storage import move_to_store

        # Create source file
        source_dir = Path(temp_db_storage["temp_dir"]) / "source"
        source_dir.mkdir(exist_ok=True)
        source_file = source_dir / "test.txt"
        source_file.write_text("test content")

        # Create destination before move (simulating race condition)
        store_dir = Path(temp_db_storage["store_dir"])

        # Calculate expected hash
        from app.services.hash import calculate_content_hash
        content_hash = calculate_content_hash(source_file)
        dest_dir = store_dir / content_hash[:2] / content_hash
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Write different content to destination (simulating another process)
        if dest_dir.is_dir():
            (dest_dir / "test.txt").write_text("existing content")

        # move_to_store should handle this gracefully
        stored_file = await move_to_store(source_file, "test.txt")

        assert stored_file is not None
        assert stored_file.content_hash == content_hash
        # Source should be deleted
        assert not source_file.exists()

    @pytest.mark.asyncio
    async def test_file_move_concurrent(self, temp_db_storage):
        """Test two processes moving to same destination."""
        from app.services.storage import move_to_store

        # Create two identical source files
        source_dir1 = Path(temp_db_storage["temp_dir"]) / "source1"
        source_dir2 = Path(temp_db_storage["temp_dir"]) / "source2"
        source_dir1.mkdir(exist_ok=True)
        source_dir2.mkdir(exist_ok=True)

        source_file1 = source_dir1 / "test.txt"
        source_file2 = source_dir2 / "test.txt"
        source_file1.write_text("identical content")
        source_file2.write_text("identical content")

        # Move both concurrently
        results = await asyncio.gather(
            move_to_store(source_file1, "test.txt"),
            move_to_store(source_file2, "test.txt"),
            return_exceptions=True,
        )

        # Both should succeed (one creates, one finds existing)
        successful = [r for r in results if isinstance(r, StoredFile)]
        assert len(successful) == 2, f"Expected 2 successful results, got {len(successful)}"

        # Both should return the same StoredFile
        assert successful[0].content_hash == successful[1].content_hash

        # Both source files should be deleted
        assert not source_file1.exists()
        assert not source_file2.exists()

        # Only one StoredFile record should exist
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.content_hash == successful[0].content_hash)
            )
            stored_files = result.all()
            assert len(stored_files) == 1


class TestStoredFileCreationRace:
    """Test StoredFile creation race conditions."""

    @pytest.mark.asyncio
    async def test_stored_file_unique_constraint(self, temp_db_storage):
        """Test that duplicate content_hash is handled correctly."""
        from sqlalchemy.exc import IntegrityError
        from sqlmodel.ext.asyncio.session import AsyncSession
        from sqlalchemy.ext.asyncio import async_sessionmaker

        content_hash = "unique_hash_123"

        # Create first StoredFile
        async with get_session() as db:
            stored_file1 = StoredFile(
                content_hash=content_hash,
                real_path="/path/to/file1",
                size=1024,
                is_directory=False,
                original_name="file1.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file1)
            await db.commit()

        # Try to create second StoredFile with same content_hash
        # Use manual session to test IntegrityError without auto-commit interference
        from app.database import _get_session_maker
        session_maker = _get_session_maker()

        async with session_maker() as db:
            stored_file2 = StoredFile(
                content_hash=content_hash,
                real_path="/path/to/file2",
                size=2048,
                is_directory=False,
                original_name="file2.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file2)

            with pytest.raises(IntegrityError):
                await db.commit()


class TestRefCountDecrementDeletesFile:
    """Test that file is deleted when ref_count reaches 0."""

    @pytest.mark.asyncio
    async def test_concurrent_ref_count_decrement_deletes_file(self, temp_db_storage, test_user_storage):
        """When ref_count reaches 0, file is deleted."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference
        from app.db import execute
        from app.core.security import hash_password
        from datetime import datetime, timezone

        # Create physical file in store
        store_dir = Path(temp_db_storage["store_dir"])
        content_hash = "delete_test_hash_789"
        file_dir = store_dir / content_hash[:2] / content_hash
        file_dir.mkdir(parents=True, exist_ok=True)
        test_file = file_dir / "test.txt"
        test_file.write_text("content to be deleted")

        # Create StoredFile record
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash=content_hash,
                real_path=str(file_dir),
                size=1024,
                is_directory=True,
                original_name="test.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            stored_file_id = stored_file.id

        # Create single user reference
        user_file = await create_user_file_reference(
            user_id=test_user_storage["id"],
            stored_file_id=stored_file_id,
            display_name="test.txt",
        )
        assert user_file is not None

        # Verify ref_count is 1
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf.ref_count == 1

        # Delete the reference (should trigger file deletion)
        result = await delete_user_file_reference(user_file.id)
        assert result is True

        # Verify StoredFile record is deleted
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf is None, "StoredFile record should be deleted"

        # Verify physical file is deleted
        assert not file_dir.exists(), "Physical file should be deleted"

    @pytest.mark.asyncio
    async def test_ref_count_decrement_atomic(self, temp_db_storage, test_user_storage):
        """Concurrent decrements don't corrupt ref_count."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference
        from app.db import execute
        from app.core.security import hash_password
        from datetime import datetime, timezone

        # Create physical file in store
        store_dir = Path(temp_db_storage["store_dir"])
        content_hash = "atomic_decrement_hash_456"
        file_dir = store_dir / content_hash[:2] / content_hash
        file_dir.mkdir(parents=True, exist_ok=True)
        test_file = file_dir / "test.txt"
        test_file.write_text("atomic test content")

        # Create StoredFile record
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash=content_hash,
                real_path=str(file_dir),
                size=1024,
                is_directory=True,
                original_name="test.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            stored_file_id = stored_file.id

        # Create multiple users
        user_ids = [test_user_storage["id"]]
        for i in range(4):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"atomicuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create references for all users
        user_file_ids = []
        for uid in user_ids:
            user_file = await create_user_file_reference(
                user_id=uid,
                stored_file_id=stored_file_id,
                display_name="test.txt",
            )
            if user_file:
                user_file_ids.append(user_file.id)

        assert len(user_file_ids) == 5

        # Verify ref_count is 5
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf.ref_count == 5

        # Delete all references concurrently
        results = await asyncio.gather(
            *[delete_user_file_reference(ufid) for ufid in user_file_ids],
            return_exceptions=True,
        )

        # All should succeed
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Unexpected exceptions: {exceptions}"

        # Verify StoredFile is deleted (ref_count reached 0)
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf is None, "StoredFile should be deleted when ref_count reaches 0"

        # Verify physical file is deleted
        assert not file_dir.exists(), "Physical file should be deleted"

    @pytest.mark.asyncio
    async def test_partial_decrement_keeps_file(self, temp_db_storage, test_user_storage):
        """Partial decrements keep file when ref_count > 0."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference
        from app.db import execute
        from app.core.security import hash_password
        from datetime import datetime, timezone

        # Create physical file in store
        store_dir = Path(temp_db_storage["store_dir"])
        content_hash = "partial_decrement_hash_123"
        file_dir = store_dir / content_hash[:2] / content_hash
        file_dir.mkdir(parents=True, exist_ok=True)
        test_file = file_dir / "test.txt"
        test_file.write_text("partial test content")

        # Create StoredFile record
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash=content_hash,
                real_path=str(file_dir),
                size=1024,
                is_directory=True,
                original_name="test.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            stored_file_id = stored_file.id

        # Create 3 users
        user_ids = [test_user_storage["id"]]
        for i in range(2):
            user_id = execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, quota)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"partialuser{i}", hash_password("pass"), 0, datetime.now(timezone.utc).isoformat(), 100 * 1024 * 1024 * 1024]
            )
            user_ids.append(user_id)

        # Create references for all users
        user_file_ids = []
        for uid in user_ids:
            user_file = await create_user_file_reference(
                user_id=uid,
                stored_file_id=stored_file_id,
                display_name="test.txt",
            )
            if user_file:
                user_file_ids.append(user_file.id)

        assert len(user_file_ids) == 3

        # Delete only 2 references
        await delete_user_file_reference(user_file_ids[0])
        await delete_user_file_reference(user_file_ids[1])

        # Verify ref_count is 1
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf is not None, "StoredFile should still exist"
            assert sf.ref_count == 1

        # Verify physical file still exists
        assert file_dir.exists(), "Physical file should still exist"


class TestSameUserConcurrentReferenceCreation:
    """Test same user creating references concurrently (IntegrityError path)."""

    @pytest.mark.asyncio
    async def test_same_user_concurrent_reference_creation(self, temp_db_storage, test_user_storage, test_stored_file):
        """Same user creating reference concurrently should not corrupt ref_count.

        This tests the IntegrityError path in create_user_file_reference.
        When two concurrent requests from the same user try to create a reference,
        one succeeds and one gets IntegrityError. The rollback should correctly
        undo the ref_count increment without double-decrementing.
        """
        from app.services.storage import create_user_file_reference

        stored_file_id = test_stored_file.id
        user_id = test_user_storage["id"]

        # Run concurrent create_user_file_reference calls for the SAME user
        results = await asyncio.gather(
            create_user_file_reference(user_id=user_id, stored_file_id=stored_file_id),
            create_user_file_reference(user_id=user_id, stored_file_id=stored_file_id),
            create_user_file_reference(user_id=user_id, stored_file_id=stored_file_id),
            return_exceptions=True,
        )

        # Only one should succeed, others should return None (not raise exception)
        successful = [r for r in results if r is not None and not isinstance(r, Exception)]
        none_results = [r for r in results if r is None]
        exceptions = [r for r in results if isinstance(r, Exception)]

        assert len(successful) == 1, f"Expected exactly 1 successful reference, got {len(successful)}"
        assert len(none_results) == 2, f"Expected 2 None results, got {len(none_results)}"
        assert len(exceptions) == 0, f"Unexpected exceptions: {exceptions}"

        # CRITICAL: Verify ref_count is exactly 1 (not 0, not negative, not > 1)
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            stored_file = result.first()
            assert stored_file is not None, "StoredFile should still exist"
            assert stored_file.ref_count == 1, (
                f"Expected ref_count=1, got {stored_file.ref_count}. "
                "This indicates a bug in IntegrityError handling."
            )

        # Verify only one UserFile exists
        async with get_session() as db:
            result = await db.exec(
                select(UserFile).where(
                    UserFile.owner_id == user_id,
                    UserFile.stored_file_id == stored_file_id,
                )
            )
            user_files = result.all()
            assert len(user_files) == 1, f"Expected 1 UserFile, got {len(user_files)}"

    @pytest.mark.asyncio
    async def test_same_user_concurrent_then_delete(self, temp_db_storage, test_user_storage):
        """After concurrent creation race, deletion should work correctly."""
        from app.services.storage import create_user_file_reference, delete_user_file_reference

        # Create physical file in store
        store_dir = Path(temp_db_storage["store_dir"])
        content_hash = "same_user_race_hash_999"
        file_dir = store_dir / content_hash[:2] / content_hash
        file_dir.mkdir(parents=True, exist_ok=True)
        test_file = file_dir / "test.txt"
        test_file.write_text("race test content")

        # Create StoredFile record
        async with get_session() as db:
            stored_file = StoredFile(
                content_hash=content_hash,
                real_path=str(file_dir),
                size=1024,
                is_directory=True,
                original_name="test.txt",
                ref_count=0,
                created_at=utc_now_str(),
            )
            db.add(stored_file)
            await db.commit()
            await db.refresh(stored_file)
            stored_file_id = stored_file.id

        user_id = test_user_storage["id"]

        # Concurrent creation (same user)
        results = await asyncio.gather(
            create_user_file_reference(user_id=user_id, stored_file_id=stored_file_id),
            create_user_file_reference(user_id=user_id, stored_file_id=stored_file_id),
            return_exceptions=True,
        )

        successful = [r for r in results if r is not None and not isinstance(r, Exception)]
        assert len(successful) == 1

        user_file = successful[0]

        # Verify ref_count is 1
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf.ref_count == 1

        # Delete the reference
        delete_result = await delete_user_file_reference(user_file.id)
        assert delete_result is True

        # Verify StoredFile is deleted (ref_count reached 0)
        async with get_session() as db:
            result = await db.exec(
                select(StoredFile).where(StoredFile.id == stored_file_id)
            )
            sf = result.first()
            assert sf is None, "StoredFile should be deleted when ref_count reaches 0"

        # Verify physical file is deleted
        assert not file_dir.exists(), "Physical file should be deleted"
