from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ...domain.content import ContentDeletionState, ContentKind
from ...domain.packs import (
    PackSourceSnapshot,
    PackStatus,
    PackStep,
    PreparedOutput,
    SourceCleanupDecision,
)

ProgressCallback = Callable[[int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    """One pack input resolved to a physical root and its display arcname prefix."""

    display_name: str
    root: Path
    kind: ContentKind


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """安装结果：规范位路径，以及本次尝试是否新建了该规范对象。"""

    storage_path: str
    created_by_this_attempt: bool


@dataclass(frozen=True, slots=True)
class PrecomputeItemView:
    user_file_id: int
    accepted: bool
    error_code: str | None = None
    message: str | None = None
    display_name: str | None = None
    size: int = 0


@dataclass(frozen=True, slots=True)
class PrecomputeResultView:
    items: list[PrecomputeItemView]
    total_size: int
    available_bytes: int


@dataclass(frozen=True, slots=True)
class PackOutputFileView:
    user_file_id: int
    display_name: str
    size: int


@dataclass(frozen=True, slots=True)
class PackView:
    id: int
    status: PackStatus
    step: PackStep | None
    step_progress: int
    step_started_at_ms: int | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    error_code: str | None
    delete_source: bool
    output_name: str = ""
    output: PackOutputFileView | None = None


@dataclass(frozen=True, slots=True)
class SourceCleanupOutcome:
    source_id: int
    pack_id: int
    decision: SourceCleanupDecision | None
    cleanup_status: str
    error_code: str | None = None



@dataclass(frozen=True, slots=True)
class PackAdmissionSource:
    """Current state of a candidate source: ownership, snapshot fields and liveness."""

    user_file_id: int
    content_id: int
    content_key: str
    display_name: str
    created_at_ms: int
    size: int
    kind: ContentKind
    storage_path: str
    digest: str
    deletion_state: ContentDeletionState

    def snapshot(self) -> PackSourceSnapshot:
        return PackSourceSnapshot(
            user_file_id=self.user_file_id,
            content_id=self.content_id,
            content_key=self.content_key,
            display_name=self.display_name,
            created_at_ms=self.created_at_ms,
            size=self.size,
        )


@dataclass(frozen=True, slots=True)
class PackJobRecord:
    id: int
    owner_id: int
    status: PackStatus
    step: PackStep | None
    step_progress: int
    step_started_at_ms: int | None
    output_name: str
    pack_format: str
    source_size: int
    reserved_bytes: int
    physical_reserved_bytes: int
    delete_source: bool
    prepared: PreparedOutput | None
    retry_count: int
    next_attempt_at_ms: int | None
    error_code: str | None
    cleanup_pending: bool
    output_content_id: int | None
    output_content_key: str | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None


@dataclass(frozen=True, slots=True)
class PackSourceRecord:
    id: int
    pack_id: int
    snapshot: PackSourceSnapshot
    cleanup_status: str
    cleanup_error_code: str | None = None


__all__ = [
    "ArchiveResult",
    "ArchiveSource",
    "InstallOutcome",
    "PackAdmissionSource",
    "PackJobRecord",
    "PackOutputFileView",
    "PackSourceRecord",
    "PackSourceSnapshot",
    "PackView",
    "PrecomputeItemView",
    "PrecomputeResultView",
    "ProgressCallback",
    "SourceCleanupOutcome",
]
