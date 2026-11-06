from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...domain.content import ContentDeletionState, ContentKind, RangeSpec


@dataclass(frozen=True, slots=True)
class ContentRecord:
    id: int
    content_key: str
    kind: ContentKind
    size: int
    storage_path: str
    digest: str
    deletion_state: ContentDeletionState
    read_leases: int = 0
    delete_lease_owner: str | None = None
    delete_lease_expires_at_ms: int | None = None
    tombstone_path: str | None = None


@dataclass(frozen=True, slots=True)
class UserFileRecord:
    id: int
    user_id: int
    content_id: int
    display_name: str
    created_at_ms: int
    kind: ContentKind
    size: int
    deletion_state: ContentDeletionState
    storage_path: str
    digest: str


@dataclass(frozen=True, slots=True)
class ContentEntryRecord:
    id: int
    content_id: int
    parent_path: str
    name: str
    path: str
    entry_kind: ContentKind
    size: int
    modified_at_ms: int | None


@dataclass(frozen=True, slots=True)
class UserFileSearchHit:
    user_file_id: int
    display_name: str
    path: str
    kind: ContentKind
    size: int


@dataclass(frozen=True, slots=True)
class StorageObjectRecord:
    content_id: int
    content_key: str
    kind: ContentKind
    size: int
    storage_path: str
    deletion_state: ContentDeletionState
    reference_count: int
    read_leases: int
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class StorageReferencingUser:
    user_id: int
    username: str
    display_name: str
    referenced_at_ms: int


@dataclass(frozen=True, slots=True)
class DeletionCandidate:
    content_id: int
    deletion_state: ContentDeletionState
    storage_path: str
    tombstone_path: str | None
    read_leases: int
    reference_count: int


@dataclass(frozen=True, slots=True)
class EntryDiff:
    """Read-model drift between content_entries and a physical scan."""

    missing: tuple[str, ...]  # 物理存在但读模型缺失
    unexpected: tuple[str, ...]  # 读模型存在但物理缺失
    size_mismatch: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (self.missing or self.unexpected or self.size_mismatch)


@dataclass(frozen=True, slots=True)
class UserFileView:
    id: int
    display_name: str
    kind: ContentKind
    size: int
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class DirectoryEntryView:
    name: str
    path: str
    kind: ContentKind
    size: int


@dataclass(frozen=True, slots=True)
class BreadcrumbItem:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class SearchHitView:
    user_file_id: int
    display_name: str
    path: str
    kind: ContentKind
    size: int


@dataclass(frozen=True, slots=True)
class StorageFileView:
    content_id: int
    content_key: str
    kind: ContentKind
    size: int
    storage_path: str
    on_disk: bool
    reference_count: int
    deletion_state: ContentDeletionState
    created_at_ms: int = 0
    file_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StorageReferencingUserView:
    user_id: int
    username: str
    display_name: str
    referenced_at_ms: int


@dataclass(frozen=True, slots=True)
class StorageDeleteOutcome:
    content_id: int
    accepted: bool
    error_code: str | None = None
    message: str | None = None


@dataclass(slots=True)
class DownloadTarget:
    """Resolved download with an acquired read lease; release exactly once."""

    content_id: int
    storage_path: str
    entry_relative_path: str | None
    physical_path: Path
    display_name: str
    total_size: int
    range_spec: RangeSpec | None
    released: bool = False
