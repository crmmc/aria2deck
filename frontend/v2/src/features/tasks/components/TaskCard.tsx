import { memo, useCallback } from "react";
import type { TaskResponse } from "@/shared/api/types";
import { formatBytes } from "@/shared/lib/format";
import { Icon, type IconName } from "@/shared/components/Icon";

function getTaskDisplayName(task: TaskResponse): string {
  return task.name || "未知文件";
}

function getTaskProgress(task: TaskResponse): number {
  if (task.total_bytes === null || task.total_bytes <= 0) {
    return 0;
  }
  return Math.min((task.completed_bytes / task.total_bytes) * 100, 100);
}

function formatTaskProgressLabel(task: TaskResponse): string {
  if (task.total_bytes === null) return "0%";
  const progress = getTaskProgress(task);
  if (task.status !== "completed" && task.completed_bytes < task.total_bytes) {
    return `${Math.min(progress, 99.9).toFixed(1)}%`;
  }
  return `${Math.min(progress, 100).toFixed(0)}%`;
}

function statusIcon(status: TaskResponse["status"]): IconName {
  if (status === "active") return "download";
  if (status === "failed") return "alert";
  if (status === "completed") return "check";
  if (status === "cancelled" || status === "expired") return "x";
  return "clock";
}

interface TaskCardProps {
  task: TaskResponse;
  isSelected: boolean;
  isOperating: boolean;
  onToggleSelection: (id: number) => void;
  onCancel: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (task: TaskResponse) => void;
}

export const TaskCard = memo(function TaskCard({
  task,
  isSelected,
  isOperating,
  onToggleSelection,
  onCancel,
  onCopyUri,
  onRetry,
}: TaskCardProps) {
  const displayName = getTaskDisplayName(task);
  const progress = getTaskProgress(task);

  const handleCardClick = useCallback(() => {
    if (task.uri) onCopyUri(task.uri);
  }, [task.uri, onCopyUri]);
  const handleCheckboxChange = useCallback(() => {
    onToggleSelection(task.id);
  }, [task.id, onToggleSelection]);
  const handleCopyClick = useCallback(() => {
    if (task.uri) onCopyUri(task.uri);
  }, [task.uri, onCopyUri]);
  const handleCancelClick = useCallback(() => {
    onCancel(task.id);
  }, [task.id, onCancel]);
  const handleRetryClick = useCallback(() => {
    onRetry(task);
  }, [task, onRetry]);

  const statusText = task.status === "queued" || task.status === "paused" || task.status === "active"
    ? task.status_label || (task.status === "queued" ? "排队中" : task.status === "paused" ? "已暂停" : "下载中")
    : task.status === "waiting"
      ? "等待中"
      : task.status === "failed"
        ? "失败"
        : task.status;
  const nameContent = (
    <>
      <span role="heading" aria-level={3} className="task-name" title={task.name || undefined}>
        {displayName}
      </span>
      <span className="task-card-meta task-meta muted tabular-nums text-sm">
        <span>
          {formatBytes(task.completed_bytes)} / {task.total_bytes === null ? "0 B" : formatBytes(task.total_bytes)}
        </span>
        {task.error_code && (
          <span className="task-error-text error-code" title={task.error_code}>
            {task.error_code}
          </span>
        )}
      </span>
    </>
  );

  return (
    <article className={`task-card-inner task-row${isSelected ? " selected" : ""}`}>
      <input
        type="checkbox"
        checked={isSelected}
        onChange={handleCheckboxChange}
        className="checkbox-sm cursor-pointer"
        aria-label={`选择 ${displayName}`}
      />

      <div className="task-main">
        <span className="file-mark"><Icon name="file" /></span>
        {task.uri ? (
          <button type="button" className="task-card-copy-target task-name-button" onClick={handleCardClick}>
            {nameContent}
          </button>
        ) : (
          <div className="task-card-copy-target non-interactive task-name-button">{nameContent}</div>
        )}
      </div>

      <div className="progress-cell">
        <div className="progress-meta">
          <span className="task-progress-text">
            {task.total_bytes !== null && task.total_bytes > 0 && task.status !== "failed"
              ? formatTaskProgressLabel(task)
              : ""}
          </span>
          <span>{task.status === "active" ? "实时进度" : "下载进度"}</span>
        </div>
        <div
          className="progress-container progress-track"
          role="progressbar"
          aria-label={`${displayName} 下载进度`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
        >
          <div
            className={`progress-bar ${task.status === "failed" ? "progress-bar-error" : "progress-bar-primary"}`}
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      <span className={`task-status status-badge task-status-${task.status}`}>
        <Icon name={statusIcon(task.status)} className="icon-sm" />
        {statusText}
      </span>

      <div className={`speed-cell${task.status === "active" ? "" : " muted"}`}>
        {task.status === "active" ? `${formatBytes(task.download_speed)}/s` : "—"}
      </div>

      <div className="task-footer-right row-actions">
        {task.uri && (
          <button type="button" className="icon-btn" onClick={handleCopyClick} title="复制链接" aria-label="复制">
            <Icon name="copy" className="icon-sm" />
          </button>
        )}
        {task.status === "failed" && task.uri && (
          <button type="button" className="icon-btn" onClick={handleRetryClick} title="重新下载" aria-label="重试">
            <Icon name="retry" className="icon-sm" />
          </button>
        )}
        {(task.status === "active" || task.status === "queued" || task.status === "waiting" || task.status === "paused") && (
          <button
            type="button"
            className={`icon-btn danger${isOperating ? " opacity-60" : ""}`}
            onClick={handleCancelClick}
            disabled={isOperating}
            title={task.status === "paused" ? "取消被暂停的任务" : "取消任务"}
            aria-label={isOperating ? "处理中..." : "取消"}
          >
            <Icon name="trash" className="icon-sm" />
          </button>
        )}
        {task.status === "failed" && (
          <button
            type="button"
            className={`icon-btn danger${isOperating ? " opacity-60" : ""}`}
            onClick={handleCancelClick}
            disabled={isOperating}
            title="删除失败任务"
            aria-label="删除"
          >
            <Icon name="trash" className="icon-sm" />
          </button>
        )}
      </div>
    </article>
  );
});
