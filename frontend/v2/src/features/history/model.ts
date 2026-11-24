/**
 * H1 历史视图模型：V2 TaskResponse（历史分页项）→ 归档参考 HistoryCard 视图。
 * C13：cancelled/expired 集中映射到 归档参考 既有展示语义；C16 只用 error_code 不编造文本。
 */
import type { TaskResponse } from "@/shared/api/types";

export type HistoryResult = "completed" | "cancelled" | "failed";

export interface HistoryRecordView {
  id: number;
  taskName: string;
  totalLength: number;
  finishedAtMs: number | null;
  createdAtMs: number;
  result: HistoryResult;
  reason: string | null;
  retryable: boolean;
  uri: string | null;
}

/** C13：expired 归到失败展示（归档参考 无 expired 枚举）。 */
export function toHistoryResult(status: TaskResponse["status"]): HistoryResult {
  if (status === "completed") return "completed";
  if (status === "cancelled") return "cancelled";
  return "failed";
}

export function toHistoryRecordView(item: TaskResponse): HistoryRecordView {
  return {
    id: item.id,
    taskName: item.name || "未知文件",
    totalLength: item.total_bytes ?? 0,
    finishedAtMs: item.finished_at_ms,
    createdAtMs: item.created_at_ms,
    result: toHistoryResult(item.status),
    // C16/C10：V2 只有 error_code，无原始错误文本与阻塞原因。
    reason: item.error_code,
    retryable: item.retryable,
    uri: item.uri,
  };
}

export function historyStatusText(result: HistoryResult): string {
  if (result === "completed") return "已完成";
  if (result === "cancelled") return "已取消";
  return "失败";
}

export function historyStatusClass(result: HistoryResult): string {
  if (result === "completed") return "task-status-complete";
  if (result === "cancelled") return "task-status-cancelled";
  return "task-status-error";
}

/** 归档参考 canShowRetry：retryable=true 显示；failed/cancelled 显示（可能被禁用）。 */
export function canShowRetry(view: HistoryRecordView): boolean {
  if (view.retryable === true) return true;
  return view.result === "failed" || view.result === "cancelled";
}

export function isRetryBlocked(view: HistoryRecordView): boolean {
  return view.retryable === false;
}

/** 归档参考 历史列表排序：created_at_ms 降序、id 降序（归档参考 user_tasks 查询口径）。 */
export function sortHistoryRecords(records: HistoryRecordView[]): HistoryRecordView[] {
  return [...records].sort(
    (a, b) => b.createdAtMs - a.createdAtMs || b.id - a.id,
  );
}

/** 完成时间展示：归档参考 用 toLocaleString；无完成时间为空串。 */
export function formatFinishedAt(finishedAtMs: number | null): string {
  if (finishedAtMs === null) return "";
  return new Date(finishedAtMs).toLocaleString();
}
