from __future__ import annotations

import re
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
