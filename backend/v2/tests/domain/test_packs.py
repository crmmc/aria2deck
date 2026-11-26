from __future__ import annotations

import pytest

from aria2deck.domain import packs as pack_domain
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import (
    GENERIC_PACK_FAILURE_MESSAGE,
    PACK_OUTPUT_DELETED_ERROR_CODE,
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
    mark_output_deleted,
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
        (PackStatus.DONE, PackStatus.FAILED, False),
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


def test_pack_output_deletion_reuses_done_history_with_a_dedicated_code() -> None:
    assert mark_output_deleted(PackStatus.DONE) is PackStatus.FAILED
    assert PACK_OUTPUT_DELETED_ERROR_CODE == "pack_output_deleted"


@pytest.mark.parametrize(
    "code,expected",
    [
        # dispatcher 落库的确定性 DomainError 码。
        ("invalid_state", "打包来源或任务状态已变化"),
        ("invalid_input", "打包参数无效"),
        ("task_too_large", "打包输出超出大小限制"),
        ("quota_exceeded", "存储配额不足"),
        ("duplicate_resource", "已有相同的打包任务"),
        # 产物物理删除后的历史改写码。
        ("pack_output_deleted", "打包产物已删除"),
        # 非 DomainError 与未知码一律通用文案，内部代号不外泄。
        ("internal", GENERIC_PACK_FAILURE_MESSAGE),
        ("some_future_code", GENERIC_PACK_FAILURE_MESSAGE),
    ],
)
def test_pack_failure_message_maps_every_recorded_code(code: str, expected: str) -> None:
    message = pack_domain.pack_failure_message(code)

    assert message == expected
    assert code not in message


def test_pack_failure_message_is_empty_without_error() -> None:
    assert pack_domain.pack_failure_message(None) == ""


@pytest.mark.parametrize(
    "status",
    [PackStatus.PENDING, PackStatus.PACKING, PackStatus.FAILED, PackStatus.CANCELLED],
)
def test_pack_output_deletion_rejects_non_done_history(status: PackStatus) -> None:
    with pytest.raises(DomainError) as caught:
        mark_output_deleted(status)
    assert caught.value.code is ErrorCode.INVALID_STATE


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


# --- 输出预算与路径边界（getattr 读取新 API，保证实现前仍可收集并失败） ---


def _budget_start() -> pack_domain.ArchiveBudget:
    return pack_domain.ArchiveBudget()


def _item(arcname: str, *, is_dir: bool = False, size: int = 0) -> pack_domain.ArchiveItem:
    return pack_domain.ArchiveItem(arcname=arcname, is_dir=is_dir, size=size)


def _summary(
    *,
    entry_count: int,
    path_bytes: int,
    file_bytes: int,
    directory_count: int,
    max_path_bytes: int,
) -> pack_domain.ArchiveEntrySummary:
    return pack_domain.ArchiveEntrySummary(
        entry_count=entry_count,
        path_bytes=path_bytes,
        file_bytes=file_bytes,
        directory_count=directory_count,
        max_path_bytes=max_path_bytes,
    )


def _require_output_within_budget(size: int, budget: int) -> None:
    pack_domain.require_pack_output_within_budget(size, budget)


def test_archive_budget_accepts_exactly_the_entry_limit_and_rejects_one_more() -> None:
    item = _item("a")
    at_limit = _budget_start().with_items([item] * pack_domain.MAX_ARCHIVE_ENTRIES)
    assert at_limit.entries == 100_000
    with pytest.raises(DomainError) as caught:
        _budget_start().with_items([item] * (pack_domain.MAX_ARCHIVE_ENTRIES + 1))
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert at_limit.entries == 100_000, "试加失败不得污染已接受来源"


def test_archive_budget_accepts_exactly_the_single_path_limit() -> None:
    at_limit = _budget_start().with_items([_item("a" * 4096)])
    assert at_limit.path_bytes == 4096
    with pytest.raises(DomainError) as caught:
        _budget_start().with_items([_item("a" * 4097)])
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_archive_budget_accepts_exactly_the_total_path_bytes_limit() -> None:
    item = _item("a" * 4096)
    at_limit = _budget_start().with_items([item] * 4096)
    assert at_limit.path_bytes == 16 * 1024 * 1024
    with pytest.raises(DomainError) as caught:
        _budget_start().with_items([item] * 4097)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_archive_budget_counts_utf8_bytes_of_the_complete_path() -> None:
    budget = _budget_start().with_items([_item("数据.bin", size=7)])
    assert budget.path_bytes == 10, "必须按 UTF-8 字节而不是字符数计费"
    assert budget.reserved_bytes == 7 + 512 + 2 * 10 + 65_536
    with pytest.raises(DomainError) as caught:
        _budget_start().with_items([_item("\ud800")])
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_archive_budget_charges_the_directory_marker() -> None:
    budget = _budget_start().with_items([_item("dir/", is_dir=True)])
    assert (budget.entries, budget.path_bytes, budget.source_bytes) == (1, 4, 0)
    assert budget.reserved_bytes == 512 + 2 * 4 + 65_536


def test_archive_budget_reserves_source_metadata_and_minimum_slack() -> None:
    budget = _budget_start().with_items([_item("src-1", size=30), _item("src-2", size=20)])
    assert budget.source_bytes == 50
    assert budget.reserved_bytes == 66_630


def test_archive_budget_trial_add_never_pollutes_accepted_sources() -> None:
    accepted = _budget_start().with_items([_item("src-1", size=10)])
    with pytest.raises(DomainError):
        accepted.with_items([_item("b" * 4097, size=10)])
    assert accepted.entries == 1 and accepted.source_bytes == 10
    merged = accepted.with_items([_item("src-2", size=25)])
    assert (merged.entries, merged.source_bytes) == (2, 35)
    assert merged.reserved_bytes == 66_615


def test_directory_summary_expands_into_the_same_budget_as_real_items() -> None:
    summary = _summary(
        entry_count=3, path_bytes=7, file_bytes=12, directory_count=1, max_path_bytes=3
    )
    expanded = _budget_start().with_items(
        [
            _item("src/", is_dir=True),
            _item("src/d/", is_dir=True),
            _item("src/d/f", size=5),
            _item("src/d/g", size=7),
        ]
    )
    assert _budget_start().with_directory(prefix="src", summary=summary) == expanded
    assert (expanded.entries, expanded.path_bytes, expanded.source_bytes) == (4, 24, 12)
    assert expanded.reserved_bytes == 12 + 4 * 512 + 2 * 24 + 65_536


def test_directory_prefix_is_charged_in_utf8_bytes() -> None:
    summary = _summary(
        entry_count=1, path_bytes=6, file_bytes=5, directory_count=0, max_path_bytes=6
    )
    budget = _budget_start().with_directory(prefix="目录", summary=summary)
    # "目录/" = 7 字节、"目录/数据" = 13 字节；按字符数会得到 10。
    assert budget.path_bytes == 20
    assert budget.reserved_bytes == 5 + 2 * 512 + 2 * 20 + 65_536


def test_directory_summary_checks_the_single_path_limit_with_its_prefix() -> None:
    summary = _summary(
        entry_count=1, path_bytes=4019, file_bytes=0, directory_count=0, max_path_bytes=4019
    )
    budget = _budget_start().with_directory(prefix="dir", summary=summary)
    assert budget.path_bytes == 2 * 4 + 4019
    with pytest.raises(DomainError) as caught:
        _budget_start().with_directory(prefix="x" * 80, summary=summary)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_directory_summary_counts_towards_the_entry_limit() -> None:
    summary = _summary(
        entry_count=pack_domain.MAX_ARCHIVE_ENTRIES - 1,
        path_bytes=pack_domain.MAX_ARCHIVE_ENTRIES - 1,
        file_bytes=0,
        directory_count=0,
        max_path_bytes=1,
    )
    assert (
        _budget_start().with_directory(prefix="a", summary=summary).entries
        == pack_domain.MAX_ARCHIVE_ENTRIES
    )
    over = _summary(
        entry_count=pack_domain.MAX_ARCHIVE_ENTRIES,
        path_bytes=pack_domain.MAX_ARCHIVE_ENTRIES,
        file_bytes=0,
        directory_count=0,
        max_path_bytes=1,
    )
    with pytest.raises(DomainError) as caught:
        _budget_start().with_directory(prefix="a", summary=over)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_archive_item_requires_a_canonical_directory_marker() -> None:
    with pytest.raises(DomainError) as caught:
        _item("dir", is_dir=True)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError):
        _item("file/", is_dir=False)
    with pytest.raises(DomainError):
        _item("")
    with pytest.raises(DomainError):
        _item("a", size=-1)


def test_pack_output_budget_allows_an_exact_size_and_rejects_one_more_byte() -> None:
    _require_output_within_budget(66_068, 66_068)
    _require_output_within_budget(0, 0)
    with pytest.raises(DomainError) as caught:
        _require_output_within_budget(66_069, 66_068)
    assert caught.value.code is ErrorCode.TASK_TOO_LARGE
    with pytest.raises(DomainError) as caught:
        _require_output_within_budget(-1, 10)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_archive_budget_rejects_negative_accounting_and_inconsistent_summaries() -> None:
    with pytest.raises(DomainError) as caught:
        pack_domain.ArchiveBudget(entries=-1)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        pack_domain.ArchiveEntrySummary(entry_count=1, path_bytes=-1)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        pack_domain.ArchiveEntrySummary(entry_count=1, directory_count=2)
    assert caught.value.code is ErrorCode.INVALID_INPUT
