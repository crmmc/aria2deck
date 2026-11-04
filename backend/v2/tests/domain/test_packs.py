from __future__ import annotations

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import (
    PackProgress,
    PackSourceSnapshot,
    PackStatus,
    PackStep,
    SourceCleanupDecision,
    advance_pack_step,
    begin_pack,
    can_delete_pack,
    complete_pack,
    failure_disposition,
    plan_source_cleanup,
    prepare_output,
    retry_delay_ms,
    transition_pack,
    validate_prepared_values,
)


def test_pack_progress_uses_ordered_recoverable_steps() -> None:
    progress = begin_pack(PackStatus.PENDING)
    assert progress == PackProgress(PackStatus.PACKING, PackStep.VALIDATING, 0)
    progress = advance_pack_step(progress)
    assert progress.step is PackStep.COMPRESSING
    progress = advance_pack_step(progress)
    assert progress.step is PackStep.VERIFYING
    prepared = prepare_output(progress, path="prepared/pack.zip", size=100, digest="ab" * 32)
    assert prepared.path == "prepared/pack.zip"
    assert complete_pack(progress, prepared) == PackProgress(PackStatus.DONE, None, 100)


def test_pack_progress_rejects_skipped_or_extra_steps() -> None:
    with pytest.raises(DomainError):
        begin_pack(PackStatus.PACKING)
    with pytest.raises(DomainError):
        advance_pack_step(PackProgress(PackStatus.PENDING, None, 0))
    with pytest.raises(DomainError):
        advance_pack_step(PackProgress(PackStatus.PACKING, PackStep.VERIFYING, 20))
    with pytest.raises(DomainError):
        complete_pack(
            PackProgress(PackStatus.PACKING, PackStep.COMPRESSING, 20),
            validate_prepared_values("prepared/pack.zip", 10, "cd" * 32),
        )


def test_prepared_output_is_all_or_none_and_install_revalidates_final_bytes() -> None:
    assert validate_prepared_values(None, None, None) is None
    for values in [("path", None, None), (None, 1, "ab" * 32), ("path", 1, None)]:
        with pytest.raises(DomainError) as caught:
            validate_prepared_values(*values)
        assert caught.value.code is ErrorCode.INVALID_STATE
    with pytest.raises(DomainError):
        validate_prepared_values("../escape", 1, "ab" * 32)
    with pytest.raises(DomainError):
        validate_prepared_values("path", -1, "ab" * 32)


def test_prepare_output_only_happens_during_verification() -> None:
    with pytest.raises(DomainError):
        prepare_output(
            PackProgress(PackStatus.PACKING, PackStep.COMPRESSING, 0),
            path="prepared/pack.zip",
            size=1,
            digest="ab" * 32,
        )


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        (PackStatus.PENDING, PackStatus.PACKING, True),
        (PackStatus.PACKING, PackStatus.PENDING, True),
        (PackStatus.PACKING, PackStatus.DONE, True),
        (PackStatus.PACKING, PackStatus.FAILED, True),
        (PackStatus.PENDING, PackStatus.CANCELLED, True),
        (PackStatus.DONE, PackStatus.PACKING, False),
        (PackStatus.FAILED, PackStatus.PENDING, False),
    ],
)
def test_pack_state_machine_supports_requeue_but_not_terminal_reuse(
    current: PackStatus, target: PackStatus, allowed: bool
) -> None:
    if allowed:
        assert transition_pack(current, target) is target
        assert transition_pack(current, current) is current
    else:
        with pytest.raises(DomainError):
            transition_pack(current, target)


def snapshot() -> PackSourceSnapshot:
    return PackSourceSnapshot(
        user_file_id=1,
        content_id=2,
        content_key="file:" + "ab" * 32,
        display_name="source.bin",
        created_at_ms=100,
        size=10,
    )


def test_source_cleanup_compares_the_complete_creation_identity() -> None:
    original = snapshot()
    assert plan_source_cleanup(original, original, "file:" + "cd" * 32) is SourceCleanupDecision.DELETE
    assert plan_source_cleanup(original, None, "file:" + "cd" * 32) is SourceCleanupDecision.ALREADY_MISSING
    changed = PackSourceSnapshot(1, 2, original.content_key, "renamed.bin", 100, 10)
    assert plan_source_cleanup(original, changed, "file:" + "cd" * 32) is SourceCleanupDecision.KEEP
    assert plan_source_cleanup(original, original, original.content_key) is SourceCleanupDecision.KEEP


@pytest.mark.parametrize(
    "status,pending,allowed",
    [
        (PackStatus.DONE, 0, True),
        (PackStatus.CANCELLED, 0, True),
        (PackStatus.DONE, 1, False),
        (PackStatus.PACKING, 0, False),
    ],
)
def test_pack_record_deletion_waits_for_terminal_state_and_source_cleanup(
    status: PackStatus, pending: int, allowed: bool
) -> None:
    assert can_delete_pack(status, pending) is allowed


def test_pack_deletion_rejects_negative_cleanup_count() -> None:
    with pytest.raises(DomainError):
        can_delete_pack(PackStatus.DONE, -1)


@pytest.mark.parametrize("count,expected", [(1, 250), (2, 1000), (3, 5000), (4, 15000), (9, 15000)])
def test_transient_failure_requeues_with_bounded_delay(count: int, expected: int) -> None:
    assert failure_disposition(transient=True).value == "requeue"
    assert retry_delay_ms(count, transient=True) == expected


def test_deterministic_failure_is_not_retried() -> None:
    assert failure_disposition(transient=False).value == "fail"
    assert retry_delay_ms(1, transient=False) is None
    with pytest.raises(DomainError):
        retry_delay_ms(0, transient=True)


@pytest.mark.parametrize("value", [-1, 101])
def test_pack_step_progress_is_bounded(value: int) -> None:
    with pytest.raises(DomainError):
        PackProgress(PackStatus.PACKING, PackStep.VALIDATING, value)


def test_pack_cannot_complete_without_prepared_output() -> None:
    with pytest.raises(DomainError) as caught:
        complete_pack(PackProgress(PackStatus.PACKING, PackStep.VERIFYING, 100), None)
    assert caught.value.code is ErrorCode.INVALID_STATE
