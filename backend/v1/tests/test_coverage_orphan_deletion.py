"""Coverage supplements for orphan_cleanup and deletion_cleanup."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services import deletion_cleanup as dc
from app.services.deletion_cleanup import DeletionCleanupManager
from tests.helpers_v0 import create_user_v0


# ---------------------------------------------------------------------------
# orphan_cleanup
# ---------------------------------------------------------------------------


def _patch_orphan(store_dir: Path, db_paths_sets, delete_path=None):
    state = {"i": 0}

    async def _list_paths():
        i = min(state["i"], len(db_paths_sets) - 1)
        state["i"] += 1
        return set(db_paths_sets[i])

    list_paths = AsyncMock(side_effect=_list_paths)
    if delete_path is None:
        delete_path = Mock(return_value=True)
    patches = (
        patch("app.services.orphan_cleanup.get_store_dir", return_value=store_dir),
        patch("app.services.orphan_cleanup.list_stored_file_real_paths", list_paths),
        patch("app.services.orphan_cleanup.safe_delete_path", delete_path),
    )
    return patches, list_paths, delete_path


@pytest.mark.asyncio
async def test_orphan_cleanup_store_dir_missing(tmp_path):
    from app.services.orphan_cleanup import cleanup_orphan_files

    with patch("app.services.orphan_cleanup.get_store_dir", return_value=tmp_path / "nope"):
        assert await cleanup_orphan_files() == 0


@pytest.mark.asyncio
async def test_orphan_cleanup_legacy_layout_deletes_orphan(tmp_path):
    from app.services.orphan_cleanup import cleanup_orphan_files

    store = tmp_path / "store"
    (store / "ab").mkdir(parents=True)
    orphan = store / "ab" / "hash-orphan"
    orphan.write_bytes(b"x")
    kept = store / "ab" / "hash-kept"
    kept.write_bytes(b"y")
    # top-level non-dir entries are skipped entirely
    (store / "loose-file").write_bytes(b"z")

    patches, list_paths, delete_path = _patch_orphan(store, [[str(kept)]])
    with patches[0], patches[1], patches[2]:
        deleted = await cleanup_orphan_files()

    assert deleted == 1
    assert delete_path.call_count == 1
    assert list_paths.await_count == 2  # initial snapshot + recheck before delete


@pytest.mark.asyncio
async def test_orphan_cleanup_v2_layout(tmp_path):
    from app.services.orphan_cleanup import cleanup_orphan_files

    store = tmp_path / "store"
    prefix = store / "v2" / "file" / "ab"
    prefix.mkdir(parents=True)
    orphan = prefix / ("b" * 60)
    orphan.write_bytes(b"x")
    # wrong object-kind dir and non-dir prefix are ignored
    (store / "v2" / "other" / "cd").mkdir(parents=True)
    (store / "v2" / "file" / "loose").write_bytes(b"y")

    patches, _, delete_path = _patch_orphan(store, [[]])
    with patches[0], patches[1], patches[2]:
        deleted = await cleanup_orphan_files()

    assert deleted == 1
    assert delete_path.call_count == 1
    target = delete_path.call_args.kwargs["target"]
    assert Path(target) == orphan


@pytest.mark.asyncio
async def test_orphan_cleanup_registered_file_never_deleted(tmp_path):
    from app.services.orphan_cleanup import cleanup_orphan_files

    store = tmp_path / "store"
    (store / "ab").mkdir(parents=True)
    registered = store / "ab" / "hash-reg"
    registered.write_bytes(b"x")

    patches, list_paths, delete_path = _patch_orphan(store, [[str(registered)]])
    with patches[0], patches[1], patches[2]:
        assert await cleanup_orphan_files() == 0
    delete_path.assert_not_called()
    assert list_paths.await_count == 1


@pytest.mark.asyncio
async def test_orphan_cleanup_delete_error_is_swallowed(tmp_path):
    from app.services.orphan_cleanup import cleanup_orphan_files

    store = tmp_path / "store"
    (store / "ab").mkdir(parents=True)
    (store / "ab" / "hash-x").write_bytes(b"x")

    delete_path = Mock(side_effect=OSError("boom"))
    patches, _, _ = _patch_orphan(store, [[]], delete_path=delete_path)
    with patches[0], patches[1], patches[2]:
        assert await cleanup_orphan_files() == 0


# ---------------------------------------------------------------------------
# deletion_cleanup helpers
# ---------------------------------------------------------------------------


def test_next_retry_ms_clamps_large_attempts():
    assert dc._next_retry_ms(99) >= dc._now_ms() + dc._RETRY_DELAYS_MS[-1]


def test_safe_error_truncates():
    assert len(dc._safe_error("前缀", ValueError("x" * 2000))) <= 1000


def test_remove_tree_cancellable_nested_and_symlink(tmp_path):
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_bytes(b"x")
    link = tree / "link"
    os.symlink(tree / "sub" / "f", link)
    dc._remove_tree_cancellable(tree, threading.Event())
    assert not tree.exists()


def test_remove_tree_cancellable_missing_path(tmp_path):
    dc._remove_tree_cancellable(tmp_path / "never-existed", threading.Event())


def test_remove_tree_cancellable_interrupted(tmp_path):
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        dc._remove_tree_cancellable(tree, cancel)
    assert tree.exists()


def test_delete_stored_path_guards(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(dc, "get_store_dir", lambda: store)
    with pytest.raises(ValueError):
        dc._delete_stored_path({"id": 1, "real_path": str(tmp_path / "out")},
                               threading.Event())
    with pytest.raises(ValueError):
        dc._delete_stored_path({"id": 1, "real_path": str(store)},
                               threading.Event())


def test_delete_stored_path_tombstone_conflict(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(dc, "get_store_dir", lambda: store)
    path = store / "f"
    path.write_bytes(b"x")
    tomb = path.parent / ".f.aria2deck-delete-1"
    tomb.write_bytes(b"t")
    with pytest.raises(FileExistsError):
        dc._delete_stored_path({"id": 1, "real_path": str(path)},
                               threading.Event())


def test_delete_stored_path_missing_path_fsyncs_parent(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(dc, "get_store_dir", lambda: store)
    path = store / "gone"
    dc._delete_stored_path({"id": 1, "real_path": str(path)}, threading.Event())


def test_delete_stored_path_moves_then_removes(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(dc, "get_store_dir", lambda: store)
    path = store / "f"
    path.write_bytes(b"x")
    dc._delete_stored_path({"id": 7, "real_path": str(path)}, threading.Event())
    assert not path.exists()


# ---------------------------------------------------------------------------
# DeletionCleanupManager lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_cancelled_during_run_once(monkeypatch):
    async def slow_run_once():
        await asyncio.sleep(30)

    monkeypatch.setattr(DeletionCleanupManager, "run_once", slow_run_once)
    await DeletionCleanupManager.start()
    await asyncio.sleep(0.05)
    await DeletionCleanupManager.shutdown()


@pytest.mark.asyncio
async def test_worker_done_callback_clears_task(monkeypatch):
    monkeypatch.setattr(dc, "_SWEEP_SECONDS", 0.01)
    monkeypatch.setattr(DeletionCleanupManager, "run_once", AsyncMock(return_value=0))
    DeletionCleanupManager._wake_event = asyncio.Event()
    task = asyncio.get_running_loop().create_task(DeletionCleanupManager._worker_loop())
    DeletionCleanupManager._worker_task = task
    task.add_done_callback(DeletionCleanupManager._consume_worker_done)
    DeletionCleanupManager._wake_event = None  # loop exits after current wait
    await asyncio.wait_for(task, timeout=2)
    assert DeletionCleanupManager._worker_task is None


@pytest.mark.asyncio
async def test_recover_startup_cancels_pending_users(monkeypatch):
    cancel_calls = []

    class FakePack:
        @staticmethod
        async def cancel_user_jobs(user_id):
            cancel_calls.append(user_id)

    monkeypatch.setattr(dc.auth_repo, "list_pending_user_ids",
                        AsyncMock(return_value=[3, 5]))
    import app.modules.pack as pack_mod

    monkeypatch.setattr(pack_mod, "PackTaskManager", FakePack)
    await DeletionCleanupManager.recover_startup()
    assert cancel_calls == [3, 5]


@pytest.mark.asyncio
async def test_worker_start_wake_shutdown(monkeypatch):
    monkeypatch.setattr(dc, "_SWEEP_SECONDS", 0.01)
    run_once = AsyncMock(side_effect=[ValueError("scan fail"), 1, 0, 0])
    monkeypatch.setattr(DeletionCleanupManager, "run_once", run_once)

    await DeletionCleanupManager.start()
    await DeletionCleanupManager.start()  # idempotent
    DeletionCleanupManager.wake()

    for _ in range(200):
        if run_once.await_count >= 3:
            break
        await asyncio.sleep(0.01)
    assert run_once.await_count >= 3  # exception did not kill the worker
    await DeletionCleanupManager.shutdown()
    await DeletionCleanupManager.shutdown()  # no worker is a no-op
    assert DeletionCleanupManager._worker_task is None


def test_consume_worker_done_exception_and_cancel():
    async def boom():
        raise RuntimeError("x")

    async def cancelled():
        await asyncio.sleep(10)

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(boom())
        task.add_done_callback(DeletionCleanupManager._consume_worker_done)
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))

        DeletionCleanupManager._worker_task = None
        task2 = loop.create_task(cancelled())
        task2.add_done_callback(DeletionCleanupManager._consume_worker_done)
        task2.cancel()
        loop.run_until_complete(asyncio.gather(task2, return_exceptions=True))
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_worker_loop_returns_when_event_removed(monkeypatch):
    monkeypatch.setattr(dc, "_SWEEP_SECONDS", 0.01)
    monkeypatch.setattr(DeletionCleanupManager, "run_once", AsyncMock(return_value=0))
    DeletionCleanupManager._wake_event = asyncio.Event()
    # simulate external shutdown clearing the event slot
    async def _drop_event():
        DeletionCleanupManager._wake_event = None
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, lambda: asyncio.ensure_future(_drop_event()))
    await asyncio.wait_for(DeletionCleanupManager._worker_loop(), timeout=2)
    DeletionCleanupManager._worker_task = None


@pytest.mark.asyncio
async def test_process_file_physical_failure_retries(monkeypatch):
    def bad_delete(row, ev):
        raise OSError("io")

    monkeypatch.setattr(dc, "_delete_stored_path", bad_delete)
    retry = AsyncMock()
    monkeypatch.setattr(dc.files_repo, "retry_claimed_stored_file_delete", retry)
    await DeletionCleanupManager._process_file(
        {"id": 2, "content_hash": "h", "delete_attempts": 1}, "token"
    )
    retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_user_cancelled(monkeypatch):
    async def cancelled_cleanup(*args):
        await asyncio.sleep(30)

    monkeypatch.setattr(DeletionCleanupManager, "_cleanup_user", cancelled_cleanup)
    task = asyncio.create_task(
        DeletionCleanupManager._process_user({"id": 1, "delete_attempts": 0}, "t")
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_process_file_finalize_none_logs(monkeypatch):
    monkeypatch.setattr(dc, "_delete_stored_path", lambda row, ev: None)
    monkeypatch.setattr(
        dc.files_repo, "hard_delete_claimed_stored_file", AsyncMock(return_value=False)
    )
    await DeletionCleanupManager._process_file(
        {"id": 1, "content_hash": "h", "delete_attempts": 0}, "token"
    )


@pytest.mark.asyncio
async def test_process_file_cancelled(monkeypatch):
    started = asyncio.Event()

    def slow_delete(row, ev):
        started.set()
        ev.wait(timeout=5)

    monkeypatch.setattr(dc, "_delete_stored_path", slow_delete)

    async def call():
        await DeletionCleanupManager._process_file(
            {"id": 1, "content_hash": "h", "delete_attempts": 0}, "token"
        )

    task = asyncio.create_task(call())
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_process_user_exception_retries(monkeypatch):
    retry = AsyncMock()
    monkeypatch.setattr(dc.auth_repo, "retry_claimed_user_delete", retry)
    monkeypatch.setattr(
        DeletionCleanupManager,
        "_cleanup_user",
        AsyncMock(side_effect=RuntimeError("cleanup fail")),
    )
    await DeletionCleanupManager._process_user({"id": 9, "delete_attempts": 0}, "t")
    retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_user_not_completed_reschedules(monkeypatch):
    retry = AsyncMock()
    monkeypatch.setattr(dc.auth_repo, "retry_claimed_user_delete", retry)
    monkeypatch.setattr(
        DeletionCleanupManager, "_cleanup_user", AsyncMock(return_value=False)
    )
    await DeletionCleanupManager._process_user({"id": 9, "delete_attempts": 0}, "t")
    retry.assert_awaited_once()
    assert retry.await_args.kwargs["error"] is None


@pytest.mark.asyncio
async def test_cleanup_user_paths(monkeypatch, temp_db):
    import app.modules.pack as pack_mod

    user = await create_user_v0(username="dcu")

    class FakePack:
        prepare = True

        @staticmethod
        async def prepare_user_deletion(uid):
            return FakePack.prepare

        @staticmethod
        async def unblock_user(uid):
            pass

    monkeypatch.setattr(pack_mod, "PackTaskManager", FakePack)
    monkeypatch.setattr(
        dc.auth_repo, "renew_claimed_user_delete", AsyncMock(return_value=True)
    )
    claimed = {"id": user["id"], "quota_bytes": 100, "delete_attempts": 0}

    # prepare_user_deletion not ready
    FakePack.prepare = False
    assert not await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)
    FakePack.prepare = True

    # lease lost on second renew
    renew = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(dc.auth_repo, "renew_claimed_user_delete", renew)
    with pytest.raises(RuntimeError):
        await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)
    monkeypatch.setattr(
        dc.auth_repo, "renew_claimed_user_delete", AsyncMock(return_value=True)
    )

    # active tasks remain after cancel attempts
    from app.db.engine import transaction
    from app.db.schema import user_tasks
    from app.core.time_utils import now_ms as _now

    dl = await create_global_for_user(user["id"])
    monkeypatch.setattr(
        dc.downloads_repo,
        "list_user_tasks",
        AsyncMock(
            side_effect=[
                [{"id": 1, "global_download_id": dl}],
                [{"id": 1, "global_download_id": dl}],
            ]
        ),
    )
    with patch.object(dc, "cancel_task", new=AsyncMock()):
        assert not await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)

    # no tasks left; pending user-file identity missing → reschedule
    monkeypatch.setattr(
        dc.downloads_repo, "list_user_tasks", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        dc.auth_repo, "delete_terminal_user_tasks_for_cleanup", AsyncMock()
    )
    monkeypatch.setattr(
        dc.files_repo, "list_pending_user_file_ids", AsyncMock(return_value=[11])
    )
    monkeypatch.setattr(
        dc.files_repo,
        "get_pending_user_file_delete_identity",
        AsyncMock(return_value=None),
    )
    assert not await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)


@pytest.mark.asyncio
async def test_cleanup_user_file_delete_refused(monkeypatch, temp_db):
    import app.modules.pack as pack_mod

    user = await create_user_v0(username="dcu2")

    class FakePack:
        @staticmethod
        async def prepare_user_deletion(uid):
            return True

        @staticmethod
        async def unblock_user(uid):
            pass

    monkeypatch.setattr(pack_mod, "PackTaskManager", FakePack)
    monkeypatch.setattr(
        dc.auth_repo, "renew_claimed_user_delete", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        dc.downloads_repo, "list_user_tasks", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        dc.auth_repo, "delete_terminal_user_tasks_for_cleanup", AsyncMock()
    )
    claimed = {"id": user["id"], "quota_bytes": 0, "delete_attempts": 0}

    # delete_user_file_reference refuses → False (line: not deleted)
    identity = {
        "content_hash": "h",
        "stored_file_id": 1,
        "created_at_ms": 1,
    }
    monkeypatch.setattr(
        dc.files_repo,
        "list_pending_user_file_ids",
        AsyncMock(side_effect=[[11], [11]]),
    )
    monkeypatch.setattr(
        dc.files_repo,
        "get_pending_user_file_delete_identity",
        AsyncMock(return_value=identity),
    )
    lock = AsyncMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(dc, "get_content_hash_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(
        dc.files_repo,
        "delete_user_file_reference",
        AsyncMock(return_value=(False, [], None)),
    )
    assert not await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)

    # hard_delete_claimed_user refuses → False
    monkeypatch.setattr(
        dc.files_repo, "list_pending_user_file_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        dc.auth_repo, "hard_delete_claimed_user", AsyncMock(return_value=False)
    )
    assert not await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)


async def create_global_for_user(user_id: int) -> int:
    from app.db.engine import transaction
    from app.db.schema import global_downloads, user_tasks
    from app.core.time_utils import now_ms as _now

    ts = _now()
    async with transaction() as conn:
        dl = (
            await conn.execute(
                global_downloads.insert()
                .values(
                    resource_key=f"http:dc-{user_id}",
                    resource_kind="http",
                    source_uri="https://example.com",
                    status="active",
                    aria2_gid="gid-dc",
                    created_at_ms=ts,
                    updated_at_ms=ts,
                )
                .returning(global_downloads.c.id)
            )
        ).scalar()
        await conn.execute(
            user_tasks.insert().values(
                user_id=user_id,
                global_download_id=dl,
                status="active",
                created_at_ms=ts,
                updated_at_ms=ts,
            )
        )
    return dl


@pytest.mark.asyncio
async def test_cleanup_user_success(monkeypatch, temp_db):
    import app.modules.pack as pack_mod
    from app.services import task_broadcast

    user = await create_user_v0(username="dcu3")

    class FakePack:
        @staticmethod
        async def prepare_user_deletion(uid):
            return True

        @staticmethod
        async def unblock_user(uid):
            pass

    monkeypatch.setattr(pack_mod, "PackTaskManager", FakePack)
    monkeypatch.setattr(
        dc.auth_repo, "renew_claimed_user_delete", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        dc.downloads_repo, "list_user_tasks", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        dc.auth_repo, "delete_terminal_user_tasks_for_cleanup", AsyncMock()
    )
    monkeypatch.setattr(
        dc.auth_repo, "hard_delete_claimed_user", AsyncMock(return_value=True)
    )
    removed = AsyncMock()
    monkeypatch.setattr(task_broadcast, "remove_connections_for_user", removed)

    identity = {"content_hash": "h", "stored_file_id": 1, "created_at_ms": 1}
    monkeypatch.setattr(
        dc.files_repo, "list_pending_user_file_ids", AsyncMock(side_effect=[[11], []])
    )
    monkeypatch.setattr(
        dc.files_repo,
        "get_pending_user_file_delete_identity",
        AsyncMock(return_value=identity),
    )
    lock = AsyncMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(dc, "get_content_hash_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(
        dc.files_repo,
        "delete_user_file_reference",
        AsyncMock(return_value=(True, [77], "/tmp/x")),
    )

    claimed = {"id": user["id"], "quota_bytes": 0, "delete_attempts": 0}
    assert await DeletionCleanupManager._cleanup_user(user["id"], "t", claimed)
    removed.assert_awaited_once_with(user["id"])
