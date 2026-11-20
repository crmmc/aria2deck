import { api } from "@/shared/api/client";
import { fetchAllPages } from "@/shared/api/pagination";
import type {
  TaskResponse,
  TaskListResponse,
  TaskBatchResponse,
  TaskWriteResultResponse,
  TorrentPreviewResponse,
} from "@/shared/api/types";

export type TaskFilter = "active" | "current" | "complete" | "error" | null;
export type TaskSortKey = "created_at" | "name" | "size";
export type TaskSortOrder = "asc" | "desc";
export type TaskSort = { key: TaskSortKey; order: TaskSortOrder };

const FILTER_PARAMS: Record<string, string[]> = {
  active: ["active"],
  current: ["queued", "active", "waiting", "paused"],
  complete: ["completed"],
  error: ["failed", "cancelled", "expired"],
};

/**
 * C21/T1 全量聚合：归档参考 当前任务视图需要 queued/active/waiting/paused + failed，
 * V2 服务端 `current` 会漏 failed，必须显式多状态参数顺序读全部分页。
 */
export async function fetchTasksAll(
  filter: "all" | "active",
  search: string,
): Promise<TaskResponse[]> {
  const statuses =
    filter === "active"
      ? ["queued", "active", "waiting", "paused"]
      : ["queued", "active", "waiting", "paused", "failed"];
  return fetchAllPages((page) =>
    fetchTasksList(statuses, page, 100, search),
  );
}

async function fetchTasksList(
  statuses: string[],
  page: number,
  pageSize: number,
  search: string,
): Promise<TaskListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  for (const status of statuses) params.append("status", status);
  const term = search.trim();
  if (term !== "") params.set("q", term);
  return api.get<TaskListResponse>(`/api/v2/tasks?${params.toString()}`);
}

export async function fetchTasks(
  filter: TaskFilter,
  page: number,
  pageSize: number,
  sort: TaskSort | null,
  search: string,
): Promise<TaskListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (filter !== null) {
    for (const status of FILTER_PARAMS[filter]) params.append("status", status);
  }
  const term = search.trim();
  if (term !== "") params.set("q", term);
  if (sort !== null) {
    params.set("sort", sort.key);
    params.set("order", sort.order);
  }
  return api.get<TaskListResponse>(`/api/v2/tasks?${params.toString()}`);
}

export interface CreateOutcome {
  acceptedCount: number;
  rejectedCount: number;
  failedItems: { uri: string; message: string }[];
}

function splitUris(text: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed === "" || seen.has(trimmed)) continue;
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

export async function createTasks(
  text: string,
  allowRename = true,
): Promise<CreateOutcome> {
  const uris = splitUris(text);
  const response = await api.post<TaskBatchResponse>("/api/v2/tasks", {
    tasks: uris.map((uri) => ({ uri, options: { allow_rename: allowRename } })),
  });
  return toOutcome(response, uris);
}

function toOutcome(response: TaskBatchResponse, inputs: string[]): CreateOutcome {
  const failedItems: { uri: string; message: string }[] = [];
  response.items.forEach((item, index) => {
    if (!item.accepted && item.error !== null) {
      failedItems.push({
        uri: inputs[index] ?? `第 ${index + 1} 条`,
        message: item.error.message,
      });
    }
  });
  return {
    acceptedCount: response.accepted_count,
    rejectedCount: response.rejected_count,
    failedItems,
  };
}

export async function previewTorrent(base64: string): Promise<TorrentPreviewResponse> {
  return api.post<TorrentPreviewResponse>("/api/v2/tasks/torrent/preview", {
    torrent: base64,
  });
}

export async function createTorrent(
  base64: string,
  selectedIndexes: number[],
  allowRename = true,
): Promise<TaskResponse> {
  // 后端按单项 TaskWriteResultResponse 返回；业务拒绝留在原向导展示。
  const response = await api.post<TaskWriteResultResponse>("/api/v2/tasks/torrent", {
    torrent: base64,
    selected_file_indexes: selectedIndexes.length > 0 ? selectedIndexes : null,
    allow_rename: allowRename,
  });
  if (!response.accepted || response.task === null) {
    throw new Error(response.error?.message ?? "创建种子任务失败");
  }
  return response.task;
}

export async function cancelTasks(taskIds: number[]): Promise<TaskBatchResponse> {
  return api.post<TaskBatchResponse>("/api/v2/tasks/cancel", { task_ids: taskIds });
}

export async function retryTask(taskId: number, allowRename = true): Promise<TaskResponse> {
  const response = await api.post<TaskWriteResultResponse>(
    `/api/v2/tasks/${taskId}/retry`,
    { allow_rename: allowRename },
  );
  if (!response.accepted || response.task === null) {
    throw new Error(response.error?.message ?? "重试失败");
  }
  return response.task;
}
