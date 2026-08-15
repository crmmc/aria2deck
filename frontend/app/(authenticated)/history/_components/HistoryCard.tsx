import { memo, useCallback } from "react";

import type { TaskHistory } from "@/types";
import { formatBytes } from "@/lib/utils";

interface HistoryCardProps {
  record: TaskHistory;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (record: TaskHistory) => void;
  onDelete: (record: TaskHistory) => void;
}

function canShowRetry(record: TaskHistory): boolean {
  // Prefer machine-readable flag; fall back for older payloads without retryable.
  if (record.retryable === true) return true;
  // When false or missing, still show for failed/cancelled so blocked reason can be disabled UI.
  return record.result === "failed" || record.result === "cancelled";
}

function isRetryBlocked(record: TaskHistory): boolean {
  return record.retryable === false;
}

export const HistoryCard = memo(function HistoryCard({
  record,
  isSelected,
  onToggleSelection,
  onCopyUri,
  onRetry,
  onDelete,
}: HistoryCardProps) {
  const handleCardClick = useCallback(() => {
    if (record.uri) {
      onCopyUri(record.uri);
    }
  }, [record.uri, onCopyUri]);

  const handleCheckboxChange = useCallback(() => {
    onToggleSelection(record.id);
  }, [record.id, onToggleSelection]);

  const handleCopyClick = useCallback(() => {
    onCopyUri(record.uri!);
  }, [record.uri, onCopyUri]);

  const retryBlocked = isRetryBlocked(record);

  const handleRetryClick = useCallback(() => {
    if (retryBlocked) return;
    onRetry(record);
  }, [record, onRetry, retryBlocked]);

  const statusText =
    record.result === "completed"
      ? "已完成"
      : record.result === "cancelled"
        ? "已取消"
        : "失败";

  const statusClass =
    record.result === "completed"
      ? "task-status-complete"
      : record.result === "cancelled"
        ? "task-status-cancelled"
        : "task-status-error";

  const interactiveProps = record.uri
    ? {
        role: "button",
        tabIndex: 0,
        onClick: handleCardClick,
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            handleCardClick();
          }
        },
        "aria-label": `复制历史任务链接 ${record.task_name}`,
      }
    : {};

  // 重试阻塞原因只对用户期待重试的终态（failed/cancelled）有意义；
  // completed 属正常成功态，不显示也不标红。
  const blockedReason =
    retryBlocked &&
    record.retry_blocked_reason &&
    (record.result === "failed" || record.result === "cancelled")
      ? record.retry_blocked_reason
      : null;
  const reasonText = blockedReason || record.reason || null;

  return (
    <div
      className={`task-card-inner${isSelected ? " selected" : ""}${record.uri ? " cursor-pointer" : ""}`}
      {...interactiveProps}
    >
      <div>
        <div className="space-between flex-start mb-3">
          <div className="task-card-header">
            <input
              type="checkbox"
              aria-label={`选择 ${record.task_name}`}
              checked={isSelected}
              onChange={handleCheckboxChange}
              onClick={(e) => e.stopPropagation()}
              className="checkbox-sm mt-2 cursor-pointer"
            />
            <div className="overflow-hidden flex-1">
              <h3 className="task-name" title={record.task_name}>
                {record.task_name}
              </h3>
              <div className="muted tabular-nums text-sm">
                {formatBytes(record.total_length)}
              </div>
            </div>
          </div>
          <div
            className="task-status-col"
            style={{ marginLeft: "auto", textAlign: "right" }}
          >
            <span className={`task-status ${statusClass}`}>{statusText}</span>
            {reasonText && (
              <div
                className={`text-sm mt-1 ${
                  record.result === "failed" ? "text-danger" : "muted"
                }`}
                style={{ textAlign: "right", maxWidth: "60%", marginLeft: "auto" }}
                title={reasonText}
              >
                {reasonText}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="task-card-footer" role="presentation" onClick={(e) => e.stopPropagation()}>
        <div className="task-footer-left">
          <span className="muted text-sm" suppressHydrationWarning>
            {new Date(record.finished_at).toLocaleString()}
          </span>
        </div>

        <div className="task-footer-right">
          {record.uri && (
            <button type="button"
              className="button secondary btn-task"
              onClick={handleCopyClick}
              title="复制链接"
            >
              复制
            </button>
          )}
          {canShowRetry(record) && (
            <button type="button"
              className={`button secondary btn-task${retryBlocked ? " opacity-60" : ""}`}
              onClick={handleRetryClick}
              disabled={retryBlocked}
              title={blockedReason || "重新下载"}
            >
              重试
            </button>
          )}
          <button type="button"
            className="button danger btn-task"
            onClick={(e) => {
              e.stopPropagation();
              onDelete(record);
            }}
            title="删除这条历史记录"
          >
            删除
          </button>
        </div>
      </div>
    </div>
  );
});
