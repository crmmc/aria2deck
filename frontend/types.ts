export type User = {
  id: number;
  username: string;
  is_admin: boolean;
  quota: number;

  is_initial_password?: boolean;  // whether user needs to reset password
  used_bytes?: number;
  reserved_bytes?: number;
  available_bytes?: number;
  usage_percent?: number;
  machine_share_percent?: number;
};

export type UserCreate = {
  username: string;
  password: string;
  is_admin?: boolean;
  quota?: number;
};

export type UserUpdate = {
  username?: string;
  password?: string;
  is_admin?: boolean;
  quota?: number;
};

export type TaskStatus =
  | "queued"
  | "active"
  | "waiting"
  | "paused"
  | "complete"
  | "error";

// Task subscription (user's view of a shared download task)
export type Task = {
  id: number;  // subscription ID
  name?: string | null;
  uri?: string | null;  // 原始 URI，用于复制
  status: TaskStatus;
  total_length: number;
  completed_length: number;
  download_speed: number;
  upload_speed: number;
  frozen_space: number;  // space frozen for this download
  error?: string | null;
  status_label?: string | null;  // backend projection label (user_visible_label)
  created_at: string;
  retryable?: boolean;
  retry_blocked_reason?: string | null;
};

export type TorrentFileNode = {
  type: "file";
  name: string;
  path: string[];
  size: number;
  index: number;
};

export type TorrentDirectoryNode = {
  type: "directory";
  name: string;
  path: string[];
  size: number;
  children: TorrentTreeNode[];
};

export type TorrentTreeNode = TorrentFileNode | TorrentDirectoryNode;

export type TorrentPreviewFile = {
  index: number;
  path: string[];
  size: number;
};

export type TorrentPreview = {
  info_hash: string;
  name: string;
  file_count: number;
  total_size: number;
  files: TorrentPreviewFile[];
  tree: TorrentTreeNode[];
  limits: {
    max_files: number;
  };
  default_selection: "all";
};

export type UploadTorrentRequest = {
  selected_file_indexes?: number[];
  options?: Record<string, unknown>;
};

export type SystemStatus = {
  download_backend: {
    status: "ok" | "degraded";
    message: string;
  };
};

export type SystemStats = {
  download_speed: number;
  upload_speed: number;
  active_task_count: number;
  disk_used_space: number;
  disk_frozen_space: number;
  disk_total_space: number;
  disk_space_limited: boolean;
};

export type SystemConfig = {
  max_task_size: number;
  min_free_disk: number;
  aria2_rpc_url: string;
  aria2_rpc_secret: string;
  aria2_bt_stop_timeout_seconds: number;
  hidden_file_extensions: string[];
  pack_format: "zip" | "tar.zst";
  pack_compression_level: number;
  ws_reconnect_max_delay: number;
  ws_reconnect_jitter: number;
  ws_reconnect_factor: number;
  site_title: string;
  rate_limit_account_security: number;
  rate_limit_authenticated_api: number;
  rate_limit_public_api: number;
  rate_limit_share_access: number;
  rate_limit_create_task: number;
  rate_limit_create_torrent: number;
  rate_limit_create_pack: number;
  rate_limit_aria2_test: number;
  rate_limit_rpc: number;
  rate_limit_file_search: number;
  download_total_connections: number;
  download_authenticated_reserved_connections: number;
  download_authenticated_per_user_connections: number;
  download_authenticated_per_file_connections: number;
  download_anonymous_base_connections: number;
  download_anonymous_borrow_connections: number;
  download_anonymous_per_ip_connections: number;
  download_anonymous_per_file_connections: number;
  history_retention_days: number;
  tracker_fixed_list: string;
  tracker_remote_urls: string;
  tracker_refresh_interval_minutes: number;
  tracker_status: TrackerStatus;
};

export type TrackerStatus = {
  entry_count: number;
  updated_at_ms: number | null;
  last_refresh_at_ms: number | null;
  last_refresh_status: "ok" | "partial" | "failed" | "never";
  last_refresh_failed_urls: string[];
};

// User file reference (user's view of a stored file)
export type FileInfo = {
  id: number;  // UserFile ID
  content_hash: string;  // Used for API URLs instead of id
  name: string;  // display_name
  size: number;
  is_directory: boolean;
  created_at: string;
};

export type FileListResponse = {
  files: FileInfo[];
  total: number;
  space: SpaceInfo;
};

export type SpaceInfo = {
  used: number;
  frozen: number;
  available: number;
};

// Browse file info (for BT directory contents)
export type BrowseFileInfo = {
  name: string;
  size: number;
  is_directory: boolean;
};

export type FileSearchItem = {
  user_file_id: number;
  content_hash: string;
  name: string;
  size: number;
  path: string;
  is_directory: boolean;
  entry_path: string | null;
  rank: 0 | 1 | 2;
  root_index: number;
};

export type FileSearchResponse = {
  items: FileSearchItem[];
  total: number;
  truncated: boolean;
};

export type MachineStats = {
  disk_total: number;
  disk_used: number;
  disk_free: number;
  download_used: number;
  system_used: number;
};

export type PackTask = {
  id: number;
  owner_id: number;
  folder_path: string;
  folder_size: number;
  reserved_space: number;
  output_path: string | null;
  output_name: string | null;
  output_size: number | null;
  stored_file_id: number | null;
  delete_source: boolean;
  status: "pending" | "packing" | "done" | "failed" | "cancelled";
  progress: number;
  step: "validating" | "compressing" | "verifying" | null;
  started_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export interface RpcAccessStatus {
  enabled: boolean;
  secret: string | null;
  created_at: string | null;
}

// Task history record (independent storage)
export type TaskHistory = {
  id: number;
  task_name: string;
  uri?: string | null;
  total_length: number;
  result: "completed" | "cancelled" | "failed";
  reason?: string | null;
  created_at: string;
  finished_at: string;
  retryable?: boolean;
  retry_blocked_reason?: string | null;
};

export type StoredFileInfo = {
  id: number;
  content_hash: string;
  original_name: string;
  size: number;
  is_directory: boolean;
  ref_count: number;
  created_at: string;
  real_path: string;
  exists_on_disk: boolean;
};

export type StoredFileListResponse = {
  files: StoredFileInfo[];
  total: number;
  page: number;
  page_size: number;
};

export type FileUserInfo = {
  user_id: number;
  username: string;
  display_name: string;
};

export type FileUsersResponse = {
  file_id: number;
  users: FileUserInfo[];
};

export type BatchDeleteFileResult = {
  content_hash: string;
  ok: boolean;
  state: string;
  accepted: boolean;
  error: string | null;
};

export type BatchDeleteFilesResponse = {
  accepted_count: number;
  failed_count: number;
  results: BatchDeleteFileResult[];
};

export type BatchDeleteShareResult = {
  share_id: number;
  ok: boolean;
  state: string;
  accepted: boolean;
  error: string | null;
};

export type BatchDeleteSharesResponse = {
  accepted_count: number;
  failed_count: number;
  results: BatchDeleteShareResult[];
};

export type BatchDeleteHistoryResult = {
  history_id: number;
  ok: boolean;
  state: string;
  accepted: boolean;
  error: string | null;
};

export type BatchDeleteHistoryResponse = {
  accepted_count: number;
  failed_count: number;
  results: BatchDeleteHistoryResult[];
};

export type BatchCancelTaskResult = {
  task_id: number;
  ok: boolean;
  state: string;
  accepted: boolean;
  error: string | null;
};

export type BatchCancelTasksResponse = {
  accepted_count: number;
  failed_count: number;
  results: BatchCancelTaskResult[];
};

export type BulkDeleteResponse = {
  deleted_count: number;
  failed_ids: number[];
  errors: string[];
};


// Share link (user's view)
export type ShareLink = {
  id: number;
  share_code: string;
  file_name: string;
  file_size: number;
  has_password: boolean;
  password: string | null;
  expires_at: string | null;
  max_downloads: number | null;
  download_count: number;
  status: "active" | "revoked";
  created_at: string;
  last_accessed_at: string | null;
};

// Public share info (no auth required)
export type ShareInfo = {
  file_name: string;
  file_size: number;
  is_directory: boolean;
  has_password: boolean;
  is_expired: boolean;
  is_exhausted: boolean;
};

export type CreateShareRequest = {
  user_file_id: number;
  password?: string;
  expires_in?: number;  // seconds
  max_downloads?: number;
};
