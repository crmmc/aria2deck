import { memo, useCallback } from "react";

import type { TaskHistory } from "@/types";
import { formatBytes } from "@/lib/utils";

interface HistoryCardProps {
  record: TaskHistory;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (record: TaskHistory) => void;
}

export const HistoryCard = memo(function HistoryCard({
  record,
  isSelected,
  onToggleSelection,
  onCopyUri,
  onRetry,
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

  const handleRetryClick = useCallback(() => {
    onRetry(record);
  }, [record, onRetry]);

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
          <span
            className={`task-status ${statusClass}`}
            style={{ marginLeft: "auto" }}
          >
            {statusText}
          </span>
        </div>

        {record.reason && (
          <div
            className={`text-sm mb-3 ${record.result === "failed" ? "text-danger" : "muted"}`}
            title={record.reason}
          >
            {record.reason}
          </div>
        )}
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
          {record.result === "failed" && record.uri && (
            <button type="button"
              className="button secondary btn-task"
              onClick={handleRetryClick}
              title="重新下载"
            >
              重试
            </button>
          )}
        </div>
      </div>
    </div>
  );
});
