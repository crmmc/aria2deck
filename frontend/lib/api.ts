import type {
  Task,
  User,
  UserCreate,
  UserUpdate,
  SystemStats,
  SystemConfig,
  FileListResponse,
  BrowseFileInfo,
  MachineStats,
  PackTask,
  RpcAccessStatus,
  TaskHistory,
  StoredFileListResponse,
  FileUsersResponse,
  BulkDeleteResponse,
  ShareLink,
  ShareInfo,
  CreateShareRequest,
  TorrentPreview,
  UploadTorrentRequest,
} from "@/types";
import { hashPassword } from "./crypto";

function getApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_BASE) {
    return process.env.NEXT_PUBLIC_API_BASE;
  }
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  return "";
}

type QueryValue = string | number | boolean | null | undefined;

function buildQuery(params: Record<string, QueryValue>): string {
  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      searchParams.set(key, String(value));
    }
  });
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}

function withQuery(path: string, params: Record<string, QueryValue>): string {
  return `${path}${buildQuery(params)}`;
}

function downloadUrl(path: string, params: Record<string, QueryValue> = {}): string {
  return `${getApiBase()}${withQuery(path, params)}`;
}

const removePackTask = (id: number) =>
  request<{ ok: boolean; message: string }>(`/api/files/pack/${id}`, {
    method: "DELETE",
  });

// 401 错误事件，用于通知 AuthContext 会话过期
export const authEvents = {
  listeners: new Set<() => void>(),
  onUnauthorized(callback: () => void): () => void {
    this.listeners.add(callback);
    return () => {
      this.listeners.delete(callback);
    };
  },
  emit() {
    this.listeners.forEach((cb) => cb());
  },
};

// 自定义错误类，用于区分错误类型
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public isUnauthorized: boolean = false,
    public isNetworkError: boolean = false
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const base = getApiBase();
  let res: Response;
  try {
    res = await fetch(`${base}${path}`, {
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(options?.headers || {}),
      },
      ...options,
    });
  } catch (err) {
    // 网络错误（无法连接服务器）
    const cause = err instanceof Error ? err.message : String(err);
    throw new ApiError(
      `网络连接失败: ${cause}`,
      0,
      false,
      true
    );
  }

  if (!res.ok) {
    // 401 错误：会话过期，触发重新登录
    if (res.status === 401) {
      authEvents.emit();
      throw new ApiError("会话已过期，请重新登录", 401, true);
    }
    const text = await res.text();
    let message = text || `请求失败: ${res.status}`;
    if (text) {
      try {
        const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown };
        if (typeof parsed.detail === "string" && parsed.detail.trim()) {
          message = parsed.detail;
        } else if (typeof parsed.message === "string" && parsed.message.trim()) {
          message = parsed.message;
        }
      } catch {
        // Keep raw text message when response is not JSON
      }
    }
    throw new ApiError(message, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  login: async (username: string, password: string) => {
    const clientHash = await hashPassword(password, username);
    return request<User>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password: clientHash }),
    });
  },
  logout: () =>
    request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),
  changePassword: async (oldPassword: string, newPassword: string, username: string) => {
    const [oldHash, newHash] = await Promise.all([
      hashPassword(oldPassword, username),
      hashPassword(newPassword, username),
    ]);
    return request<{ ok: boolean; message: string }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldHash, new_password: newHash }),
    });
  },

  // Tasks (subscription-based)
  listTasks: (statusFilter?: string) =>
    request<Task[]>(withQuery("/api/tasks", { status_filter: statusFilter })),
  createTask: (uri: string) =>
    request<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ uri }),
    }),
  previewTorrent: (torrent: string) =>
    request<TorrentPreview>("/api/tasks/torrent/preview", {
      method: "POST",
      body: JSON.stringify({ torrent }),
    }),
  uploadTorrent: (torrent: string, payload?: UploadTorrentRequest) =>
    request<Task>("/api/tasks/torrent", {
      method: "POST",
      body: JSON.stringify({
        torrent,
        ...(payload?.selected_file_indexes
          ? { selected_file_indexes: payload.selected_file_indexes }
          : {}),
        ...(payload?.options ? { options: payload.options } : {}),
      }),
    }),
  cancelTask: (subscriptionId: number) =>
    request<{ ok: boolean }>(`/api/tasks/${subscriptionId}`, {
      method: "DELETE",
    }),

  // Task History (independent storage)
  listHistory: () => request<TaskHistory[]>("/api/history"),
  deleteHistory: (historyId: number) =>
    request<{ ok: boolean }>(`/api/history/${historyId}`, {
      method: "DELETE",
    }),
  clearHistory: () =>
    request<{ ok: boolean; count: number }>("/api/history", {
      method: "DELETE",
    }),

  // Stats & Config
  getStats: () => request<SystemStats>("/api/stats"),
  getMachineStats: () => request<MachineStats>("/api/stats/machine"),
  getConfig: () => request<SystemConfig>("/api/config"),
  updateConfig: (config: Partial<SystemConfig>) =>
    request<SystemConfig>("/api/config", {
      method: "PUT",
      body: JSON.stringify(config),
    }),
  getAria2Version: () =>
    request<{
      connected: boolean;
      version?: string;
      enabled_features?: string[];
      error?: string;
    }>("/api/config/aria2/version"),
  testAria2Connection: (aria2_rpc_url: string, aria2_rpc_secret?: string) =>
    request<{
      connected: boolean;
      version?: string;
      enabled_features?: string[];
      error?: string;
    }>("/api/config/aria2/test", {
      method: "POST",
      body: JSON.stringify({ aria2_rpc_url, aria2_rpc_secret }),
    }),

  // RPC Access
  getRpcAccess: () => request<RpcAccessStatus>("/api/users/me/rpc-access"),
  setRpcAccess: (enabled: boolean) =>
    request<RpcAccessStatus>("/api/users/me/rpc-access", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  refreshRpcSecret: () =>
    request<RpcAccessStatus>("/api/users/me/rpc-access/refresh", {
      method: "POST",
    }),

  // Users (Admin)
  listUsers: () => request<User[]>("/api/users"),
  createUser: async (data: UserCreate) => {
    const clientHash = await hashPassword(data.password, data.username);
    return request<User>("/api/users", {
      method: "POST",
      body: JSON.stringify({ ...data, password: clientHash }),
    });
  },
  updateUser: async (id: number, data: UserUpdate, username: string) => {
    const payload = { ...data };
    if (data.password) {
      // 使用目标用户的用户名（可能已修改）
      const targetUsername = data.username || username;
      payload.password = await hashPassword(data.password, targetUsername);
    }
    return request<User>(`/api/users/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  deleteUser: (id: number) =>
    request<{ ok: boolean }>(`/api/users/${id}`, { method: "DELETE" }),

  // Files (UserFile-based)
  listFiles: (page = 1, pageSize = 10) =>
    request<FileListResponse>(withQuery("/api/files", { page, page_size: pageSize })),
  browseFile: (fileHash: string, path?: string) =>
    request<BrowseFileInfo[]>(
      withQuery(`/api/files/${fileHash}/browse`, { path })
    ),
  downloadFileUrl: (fileHash: string, path?: string) =>
    downloadUrl(`/api/files/${fileHash}/download`, { path }),
  deleteFile: (fileHash: string) =>
    request<{ ok: boolean }>(`/api/files/${fileHash}`, {
      method: "DELETE",
    }),
  renameFile: (fileHash: string, newName: string) =>
    request<{ ok: boolean }>(`/api/files/${fileHash}/rename`, {
      method: "PUT",
      body: JSON.stringify({ name: newName }),
    }),

  // Pack Tasks
  listPackTasks: () => request<PackTask[]>("/api/files/pack"),

  createPackTask: (fileIds: number[], outputName?: string, deleteSource: boolean = false) =>
    request<PackTask>("/api/files/pack", {
      method: "POST",
      body: JSON.stringify({ file_ids: fileIds, output_name: outputName, delete_source: deleteSource }),
    }),

  calculatePackSize: (fileIds: number[]) =>
    request<{ total_size: number }>("/api/files/pack/calculate-size", {
      method: "POST",
      body: JSON.stringify({ file_ids: fileIds }),
    }),

  getAvailableSpace: () =>
    request<{ available: number; quota: number; used: number }>(
      "/api/files/pack/available-space"
    ),

  cancelPackTask: (id: number) =>
    removePackTask(id),

  // 删除打包任务记录（与 cancelPackTask 共用后端端点）
  deletePackTask: (id: number) =>
    removePackTask(id),

  clearPackTasks: () =>
    request<{ ok: boolean; count: number }>("/api/files/pack", {
      method: "DELETE",
    }),

  listStoredFiles: (page = 1, pageSize = 20, search?: string, orphanOnly?: boolean) =>
    request<StoredFileListResponse>(
      withQuery("/api/admin/storage/files", {
        page,
        page_size: pageSize,
        search,
        orphan_only: orphanOnly || undefined,
      })
    ),

  getFileUsers: (fileId: number) =>
    request<FileUsersResponse>(`/api/admin/storage/files/${fileId}/users`),

  bulkDeleteStoredFiles: (fileIds: number[]) =>
    request<BulkDeleteResponse>("/api/admin/storage/files", {
      method: "DELETE",
      body: JSON.stringify({ file_ids: fileIds }),
    }),
  // Shares
  createShare: (data: CreateShareRequest) =>
    request<ShareLink>("/api/shares", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  listShares: () => request<ShareLink[]>("/api/shares"),
  revokeShare: (shareId: number) =>
    request<{ ok: boolean }>(`/api/shares/${shareId}/revoke`, {
      method: "PUT",
    }),
  deleteShare: (shareId: number) =>
    request<{ ok: boolean }>(`/api/shares/${shareId}`, {
      method: "DELETE",
    }),
  revokeAllShares: () =>
    request<{ ok: boolean; count: number }>("/api/shares/revoke-all", {
      method: "PUT",
    }),
  // Public share (no auth)
  getShareInfo: (code: string) =>
    request<ShareInfo>(`/api/s/${code}`),
  accessShare: (code: string, password: string) =>
    request<{ access_token: string }>(`/api/s/${code}/access`, {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  shareDownloadUrl: (code: string, token?: string, subpath?: string) => {
    return downloadUrl(`/api/s/${code}/download`, { token, subpath });
  },
  browseShare: (code: string, token?: string, subpath?: string) => {
    return request<Array<{ name: string; is_dir: boolean; size: number; path: string }>>(
      withQuery(`/api/s/${code}/browse`, { token, subpath })
    );
  },
  // 公开 API（无需认证）
  getSiteInfo: () => request<{ site_title: string }>("/api/config/public/site-info"),
};

export function taskWsUrl(): string {
  const base = getApiBase();
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/tasks";
  return url.toString();
}
