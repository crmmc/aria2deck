export type User = {
  id: number;
  username: string;
  is_admin: boolean;
  quota: number;
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

export type Task = {
  id: number;
  owner_id: number;
  gid?: string | null;
  uri: string;
  status: string;
  name?: string | null;
  total_length: number;
  completed_length: number;
  download_speed: number;
  upload_speed: number;
  error?: string | null;
  created_at: string;
  updated_at: string;
  artifact_path?: string | null;
  artifact_token?: string | null;
};

export type TaskFile = {
  index: number;
  path: string;
  length: number;
  completed_length: number;
  selected: boolean;
  uris: string[];
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
};

export type FileInfo = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified_at: number;
};

export type FileListResponse = {
  current_path: string;
  parent_path: string | null;
  files: FileInfo[];
};

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
