/** 契约类型：与 backend/v2/src/aria2deck/api/contracts.py 逐字段对齐的核心子集。 */

export interface ApiError {
  code: string;
  message: string;
}

export interface OkResponse {
  ok: boolean;
}

export type AccountStatus = "active" | "frozen" | "locked";

export interface UserResponse {
  id: number;
  username: string;
  status: AccountStatus;
  is_admin: boolean;
  quota_bytes: number;
  is_initial_password: boolean;
  pending_delete: boolean;
  used_bytes: number;
  reserved_bytes: number;
  available_bytes: number;
}

export interface UserPasswordResetResponse {
  user: UserResponse;
  temporary_password: string;
}

export type TaskStatus =
  | "queued"
  | "active"
  | "waiting"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";

export type HistoryStatus = TaskStatus;

export interface TaskFileResponse {
  name: string;
  total_bytes: number;
  completed_bytes: number;
  selected: boolean;
}

export interface TaskResponse {
  id: number;
  status: TaskStatus;
  status_label: string;
  name: string;
  uri: string | null;
  total_bytes: number | null;
  completed_bytes: number;
  download_speed: number;
  upload_speed: number;
  peer_count: number;
  connection_count: number;
  files: TaskFileResponse[];
  observation_fresh: boolean;
  reserved_bytes: number;
  error_code: string | null;
  /** error_code 对应的中文原因（后端同源映射）；无错误时为空串。 */
  error_message: string;
  retryable: boolean;
  created_at_ms: number;
  updated_at_ms: number;
  finished_at_ms: number | null;
}

export interface TaskListResponse {
  items: TaskResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface TaskStatusCountsResponse {
  queued: number;
  active: number;
  waiting: number;
  paused: number;
  completed: number;
  failed: number;
  cancelled: number;
  expired: number;
}

export interface TaskQuotaResponse {
  quota_bytes: number;
  used_bytes: number;
  reserved_bytes: number;
  available_bytes: number;
  limited: boolean;
}

export interface TaskStatsResponse {
  counts: TaskStatusCountsResponse;
  total: number;
  download_speed: number;
  upload_speed: number;
  quota: TaskQuotaResponse;
  disk: DiskSpaceResponse;
}

/** 用户可见磁盘口径（STAT-01）：与后端 DiskSpaceResponse 逐字段一致。 */
export interface DiskSpaceResponse {
  total_bytes: number;
  used_bytes: number;
  reserved_bytes: number;
  available_bytes: number;
  limited: boolean;
}

export interface TaskWriteResultResponse {
  accepted: boolean;
  task: TaskResponse | null;
  outcome: string | null;
  error: ApiError | null;
  stop_confirmed: boolean | null;
}

export interface TaskBatchResponse {
  items: TaskWriteResultResponse[];
  accepted_count: number;
  rejected_count: number;
}

export interface TorrentFilePreviewResponse {
  index: number;
  path: string;
  size: number;
}

export interface TorrentPreviewResponse {
  info_hash: string;
  name: string;
  files: TorrentFilePreviewResponse[];
  total_size: number;
  default_selection: number[];
}

export interface BackendStatusResponse {
  status: "ok" | "degraded";
  message: string;
}

/** 统一分页参数（§3.1：页码从 1 开始）。 */
/* ============ 文件库（FILE-02~08） ============ */

export type FileKind = "file" | "directory";

export interface FileItemResponse {
  id: number;
  display_name: string;
  kind: FileKind;
  size: number;
  created_at_ms: number;
}

export interface FileListResponse {
  items: FileItemResponse[];
  total: number;
  page: number;
  page_size: number;
  space: TaskQuotaResponse;
}

export interface FileEntryItemResponse {
  name: string;
  path: string;
  kind: FileKind;
  size: number;
}

export interface FileEntryListResponse {
  items: FileEntryItemResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface FileSearchHitResponse {
  user_file_id: number;
  display_name: string;
  path: string;
  kind: FileKind;
  size: number;
}

export interface FileSearchResponse {
  hits: FileSearchHitResponse[];
  truncated: boolean;
}

/** FILE-06：真正的批量删除端点（DELETE /api/v2/files，body 为 file_ids）。 */
export interface FileBatchDeleteResultResponse {
  file_id: number;
  accepted: boolean;
  error: ApiError | null;
}

export interface FileBatchDeleteResponse {
  accepted_count: number;
  rejected_count: number;
  results: FileBatchDeleteResultResponse[];
}

/* ============ 打包（PACK-01~04） ============ */

export interface PackPrecomputeItemResponse {
  user_file_id: number;
  accepted: boolean;
  display_name: string | null;
  size: number;
  error: ApiError | null;
}

export interface PackPrecomputeResponse {
  items: PackPrecomputeItemResponse[];
  total_size: number;
  available_bytes: number;
}

export type PackStatus = "pending" | "packing" | "done" | "failed" | "cancelled";

export interface PackViewResponse {
  id: number;
  status: PackStatus;
  step: "validating" | "compressing" | "verifying" | null;
  step_progress: number;
  step_started_at_ms: number | null;
  created_at_ms: number;
  updated_at_ms: number;
  finished_at_ms: number | null;
  error_code: string | null;
  /** error_code 对应的中文原因（后端同源映射）；无错误时为空串。 */
  error_message: string;
  delete_source: boolean;
  output_name: string;
  output: { user_file_id: number; display_name: string; size: number } | null;
}

export interface PackListResponse {
  items: PackViewResponse[];
}

/** PACK A3：清空本人全部终态打包记录（保留输出文件）。 */
export interface PackClearRecordsResponse {
  cleared_count: number;
}

export interface MachineStatsResponse {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  download_used_bytes: number;
  system_used_bytes: number;
}

/* ============ 分享管理（SHARE-01~03） ============ */

export type ShareItemStatus = "active" | "expired" | "revoked" | "exhausted" | "unavailable";

export interface ShareCreateOptions {
  expires_in_seconds?: number | null;
  max_downloads?: number | null;
  password?: string | null;
}

export interface ShareItemResponse {
  id: number;
  code: string;
  file_name: string;
  file_size: number;
  file_kind: string;
  status: ShareItemStatus;
  expires_at_ms: number | null;
  max_downloads: number | null;
  download_count: number;
  created_at_ms: number;
  has_password: boolean;
}

export interface ShareListResponse {
  items: ShareItemResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface ShareBatchItemResponse {
  share_id: number;
  ok: boolean;
  already_revoked: boolean;
  error: ApiError | null;
}

export interface ShareBatchResponse {
  results: ShareBatchItemResponse[];
  accepted_count: number;
  rejected_count: number;
}

export interface ShareRevokeAllResponse {
  revoked_count: number;
}

/* ============ 公开分享访问（SHARE-04~08，/api/s） ============ */

export interface PublicShareInfoResponse {
  file_name: string;
  file_size: number;
  file_kind: string;
  has_password: boolean;
  expires_at_ms: number | null;
  max_downloads: number | null;
  download_count: number;
}

export interface ShareVerifyResponse {
  access_token: string;
}

export interface PublicEntryListResponse {
  items: FileEntryItemResponse[];
  total: number;
  page: number;
  page_size: number;
}

/* ============ 账户与凭证（ACC-04~11） ============ */

export interface UserListResponse {
  items: UserResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface DeleteUserResponse {
  ok: boolean;
  state: "pending";
  accepted: boolean;
}

export interface RpcAccessResponse {
  enabled: boolean;
  secret: string | null;
}

export interface CredentialInvalidateResponse {
  ok: boolean;
  api_token_count: number;
  rpc_secret_count: number;
}

/* ============ 系统设置（CFG-02~04） ============ */

export type PackFormat = "zip" | "tar.zst";

export interface SettingsResponse {
  site_title: string;
  max_task_size: number;
  min_free_disk: number;
  aria2_rpc_url: string;
  aria2_rpc_secret: string | null;
  aria2_bt_stop_timeout_seconds: number;
  pack_format: PackFormat;
  pack_compression_level: number;
  ws_reconnect_max_delay_seconds: number;
  ws_reconnect_jitter_seconds: number;
  ws_reconnect_factor: number;
  history_retention_days: number;
  tracker_fixed_list: string[];
  tracker_remote_urls: string[];
  tracker_refresh_interval_minutes: number;
  download_connection_limits: Record<string, number>;
  account_security_rate_limit: number;
  authenticated_api_rate_limit: number;
  public_api_rate_limit: number;
  share_access_rate_limit: number;
  create_task_rate_limit: number;
  create_torrent_rate_limit: number;
  create_pack_rate_limit: number;
  aria2_test_rate_limit: number;
  rpc_rate_limit: number;
  file_search_rate_limit: number;
  create_share_rate_limit: number;
  updated_at_ms: number;
}

export type SettingsUpdateRequest = Partial<
  Omit<SettingsResponse, "updated_at_ms" | "aria2_rpc_secret">
> & { aria2_rpc_secret?: string | null };

/** CFG-01 公开站点信息：重连策略为客户端行为调优参数，可公开。 */
export interface PublicSiteInfoResponse {
  site_title: string;
  ws_reconnect_max_delay_seconds: number;
  ws_reconnect_jitter_seconds: number;
  ws_reconnect_factor: number;
}

export interface TrackerStatusResponse {
  items: string[];
  entry_count: number;
  refresh_status: string;
  failed_urls: string[];
  updated_at_ms: number | null;
  last_refresh_at_ms: number | null;
}

/* ============ 存储管理（STORE-01~03） ============ */

export interface StorageFileResponse {
  content_id: number;
  content_key: string;
  kind: FileKind;
  size: number;
  on_disk: boolean;
  reference_count: number;
  deletion_state: "active" | "pending_delete" | "tombstoned";
  created_at_ms: number;
  file_names: string[];
}

export interface StorageFileListResponse {
  items: StorageFileResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface StorageReferencingUserResponse {
  user_id: number;
  username: string;
  display_name: string;
  referenced_at_ms: number;
}

export interface StorageReferencingUserListResponse {
  items: StorageReferencingUserResponse[];
}

export interface StorageDeleteResultResponse {
  content_id: number;
  accepted: boolean;
  error: ApiError | null;
}

export interface StorageDeleteResponse {
  accepted_count: number;
  rejected_count: number;
  results: StorageDeleteResultResponse[];
}


/* ============ 磁盘未登记文件扫描与入库队列 ============ */

export interface DiskOrphanItemResponse {
  storage_path: string;
  kind: FileKind;
  digest: string;
  size: number;
  modified_at_ms: number;
}

export interface DiskOrphanListResponse {
  items: DiskOrphanItemResponse[];
  total: number;
  page: number;
  page_size: number;
  unrecognized_count: number;
}

export type QueueState = "queued" | "running" | "done" | "failed";

export interface QueueItemResponse {
  storage_path: string;
  kind: FileKind;
  digest: string;
  size: number;
  state: QueueState;
  enqueued_at_ms: number;
  started_at_ms: number | null;
  finished_at_ms: number | null;
  processed_bytes: number;
  content_id: number | null;
  error: ApiError | null;
}

export interface QueueSnapshotResponse {
  running: boolean;
  items: QueueItemResponse[];
  queued_count: number;
  running_count: number;
  done_count: number;
  failed_count: number;
  pending_bytes: number;
}

export interface QueueEnqueueResultResponse {
  storage_path: string;
  accepted: boolean;
  state: QueueState | null;
  error: ApiError | null;
}

export type QueueDeleteMode = "immediate" | "deferred";

export interface QueueDeleteResultResponse {
  storage_path: string;
  accepted: boolean;
  mode: QueueDeleteMode | null;
  freed_bytes: number;
  error: ApiError | null;
}
