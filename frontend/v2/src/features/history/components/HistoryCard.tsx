import { memo, useCallback } from "react";

import { formatBytes } from "@/shared/lib/format";
import { copyTargetUri } from "@/shared/lib/uri";
import { Icon } from "@/shared/components/Icon";
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
    if (record.uri) onCopyUri(copyTargetUri(record.uri));
  }, [record.uri, onCopyUri]);
  const handleCheckboxChange = useCallback(() => {
    onToggleSelection(record.id);
  }, [record.id, onToggleSelection]);
  const handleCopyClick = useCallback(() => {
    if (record.uri) onCopyUri(copyTargetUri(record.uri));
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
        onKeyDown: (event: React.KeyboardEvent<HTMLElement>) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            handleCardClick();
          }
        },
        "aria-label": `复制历史任务链接 ${record.taskName}`,
      }
    : {};
  const reasonText = record.reason;

  return (
    <article
      className={`task-card-inner record-row${isSelected ? " selected" : ""}${record.uri ? " cursor-pointer" : ""}`}
      {...interactiveProps}
    >
      <input
        type="checkbox"
        aria-label={`选择 ${record.taskName}`}
        checked={isSelected}
        onChange={handleCheckboxChange}
        onClick={(event) => event.stopPropagation()}
        className="checkbox-sm cursor-pointer"
      />
      <div className="task-main">
        <span className="file-mark"><Icon name="file" /></span>
        <div className="task-name">
          <h3 className="task-name-heading" title={record.taskName}>{record.taskName}</h3>
          <div className="task-meta muted tabular-nums text-sm">
            <span>{formatBytes(record.totalLength)}</span>
            {reasonText && (
              <span
                className={`task-status-reason error-code ${record.result === "failed" ? "text-danger" : "muted"}`}
                title={reasonText}
              >
                {reasonText}
              </span>
            )}
          </div>
        </div>
      </div>
      <span className={`task-status status-badge ${historyStatusClass(record.result)}`}>
        <Icon name={record.result === "completed" ? "check" : record.result === "failed" ? "alert" : "x"} className="icon-sm" />
        {historyStatusText(record.result)}
      </span>
      <span className="record-time" suppressHydrationWarning>{formatFinishedAt(record.finishedAtMs)}</span>
      <div className="task-footer-right record-actions" role="presentation" onClick={(event) => event.stopPropagation()}>
        {record.uri && (
          <button type="button" className="icon-btn" onClick={handleCopyClick} title="复制链接" aria-label="复制">
            <Icon name="copy" className="icon-sm" />
          </button>
        )}
        {canShowRetry(record) && (
          <button
            type="button"
            className={`icon-btn${retryBlocked ? " opacity-60" : ""}`}
            onClick={handleRetryClick}
            disabled={retryBlocked}
            title="重新下载"
            aria-label="重试"
          >
            <Icon name="retry" className="icon-sm" />
          </button>
        )}
        <button
          type="button"
          className="icon-btn danger"
          onClick={() => onDelete(record)}
          title="删除这条历史记录"
          aria-label="删除"
        >
          <Icon name="trash" className="icon-sm" />
        </button>
      </div>
    </article>
  );
});
