from __future__ import annotations

import pytest

from aria2deck.application.library.disk_scan import (
    AdmissionCancelled,
    DiskAdmissionQueue,
    QueueEntry,
    QueueState,
)
from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import ErrorCode


def make_entry(storage_path: str, *, size: int = 10, enqueued_at_ms: int = 1) -> QueueEntry:
    return QueueEntry(
        storage_path=storage_path,
        kind=ContentKind.FILE,
        digest=storage_path.rpartition("-")[2] or "0" * 64,
        size=size,
        enqueued_at_ms=enqueued_at_ms,
    )


def test_enqueue_is_idempotent_and_reports_the_existing_state() -> None:
    queue = DiskAdmissionQueue()
    first = queue.enqueue([make_entry("objects/file-a")])
    again = queue.enqueue([make_entry("objects/file-a")])
    assert [(item.accepted, item.state) for item in first] == [(True, QueueState.QUEUED)]
    assert [(item.accepted, item.state) for item in again] == [(True, QueueState.QUEUED)]

    claimed = queue.claim_next(now_ms=5)
    assert claimed is not None
    queue.mark_done(claimed.storage_path, content_id=7, now_ms=6)
    done = queue.enqueue([make_entry("objects/file-a")])
    assert [(item.accepted, item.state) for item in done] == [(True, QueueState.DONE)]
    assert queue.snapshot().done_count == 1


def test_enqueue_rejects_when_pending_limit_is_reached() -> None:
    queue = DiskAdmissionQueue(max_pending=1)
    assert queue.enqueue([make_entry("objects/file-a")])[0].accepted is True

    rejected = queue.enqueue([make_entry("objects/file-b")])[0]

    assert rejected.accepted is False
    assert rejected.state is None
    assert rejected.error_code == ErrorCode.INVALID_STATE.value
    assert "上限 1 条" in (rejected.message or "")

    assert queue.claim_next(now_ms=2) is not None  # 排队 → 处理中，腾出一个位
    assert queue.enqueue([make_entry("objects/file-b")])[0].accepted is True


def test_claim_next_takes_the_oldest_queued_entry_only() -> None:
    queue = DiskAdmissionQueue()
    queue.enqueue(
        [
            make_entry("objects/file-a", enqueued_at_ms=1),
            make_entry("objects/file-b", enqueued_at_ms=2),
        ]
    )
    assert queue.claim_next(now_ms=10) is not None
    second = queue.claim_next(now_ms=11)
    assert second is not None
    assert second.storage_path == "objects/file-b"
    assert (second.state, second.started_at_ms, second.processed_bytes) == (
        QueueState.RUNNING,
        11,
        0,
    )
    assert queue.claim_next(now_ms=12) is None


def test_update_progress_writes_bytes_and_stop_makes_it_abort() -> None:
    queue = DiskAdmissionQueue()
    queue.enqueue([make_entry("objects/file-a")])
    queue.start()
    entry = queue.claim_next(now_ms=1)
    assert entry is not None

    queue.update_progress("objects/file-a", 1024)
    assert queue.snapshot().items[0].processed_bytes == 1024

    queue.stop()
    with pytest.raises(AdmissionCancelled):
        queue.update_progress("objects/file-a", 2048)
    assert queue.snapshot().running is False


def test_update_progress_ignores_unknown_and_non_running_entries() -> None:
    queue = DiskAdmissionQueue()
    queue.update_progress("objects/file-missing", 1)  # 未知路径：静默忽略
    queue.enqueue([make_entry("objects/file-a")])
    queue.stop()
    queue.update_progress("objects/file-a", 1)  # 排队中：不写入也不取消


def test_stop_releases_the_running_entry_back_to_the_queue() -> None:
    queue = DiskAdmissionQueue()
    queue.enqueue([make_entry("objects/file-a")])
    queue.start()
    entry = queue.claim_next(now_ms=1)
    assert entry is not None
    queue.update_progress("objects/file-a", 2048)

    queue.release("objects/file-a")

    snapshot = queue.snapshot()
    assert snapshot.queued_count == 1
    assert snapshot.running_count == 0
    item = snapshot.items[0]
    assert (item.state, item.started_at_ms, item.processed_bytes) == (QueueState.QUEUED, None, 0)


def test_failed_entry_can_be_requeued_and_moves_to_the_tail() -> None:
    queue = DiskAdmissionQueue()
    queue.enqueue([make_entry("objects/file-a"), make_entry("objects/file-b")])
    claimed = queue.claim_next(now_ms=1)
    assert claimed is not None
    queue.mark_failed(
        claimed.storage_path, code=ErrorCode.INVALID_STATE.value, message="摘要不一致", now_ms=2
    )
    failed = queue.get("objects/file-a")
    assert (failed is not None, failed.state if failed else None) == (True, QueueState.FAILED)

    outcomes = queue.enqueue([make_entry("objects/file-a", enqueued_at_ms=3)])

    assert [(item.accepted, item.state) for item in outcomes] == [(True, QueueState.QUEUED)]
    requeued = queue.get("objects/file-a")
    assert requeued is not None
    assert (requeued.state, requeued.error_code, requeued.message) == (
        QueueState.QUEUED,
        None,
        None,
    )
    assert [item.storage_path for item in queue.snapshot().items] == [
        "objects/file-b",
        "objects/file-a",
    ], "重试项按新加入顺序排到队尾"


def test_remove_returns_the_entry_and_drops_it_from_the_snapshot() -> None:
    queue = DiskAdmissionQueue()
    queue.enqueue([make_entry("objects/file-a")])
    removed = queue.remove("objects/file-a")
    assert removed is not None
    assert removed.storage_path == "objects/file-a"
    assert queue.remove("objects/file-a") is None
    assert queue.snapshot().items == ()
    assert queue.get("objects/file-a") is None


def test_snapshot_counts_orders_and_copies_entries() -> None:
    queue = DiskAdmissionQueue()
    queue.start()
    queue.enqueue(
        [
            make_entry("objects/file-a", size=5, enqueued_at_ms=1),
            make_entry("objects/file-b", size=7, enqueued_at_ms=2),
            make_entry("objects/file-c", size=11, enqueued_at_ms=3),
        ]
    )
    running = queue.claim_next(now_ms=100)
    assert running is not None
    queue.mark_done("objects/file-b", content_id=1, now_ms=200)
    queue.mark_failed("objects/file-c", code=ErrorCode.INVALID_STATE.value, message="x", now_ms=300)

    snapshot = queue.snapshot()

    assert (snapshot.running, snapshot.queued_count, snapshot.running_count) == (True, 0, 1)
    assert (snapshot.done_count, snapshot.failed_count) == (1, 1)
    assert snapshot.pending_bytes == 0
    assert [item.storage_path for item in snapshot.items] == [
        "objects/file-a",
        "objects/file-c",
        "objects/file-b",
    ], "处理中 → 排队中 →（失败/已入库按完成时间倒序）"

    snapshot.items[0].processed_bytes = 999
    assert queue.get("objects/file-a") is not None
    assert queue.get("objects/file-a").processed_bytes == 0  # type: ignore[union-attr]


def test_snapshot_pending_bytes_sums_queued_sizes_only() -> None:
    queue = DiskAdmissionQueue()
    queue.enqueue(
        [make_entry("objects/file-a", size=5), make_entry("objects/file-b", size=7)]
    )
    queue.start()
    queue.claim_next(now_ms=1)
    snapshot = queue.snapshot()
    assert (snapshot.pending_bytes, snapshot.queued_count, snapshot.running_count) == (7, 1, 1)


def test_start_and_stop_are_idempotent() -> None:
    queue = DiskAdmissionQueue()
    assert queue.running is False
    queue.start()
    queue.start()
    assert queue.running is True
    queue.stop()
    queue.stop()
    assert queue.running is False
