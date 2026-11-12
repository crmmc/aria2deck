from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import overload

from .content import normalize_relative_path
from .errors import DomainError, ErrorCode


class PackStatus(StrEnum):
    PENDING = "pending"
    PACKING = "packing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PackStep(StrEnum):
    VALIDATING = "validating"
    COMPRESSING = "compressing"
    VERIFYING = "verifying"


_PACK_TRANSITIONS = {
    PackStatus.PENDING: frozenset({PackStatus.PACKING, PackStatus.FAILED, PackStatus.CANCELLED}),
    PackStatus.PACKING: frozenset(
        {PackStatus.PENDING, PackStatus.DONE, PackStatus.FAILED, PackStatus.CANCELLED}
    ),
    PackStatus.DONE: frozenset(),
    PackStatus.FAILED: frozenset(),
    PackStatus.CANCELLED: frozenset(),
}


def transition_pack(current: PackStatus, target: PackStatus) -> PackStatus:
    if current == target:
        return current
    if target not in _PACK_TRANSITIONS[current]:
        raise DomainError(ErrorCode.INVALID_STATE, "打包任务不允许该状态转换")
    return target


PACK_OUTPUT_DELETED_ERROR_CODE = "pack_output_deleted"


def mark_output_deleted(status: PackStatus) -> PackStatus:
    """产物已物理删除时只允许已完成历史改写为失败；通用终态复用规则不因此放开。"""
    if status is not PackStatus.DONE:
        raise DomainError(ErrorCode.INVALID_STATE, "只有已完成的打包历史可以标记产物已删除")
    return PackStatus.FAILED


@dataclass(frozen=True, slots=True)
class PackProgress:
    status: PackStatus
    step: PackStep | None
    step_progress: int

    def __post_init__(self) -> None:
        if not 0 <= self.step_progress <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包阶段进度必须在 0 到 100 之间")


def begin_pack(status: PackStatus) -> PackProgress:
    if status is not PackStatus.PENDING:
        raise DomainError(ErrorCode.INVALID_STATE, "只有排队中的打包任务可以开始")
    return PackProgress(PackStatus.PACKING, PackStep.VALIDATING, 0)


def advance_pack_step(progress: PackProgress) -> PackProgress:
    if progress.status is not PackStatus.PACKING:
        raise DomainError(ErrorCode.INVALID_STATE, "只有执行中的打包任务可以切换阶段")
    next_step = {
        PackStep.VALIDATING: PackStep.COMPRESSING,
        PackStep.COMPRESSING: PackStep.VERIFYING,
        PackStep.VERIFYING: None,
        None: None,
    }.get(progress.step)
    if next_step is None:
        raise DomainError(ErrorCode.INVALID_STATE, "验收阶段必须完成安装，不能继续切换")
    return PackProgress(PackStatus.PACKING, next_step, 0)


@dataclass(frozen=True, slots=True)
class PreparedOutput:
    path: str
    size: int
    digest: str


@overload
def validate_prepared_values(path: str, size: int, digest: str) -> PreparedOutput: ...


@overload
def validate_prepared_values(path: None, size: None, digest: None) -> None: ...


@overload
def validate_prepared_values(
    path: str | None, size: int | None, digest: str | None
) -> PreparedOutput | None: ...


def validate_prepared_values(
    path: str | None, size: int | None, digest: str | None
) -> PreparedOutput | None:
    values = (path, size, digest)
    if all(value is None for value in values):
        return None
    if path is None or size is None or digest is None:
        raise DomainError(ErrorCode.INVALID_STATE, "prepared 输出字段必须同时存在")
    if size < 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise DomainError(ErrorCode.INVALID_INPUT, "prepared 输出大小或摘要无效")
    return PreparedOutput(normalize_relative_path(path), size, digest.lower())


def prepare_output(
    progress: PackProgress, *, path: str, size: int, digest: str
) -> PreparedOutput:
    if progress.status is not PackStatus.PACKING or progress.step is not PackStep.VERIFYING:
        raise DomainError(ErrorCode.INVALID_STATE, "只有验收阶段可以登记 prepared 输出")
    return validate_prepared_values(path, size, digest)


def complete_pack(progress: PackProgress, prepared: PreparedOutput | None) -> PackProgress:
    if prepared is None:
        raise DomainError(ErrorCode.INVALID_STATE, "没有 prepared 输出，不能完成打包")
    if progress.status is not PackStatus.PACKING or progress.step is not PackStep.VERIFYING:
        raise DomainError(ErrorCode.INVALID_STATE, "打包任务尚未完成最终验收")
    return PackProgress(PackStatus.DONE, None, 100)


MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_PATH_BYTES = 4096
MAX_TOTAL_ARCHIVE_PATH_BYTES = 16 * 1024 * 1024
METADATA_ENTRY_BYTES = 512
METADATA_MIN_SLACK_BYTES = 64 * 1024


def _pack_path_bytes(arcname: str) -> int:
    try:
        return len(arcname.encode("utf-8"))
    except UnicodeEncodeError:
        raise DomainError(ErrorCode.INVALID_INPUT, "打包路径编码无效") from None


@dataclass(frozen=True, slots=True)
class ArchiveItem:
    """One physical archive entry: a source file, a source root or a directory marker."""

    arcname: str
    is_dir: bool
    size: int = 0

    def __post_init__(self) -> None:
        if self.size < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包条目大小不能为负数")
        if not self.arcname:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包条目名称不能为空")
        if self.arcname.endswith("/") is not self.is_dir:
            raise DomainError(ErrorCode.INVALID_INPUT, "目录条目必须以 / 结尾，文件条目不能以 / 结尾")


@dataclass(frozen=True, slots=True)
class ArchiveEntrySummary:
    """Aggregate of one directory content; used instead of loading raw entry rows.

    `path_bytes` excludes directory trailing markers, `max_path_bytes` includes them.
    """

    entry_count: int = 0
    path_bytes: int = 0
    file_bytes: int = 0
    directory_count: int = 0
    max_path_bytes: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.entry_count,
                self.path_bytes,
                self.file_bytes,
                self.directory_count,
                self.max_path_bytes,
            )
            < 0
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "目录聚合摘要不能为负数")
        if self.directory_count > self.entry_count:
            raise DomainError(ErrorCode.INVALID_INPUT, "目录条目数不能超过总条目数")


def _require_archive_limits(entries: int, path_bytes: int) -> None:
    if entries > MAX_ARCHIVE_ENTRIES:
        raise DomainError(ErrorCode.INVALID_INPUT, "打包条目超过 100000 条上限")
    if path_bytes > MAX_TOTAL_ARCHIVE_PATH_BYTES:
        raise DomainError(ErrorCode.INVALID_INPUT, "打包路径总量超过 16 MiB 上限")


@dataclass(frozen=True, slots=True)
class ArchiveBudget:
    """Empirical output reservation: source bytes + entry metadata + minimum slack.

    Every trial add returns a new instance, so a rejected source cannot pollute the
    already accepted ones. The same accumulator serves precompute, creation and the
    persisted budget re-checks.
    """

    entries: int = 0
    path_bytes: int = 0
    source_bytes: int = 0

    def __post_init__(self) -> None:
        if min(self.entries, self.path_bytes, self.source_bytes) < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包预算不能为负数")

    @property
    def metadata_bytes(self) -> int:
        return self.entries * METADATA_ENTRY_BYTES + 2 * self.path_bytes

    @property
    def reserved_bytes(self) -> int:
        if not self.entries:
            return 0
        return self.source_bytes + self.metadata_bytes + max(
            METADATA_MIN_SLACK_BYTES, self.source_bytes // 100
        )

    def with_items(self, items: Iterable[ArchiveItem]) -> ArchiveBudget:
        entries, path_bytes, source_bytes = self.entries, self.path_bytes, self.source_bytes
        for item in items:
            encoded = _pack_path_bytes(item.arcname)
            if encoded > MAX_ARCHIVE_PATH_BYTES:
                raise DomainError(ErrorCode.INVALID_INPUT, "打包路径超过 4096 字节上限")
            entries += 1
            path_bytes += encoded
            if not item.is_dir:
                source_bytes += item.size
        _require_archive_limits(entries, path_bytes)
        return ArchiveBudget(entries, path_bytes, source_bytes)

    def with_directory(self, *, prefix: str, summary: ArchiveEntrySummary) -> ArchiveBudget:
        """Expand one directory source from its aggregate summary, without entry rows."""
        prefix_bytes = _pack_path_bytes(prefix)
        root_bytes = prefix_bytes + 1
        if root_bytes + summary.max_path_bytes > MAX_ARCHIVE_PATH_BYTES:
            raise DomainError(ErrorCode.INVALID_INPUT, "打包路径超过 4096 字节上限")
        entries = self.entries + summary.entry_count + 1
        path_bytes = (
            self.path_bytes
            + (summary.entry_count + 1) * root_bytes
            + summary.path_bytes
            + summary.directory_count
        )
        _require_archive_limits(entries, path_bytes)
        return ArchiveBudget(
            entries, path_bytes, self.source_bytes + summary.file_bytes
        )


def require_pack_output_within_budget(size: int, budget: int) -> None:
    """A real output length may equal the approved budget but never exceed it."""
    if size < 0 or budget < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "打包输出大小与预算不能为负数")
    if size > budget:
        raise DomainError(ErrorCode.TASK_TOO_LARGE, "打包输出超过已批准的空间预算")


@dataclass(frozen=True, slots=True)
class PackSourceSnapshot:
    user_file_id: int
    content_id: int
    content_key: str
    display_name: str
    created_at_ms: int
    size: int


class SourceCleanupDecision(StrEnum):
    DELETE = "delete"
    KEEP = "keep"
    ALREADY_MISSING = "already_missing"


def plan_source_cleanup(
    snapshot: PackSourceSnapshot,
    current: PackSourceSnapshot | None,
    output_content_key: str,
) -> SourceCleanupDecision:
    if current is None:
        return SourceCleanupDecision.ALREADY_MISSING
    if current.content_key == output_content_key:
        return SourceCleanupDecision.KEEP
    if current != snapshot:
        return SourceCleanupDecision.KEEP
    return SourceCleanupDecision.DELETE


def can_delete_pack(status: PackStatus, pending_source_cleanups: int) -> bool:
    if pending_source_cleanups < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "来源清理数量不能为负数")
    return status in {PackStatus.DONE, PackStatus.FAILED, PackStatus.CANCELLED} and not pending_source_cleanups


class FailureDisposition(StrEnum):
    REQUEUE = "requeue"
    FAIL = "fail"


def failure_disposition(*, transient: bool) -> FailureDisposition:
    return FailureDisposition.REQUEUE if transient else FailureDisposition.FAIL


def retry_delay_ms(failure_count: int, *, transient: bool) -> int | None:
    if failure_count < 1:
        raise DomainError(ErrorCode.INVALID_INPUT, "失败次数必须从 1 开始")
    if not transient:
        return None
    delays = (250, 1000, 5000, 15000)
    return delays[min(failure_count - 1, len(delays) - 1)]
