import { memo, useCallback } from "react";

import { formatBytes } from "@/shared/lib/format";
import type { HistoryRecordView } from "../model";
import {
  canShowRetry,
  formatFinishedAt,
  historyStatusClass,
  historyStatusText,
  isRetryBlocked,
} from "../model";

interface HistoryCardProps {
  record: HistoryRecordView;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (record: HistoryRecordView) => void;
  onDelete: (record: HistoryRecordView) => void;
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
    if (record.uri) onCopyUri(record.uri);
  }, [record.uri, onCopyUri]);

  const retryBlocked = isRetryBlocked(record);

  const handleRetryClick = useCallback(() => {
    if (retryBlocked) return;
    onRetry(record);
  }, [record, onRetry, retryBlocked]);

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
        "aria-label": `复制历史任务链接 ${record.taskName}`,
      }
    : {};

  const reasonText = record.reason;

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
              aria-label={`选择 ${record.taskName}`}
              checked={isSelected}
              onChange={handleCheckboxChange}
              onClick={(e) => e.stopPropagation()}
              className="checkbox-sm mt-2 cursor-pointer"
            />
            <div className="overflow-hidden flex-1">
              <h3 className="task-name" title={record.taskName}>
                {record.taskName}
              </h3>
              <div className="muted tabular-nums text-sm">
                {formatBytes(record.totalLength)}
              </div>
            </div>
          </div>
          <div className="task-status-col">
            <span className={`task-status ${historyStatusClass(record.result)}`}>
              {historyStatusText(record.result)}
            </span>
            {reasonText && (
              <div
                className={`task-status-reason text-sm ${
                  record.result === "failed" ? "text-danger" : "muted"
                }`}
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
            {formatFinishedAt(record.finishedAtMs)}
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
              title="重新下载"
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
