import { api } from "@/shared/api/client";
import { fetchAllPages } from "@/shared/api/pagination";
import type {
  TaskResponse,
  TaskListResponse,
  TaskBatchResponse,
  TaskWriteResultResponse,
  TorrentPreviewResponse,
} from "@/shared/api/types";
import { CURRENT_LOAD_STATUSES } from "./model";

/**
 * C21/T1 全量聚合：为保持归档参考的无分页界面，顺序读取全部页。
 * 初始加载只取在下载管线的状态（与归档参考 `status_filter=current` 同口径）。
 * 失败任务由 WebSocket 单帧保留在当前视图，刷新后才消失。
 */
export async function fetchTasksAll(): Promise<TaskResponse[]> {
  return fetchAllPages((page) =>
    fetchTasksList([...CURRENT_LOAD_STATUSES], page, 100),
  );
}

async function fetchTasksList(
  statuses: string[],
  page: number,
  pageSize: number,
): Promise<TaskListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  for (const status of statuses) params.append("status", status);
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
