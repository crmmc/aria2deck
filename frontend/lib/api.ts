import type {
  Task,
  User,
  UserCreate,
  UserUpdate,
  SystemStats,
  SystemConfig,
  FileListResponse,
  BrowseFileInfo,
  SpaceInfo,
  QuotaResponse,
  MachineStats,
  PackTask,
  PackAvailableSpace,
  RpcAccessStatus,
} from "@/types";
import { hashPassword } from "./crypto";

function getApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_BASE) {
    return process.env.NEXT_PUBLIC_API_BASE;
  }
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  return "http://localhost:8000";
}

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

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const base = getApiBase();
  const res = await fetch(`${base}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    ...options,
  });
  if (!res.ok) {
    // 401 错误：会话过期，触发重新登录
    if (res.status === 401) {
      authEvents.emit();
      throw new Error("会话已过期，请重新登录");
    }
    const text = await res.text();
    throw new Error(text || `请求失败: ${res.status}`);
  }
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
    const oldHash = await hashPassword(oldPassword, username);
    const newHash = await hashPassword(newPassword, username);
    return request<{ ok: boolean; message: string }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldHash, new_password: newHash }),
    });
  },

  // Tasks (subscription-based)
  listTasks: (statusFilter?: string) =>
    request<Task[]>(`/api/tasks${statusFilter ? `?status_filter=${statusFilter}` : ""}`),
  createTask: (uri: string) =>
    request<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ uri }),
    }),
  uploadTorrent: (torrent: string, options?: Record<string, unknown>) =>
    request<Task>("/api/tasks/torrent", {
      method: "POST",
      body: JSON.stringify({ torrent, options }),
    }),
  cancelTask: (subscriptionId: number) =>
    request<{ ok: boolean }>(`/api/tasks/${subscriptionId}`, {
      method: "DELETE",
    }),
  clearHistory: () =>
    request<{ ok: boolean; count: number }>("/api/tasks", {
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
  getUser: (id: number) => request<User>(`/api/users/${id}`),
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
  deleteUser: (id: number, deleteFiles: boolean = false) =>
    request<{ ok: boolean }>(`/api/users/${id}?delete_files=${deleteFiles}`, { method: "DELETE" }),

  // Files (UserFile-based)
  listFiles: () => request<FileListResponse>("/api/files"),
  browseFile: (fileId: number, path?: string) =>
    request<BrowseFileInfo[]>(
      `/api/files/${fileId}/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`
    ),
  downloadFileUrl: (fileId: number, path?: string) => {
    const base = getApiBase();
    const pathParam = path ? `?path=${encodeURIComponent(path)}` : "";
    return `${base}/api/files/${fileId}/download${pathParam}`;
  },
  deleteFile: (fileId: number) =>
    request<{ ok: boolean }>(`/api/files/${fileId}`, {
      method: "DELETE",
    }),
  renameFile: (fileId: number, newName: string) =>
    request<{ ok: boolean }>(`/api/files/${fileId}/rename`, {
      method: "PUT",
      body: JSON.stringify({ name: newName }),
    }),
  getSpace: () => request<SpaceInfo>("/api/files/space"),
  getQuota: () => request<QuotaResponse>("/api/files/quota"),

  // Pack Tasks
  createPackTask: (folderPath: string, outputName?: string) =>
    request<PackTask>("/api/files/pack", {
      method: "POST",
      body: JSON.stringify({ folder_path: folderPath, output_name: outputName }),
    }),

  createPackTaskMulti: (paths: string[], outputName: string) =>
    request<PackTask>("/api/files/pack", {
      method: "POST",
      body: JSON.stringify({ paths, output_name: outputName }),
    }),

  calculateFilesSize: (paths: string[]) =>
    request<{ total_size: number; user_available: number }>("/api/files/pack/calculate-size", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),

  listPackTasks: () => request<PackTask[]>("/api/files/pack"),

  getPackTask: (id: number) => request<PackTask>(`/api/files/pack/${id}`),

  cancelPackTask: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/files/pack/${id}`, {
      method: "DELETE",
    }),

  downloadPackResult: (id: number) => {
    const base = getApiBase();
    return `${base}/api/files/pack/${id}/download`;
  },

  getPackAvailableSpace: (folderPath?: string) =>
    request<PackAvailableSpace>(
      folderPath
        ? `/api/files/pack/available-space?folder_path=${encodeURIComponent(folderPath)}`
        : "/api/files/pack/available-space"
    ),
};

export function taskWsUrl(): string {
  const base = getApiBase();
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/tasks";
  return url.toString();
}
