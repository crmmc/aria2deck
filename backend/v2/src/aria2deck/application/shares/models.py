from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...domain.content import ContentKind
from ...domain.packs import PreparedOutput
from ...domain.shares import DownloadDecision, Share, ShareStatus


@dataclass(frozen=True, slots=True)
class ShareRecord:
    id: int
    code: str
    owner_id: int
    user_file_id: int | None
    file_name: str
    file_size: int
    file_kind: str
    content_key: str
    password_hash: str | None
    expires_at_ms: int | None
    revoked_at_ms: int | None
    max_downloads: int | None
    download_count: int
    created_at_ms: int
    file_pending_delete: bool

    def as_domain(self) -> Share:
        return Share(
            expires_at_ms=self.expires_at_ms,
            revoked_at_ms=self.revoked_at_ms,
            max_downloads=self.max_downloads,
            download_count=self.download_count,
            file_pending_delete=self.file_pending_delete,
        )


__all__ = [
    "DownloadDecision",
    "PreparedOutput",
    "PublicShareDownload",
    "ShareRecord",
    "PublicShareInfoView",
    "PublicShareEntryView",
    "ShareCreatedView",
    "ShareEntryBreadcrumb",
    "ShareView",
]


@dataclass(frozen=True, slots=True)
class ShareView:
    id: int
    code: str
    file_name: str
    file_size: int
    file_kind: str
    status: ShareStatus
    expires_at_ms: int | None
    max_downloads: int | None
    download_count: int
    created_at_ms: int
    has_password: bool


@dataclass(frozen=True, slots=True)
class ShareCreatedView:
    code: str
    view: ShareView


@dataclass(frozen=True, slots=True)
class PublicShareInfoView:
    file_name: str
    file_size: int
    file_kind: str
    has_password: bool
    expires_at_ms: int | None
    max_downloads: int | None
    download_count: int


@dataclass(frozen=True, slots=True)
class PublicShareEntryView:
    name: str
    path: str
    kind: ContentKind
    size: int


@dataclass(frozen=True, slots=True)
class ShareEntryBreadcrumb:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class PublicShareDownload:
    """Resolved public download: decision plus a leased target ready for streaming."""

    decision: DownloadDecision
    content_id: int
    storage_path: str
    entry_relative_path: str | None
    physical_path: Path
    display_name: str
    total_size: int
