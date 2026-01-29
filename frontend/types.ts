export type User = {
  id: number;
  username: string;
  is_admin: boolean;
  quota: number;
  is_default_password?: boolean;  // deprecated
  is_initial_password?: boolean;  // whether user needs to reset password
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

// Task subscription (user's view of a shared download task)
export type Task = {
  id: number;  // subscription ID
  name?: string | null;
  uri?: string | null;  // 原始 URI，用于复制
  status: string;  // effective status: queued, active, complete, error
  total_length: number;
  completed_length: number;
  download_speed: number;
  upload_speed: number;
  frozen_space: number;  // space frozen for this download
  error?: string | null;
  created_at: string;
};

export type SystemStats = {
  download_speed: number;
  upload_speed: number;
  active_task_count: number;
  disk_used_space: number;
  disk_total_space: number;
  disk_space_limited: boolean;
};

export type SystemConfig = {
  max_task_size: number;
  min_free_disk: number;
  aria2_rpc_url: string;
  aria2_rpc_secret: string;
  hidden_file_extensions: string[];
  pack_format: "zip" | "7z";
  pack_compression_level: number;
  pack_extra_args: string;
  ws_reconnect_max_delay: number;
  ws_reconnect_jitter: number;
  ws_reconnect_factor: number;
  download_token_expiry: number;
};

// User file reference (user's view of a stored file)
export type FileInfo = {
  id: number;  // UserFile ID
  name: string;  // display_name
  size: number;
  is_directory: boolean;
  created_at: string;
};

export type FileListResponse = {
  files: FileInfo[];
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

// Legacy quota response (for backward compatibility)
export type QuotaResponse = {
  used: number;
  total: number;
  percentage: number;
};

export type MachineStats = {
  disk_total: number;
  disk_used: number;
  disk_free: number;
};

export type PackTask = {
  id: number;
  owner_id: number;
  folder_path: string;
  folder_size: number;
  reserved_space: number;
  output_path: string | null;
  output_size: number | null;
  status: "pending" | "packing" | "done" | "failed" | "cancelled";
  progress: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type PackAvailableSpace = {
  user_available: number;
  server_available: number;
  folder_size?: number;
};

export interface RpcAccessStatus {
  enabled: boolean;
  secret: string | null;
  created_at: string | null;
}
