import { api } from "@/shared/api/client";
import type {
  TaskBatchResponse,
  TaskListResponse,
  TaskWriteResultResponse,
  TaskResponse,
  HistoryStatus,
} from "@/shared/api/types";

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

/** C29：重试是单项 TaskWriteResultResponse；HTTP 200 但 accepted=false 是业务拒绝，必须抛错。 */
export async function retryFromHistory(taskId: number): Promise<TaskResponse> {
  const response = await api.post<TaskWriteResultResponse>(`/api/v2/tasks/${taskId}/retry`);
  if (!response.accepted || response.task === null) {
    throw new Error(response.error?.message ?? "重试失败");
  }
  return response.task;
}
