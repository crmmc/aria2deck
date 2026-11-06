import { api } from "@/shared/api/client";
import type {
  TaskResponse,
  TaskListResponse,
  TaskBatchResponse,
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

export async function createTasks(text: string): Promise<CreateOutcome> {
  const uris = splitUris(text);
  const response = await api.post<TaskBatchResponse>("/api/v2/tasks", {
    tasks: uris.map((uri) => ({ uri })),
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
): Promise<CreateOutcome> {
  const response = await api.post<TaskBatchResponse>("/api/v2/tasks/torrent", {
    torrent: base64,
    selected_file_indexes: selectedIndexes.length > 0 ? selectedIndexes : null,
  });
  return toOutcome(response, ["种子"]);
}

export async function cancelTasks(taskIds: number[]): Promise<TaskBatchResponse> {
  return api.post<TaskBatchResponse>("/api/v2/tasks/cancel", { task_ids: taskIds });
}

export async function retryTask(taskId: number): Promise<TaskResponse> {
  const response = await api.post<{ accepted: boolean; task: TaskResponse | null }>(
    `/api/v2/tasks/${taskId}/retry`,
  );
  if (response.task === null) throw new Error("重试失败");
  return response.task;
}
