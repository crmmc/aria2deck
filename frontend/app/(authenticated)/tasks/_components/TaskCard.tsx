import { memo, useCallback } from "react";
import type { Task } from "@/types";
import { formatBytes } from "@/lib/utils";

function getTaskDisplayName(task: Task): string {
  return task.name || "未知文件";
}

function getTaskProgress(task: Task): number {
  if (task.total_length <= 0) {
    return 0;
  }
  return Math.min((task.completed_length / task.total_length) * 100, 100);
}

function formatTaskProgressLabel(task: Task): string {
  const progress = getTaskProgress(task);
  if (task.status !== "complete" && task.completed_length < task.total_length) {
    return `${Math.min(progress, 99.9).toFixed(1)}%`;
  }
  return `${Math.min(progress, 100).toFixed(0)}%`;
}

interface TaskCardProps {
  task: Task;
  isSelected: boolean;
  isOperating: boolean;
  onToggleSelection: (id: number) => void;
  onCancel: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (task: Task) => void;
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
    if (task.uri) {
      onCopyUri(task.uri);
    }
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

  const mainContent = (
    <>
      <div className="space-between flex-start mb-3">
        <div className="overflow-hidden flex-1">
          <div className="task-card-title-row">
            <h3 className="task-name" title={task.name || undefined}>
              {displayName}
            </h3>
            <span className={`task-status task-status-${task.status}`}>
              {task.status === "queued" || task.status === "paused"
                ? task.status_label || (task.status === "queued" ? "排队中" : "已暂停")
                : task.status === "active"
                  ? "下载中"
                  : task.status === "waiting"
                    ? "等待中"
                    : task.status === "error"
                      ? "失败"
                      : task.status}
            </span>
          </div>
          <div className="task-card-meta muted tabular-nums text-sm">
            {task.status === "active" && (
              <span className="task-speed">{formatBytes(task.download_speed)}/s</span>
            )}
            <span>
              {formatBytes(task.completed_length)} / {formatBytes(task.total_length)}
            </span>
          </div>
        </div>
      </div>

      <div className="task-progress-row mb-3">
        <div
          className="progress-container"
          role="progressbar"
          aria-label={`${displayName} 下载进度`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
        >
          <div
            className={`progress-bar ${
              task.status === "active"
                ? "progress-bar-active progress-bar-primary"
                : task.status === "error"
                  ? "progress-bar-error"
                  : "progress-bar-primary"
            }`}
            style={{
              width: `${progress}%`,
            }}
          />
        </div>
        {task.total_length > 0 && task.status !== "error" && (
          <span className="task-progress-text">
            {formatTaskProgressLabel(task)}
          </span>
        )}
      </div>
    </>
  );

  return (
    <div
      className={`task-card-inner${isSelected ? " selected" : ""}`}
    >
      <div className="task-card-header">
        <input
          type="checkbox"
          checked={isSelected}
          onChange={handleCheckboxChange}
          className="checkbox-sm mt-2 cursor-pointer"
          aria-label={`选择 ${displayName}`}
        />
        {task.uri ? (
          <button
            type="button"
            className="task-card-copy-target"
            onClick={handleCardClick}
          >
            {mainContent}
          </button>
        ) : (
          <div className="task-card-copy-target non-interactive">
            {mainContent}
          </div>
        )}
      </div>

        <div
          className="task-card-footer"
        >
          <div className="task-footer-left">
            {task.error && (
              <span className="task-error-text" title={task.error}>
                {task.error}
              </span>
            )}
          </div>

          <div className="task-footer-right">
            {task.uri && (
              <button
                type="button"
                className="button secondary btn-task"
                onClick={handleCopyClick}
                title="复制链接"
              >
                复制
              </button>
            )}
            {task.status === "error" && task.uri && (
              <button
                type="button"
                className="button secondary btn-task"
                onClick={handleRetryClick}
                title="重新下载"
              >
                重试
              </button>
            )}
            {(task.status === "active" ||
              task.status === "queued" ||
              task.status === "waiting" ||
              task.status === "paused") && (
              <button
                type="button"
                className={`button secondary danger btn-task${isOperating ? " opacity-60" : ""}`}
                onClick={handleCancelClick}
                disabled={isOperating}
                title={task.status === "paused" ? "取消被暂停的任务" : "取消任务"}
              >
                {isOperating ? "处理中..." : "取消"}
              </button>
            )}
            {task.status === "error" && (
              <button
                type="button"
                className={`button secondary danger btn-task${isOperating ? " opacity-60" : ""}`}
                onClick={handleCancelClick}
                disabled={isOperating}
                title="删除失败任务"
              >
                删除
              </button>
            )}
          </div>
        </div>
    </div>
  );
});
