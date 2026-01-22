import type {
  Task,
  User,
  UserCreate,
  UserUpdate,
  SystemStats,
  SystemConfig,
  TaskFile,
  FileListResponse,
  QuotaResponse,
  MachineStats,
} from "@/types";

function getApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_BASE) {
    return process.env.NEXT_PUBLIC_API_BASE;
  }
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  return "http://localhost:8000";
}

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
    const text = await res.text();
    throw new Error(text || `请求失败: ${res.status}`);
  }
  return (await res.json()) as T;
}

export const api = {
  login: (username: string, password: string) =>
    request<User>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () =>
    request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),

  // Tasks
  listTasks: () => request<Task[]>("/api/tasks"),
  getTask: (id: number) => request<Task>(`/api/tasks/${id}`),
  createTask: (uri: string) =>
    request<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ uri }),
    }),
  // Replaces actionTask with more specific status update
  updateTaskStatus: (id: number, status: string) =>
    request<{ ok: boolean }>(`/api/tasks/${id}/status`, {
      method: "PUT",
      body: JSON.stringify({ status }),
    }),
  deleteTask: (id: number, deleteFiles: boolean = false) =>
    request<{ ok: boolean }>(`/api/tasks/${id}?delete_files=${deleteFiles}`, {
      method: "DELETE",
    }),
  clearHistory: (deleteFiles: boolean = false) =>
    request<{ ok: boolean; count: number }>(
      `/api/tasks?delete_files=${deleteFiles}`,
      {
        method: "DELETE",
      },
    ),
  getTaskFiles: (id: number) => request<TaskFile[]>(`/api/tasks/${id}/files`),
  getTaskDetail: (id: number) => request<any>(`/api/tasks/${id}/detail`),

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

  // Users (Admin)
  listUsers: () => request<User[]>("/api/users"),
  getUser: (id: number) => request<User>(`/api/users/${id}`),
  createUser: (data: UserCreate) =>
    request<User>("/api/users", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateUser: (id: number, data: UserUpdate) =>
    request<User>(`/api/users/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  deleteUser: (id: number) =>
    request<{ ok: boolean }>(`/api/users/${id}`, { method: "DELETE" }),

  // Files
  listFiles: (path?: string) =>
    request<FileListResponse>(
      `/api/files${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  downloadFile: (path: string) => {
    const base = getApiBase();
    return `${base}/api/files/download?path=${encodeURIComponent(path)}`;
  },
  deleteFile: (path: string) =>
    request<{ ok: boolean; message: string }>(
      `/api/files?path=${encodeURIComponent(path)}`,
      {
        method: "DELETE",
      },
    ),
  renameFile: (oldPath: string, newName: string) =>
    request<{ ok: boolean; message: string; new_path: string }>(
      "/api/files/rename",
      {
        method: "PUT",
        body: JSON.stringify({ old_path: oldPath, new_name: newName }),
      },
    ),
  getQuota: () => request<QuotaResponse>("/api/files/quota"),
};

export function taskWsUrl(): string {
  const base = getApiBase();
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/tasks";
  return url.toString();
}
