from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from ...domain.content import ContentEntryIdentity, ContentIdentity
from ...domain.downloads import AttemptStatus, RegistrationOutcome, SubscriptionStatus
from ...domain.quota import Quota, VisibleSpace


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    id: int
    attempt_id: int
    gid: str
    subscription_status: SubscriptionStatus
    attempt_status: AttemptStatus
    name: str
    display_uri: str | None
    total_bytes: int | None
    completed_bytes: int
    reserved_bytes: int
    error_code: str | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    ingestion_pending: bool = False


@dataclass(frozen=True, slots=True)
class ObservedFile:
    name: str
    total_bytes: int
    completed_bytes: int
    selected: bool


@dataclass(frozen=True, slots=True)
class DownloadObservation:
    gid: str
    observed_at_ms: int
    status: str
    download_speed: int
    upload_speed: int
    peer_count: int
    connection_count: int
    files: tuple[ObservedFile, ...] = ()
    total_length: int | None = None
    completed_length: int = 0
    followed_by: tuple[str, ...] = ()
    following: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskProjection:
    id: int
    status: SubscriptionStatus
    status_label: str
    name: str
    display_uri: str | None
    total_bytes: int | None
    completed_bytes: int
    download_speed: int
    upload_speed: int
    peer_count: int
    connection_count: int
    files: tuple[ObservedFile, ...]
    observation_fresh: bool
    reserved_bytes: int
    error_code: str | None
    retryable: bool
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: list[TaskProjection]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class TaskStats:
    counts: dict[SubscriptionStatus, int]
    total: int
    download_speed: int
    upload_speed: int
    quota: Quota
    disk: VisibleSpace


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: int
    source_id: int
    resource_key: str
    gid: str
    status: AttemptStatus
    completed_content_id: int | None
    submission_state: str
    total_length: int | None
    completed_length: int
    physical_reserved_bytes: int
    result_consumed_at_ms: int | None = None
    error_code: str | None = None
    created_at_ms: int = 0
    ingestion_started_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ContentPreparation:
    identity: ContentIdentity
    size: int
    storage_path: str
    entries: tuple[ContentEntryIdentity, ...]


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: int
    kind: str
    resource_key: str
    payload: Mapping[str, object]
    options: Mapping[str, object]
    selected_file_indexes: tuple[int, ...] | None
    display_name: str
    display_uri: str | None
    detached_at_ms: int | None


@dataclass(frozen=True, slots=True)
class UriDownloadInput:
    uri: str
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreparedDownload:
    kind: str
    resource_key: str
    payload: Mapping[str, object]
    display_name: str
    options: Mapping[str, object] = field(default_factory=dict)
    selected_file_indexes: tuple[int, ...] | None = None
    display_uri: str | None = None
    total_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class Submission:
    attempt_id: int
    gid: str
    source: SourceRecord
    created_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class BackendSubmissionResult:
    accepted: bool
    gid: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    task_id: int
    attempt_id: int
    gid: str
    outcome: RegistrationOutcome
    should_submit: bool


@dataclass(frozen=True, slots=True)
class DownloadWriteResult:
    accepted: bool
    task_id: int | None = None
    attempt_id: int | None = None
    outcome: RegistrationOutcome | None = None
    error_code: str | None = None
    message: str | None = None
    stop_confirmed: bool | None = None


@dataclass(frozen=True, slots=True)
class CancellationResult:
    task_id: int
    attempt_id: int
    gid: str
    stop_attempt: bool
    submission_state: str


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status: int
    headers: Mapping[str, str]
    body: AsyncIterator[bytes]
    total_bytes: int | None
    body_bytes: int | None
    close: Callable[[], Awaitable[None]]
    content_offset: int = 0


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    status: int
    headers: Mapping[str, str]
    body: AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class HistoryExpiration:
    expired_tasks: int
    detached_sources: int
