"""补充覆盖率测试：app/modules/pack/__init__.py 与 app/repositories/pack.py。"""
from __future__ import annotations

import asyncio
import errno
import io
import json
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import pack_task_sources, pack_tasks, stored_files, user_storage_usage
from app.domain.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.modules import pack as pack_service
from app.modules.pack import (
    PackBoundaryError,
    PackTaskManager,
    _ArchiveBudget,
    _BoundedSink,
    _CancelAwareReader,
    _ProgressTracker,
    _durable_copy_file,
    _durable_link_file,
    _fsync_file_and_parent,
    _prepare_source_delete,
    _remove_tree_cancellable,
    _unlink_file_and_fsync_parent,
    calculate_folder_size,
    cancel_or_delete_pack_task,
    create_pack_task_from_user_files,
    _validate_output_name,
)
from app.repositories.errors import RepositoryConflictError
from app.repositories import pack as pack_repo
from app.repositories.pack import (
    PackAdmissionError,
    _release_reservation_locked,
    clear_terminal_pack_tasks,
    create_pending_pack_with_reservation,
    delete_user_pack_task,
    fail_active_pack_task,
    finalize_prepared_pack_task,
    list_user_pack_cleanup_rows,
    mark_source_cleanup_complete,
    reserve_pack_install_bytes,
    schedule_pack_retry,
    settle_user_pack_markers,
)
from tests.helpers_v0 import create_user_file_v0, create_user_v0, now_ms
from tests.test_pack import _insert_pack_task


def _event() -> threading.Event:
    return threading.Event()


async def _make_file_user(
    *, username: str, content: bytes = b"hello", name: str = "src.txt"
) -> tuple[dict, dict, Path]:
    user = await create_user_v0(username=username, quota_bytes=10_000_000)
    source = Path(settings.download_dir) / "store" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=source,
        content_hash=f"hash_{username}",
        display_name=name,
        size_bytes=len(content),
    )
    return user, user_file, source


# ---------------------------------------------------------------- low-level sync helpers


@pytest.fixture
def plain_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 cleanup_pack_output 直接删除 tmp_path 下的临时文件。"""

    def _delete(path: Path) -> bool:
        Path(path).unlink(missing_ok=True)
        return True

    monkeypatch.setattr(pack_service, "cleanup_pack_output", _delete)



def test_bounded_sink_tell_seek_fileno_and_cancel(tmp_path: Path) -> None:
    path = tmp_path / "sink.bin"
    cancel = _event()
    with _BoundedSink(path, max_bytes=100, min_free_bytes=0, cancel_event=cancel) as sink:
        assert sink.writable()
        assert sink.seekable()
        sink.write(b"abc")
        assert sink.tell() == 3
        assert sink.seek(0) == 0
        assert sink.fileno() > 0
        cancel.set()
        with pytest.raises(InterruptedError):
            sink.write(b"x")
        cancel.clear()
    # 关闭后 flush 不应报错
    sink.flush()


def test_cleanup_pack_output_swallows_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.storage as storage

    monkeypatch.setattr(
        storage, "safe_delete_path", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert pack_service.cleanup_pack_output(tmp_path / "x") is False


def test_fsync_file_and_parent_cancel(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        _fsync_file_and_parent(path, cancel)


def test_unlink_missing_file_is_noop(tmp_path: Path) -> None:
    _unlink_file_and_fsync_parent(tmp_path / "missing")


def test_durable_link_cancel_and_unexpected_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plain_cleanup: None
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"data")
    target = tmp_path / "target"
    temp = tmp_path / "tmp"
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        _durable_link_file(src, target, temp, cancel)
    cancel.clear()
    exc = OSError(errno.ENOENT, "no ent")
    monkeypatch.setattr(pack_service.os, "link", lambda *_a: (_ for _ in ()).throw(exc))
    with pytest.raises(OSError):
        _durable_link_file(src, target, temp, cancel)


def test_durable_link_fsync_failure_unlinks_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plain_cleanup: None
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"data")
    target = tmp_path / "target"
    temp = tmp_path / "tmp"

    def boom(*_args: Any) -> None:
        raise RuntimeError("fsync failed")

    monkeypatch.setattr(pack_service, "_fsync_file_and_parent", boom)
    with pytest.raises(RuntimeError):
        _durable_link_file(src, target, temp, _event())
    assert not target.exists()


def test_durable_link_success(tmp_path: Path, plain_cleanup: None) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"data")
    target = tmp_path / "target"
    assert _durable_link_file(src, target, tmp_path / "tmp", _event()) is True
    assert target.read_bytes() == b"data"


def test_prepare_source_delete_branches(tmp_path: Path) -> None:
    cancel = _event()
    # original 与 tombstone 同名且存在
    tomb = tmp_path / ".aria2deck-pack-delete-1-0"
    tomb.write_bytes(b"x")
    assert _prepare_source_delete(tomb, tomb, cancel) is tomb
    # 同名且不存在
    missing = tmp_path / ".aria2deck-pack-delete-1-1"
    assert _prepare_source_delete(missing, missing, cancel) is None
    # original 缺失、tombstone 存在
    original = tmp_path / "orig_file"
    tomb2 = tmp_path / ".aria2deck-pack-delete-1-2"
    tomb2.write_bytes(b"x")
    assert _prepare_source_delete(original, tomb2, cancel) is tomb2
    # 两者都缺失
    tomb3 = tmp_path / ".aria2deck-pack-delete-1-3"
    assert _prepare_source_delete(tmp_path / "gone", tomb3, cancel) is None
    # 普通文件直接删除 → 返回 None
    original.write_bytes(b"y")
    tomb4 = tmp_path / ".aria2deck-pack-delete-1-4"
    assert _prepare_source_delete(original, tomb4, cancel) is None
    assert not original.exists()
    # 目录 + tombstone 已存在 → FileExistsError
    original_dir = tmp_path / "orig_dir"
    original_dir.mkdir()
    tomb5 = tmp_path / ".aria2deck-pack-delete-1-5"
    tomb5.mkdir()
    with pytest.raises(FileExistsError):
        _prepare_source_delete(original_dir, tomb5, cancel)
    # 普通文件直接删除 → 返回 None
    plain = tmp_path / "plain"
    plain.write_bytes(b"z")
    assert _prepare_source_delete(plain, tmp_path / ".aria2deck-pack-delete-1-6", cancel) is None
    assert not plain.exists()
    # 目录移动 → 返回 tombstone
    dir2 = tmp_path / "dir2"
    dir2.mkdir()
    tomb7 = tmp_path / ".aria2deck-pack-delete-1-7"
    assert _prepare_source_delete(dir2, tomb7, cancel) == tomb7


def test_prepare_source_delete_cancel(tmp_path: Path) -> None:
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        _prepare_source_delete(tmp_path / "a", tmp_path / "b", cancel)


def test_remove_tree_cancellable_cancel(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f").write_bytes(b"x")
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        _remove_tree_cancellable(root, cancel)
    cancel.clear()
    _remove_tree_cancellable(root, cancel)
    assert not root.exists()


def test_durable_copy_file_cancel_and_boundaries(
    tmp_path: Path, plain_cleanup: None
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"abcdef")
    target = tmp_path / "target"
    temp = tmp_path / "tmp"
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        _durable_copy_file(src, target, temp, cancel, 100, 0)
    cancel.clear()
    with pytest.raises(PackBoundaryError):
        _durable_copy_file(src, target, temp, cancel, 3, 0)
    assert not target.exists()
    with pytest.raises(PackBoundaryError):
        _durable_copy_file(src, target, temp, cancel, 100, 10**12)
    cancel.set()
    with pytest.raises(InterruptedError):
        _durable_copy_file(src, target, temp, cancel, 100, 0)
    # 写完后、替换前取消
    real_replace = pack_service.os.replace

    def replace_after_cancel(temp: Any, dest: Any) -> None:
        cancel.set()
        real_replace(temp, dest)

    with pytest.raises(InterruptedError):
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(pack_service.os, "replace", replace_after_cancel)
            _durable_copy_file(src, target, temp, cancel, 100, 0)
    cancel.clear()
    _durable_copy_file(src, target, temp, cancel, 100, 0)
    assert target.read_bytes() == b"abcdef"


def test_durable_copy_fsync_failure_cleans_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plain_cleanup: None
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"data")
    target = tmp_path / "target"

    def boom(*_args: Any) -> None:
        raise RuntimeError("fsync failed")

    monkeypatch.setattr(pack_service, "_fsync_file_and_parent", boom)
    with pytest.raises(RuntimeError):
        _durable_copy_file(src, target, tmp_path / "tmp", _event(), 100, 0)
    assert not target.exists()


def test_archive_budget_rejects_surrogate_encoding() -> None:
    budget = _ArchiveBudget()
    with pytest.raises(PackBoundaryError):
        budget.add("\ud800")


def test_progress_tracker_edges() -> None:
    tracker = _ProgressTracker(0)
    tracker.add(0)
    tracker.add(-1)
    assert tracker.snapshot() == (0, 0, 100)
    tracker2 = _ProgressTracker(10)
    tracker2.add(5)
    assert tracker2.snapshot() == (5, 10, 50)


def test_cancel_aware_reader_cancel(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    cancel = _event()
    cancel.set()
    reader = _CancelAwareReader(path.open("rb"), cancel, _ProgressTracker(1))
    with pytest.raises(InterruptedError):
        reader.read()
    reader._source.close()


def test_calculate_folder_size_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising_rglob(self: Path, _pattern: str) -> Any:
        raise OSError("io")
        yield  # pragma: no cover

    monkeypatch.setattr(Path, "rglob", raising_rglob)
    assert calculate_folder_size(Path("/anywhere")) == 0


def test_safe_archive_name_fallbacks() -> None:
    safe = PackTaskManager._safe_archive_name
    assert safe("", "") == "archive"
    assert safe(".", "..") == "archive"
    assert safe("  ", "con/name") == "name"
    assert safe("a/b\\c:d\x00*e?", "") == "b_c:d_*e?"
    assert safe("x", "x") == "x"
    assert safe("...", "") == "archive"


def test_join_arcname_variants() -> None:
    join = PackTaskManager._join_arcname
    dedupe = PackTaskManager._deduplicate_root_name
    assert join("", Path("."), "f", False) == "f"
    assert join("root", Path("a/b"), "d", True) == "root/a/b/d/"
    assert dedupe("n", {"n"}) == "n_1"
    assert dedupe("n", {"n", "n_1"}) == "n_2"


def test_validate_output_name_edges() -> None:
    _validate_output_name(None)
    _validate_output_name("")
    with pytest.raises(BadRequestError):
        _validate_output_name("n" * 201)
    with pytest.raises(BadRequestError):
        _validate_output_name("名" * 101)
    with pytest.raises(BadRequestError):
        _validate_output_name("a/b")


# ---------------------------------------------------------------- archive item building


def test_build_archive_items_directory_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.storage as storage

    monkeypatch.setattr(storage, "is_canonical_store_path", lambda _p, _h: False)
    root = tmp_path / "tree"
    (root / "sub1" / "sub2").mkdir(parents=True)
    (root / "sub1" / "sub2" / "leaf.txt").write_bytes(b"leaf")
    (root / "top.txt").write_bytes(b"top")
    (root / "sub1" / "skip_dir").symlink_to(root / "sub1" / "sub2")
    (root / "sub1" / "skip_link").symlink_to(root / "top.txt")

    items = PackTaskManager._build_archive_items([root], None, ["dirhash"])
    arcnames = [item.arcname for item in items]
    assert arcnames == [
        "sub1/", "top.txt", "sub1/sub2/", "sub1/sub2/leaf.txt",
    ]
    assert all(not item.is_dir or item.size == 0 for item in items)

    # 单一目录源不会带前缀；多源目录会展开根名并去重
    multi = PackTaskManager._build_archive_items(
        [root, root], ["tree", "tree"], ["h1", "h2"]
    )
    roots = {item.arcname.split("/")[0] for item in multi}
    assert roots == {"tree", "tree_1"}
    prefixed = PackTaskManager._build_archive_items(
        [root, root / "top.txt"], ["tree", "top.txt"], ["h1", "h2"]
    )
    assert prefixed[0].arcname == "tree/"


def test_build_archive_items_cancel(tmp_path: Path) -> None:
    root = tmp_path / "d"
    root.mkdir()
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        PackTaskManager._build_archive_items([root], None, None, cancel)


def test_build_archive_items_wraps_canonical_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.storage as storage
    from app.services.storage_index import scan_storage_path

    root = tmp_path / "wrap"
    inner = root / "inner"
    inner.mkdir(parents=True)
    (inner / "f.txt").write_bytes(b"x")

    dir_hash = scan_storage_path(inner).content_hash
    monkeypatch.setattr(storage, "is_canonical_store_path", lambda _p, _h: True)
    items = PackTaskManager._build_archive_items([root], ["dir"], [dir_hash])
    assert items[0].arcname == "f.txt"

    # 子目录数量不为 1 → 原样返回
    (root / "extra").mkdir()
    items2 = PackTaskManager._build_archive_items([root], ["dir"], [dir_hash])
    assert items2[0].arcname == "extra/"

    # 非法 v2 hash → ValueError 由上层转为任务错误
    with pytest.raises(ValueError):
        PackTaskManager._unwrap_stored_directory(root, "v2:file")
    items3 = PackTaskManager._build_archive_items([root], ["dir"], ["zzz"])
    assert items3[0].arcname == "extra/"


# ---------------------------------------------------------------- archive writers


@pytest.mark.parametrize("pack_format", ["zip", "tar.zst"])
def test_archive_writers_handle_files_and_dirs(
    tmp_path: Path, pack_format: str
) -> None:
    from app.modules.pack import _ArchiveItem

    src_dir = tmp_path / "d"
    src_dir.mkdir()
    (src_dir / "inner.txt").write_bytes(b"inner-data")
    src_file = tmp_path / "f.txt"
    src_file.write_bytes(b"file-data")

    items = [
        _ArchiveItem(path=src_dir, arcname="d/", is_dir=True, size=0),
        _ArchiveItem(path=src_file, arcname="f.txt", is_dir=False, size=9),
    ]
    output = tmp_path / f"out.{pack_format}"
    tracker = _ProgressTracker(100)
    if pack_format == "zip":
        PackTaskManager._write_zip_sync(output, 5, items, tracker, _event(), 10_000, 0)
        with zipfile.ZipFile(output) as zf:
            assert sorted(zf.namelist()) == ["d/", "f.txt"]
            assert zf.read("f.txt") == b"file-data"
    else:
        PackTaskManager._write_tar_zst_sync(
            output, 5, items, tracker, _event(), 10_000, 0
        )
        import tarfile
        import zstandard as zstd

        with zstd.open(output) as raw, tarfile.open(fileobj=raw, mode="r|*") as tf:
            assert sorted(tf.getnames()) == ["d", "f.txt"]
    assert tracker.snapshot()[0] == 9

    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        if pack_format == "zip":
            PackTaskManager._write_zip_sync(
                output, 5, items, tracker, cancel, 10_000, 0
            )
        else:
            PackTaskManager._write_tar_zst_sync(
                output, 5, items, tracker, cancel, 10_000, 0
            )


def test_zip_writer_cancel_mid_file(tmp_path: Path) -> None:
    from app.modules.pack import _ArchiveItem

    src = tmp_path / "big.txt"
    src.write_bytes(b"z" * 4096)
    item = _ArchiveItem(path=src, arcname="big.txt", is_dir=False, size=4096)
    output = tmp_path / "out.zip"

    class CancelAfterOpen:
        def __init__(self) -> None:
            self.calls = 0

        def check(self) -> bool:
            self.calls += 1
            return self.calls > 2

    cancel = _event()
    original_write = zipfile.ZipFile.open

    def open_and_arm(self, name, mode="r", *args, **kwargs):  # noqa: ANN001
        handle = original_write(self, name, mode, *args, **kwargs)
        if mode == "w":
            cancel.set()
        return handle

    with pytest.raises(InterruptedError):
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(zipfile.ZipFile, "open", open_and_arm)
            PackTaskManager._write_zip_sync(
                output, 1, [item], _ProgressTracker(4096), cancel, 10_000, 0
            )


def test_write_archive_sync_dispatches_zip(tmp_path: Path) -> None:
    from app.modules.pack import _ArchiveItem

    src = tmp_path / "dispatch.txt"
    src.write_bytes(b"dispatched")
    output = tmp_path / "out.zip"
    items = [_ArchiveItem(path=src, arcname="dispatch.txt", is_dir=False, size=10)]
    PackTaskManager._write_archive_sync(
        output, "zip", 3, items, _ProgressTracker(10), _event(), 10_000, 0
    )
    with zipfile.ZipFile(output) as zf:
        assert zf.read("dispatch.txt") == b"dispatched"


# ---------------------------------------------------------------- repository layer


async def test_admission_rejects_invalid_source_payloads(temp_db: str) -> None:
    user = await create_user_v0(username="pack_admission")
    kwargs = dict(
        user_id=user["id"],
        source_user_file_ids_json="[1]",
        source_size_bytes=10,
        reserved_bytes=10,
        output_name=None,
        delete_source=False,
        disk_available_bytes=10**9,
    )
    with pytest.raises(PackAdmissionError) as ei:
        await create_pending_pack_with_reservation(
            **{**kwargs, "source_size_bytes": 0}
        )
    assert ei.value.reason == "source"

    for payload in ("not-json", "null", "[1, 1]", '["x"]', "[0]", "[true]"):
        with pytest.raises(PackAdmissionError) as ei:
            await create_pending_pack_with_reservation(
                **{**kwargs, "source_user_file_ids_json": payload}
            )
        assert ei.value.reason == "source"


async def test_admission_rejects_completed_duplicate_and_missing_user(
    temp_db: str,
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_dup")
    async with transaction() as conn:
        await conn.execute(
            insert(pack_tasks).values(
                user_id=user["id"],
                source_user_file_ids_json=json.dumps([user_file["id"]], separators=(",", ":")),
                source_size_bytes=5,
                reserved_bytes=0,
                status="completed",
                output_stored_file_id=1,
                created_at_ms=now_ms(),
                updated_at_ms=now_ms(),
            )
        )
    with pytest.raises(PackAdmissionError) as ei:
        await create_pending_pack_with_reservation(
            user_id=user["id"],
            source_user_file_ids_json=json.dumps([user_file["id"]], separators=(",", ":")),
            source_size_bytes=5,
            reserved_bytes=10,
            output_name=None,
            delete_source=False,
            disk_available_bytes=10**9,
        )
    assert ei.value.reason == "completed"

    with pytest.raises(PackAdmissionError) as ei:
        await create_pending_pack_with_reservation(
            user_id=999_999,
            source_user_file_ids_json="[1]",
            source_size_bytes=5,
            reserved_bytes=10,
            output_name=None,
            delete_source=False,
            disk_available_bytes=10**9,
        )
    assert ei.value.reason == "user_missing"

    # 源文件行缺失
    with pytest.raises(PackAdmissionError) as ei:
        await create_pending_pack_with_reservation(
            user_id=user["id"],
            source_user_file_ids_json="[424242]",
            source_size_bytes=5,
            reserved_bytes=10,
            output_name=None,
            delete_source=False,
            disk_available_bytes=10**9,
        )
    assert ei.value.reason == "source"


async def test_release_reservation_conflict(temp_db: str) -> None:
    user = await create_user_v0(username="pack_drift")
    async with transaction() as conn:
        with pytest.raises(RepositoryConflictError):
            await _release_reservation_locked(
                conn, {"user_id": user["id"], "reserved_bytes": 50}, timestamp=now_ms()
            )


async def test_reserve_install_bytes_edges(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_reserve")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )
    assert await reserve_pack_install_bytes(task["id"], 0, 100) is True
    assert await reserve_pack_install_bytes(task["id"], 10, 100) is False

    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                status="packing",
                prepared_content_hash="v2:file:" + "a" * 64,
                prepared_size_bytes=10,
                prepared_filename="out.zip",
            )
        )
    assert await reserve_pack_install_bytes(task["id"], 10**15, 100) is False
    assert await reserve_pack_install_bytes(task["id"], 10, 100) is True
    # 已有预留 → 不再预留
    assert await reserve_pack_install_bytes(task["id"], 10, 100) is False


async def test_schedule_retry_updates_task(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_retry")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10,
    )
    assert await schedule_pack_retry(task["id"], retry_count=2, next_retry_at_ms=5) is True
    assert await schedule_pack_retry(999_999, retry_count=1, next_retry_at_ms=5) is False


async def test_fail_active_pack_task_returns_none_for_terminal(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_fail_none")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="failed",
    )
    assert await fail_active_pack_task(task["id"], "err") is None


async def test_finalize_prepared_rejects_pending_delete_and_size_mismatch(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="pack_finalize_conflict")
    content_hash = "v2:file:" + "b" * 64
    timestamp = now_ms()
    async with transaction() as conn:
        stored = (
            (
                await conn.execute(
                    insert(stored_files).values(
                        content_hash=content_hash,
                        content_hash_version="v2",
                        content_object_kind="file",
                        content_digest="b" * 64,
                        real_path="/tmp/whatever",
                        size_bytes=10,
                        original_name="o.bin",
                        pending_delete=1,
                        created_at_ms=timestamp,
                    ).returning(stored_files)
                )
            ).mappings().one()
        )
        await conn.execute(
            update(user_storage_usage).where(
                user_storage_usage.c.user_id == user["id"]
            ).values(reserved_bytes=100, updated_at_ms=timestamp)
        )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                prepared_content_hash=content_hash,
                prepared_size_bytes=10,
                prepared_filename="out.zip",
            )
        )
    with pytest.raises(RepositoryConflictError):
        await finalize_prepared_pack_task(
            task["id"], content_hash=content_hash, size_bytes=10,
            filename="out.zip", real_path="/tmp/whatever",
        )
    # 尺寸不一致
    async with transaction() as conn:
        await conn.execute(
            update(stored_files).where(stored_files.c.id == stored["id"]).values(
                pending_delete=0, size_bytes=999
            )
        )
    with pytest.raises(RepositoryConflictError):
        await finalize_prepared_pack_task(
            task["id"], content_hash=content_hash, size_bytes=10,
            filename="out.zip", real_path="/tmp/whatever",
        )


async def test_finalize_prepared_rejects_oversized_output(temp_db: str) -> None:
    user = await create_user_v0(username="pack_finalize_oversize")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=5, status="packing",
    )
    content_hash = "v2:file:" + "c" * 64
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                prepared_content_hash=content_hash,
                prepared_size_bytes=50,
                prepared_filename="out.zip",
            )
        )
    with pytest.raises(RepositoryConflictError):
        await finalize_prepared_pack_task(
            task["id"], content_hash=content_hash, size_bytes=50,
            filename="out.zip", real_path="/tmp/new-path",
        )


async def test_finalize_prepared_mismatched_task_returns_none(temp_db: str) -> None:
    user = await create_user_v0(username="pack_finalize_none")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    assert await finalize_prepared_pack_task(
        task["id"], content_hash="v2:file:" + "d" * 64, size_bytes=5,
        filename="out.zip", real_path="/tmp/x",
    ) is None


async def test_finalize_prepared_drift(temp_db: str) -> None:
    user = await create_user_v0(username="pack_finalize_drift")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    content_hash = "v2:file:" + "e" * 64
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                prepared_content_hash=content_hash,
                prepared_size_bytes=5,
                prepared_filename="out.zip",
            )
        )
    with pytest.raises(RepositoryConflictError):
        await finalize_prepared_pack_task(
            task["id"], content_hash=content_hash, size_bytes=5,
            filename="out.zip", real_path="/tmp/y",
        )


async def test_cleanup_marker_helpers(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_markers")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
        delete_source=True,
    )
    # 存在 pending 源 → 不能标记完成
    assert await mark_source_cleanup_complete(task["id"]) is False
    assert await settle_user_pack_markers(task["id"], 999) is False
    rows = await list_user_pack_cleanup_rows(user["id"])
    assert [row["id"] for row in rows] == [task["id"]]
    # 置为 retained 后可清理
    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(cleanup_state="retained")
        )
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=0
            )
        )
    assert await settle_user_pack_markers(task["id"], user["id"]) is True
    assert await clear_terminal_pack_tasks(user["id"]) == 1
    assert await list_user_pack_cleanup_rows(user["id"]) == []
    # 删除路径
    task2 = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="cancelled",
    )
    assert await delete_user_pack_task(user["id"], task2["id"]) is True
    assert await delete_user_pack_task(user["id"], task2["id"]) is False


# ---------------------------------------------------------------- manager: submit / dispatcher


async def test_manager_misc_queries(temp_db: str) -> None:
    assert PackTaskManager.is_any_task_running() is False
    assert await PackTaskManager.cancel_pack(999_999) is False
    assert await PackTaskManager._is_task_status(999_999, "pending") is False


async def test_submit_rejects_duplicate_and_capacity(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_submit")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="failed",
    )
    fake_job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    PackTaskManager._running_tasks[task["id"]] = fake_job
    try:
        assert await PackTaskManager.submit(task["id"]) is False
        PackTaskManager._running_tasks.clear()
        PackTaskManager._running_tasks[999_998] = fake_job
        assert await PackTaskManager.submit(task["id"]) is False
    finally:
        PackTaskManager._running_tasks.clear()


async def test_run_thread_cancellation_waits_for_thread(temp_db: str) -> None:
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    release = _event()

    def blocking() -> str:
        release.wait(5)
        return "done"

    async def runner() -> None:
        await PackTaskManager._run_thread(job, blocking)

    outer = asyncio.create_task(runner())
    await asyncio.sleep(0.05)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    release.set()
    await PackTaskManager._wait_thread_tasks(job)
    assert job.cancel_event.is_set()


async def test_dispatcher_exception_isolated(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_loop() -> None:
        raise RuntimeError("dispatcher boom")

    monkeypatch.setattr(PackTaskManager, "_dispatcher_loop", failing_loop)
    await PackTaskManager.start_dispatcher()
    await asyncio.sleep(0.1)
    await PackTaskManager.shutdown()
    assert PackTaskManager._dispatcher_task is None

    # 幂等启动
    calls = 0

    async def sleeping_loop() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)

    monkeypatch.setattr(PackTaskManager, "_dispatcher_loop", sleeping_loop)
    await PackTaskManager.start_dispatcher()
    await PackTaskManager.start_dispatcher()
    await asyncio.sleep(0.1)
    assert calls == 1
    await PackTaskManager.shutdown()


async def test_dispatcher_loop_swallows_scan_errors(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def flaky(*_a: Any, **_k: Any) -> list[int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("scan failed")
        raise asyncio.CancelledError()

    monkeypatch.setattr(pack_service, "list_pack_dispatch_task_ids", flaky)
    with pytest.raises(asyncio.CancelledError):
        await PackTaskManager._dispatcher_loop()
    assert calls == 2


async def test_prepare_user_deletion_replays_and_reports_unsettled(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_userdel")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=1
            )
        )
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(cleanup_real_path="/tmp/tomb")
        )
    replayed = []
    monkeypatch.setattr(
        PackTaskManager,
        "_replay_source_cleanup",
        AsyncMock(side_effect=lambda *_a, **_k: replayed.append(1) or True),
    )
    # settle 失败（cleanup_real_path 仍在）→ 返回 False
    assert await PackTaskManager.prepare_user_deletion(user["id"]) is False
    assert replayed == [1]

    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(cleanup_real_path=None, cleanup_state="retained")
        )
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=0
            )
        )
    assert await PackTaskManager.prepare_user_deletion(user["id"]) is True


async def test_prepare_user_deletion_cancellation(temp_db: str) -> None:
    user = await create_user_v0(username="pack_userdel_cancel")
    started = _event()

    async def blocking_second(user_id: int) -> list[dict[str, Any]]:
        if started.is_set():
            await asyncio.sleep(5)
        started.set()
        return []

    original = pack_service.list_user_pack_cleanup_rows
    patch = pytest.MonkeyPatch()
    patch.setattr(pack_service, "list_user_pack_cleanup_rows", blocking_second)
    try:
        task = asyncio.create_task(PackTaskManager.prepare_user_deletion(user["id"]))
        for _ in range(60):
            if started.is_set():
                break
            await asyncio.sleep(0.05)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        patch.undo()
        assert await original(user["id"]) == []


async def test_recover_startup_isolates_task_failure(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_recover_iso")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )

    async def broken(_row: dict[str, Any], _job: Any) -> None:
        raise ValueError("recover boom")

    monkeypatch.setattr(PackTaskManager, "_recover_one_startup", broken)
    await PackTaskManager.recover_startup()
    assert await pack_repo.get_pack_task_row(task["id"]) is not None


async def test_recover_one_startup_packing_without_prepared(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_recover_requeue")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    pack_dir = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "stale.partial").write_bytes(b"x")

    monkey_job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    await PackTaskManager._recover_one_startup(
        await pack_repo.get_pack_task_row(task["id"]), monkey_job
    )
    row = await pack_repo.get_pack_task_row(task["id"])
    assert row["status"] == "pending"
    assert row["materialized_bytes"] == 0
    assert not pack_dir.exists()


# ---------------------------------------------------------------- schedule retry / dispatch


async def test_schedule_retry_skips_terminal_task(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_retry_terminal")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="failed",
    )
    await PackTaskManager._schedule_retry(task["id"])
    row = await pack_repo.get_pack_task_row(task["id"])
    assert row["retry_count"] == 0


async def test_schedule_retry_requeues_packing_without_prepared(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_retry_requeue")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    await PackTaskManager._schedule_retry(task["id"])
    row = await pack_repo.get_pack_task_row(task["id"])
    assert row["status"] == "pending"
    assert row["retry_count"] == 1
    assert row["next_retry_at_ms"] is not None


async def test_run_persistent_pack_early_returns(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_early")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="failed",
    )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    await PackTaskManager._run_persistent_pack(task["id"], job, None)


async def test_dispatch_interrupted_error_cleans_failed_dir(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_dispatch_intr")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="failed",
    )
    pack_dir = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())

    async def interrupted(_tid: int, _job: Any, _cb: Any) -> None:
        raise InterruptedError("pack cancelled")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(PackTaskManager, "_run_persistent_pack", interrupted)
    try:
        await PackTaskManager._dispatch_persistent_pack(task["id"], job, None)
    finally:
        monkey.undo()
    assert not pack_dir.exists()


async def test_dispatch_persistence_failure_schedules_retry(
    temp_db: str,
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_dispatch_persist")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())

    async def boundary(_tid: int, _job: Any, _cb: Any) -> None:
        raise PackBoundaryError("too big")

    async def persist_fails(_tid: int, _msg: str) -> None:
        raise RuntimeError("db down")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(PackTaskManager, "_run_persistent_pack", boundary)
    monkey.setattr(PackTaskManager, "_update_task_error", persist_fails)
    try:
        await PackTaskManager._dispatch_persistent_pack(task["id"], job, None)
    finally:
        monkey.undo()
    row = await pack_repo.get_pack_task_row(task["id"])
    assert row["next_retry_at_ms"] is not None


async def test_run_persistent_pack_completed_cleanup_pending(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_completed")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=1
            )
        )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())

    async def replay_fail(*_a: Any, **_k: Any) -> bool:
        return False

    monkey = pytest.MonkeyPatch()
    monkey.setattr(PackTaskManager, "_replay_source_cleanup", replay_fail)
    try:
        with pytest.raises(OSError):
            await PackTaskManager._run_persistent_pack(task["id"], job, None)
    finally:
        monkey.undo()


async def test_run_persistent_pack_finalizes_prepared(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_prepared")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                prepared_content_hash="v2:file:" + "f" * 64,
                prepared_size_bytes=5,
                prepared_filename="out.zip",
            )
        )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    calls = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        PackTaskManager,
        "_finalize_prepared",
        AsyncMock(side_effect=lambda *a, **k: calls.append(a) or {}),
    )
    try:
        await PackTaskManager._run_persistent_pack(task["id"], job, None)
    finally:
        monkey.undo()
    assert calls


# ---------------------------------------------------------------- source resolution / hashing


async def test_resolve_task_sources_errors(temp_db: str) -> None:
    user, user_file, source = await _make_file_user(username="pack_resolve")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    task_row = await pack_repo.get_pack_task_row(task["id"])

    monkey = pytest.MonkeyPatch()

    async def cancelled(*_a: Any) -> list[dict[str, Any]]:
        raise asyncio.CancelledError()

    monkey.setattr(pack_service, "list_pack_task_source_rows", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await PackTaskManager._resolve_task_sources(task_row)
    monkey.undo()

    async def empty_resolved(_tid: int) -> list[dict[str, Any]]:
        return []

    monkey.setattr(pack_service, "resolve_pack_task_source_rows", empty_resolved)
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._resolve_task_sources(task_row)
    monkey.undo()

    # 顺序被破坏
    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(ordinal=5)
        )
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._resolve_task_sources(task_row)
    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(ordinal=0)
        )

    # 源文件丢失
    source.unlink()
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._resolve_task_sources(task_row)
    source.write_bytes(b"hello")


async def test_validate_source_hashes_v2_and_failures(tmp_path: Path) -> None:
    from app.services.storage_index import scan_storage_path

    src = tmp_path / "f"
    src.write_bytes(b"payload")
    good = scan_storage_path(src).content_hash

    tracker = _ProgressTracker(len(b"payload"))
    PackTaskManager._validate_source_hashes([src], [good], _event(), tracker)
    assert tracker.snapshot() == (len(b"payload"), len(b"payload"), 100)
    with pytest.raises(PackBoundaryError):
        PackTaskManager._validate_source_hashes(
            [src], ["v2:file:" + "0" * 64], _event()
        )
    with pytest.raises(PackBoundaryError):
        PackTaskManager._validate_source_hashes([src], ["v2:file"], _event())
    # 非规范 legacy hash 直接跳过校验
    PackTaskManager._validate_source_hashes([src], ["zzz"], _event())


async def test_wait_progress_thread_maps_running_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[int] = []
    tracker = _ProgressTracker(10)
    started = _event()
    release = _event()
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())

    async def capture_progress(_task_id: int, progress: int) -> None:
        updates.append(progress)

    def work() -> str:
        tracker.add(5)
        started.set()
        release.wait(2)
        tracker.add(5)
        return "done"

    monkeypatch.setattr(PackTaskManager, "_update_task_progress", capture_progress)
    worker = PackTaskManager._start_thread(job, work)
    waiting = asyncio.create_task(
        PackTaskManager._wait_progress_thread(
            job, worker, tracker, 1, 0, 40, None
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    await asyncio.sleep(0.25)
    release.set()

    assert await waiting == "done"
    assert 20 in updates
    assert updates[-1] == 40
    assert updates == sorted(updates)


# ---------------------------------------------------------------- write & prepare internals


async def test_write_and_prepare_reports_progress_and_missing_partial(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, source = await _make_file_user(
        username="pack_progress", content=b"progress-data"
    )
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=len(b"progress-data"), reserved_bytes=10_000,
        status="packing", output_name="progress",
    )
    await _set_reserved(user["id"], 10_000)
    from app.modules.pack import _ArchiveItem

    items = [
        _ArchiveItem(
            path=source, arcname="src.txt", is_dir=False,
            size=len(b"progress-data"),
        )
    ]
    pack_dir = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    partial = pack_dir / "progress.tar.zst.partial"
    prepared = pack_dir / "progress.tar.zst"

    progress_calls: list[tuple[int, int]] = []
    writer_started = _event()
    file_ready = _event()
    release = _event()

    original_write = PackTaskManager._write_archive_sync

    def slow_writer(output_path: Path, *args: Any) -> None:
        writer_started.set()
        file_ready.wait(5)
        original_write(output_path, *args)
        release.wait(5)

    monkeypatch.setattr(PackTaskManager, "_write_archive_sync", slow_writer)
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())

    async def run() -> None:
        await PackTaskManager._write_and_prepare(
            await pack_repo.get_pack_task_row(task["id"]),
            job, items, partial, prepared, "progress.tar.zst",
            lambda tid, pct: progress_calls.append((tid, pct)),
        )

    outer = asyncio.create_task(run())
    for _ in range(60):
        if writer_started.is_set():
            break
        await asyncio.sleep(0.05)
    assert writer_started.is_set()
    await asyncio.sleep(0.3)  # partial 尚未创建：stat 抛 FileNotFoundError
    file_ready.set()
    await asyncio.sleep(2.3)  # 触发物化字节的时间窗口更新
    release.set()
    await outer

    assert progress_calls
    progress_values = [progress for _task_id, progress in progress_calls]
    assert progress_values == sorted(progress_values)
    assert 40 in progress_values
    assert 90 in progress_values
    assert progress_values[-1] == 99
    assert await pack_repo.get_pack_task_row(task["id"])
    assert prepared.exists()


async def test_write_and_prepare_cancelled_after_writer(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, source = await _make_file_user(username="pack_cancel_after")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10_000, status="packing",
    )
    await _set_reserved(user["id"], 10_000)
    from app.modules.pack import _ArchiveItem

    items = [_ArchiveItem(path=source, arcname="s.txt", is_dir=False, size=5)]
    pack_dir = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    partial = pack_dir / "o.tar.zst.partial"

    def writer_then_cancel(output_path: Path, *_args: Any) -> None:
        output_path.write_bytes(b"data!")

    monkeypatch.setattr(PackTaskManager, "_write_archive_sync", writer_then_cancel)
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())

    async def run() -> None:
        await PackTaskManager._write_and_prepare(
            await pack_repo.get_pack_task_row(task["id"]),
            job, items, partial, pack_dir / "o.tar.zst", "o.tar.zst", None,
        )

    run_task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    job.cancel_event.set()
    with pytest.raises(InterruptedError):
        await run_task


async def test_write_and_prepare_persist_failure(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, source = await _make_file_user(username="pack_persist_fail")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10_000, status="packing",
    )
    await _set_reserved(user["id"], 10_000)
    from app.modules.pack import _ArchiveItem

    items = [_ArchiveItem(path=source, arcname="s.txt", is_dir=False, size=5)]
    pack_dir = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        PackTaskManager, "_write_archive_sync",
        lambda output_path, *_a: output_path.write_bytes(b"tiny"),
    )
    monkeypatch.setattr(pack_service, "persist_pack_prepared", AsyncMock(return_value=False))
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    with pytest.raises(InterruptedError):
        await PackTaskManager._write_and_prepare(
            await pack_repo.get_pack_task_row(task["id"]),
            job, items, pack_dir / "o.tar.zst.partial", pack_dir / "o.tar.zst",
            "o.tar.zst", None,
        )
    assert not (pack_dir / "o.tar.zst").exists()


# ---------------------------------------------------------------- finalize internals


async def test_finalize_prepared_invalid_records(temp_db: str) -> None:
    user = await create_user_v0(username="pack_finalize_invalid")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    base = dict(await pack_repo.get_pack_task_row(task["id"]))
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        await PackTaskManager._finalize_prepared(base, cancel)
    cancel.clear()

    with pytest.raises(PackBoundaryError):
        await PackTaskManager._finalize_prepared({**base, "prepared_content_hash": "zzz"}, cancel)
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._finalize_prepared(
            {**base, "prepared_content_hash": "v2:file:" + "g" * 64,
             "prepared_size_bytes": -1, "prepared_filename": "f"}, cancel)
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._finalize_prepared(
            {**base, "prepared_content_hash": "v2:file:" + "g" * 64,
             "prepared_size_bytes": 1, "prepared_filename": "a/b"}, cancel)
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._finalize_prepared(
            {**base, "prepared_content_hash": "legacy!", "prepared_size_bytes": 1,
             "prepared_filename": "f"}, cancel)


async def test_finalize_prepared_conflict_terminalizes(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="pack_finalize_conflict_term")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    await _set_reserved(user["id"], 100)
    content_hash = "v2:file:" + "1" * 64
    orphan = Path(settings.download_dir) / "store" / "orphan_term.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    from app.modules.pack import _InstalledPrepared

    async def fake_install(*_a: Any, **_k: Any) -> _InstalledPrepared:
        return _InstalledPrepared(orphan, True)

    async def raise_conflict(*_a: Any, **_k: Any) -> None:
        raise RepositoryConflictError("boom")

    monkeypatch.setattr(PackTaskManager, "_install_prepared_file", fake_install)
    monkeypatch.setattr(
        pack_service, "finalize_prepared_pack_task", AsyncMock(side_effect=raise_conflict)
    )
    result = await PackTaskManager._finalize_prepared(
        {**await _task_row(task["id"]), "prepared_content_hash": content_hash,
         "prepared_size_bytes": 6, "prepared_filename": "o.bin"},
        _event(),
    )
    assert result is None
    assert await pack_repo.get_pack_task_status(task["id"]) == "failed"
    assert not orphan.exists()


async def test_finalize_prepared_generic_exception_cleans_and_raises(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="pack_finalize_generic")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    await _set_reserved(user["id"], 100)
    content_hash = "v2:file:" + "2" * 64
    orphan = Path(settings.download_dir) / "store" / "orphan_gen.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    from app.modules.pack import _InstalledPrepared

    monkeypatch.setattr(
        PackTaskManager,
        "_install_prepared_file",
        AsyncMock(return_value=_InstalledPrepared(orphan, True)),
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                status="failed", updated_at_ms=now_ms()
            )
        )
    monkeypatch.setattr(
        pack_service,
        "finalize_prepared_pack_task",
        AsyncMock(side_effect=RuntimeError("db boom")),
    )
    with pytest.raises(RuntimeError):
        await PackTaskManager._finalize_prepared(
            {**await _task_row(task["id"]), "prepared_content_hash": content_hash,
             "prepared_size_bytes": 6, "prepared_filename": "o.bin"},
            _event(),
        )
    assert not orphan.exists()


async def test_finalize_prepared_transition_failure_raises_oserror(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="pack_finalize_transition")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    await _set_reserved(user["id"], 100)
    content_hash = "v2:file:" + "3" * 64
    from app.modules.pack import _InstalledPrepared

    monkeypatch.setattr(
        PackTaskManager,
        "_install_prepared_file",
        AsyncMock(return_value=_InstalledPrepared(Path("/nonexistent"), False)),
    )
    monkeypatch.setattr(
        pack_service,
        "finalize_prepared_pack_task",
        AsyncMock(side_effect=RepositoryConflictError("boom")),
    )

    async def broken_cas(_tid: int, _msg: str) -> str | None:
        raise RuntimeError("cas boom")

    monkeypatch.setattr(PackTaskManager, "_terminalize_finalize_cas", broken_cas)
    with pytest.raises(OSError):
        await PackTaskManager._finalize_prepared(
            {**await _task_row(task["id"]), "prepared_content_hash": content_hash,
             "prepared_size_bytes": 6, "prepared_filename": "o.bin"},
            _event(),
        )


async def test_finalize_prepared_completed_cleanup_pending_fails(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="pack_finalize_cleanup")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    content_hash = "v2:file:" + "4" * 64
    from app.modules.pack import _InstalledPrepared

    monkeypatch.setattr(
        PackTaskManager,
        "_install_prepared_file",
        AsyncMock(return_value=_InstalledPrepared(Path("/nonexistent"), False)),
    )

    async def fake_finalize(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"source_cleanup_pending": 1}

    monkeypatch.setattr(
        pack_service, "finalize_prepared_pack_task", AsyncMock(side_effect=fake_finalize)
    )
    monkeypatch.setattr(
        PackTaskManager, "_replay_source_cleanup", AsyncMock(return_value=False)
    )
    with pytest.raises(OSError):
        await PackTaskManager._finalize_prepared(
            {**await _task_row(task["id"]), "prepared_content_hash": content_hash,
             "prepared_size_bytes": 6, "prepared_filename": "o.bin"},
            _event(),
        )



async def _set_reserved(user_id: int, amount: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=amount, updated_at_ms=now_ms())
        )

async def _task_row(task_id: int) -> dict[str, Any]:
    row = await pack_repo.get_pack_task_row(task_id)
    return dict(row or {})


async def test_terminalize_cas_marks_failed(temp_db: str) -> None:
    user = await create_user_v0(username="pack_cas")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=10, status="packing",
    )
    await _set_reserved(user["id"], 10)
    status = await PackTaskManager._terminalize_finalize_cas(task["id"], "msg")
    assert status == "failed"


async def test_cleanup_unowned_install_skips(temp_db: str) -> None:
    from app.modules.pack import _InstalledPrepared

    installed = _InstalledPrepared(Path("/nonexistent"), False)
    await PackTaskManager._cleanup_unowned_install(installed, "h", _event(), None)

    owned = _InstalledPrepared(Path("/also/missing"), True)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        pack_service, "get_stored_file_by_real_path", AsyncMock(return_value={"id": 1})
    )
    try:
        await PackTaskManager._cleanup_unowned_install(owned, "h", _event(), None)
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------- install prepared


async def test_install_prepared_reuses_stored_existing(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.storage_index import scan_storage_path

    user = await create_user_v0(username="pack_install_stored")
    content = b"install-me"
    await _set_reserved(user["id"], 10)
    scratch = _write_store_file("install.bin", content)
    content_hash = scan_storage_path(scratch).content_hash
    from app.domain.content_identity import content_identity_from_content_hash

    identity = content_identity_from_content_hash(content_hash)
    existing = _store_path_for(content_hash)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(content)
    timestamp = now_ms()
    async with transaction() as conn:
        await conn.execute(
            insert(stored_files).values(
                content_hash=content_hash,
                content_hash_version="v2",
                content_object_kind=identity.object_kind,
                content_digest=identity.digest,
                real_path=str(existing),
                size_bytes=len(content),
                original_name="install.bin",
                created_at_ms=timestamp,
            )
        )
    monkeypatch.setattr(
        pack_service, "get_stored_file_by_identity",
        AsyncMock(return_value={"real_path": str(existing)}),
    )
    result = await PackTaskManager._install_prepared_file(
        1, content_hash=content_hash, size_bytes=len(content),
        filename="install.bin", cancel_event=_event(), job=None,
    )
    assert result.path == existing
    assert result.created_by_this_attempt is False


def _write_store_file(name: str, content: bytes) -> Path:
    path = Path(settings.download_dir) / "store" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _store_path_for(content_hash: str) -> Path:
    from app.services.storage import get_store_path_for_hash

    return get_store_path_for_hash(content_hash)


async def test_install_prepared_from_scratch_links_prepared(
    temp_db: str,
) -> None:
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_link")
    content = b"link-me"
    src = _write_store_file("link.bin", content)
    content_hash = scan_storage_path(src).content_hash
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_1"
    pack_dir.mkdir(parents=True, exist_ok=True)
    prepared = pack_dir / "link.bin"
    prepared.write_bytes(content)

    result = await PackTaskManager._install_prepared_file(
        1, content_hash=content_hash, size_bytes=len(content),
        filename=prepared.name, cancel_event=_event(), job=None,
    )
    assert result.created_by_this_attempt is True
    assert result.path.read_bytes() == content
    result.path.unlink()

    # prepared 缺失 → PackBoundaryError
    prepared.unlink()
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._install_prepared_file(
            1, content_hash=content_hash, size_bytes=len(content),
            filename=prepared.name, cancel_event=_event(), job=None,
        )


async def test_install_prepared_target_owner_conflict(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_owner")
    content = b"owner"
    src = _write_store_file("owner.bin", content)
    content_hash = scan_storage_path(src).content_hash
    monkeypatch.setattr(
        pack_service, "get_stored_file_by_real_path",
        AsyncMock(return_value={"content_hash": "other"}),
    )
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._install_prepared_file(
            1, content_hash=content_hash, size_bytes=len(content),
            filename="owner.bin", cancel_event=_event(), job=None,
        )


async def test_install_prepared_copy_reservation_failure_cleans(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pack_service, "_durable_link_file", lambda *_a: False
    )
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_copy")
    content = b"copy-me"
    src = _write_store_file("copy.bin", content)
    content_hash = scan_storage_path(src).content_hash
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_2"
    pack_dir.mkdir(parents=True, exist_ok=True)
    prepared = pack_dir / "copy.bin"
    prepared.write_bytes(content)

    monkeypatch.setattr(
        pack_service, "reserve_pack_install_bytes", AsyncMock(return_value=False)
    )
    with pytest.raises(PackBoundaryError):
        await PackTaskManager._install_prepared_file(
            2, content_hash=content_hash, size_bytes=len(content),
            filename=prepared.name, cancel_event=_event(), job=None,
        )


async def test_install_prepared_release_exception_logged(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pack_service, "_durable_link_file", lambda *_a: False
    )
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_release")
    content = b"release-me"
    src = _write_store_file("release.bin", content)
    content_hash = scan_storage_path(src).content_hash
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_3"
    pack_dir.mkdir(parents=True, exist_ok=True)
    prepared = pack_dir / "release.bin"
    prepared.write_bytes(content)

    async def raise_exc(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("release boom")

    monkeypatch.setattr(
        pack_service, "reserve_pack_install_bytes", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        pack_service, "clear_pack_install_reservation", AsyncMock(side_effect=raise_exc)
    )
    result = await PackTaskManager._install_prepared_file(
        3, content_hash=content_hash, size_bytes=len(content),
        filename=prepared.name, cancel_event=_event(), job=None,
    )
    assert result.created_by_this_attempt is True
    result.path.unlink()


# ---------------------------------------------------------------- source cleanup replay


async def test_durable_delete_source_path_guards_and_tombstone(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_user_v0(username="pack_delete_source")
    # 路径超出存储目录
    with pytest.raises(ValueError):
        await PackTaskManager._durable_delete_source_path(
            1, 0, "/definitely/outside/store", _event(), None
        )

    store_dir = Path(settings.download_dir) / "store"
    plain = store_dir / "plain_del.bin"
    plain.write_bytes(b"x")
    # 普通文件：直接删除，durable_path 为 None
    await PackTaskManager._durable_delete_source_path(
        1, 0, str(plain), _event(), None
    )
    assert not plain.exists()

    tomb_dir = store_dir / "tomb_dir"
    tomb_dir.mkdir()
    (tomb_dir / "inner").write_bytes(b"y")
    persisted = []
    monkeypatch.setattr(
        pack_service, "set_pack_source_cleanup_real_path",
        AsyncMock(side_effect=lambda tid, o, p: persisted.append((tid, o, p)) or True),
    )
    await PackTaskManager._durable_delete_source_path(
        1, 1, str(tomb_dir), _event(), None
    )
    assert persisted
    assert not tomb_dir.exists()

    # persisted False → RepositoryConflictError
    tomb_dir2 = store_dir / "tomb_dir2"
    tomb_dir2.mkdir()
    monkeypatch.setattr(
        pack_service, "set_pack_source_cleanup_real_path", AsyncMock(return_value=False)
    )
    with pytest.raises(RepositoryConflictError):
        await PackTaskManager._durable_delete_source_path(
            1, 2, str(tomb_dir2), _event(), None
        )


async def test_replay_source_cleanup_noop_and_missing_hash(
    temp_db: str,
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_replay")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
    )
    row = await _task_row(task["id"])
    # 无清理标记 → True
    assert await PackTaskManager._replay_source_cleanup(row) is True

    # content_hash 缺失 → 记录错误并返回 False
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=1
            )
        )
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(cleanup_state="pending", content_hash="")
        )
    row = await _task_row(task["id"])
    assert await PackTaskManager._replay_source_cleanup(row) is False
    sources = await pack_repo.list_pack_task_source_rows(task["id"])
    assert sources[0]["cleanup_error"]


async def test_replay_source_cleanup_cancel(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_replay_cancel")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=1
            )
        )
    row = await _task_row(task["id"])
    cancel = _event()
    cancel.set()
    with pytest.raises(InterruptedError):
        await PackTaskManager._replay_source_cleanup(row, cancel_event=cancel)

    # 锁内取消
    cancel.clear()
    entered = _event()

    class ArmingLock:
        async def __aenter__(self) -> "ArmingLock":
            entered.set()
            await asyncio.sleep(0.2)
            cancel.set()
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        pack_service, "get_content_hash_lock", AsyncMock(return_value=ArmingLock())
    )
    try:
        with pytest.raises(InterruptedError):
            await PackTaskManager._replay_source_cleanup(row, cancel_event=cancel)
    finally:
        monkey.undo()


async def test_replay_source_cleanup_exception_with_real_path(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_replay_exc")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
        delete_source=True,
    )
    tomb = "/tmp/some-tombstone-path"
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=1
            )
        )
        await conn.execute(
            update(pack_task_sources).where(
                pack_task_sources.c.task_id == task["id"]
            ).values(cleanup_real_path=tomb)
        )
    row = await _task_row(task["id"])

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("delete boom")

    monkeypatch.setattr(PackTaskManager, "_durable_delete_source_path", boom)
    assert await PackTaskManager._replay_source_cleanup(row) is False
    sources = await pack_repo.list_pack_task_source_rows(task["id"])
    assert sources[0]["cleanup_error"]


# ---------------------------------------------------------------- measure / stale dirs


def test_measure_pack_materialized_bytes_edges(tmp_path: Path) -> None:
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_987001"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "a").write_bytes(b"x" * 10)
    task = {"id": 987001, "reserved_bytes": 100, "prepared_content_hash": "zzz"}
    assert PackTaskManager._measure_pack_materialized_bytes(task, _event()) == 10
    task2 = {
        "id": 987001, "reserved_bytes": 5, "prepared_content_hash": "v2:file",
    }
    assert PackTaskManager._measure_pack_materialized_bytes(task2, _event()) == 5

    cancel = _event()
    (pack_dir / "b").write_bytes(b"y")
    cancel.set()
    with pytest.raises(InterruptedError):
        PackTaskManager._measure_pack_materialized_bytes(
            {"id": 987001, "reserved_bytes": 100, "prepared_content_hash": ""}, cancel
        )


def test_cleanup_stale_pack_dirs_skips_malformed() -> None:
    downloading = Path(settings.download_dir) / "downloading"
    downloading.mkdir(parents=True, exist_ok=True)
    (downloading / "pack_notanid").mkdir()
    (downloading / "pack_7").mkdir()
    (downloading / "unrelated.txt").write_bytes(b"x")
    PackTaskManager._cleanup_stale_pack_dirs({7})
    assert (downloading / "pack_7").exists()
    assert not (downloading / "pack_notanid").exists()
    assert (downloading / "unrelated.txt").exists()


# ---------------------------------------------------------------- public service functions


async def test_cancel_or_delete_pack_task_branches(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_cancel_or_delete")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
    )
    await _set_reserved(user["id"], 10)
    # 终态 → 删除
    result = await cancel_or_delete_pack_task(user["id"], task["id"])
    assert result["ok"] is True
    # 不存在
    with pytest.raises(NotFoundError):
        await cancel_or_delete_pack_task(user["id"], task["id"])

    active = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )
    await _set_reserved(user["id"], 10)
    # cancel 失败且任务仍在 → BadRequest
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        pack_service, "cancel_active_pack_task", AsyncMock(return_value={"status": "packing"})
    )
    try:
        with pytest.raises(BadRequestError):
            await cancel_or_delete_pack_task(user["id"], active["id"])
    finally:
        monkey.undo()

    # cancel 后任务消失 → NotFound
    async def vanish(uid: int, tid: int) -> None:
        await pack_repo.cancel_active_pack_task(uid, tid)
        async with transaction() as conn:
            await conn.execute(
                pack_tasks.delete().where(pack_tasks.c.id == tid)
            )
        return None

    monkey.setattr(pack_service, "cancel_active_pack_task", vanish)
    try:
        with pytest.raises(NotFoundError):
            await cancel_or_delete_pack_task(user["id"], active["id"])
    finally:
        monkey.undo()

    # cancel 返回 None，但任务被并发置为终态 → 任务已结束
    concurrent = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    await _set_reserved(user["id"], 10)

    async def fail_then_none(uid: int, tid: int) -> None:
        async with transaction() as conn:
            await conn.execute(
                update(pack_tasks).where(pack_tasks.c.id == tid).values(
                    status="failed", updated_at_ms=now_ms()
                )
            )
        return None

    monkey.setattr(pack_service, "cancel_active_pack_task", fail_then_none)
    try:
        result = await cancel_or_delete_pack_task(user["id"], concurrent["id"])
        assert result == {"ok": True, "message": "任务已结束"}
    finally:
        monkey.undo()


async def test_create_pack_task_from_user_files_errors(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, source = await _make_file_user(username="pack_create_err")
    monkeypatch.setattr(PackTaskManager, "submit", AsyncMock(return_value=True))
    kwargs = dict(
        user_id=user["id"], quota_bytes=10**9, output_name=None,
        delete_source=False,
    )
    # 源不可用
    source.unlink()
    with pytest.raises(BadRequestError):
        await create_pack_task_from_user_files(
            file_ids=[user_file["id"]], **kwargs
        )
    source.write_bytes(b"hello")

    # 空文件 → BadRequest
    empty = Path(settings.download_dir) / "store" / "empty.txt"
    empty.write_bytes(b"")
    empty_ref = await create_user_file_v0(
        user_id=user["id"], real_path=empty, content_hash="hash_empty",
        display_name="empty.txt", size_bytes=0,
    )
    with pytest.raises(BadRequestError):
        await create_pack_task_from_user_files(
            file_ids=[empty_ref["id"]], **kwargs
        )

    async def raise_boundary(*_a: Any, **_k: Any) -> list[Any]:
        raise PackBoundaryError("too many")

    monkeypatch.setattr(pack_service, "_scan_archive_items_for_admission", raise_boundary)
    with pytest.raises(BadRequestError):
        await create_pack_task_from_user_files(file_ids=[user_file["id"]], **kwargs)

    async def raise_oserror(*_a: Any, **_k: Any) -> list[Any]:
        raise OSError("io")

    monkeypatch.setattr(pack_service, "_scan_archive_items_for_admission", raise_oserror)
    with pytest.raises(BadRequestError):
        await create_pack_task_from_user_files(file_ids=[user_file["id"]], **kwargs)
    monkeypatch.undo()

    # 重复 file_ids
    with pytest.raises(BadRequestError):
        await create_pack_task_from_user_files(
            file_ids=[user_file["id"], user_file["id"]], **kwargs
        )

    # 各类 admission 拒绝
    cases = {
        "disk": ForbiddenError,
        "duplicate": ConflictError,
        "completed": ConflictError,
        "source": BadRequestError,
        "user_missing": NotFoundError,
    }
    for reason, exc_type in cases.items():
        async def reject(*_a: Any, _reason: str = reason, **_k: Any) -> None:
            raise PackAdmissionError(_reason)

        monkeypatch.setattr(pack_service, "create_pending_pack_with_reservation", reject)
        with pytest.raises(exc_type):
            await create_pack_task_from_user_files(file_ids=[user_file["id"]], **kwargs)
    monkeypatch.undo()



async def test_admission_single_directory_pack_unwraps_canonical_dir(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """admission 单目录打包必须携带真实 content_hash，canonical 目录剥掉单层根目录。"""
    from app.services.storage import get_store_path_for_hash
    from app.services.storage_index import scan_storage_path

    user = await create_user_v0(username="pack_dir_admission", quota_bytes=10**9)
    inner = Path(settings.download_dir) / "seed" / "bt_dir"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "f.txt").write_bytes(b"data")

    dir_hash = scan_storage_path(inner).content_hash
    store_dir = get_store_path_for_hash(dir_hash)
    store_dir.mkdir(parents=True, exist_ok=True)
    # canonical 存储布局：store/<v2 路径>/<单一同名子目录>/内容
    (store_dir / "bt_dir").mkdir(exist_ok=True)
    shutil.copy2(inner / "f.txt", store_dir / "bt_dir" / "f.txt")

    user_file = await create_user_file_v0(
        user_id=user["id"],
        real_path=store_dir,
        content_hash=dir_hash,
        display_name="bt_dir",
        size_bytes=4,
        is_directory=True,
    )
    monkeypatch.setattr(PackTaskManager, "submit", AsyncMock(return_value=True))

    items = await pack_service._scan_archive_items_for_admission(
        [store_dir.resolve()], ["bt_dir"], [dir_hash]
    )
    assert [item.arcname for item in items] == ["f.txt"]

    task = await pack_service.create_pack_task_from_user_files(
        user_id=user["id"], quota_bytes=10**9,
        file_ids=[user_file["id"]], output_name=None, delete_source=False,
    )
    assert task["status"] == "pending"

async def test_create_pack_task_success_path(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_create_ok")
    submitted = []
    async def fake_submit(task_id: int) -> bool:
        submitted.append(task_id)
        return True

    monkey = pytest.MonkeyPatch()
    monkey.setattr(PackTaskManager, "submit", fake_submit)
    try:
        result = await create_pack_task_from_user_files(
            user_id=user["id"], quota_bytes=10**9, file_ids=[user_file["id"]],
            output_name=None, delete_source=False,
        )
    finally:
        monkey.undo()
    assert result["status"] == "pending"
    assert submitted


# ---------------------------------------------------------------- 第二批补充：收尾未覆盖分支


def test_cancel_aware_reader_readable() -> None:
    from app.modules.pack import _CancelAwareReader

    class FakeSource(io.IOBase):
        def read(self, _size: int = -1) -> bytes:
            return b""

    reader = _CancelAwareReader(FakeSource(), _event(), _ProgressTracker(1))
    assert reader.readable() is True


def test_measure_pack_materialized_bytes_counts_canonical(tmp_path: Path) -> None:
    from app.services.storage import get_store_path_for_hash

    legacy_hash = "ab" * 32
    canonical = get_store_path_for_hash(legacy_hash)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"z" * 7)
    task = {"id": 987002, "reserved_bytes": 100, "prepared_content_hash": legacy_hash}
    assert PackTaskManager._measure_pack_materialized_bytes(task, _event()) == 7


async def test_run_optional_thread_cancellation(temp_db: str) -> None:
    release = _event()

    def blocking() -> str:
        release.wait(5)
        return "done"

    async def runner() -> None:
        await PackTaskManager._run_optional_thread(None, _event(), blocking)

    outer = asyncio.create_task(runner())
    await asyncio.sleep(0.05)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    release.set()


async def test_wait_thread_tasks_gathers_pending(temp_db: str) -> None:
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    release = _event()
    started = _event()

    def blocking() -> None:
        started.set()
        release.wait(5)

    thread_task = asyncio.create_task(asyncio.to_thread(blocking))
    job.thread_tasks.add(thread_task)
    for _ in range(60):
        if started.is_set():
            break
        await asyncio.sleep(0.05)
    assert started.is_set()
    waiter = asyncio.create_task(PackTaskManager._wait_thread_tasks(job))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    release.set()
    await waiter


async def test_submit_rejects_blocked_user(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_submit_block")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="failed",
    )
    PackTaskManager._blocked_user_ids.add(user["id"])
    try:
        assert await PackTaskManager.submit(task["id"]) is False
    finally:
        PackTaskManager._blocked_user_ids.discard(user["id"])


async def test_submit_pending_without_capacity(temp_db: str) -> None:
    PackTaskManager._running_tasks[999_997] = pack_service._RunningPackJob(
        task=AsyncMock(), cancel_event=_event()
    )
    try:
        await PackTaskManager.submit_pending()
    finally:
        PackTaskManager._running_tasks.clear()


async def test_recover_startup_persistence_failure_isolated(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_recover_persist")
    await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )

    async def boundary(_row: dict[str, Any], _job: Any) -> None:
        raise PackBoundaryError("bad")

    async def persist_fails(_tid: int, _msg: str) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(PackTaskManager, "_recover_one_startup", boundary)
    monkeypatch.setattr(PackTaskManager, "_update_task_error", persist_fails)
    await PackTaskManager.recover_startup()


async def test_recover_one_startup_pending_clears_dir(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_recover_pending")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )
    pack_dir = Path(settings.download_dir) / "downloading" / f"pack_{task['id']}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "junk").write_bytes(b"x")
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    await PackTaskManager._recover_one_startup(
        await pack_repo.get_pack_task_row(task["id"]), job
    )
    assert not pack_dir.exists()
    row = await pack_repo.get_pack_task_row(task["id"])
    assert row["materialized_bytes"] == 0


async def test_schedule_retry_task_disappears_after_requeue(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_retry_gone")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    real_row = await pack_repo.get_pack_task_row(task["id"])
    calls = 0

    async def vanish(tid: int) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        return real_row if calls == 1 else None

    monkeypatch.setattr(pack_service, "get_pack_task_row", vanish)
    await PackTaskManager._schedule_retry(task["id"])


async def test_run_persistent_pack_completed_replay_succeeds(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_replay_ok")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="completed",
        delete_source=True,
    )
    async with transaction() as conn:
        await conn.execute(
            update(pack_tasks).where(pack_tasks.c.id == task["id"]).values(
                source_cleanup_pending=1
            )
        )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    monkeypatch.setattr(
        PackTaskManager, "_replay_source_cleanup", AsyncMock(return_value=True)
    )
    await PackTaskManager._run_persistent_pack(task["id"], job, None)


async def test_run_persistent_pack_packing_without_prepared(temp_db: str) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_packing")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="packing",
    )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    await PackTaskManager._run_persistent_pack(task["id"], job, None)


async def test_run_persistent_pack_mark_conflict(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_mark")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )
    job = pack_service._RunningPackJob(task=AsyncMock(), cancel_event=_event())
    monkeypatch.setattr(
        pack_service, "mark_pack_task_packing_if_pending", AsyncMock(return_value=False)
    )
    await PackTaskManager._run_persistent_pack(task["id"], job, None)

    real_row = await pack_repo.get_pack_task_row(task["id"])
    calls = 0

    async def row_then_none(tid: int) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        return real_row if calls == 1 else None

    monkeypatch.setattr(pack_service, "get_pack_task_row", row_then_none)
    monkeypatch.setattr(
        pack_service, "mark_pack_task_packing_if_pending", AsyncMock(return_value=True)
    )
    await PackTaskManager._run_persistent_pack(task["id"], job, None)


async def test_run_persistent_pack_cancellation_midway(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, user_file, _src = await _make_file_user(username="pack_run_cancel")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[user_file["id"]],
        source_size_bytes=5, reserved_bytes=10, status="pending",
    )
    await _set_reserved(user["id"], 10)
    started = _event()

    async def blocking(*_a: Any, **_k: Any) -> tuple[Any, Any, Any, Any]:
        started.set()
        await asyncio.sleep(5)
        raise AssertionError("should be cancelled")

    monkeypatch.setattr(PackTaskManager, "_resolve_task_sources", blocking)
    outer = asyncio.create_task(PackTaskManager.start_pack(task["id"], user["id"], [], []))
    for _ in range(60):
        if started.is_set():
            break
        await asyncio.sleep(0.05)
    assert started.is_set()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer


async def test_finalize_prepared_none_then_transition_failure(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="pack_finalize_none_cas")
    task = await _insert_pack_task(
        user_id=user["id"], source_ids=[], source_size_bytes=0,
        reserved_bytes=100, status="packing",
    )
    from app.modules.pack import _InstalledPrepared

    monkeypatch.setattr(
        PackTaskManager,
        "_install_prepared_file",
        AsyncMock(return_value=_InstalledPrepared(Path("/nonexistent"), False)),
    )
    monkeypatch.setattr(
        pack_service, "finalize_prepared_pack_task", AsyncMock(return_value=None)
    )

    async def broken_cas(_tid: int, _msg: str) -> str | None:
        raise RuntimeError("cas boom")

    monkeypatch.setattr(PackTaskManager, "_terminalize_finalize_cas", broken_cas)
    with pytest.raises(OSError):
        await PackTaskManager._finalize_prepared(
            {**await _task_row(task["id"]),
             "prepared_content_hash": "v2:file:" + "5" * 64,
             "prepared_size_bytes": 5, "prepared_filename": "o.bin"},
            _event(),
        )


async def test_install_prepared_size_mismatch_falls_through(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_size")
    content = b"size-mismatch"
    scratch = _write_store_file("mismatch.bin", content)
    content_hash = scan_storage_path(scratch).content_hash
    # stored 记录指向同 hash 但尺寸不符的文件 → matches 返回 False
    wrong = _write_store_file("wrong_size.bin", b"other-length")
    monkeypatch.setattr(
        pack_service, "get_stored_file_by_identity",
        AsyncMock(return_value={"real_path": str(wrong)}),
    )
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_4"
    pack_dir.mkdir(parents=True, exist_ok=True)
    prepared = pack_dir / "mismatch.bin"
    prepared.write_bytes(content)
    result = await PackTaskManager._install_prepared_file(
        4, content_hash=content_hash, size_bytes=len(content),
        filename=prepared.name, cancel_event=_event(), job=None,
    )
    assert result.created_by_this_attempt is True
    result.path.unlink()


async def test_install_prepared_scan_value_error_returns_false(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_scan_err")
    content = b"scan-err"
    scratch = _write_store_file("scanerr.bin", content)
    content_hash = scan_storage_path(scratch).content_hash
    monkeypatch.setattr(
        pack_service, "get_stored_file_by_identity",
        AsyncMock(return_value={"real_path": str(scratch)}),
    )

    def bad_scan(*_a: Any, **_k: Any) -> Any:
        raise ValueError("corrupt")

    monkeypatch.setattr(pack_service, "scan_storage_path", bad_scan)
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_5"
    pack_dir.mkdir(parents=True, exist_ok=True)
    prepared = pack_dir / "scanerr.bin"
    prepared.write_bytes(content)
    with pytest.raises(PackBoundaryError, match="校验失败"):
        await PackTaskManager._install_prepared_file(
            5, content_hash=content_hash, size_bytes=len(content),
            filename=prepared.name, cancel_event=_event(), job=None,
        )


async def test_install_prepared_release_cancelled_unlinks_target(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.storage_index import scan_storage_path

    await create_user_v0(username="pack_install_cancel")
    content = b"cancel-me"
    scratch = _write_store_file("cancelme.bin", content)
    content_hash = scan_storage_path(scratch).content_hash
    pack_dir = Path(settings.download_dir) / "downloading" / "pack_6"
    pack_dir.mkdir(parents=True, exist_ok=True)
    prepared = pack_dir / "cancelme.bin"
    prepared.write_bytes(content)

    monkeypatch.setattr(
        pack_service, "_durable_link_file", lambda *_a: False
    )
    monkeypatch.setattr(
        pack_service, "reserve_pack_install_bytes", AsyncMock(return_value=True)
    )

    async def raise_cancelled(*_a: Any, **_k: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        pack_service, "clear_pack_install_reservation",
        AsyncMock(side_effect=raise_cancelled),
    )
    with pytest.raises(asyncio.CancelledError):
        await PackTaskManager._install_prepared_file(
            6, content_hash=content_hash, size_bytes=len(content),
            filename=prepared.name, cancel_event=_event(), job=None,
        )
    assert not _store_path_for(content_hash).exists()


def test_unwrap_stored_directory_invalid_hash_raises(tmp_path):
    """非法 content_hash（空/含路径分隔符）应抛 ValueError 由上层转任务错误。"""
    import threading

    from app.modules.pack import PackTaskManager

    source = tmp_path / "legacy_dir"
    source.mkdir()
    (source / "inner").mkdir()
    for bad_hash in ("", "sub/dir", "v2:file"):
        with pytest.raises(ValueError):
            PackTaskManager._unwrap_stored_directory(
                source, bad_hash, threading.Event()
            )
    # 合法 legacy key（非空且路径安全）不在此列，走 canonical 判定后原样返回
    assert (
        PackTaskManager._unwrap_stored_directory(
            source, "a" * 64, threading.Event()
        )
        is source
    )
