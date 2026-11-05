import { api } from "@/shared/api/client";
import type { TaskBatchResponse, TaskListResponse, HistoryStatus } from "@/shared/api/types";

export async function fetchHistory(
  status: HistoryStatus | null,
  search: string,
  page: number,
  pageSize: number,
): Promise<TaskListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (status !== null) params.set("status", status);
  const trimmed = search.trim();
  if (trimmed !== "") params.set("q", trimmed);
  return api.get<TaskListResponse>(`/api/v2/history?${params.toString()}`);
}

export async function deleteHistory(taskIds: number[]): Promise<TaskBatchResponse> {
  return api.del<TaskBatchResponse>("/api/v2/history", { task_ids: taskIds });
}

export async function retryFromHistory(taskId: number): Promise<void> {
  await api.post(`/api/v2/tasks/${taskId}/retry`);
}
