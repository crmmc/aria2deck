from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.downloads import SubscriptionStatus


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class OkResponse(BaseModel):
    ok: bool = True


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_password: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    new_password: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    is_admin: bool = False
    is_initial_password: bool = False
    quota_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    is_admin: bool | None = None
    is_initial_password: bool | None = None
    quota_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)


class UserResponse(BaseModel):
    id: int
    username: str
    is_admin: bool
    quota_bytes: int
    is_initial_password: bool
    pending_delete: bool
    used_bytes: int
    reserved_bytes: int
    available_bytes: int


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    page: int
    page_size: int


class DeleteUserResponse(BaseModel):
    ok: bool = True
    state: Literal["pending"] = "pending"
    accepted: bool = True


class ApiTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="默认 Token", max_length=100)


class ApiTokenResponse(BaseModel):
    id: int
    name: str
    prefix: str
    created_at_ms: int
    last_used_at_ms: int | None


class CreatedApiTokenResponse(ApiTokenResponse):
    token: str


class ApiTokenListResponse(BaseModel):
    items: list[ApiTokenResponse]
    total: int
    page: int
    page_size: int


class RpcAccessUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class RpcAccessResponse(BaseModel):
    enabled: bool
    secret_prefix: str | None
    secret: str | None


class CredentialInvalidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


class CredentialInvalidateResponse(BaseModel):
    ok: bool = True
    api_token_count: int
    rpc_secret_count: int



class PublicSiteInfoResponse(BaseModel):
    site_title: str


class SettingsResponse(BaseModel):
    site_title: str
    max_task_size: int
    min_free_disk: int
    aria2_rpc_url: str
    aria2_rpc_secret: str | None = Field(description="仅返回掩码，不返回密钥原文")
    aria2_bt_stop_timeout_seconds: int
    hidden_file_extensions: list[str]
    pack_format: Literal["zip", "tar.zst"]
    pack_compression_level: int
    ws_reconnect_max_delay_seconds: int
    ws_reconnect_jitter_seconds: int
    ws_reconnect_factor: int
    history_retention_days: int
    tracker_fixed_list: list[str]
    tracker_remote_urls: list[str]
    tracker_refresh_interval_minutes: int
    download_connection_limits: dict[str, int]
    account_security_rate_limit: int
    authenticated_api_rate_limit: int
    public_api_rate_limit: int
    share_access_rate_limit: int
    create_task_rate_limit: int
    create_torrent_rate_limit: int
    create_pack_rate_limit: int
    aria2_test_rate_limit: int
    rpc_rate_limit: int
    file_search_rate_limit: int
    create_share_rate_limit: int
    updated_at_ms: int


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_title: str | None = Field(default=None, max_length=100)
    max_task_size: int | None = Field(default=None, ge=0, le=2**63 - 1)
    min_free_disk: int | None = Field(default=None, ge=0, le=2**63 - 1)
    aria2_rpc_url: str | None = Field(default=None, max_length=2048)
    aria2_rpc_secret: str | None = Field(default=None, max_length=4096)
    aria2_bt_stop_timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    hidden_file_extensions: list[str] | None = None
    pack_format: Literal["zip", "tar.zst"] | None = None
    pack_compression_level: int | None = Field(default=None, ge=0, le=22)
    ws_reconnect_max_delay_seconds: int | None = Field(default=None, ge=1, le=300)
    ws_reconnect_jitter_seconds: int | None = Field(default=None, ge=0, le=30)
    ws_reconnect_factor: int | None = Field(default=None, ge=1, le=10)
    history_retention_days: int | None = Field(default=None, ge=1, le=3650)
    tracker_fixed_list: list[str] | None = None
    tracker_remote_urls: list[str] | None = None
    tracker_refresh_interval_minutes: int | None = Field(default=None, ge=0, le=10080)
    download_connection_limits: dict[str, int] | None = None
    account_security_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    authenticated_api_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    public_api_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    share_access_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    create_task_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    create_torrent_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    create_pack_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    aria2_test_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    rpc_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    file_search_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    create_share_rate_limit: int | None = Field(default=None, ge=1, le=1_000_000)


class BackendStatusResponse(BaseModel):
    status: Literal["ok", "degraded"]
    message: str


class SystemStatusResponse(BaseModel):
    download_backend: BackendStatusResponse


class TaskFileResponse(BaseModel):
    name: str
    total_bytes: int
    completed_bytes: int
    selected: bool


class TaskResponse(BaseModel):
    id: int
    status: SubscriptionStatus
    status_label: str
    name: str
    uri: str | None
    total_bytes: int | None
    completed_bytes: int
    download_speed: int
    upload_speed: int
    peer_count: int
    connection_count: int
    files: list[TaskFileResponse]
    observation_fresh: bool
    reserved_bytes: int
    error_code: str | None
    retryable: bool
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int
    page: int
    page_size: int


class TaskStatusCountsResponse(BaseModel):
    queued: int
    active: int
    waiting: int
    paused: int
    completed: int
    failed: int
    cancelled: int
    expired: int


class TaskQuotaResponse(BaseModel):
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int
    available_bytes: int


class TaskStatsResponse(BaseModel):
    counts: TaskStatusCountsResponse
    total: int
    download_speed: int
    upload_speed: int
    quota: TaskQuotaResponse


class DownloadOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headers: dict[str, str] = Field(default_factory=dict)
    mirrors: list[str] = Field(default_factory=list, max_length=10)


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str = Field(min_length=1, max_length=8192)
    options: DownloadOptionsRequest = Field(default_factory=DownloadOptionsRequest)


class TaskBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[TaskCreateRequest] = Field(min_length=1, max_length=30)


class TaskWriteResultResponse(BaseModel):
    accepted: bool
    task: TaskResponse | None = None
    outcome: str | None = None
    error: ErrorBody | None = None
    stop_confirmed: bool | None = None


class TaskBatchResponse(BaseModel):
    items: list[TaskWriteResultResponse]
    accepted_count: int
    rejected_count: int


class TaskActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[int] = Field(min_length=1, max_length=1000)


class TorrentPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    torrent: str = Field(min_length=1, max_length=14_000_000)


class TorrentCreateRequest(TorrentPreviewRequest):
    selected_file_indexes: list[int] | None = Field(default=None, min_length=1)
    model_config = ConfigDict(extra="forbid")


class TorrentFilePreviewResponse(BaseModel):
    index: int
    path: str
    size: int


class TorrentPreviewResponse(BaseModel):
    info_hash: str
    name: str
    files: list[TorrentFilePreviewResponse]
    total_size: int
    default_selection: list[int]


class HistoryPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_ms: int | None = Field(default=None, ge=0)
    older_than_days: int | None = Field(default=None, ge=1, le=3650)


class HistoryPurgeResponse(BaseModel):
    expired_tasks: int
    detached_sources: int


class TrackerStatusResponse(BaseModel):
    items: list[str]
    entry_count: int
    remote_items: list[str]
    refresh_status: str
    failed_urls: list[str]
    updated_at_ms: int | None
    last_refresh_at_ms: int | None


class FileItemResponse(BaseModel):
    id: int
    display_name: str
    kind: Literal["file", "directory"]
    size: int
    created_at_ms: int


class FileListResponse(BaseModel):
    items: list[FileItemResponse]
    total: int
    page: int
    page_size: int
    space: TaskQuotaResponse


class FileEntryItemResponse(BaseModel):
    name: str
    path: str
    kind: Literal["file", "directory"]
    size: int


class DirectoryBreadcrumbResponse(BaseModel):
    name: str
    path: str


class FileEntryListResponse(BaseModel):
    items: list[FileEntryItemResponse]
    total: int
    page: int
    page_size: int
    breadcrumbs: list[DirectoryBreadcrumbResponse]


class FileSearchHitResponse(BaseModel):
    user_file_id: int
    display_name: str
    path: str
    kind: Literal["file", "directory"]
    size: int


class FileSearchResponse(BaseModel):
    hits: list[FileSearchHitResponse]
    truncated: bool


class FileRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class StorageFileResponse(BaseModel):
    content_id: int
    content_key: str
    kind: Literal["file", "directory"]
    size: int
    storage_path: str
    on_disk: bool
    reference_count: int
    deletion_state: Literal["active", "pending_delete", "tombstoned"]


class StorageFileListResponse(BaseModel):
    items: list[StorageFileResponse]
    total: int
    page: int
    page_size: int


class StorageReferencingUserResponse(BaseModel):
    user_id: int
    username: str
    display_name: str
    referenced_at_ms: int


class StorageReferencingUserListResponse(BaseModel):
    items: list[StorageReferencingUserResponse]


class StorageDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_ids: list[int] = Field(min_length=1, max_length=1000)


class StorageDeleteResultResponse(BaseModel):
    content_id: int
    accepted: bool
    error: ErrorBody | None = None


class StorageDeleteResponse(BaseModel):
    accepted_count: int
    rejected_count: int
    results: list[StorageDeleteResultResponse]
