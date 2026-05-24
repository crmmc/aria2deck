import { memo, useCallback } from "react";
import type { Task } from "@/types";
import { formatBytes } from "@/lib/utils";

function getTaskDisplayName(task: Task): string {
  return task.name || "未知文件";
}

function formatTaskProgressLabel(task: Task): string {
  if (task.total_length <= 0) {
    return "0%";
  }
  const progress = (task.completed_length / task.total_length) * 100;
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

  return (
    <div
      className={`task-card-inner${isSelected ? " selected" : ""}${task.uri ? " cursor-pointer" : ""}`}
      onClick={task.uri ? handleCardClick : undefined}
      role={task.uri ? "button" : undefined}
      tabIndex={task.uri ? 0 : undefined}
      onKeyDown={task.uri ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleCardClick(); } } : undefined}
    >
      <div>
          <div className="space-between flex-start mb-3">
            <div className="task-card-header">
              <input
                type="checkbox"
                checked={isSelected}
                onChange={handleCheckboxChange}
                onClick={(e) => e.stopPropagation()}
                className="checkbox-sm mt-2 cursor-pointer"
                aria-label={`选择 ${getTaskDisplayName(task)}`}
              />
              <div className="overflow-hidden flex-1">
                <h3 className="task-name" title={task.name || undefined}>
                  {getTaskDisplayName(task)}
                </h3>
                <div className="muted tabular-nums text-sm">
                  {formatBytes(task.completed_length)} /{" "}
                  {formatBytes(task.total_length)}
                </div>
              </div>
            </div>
            {task.status === "active" && (
              <span className="badge active tabular-nums">
                {formatBytes(task.download_speed)}/s
              </span>
            )}
          </div>

          <div className="progress-container mb-3">
            <div
              className={`progress-bar ${
                task.status === "active"
                  ? "progress-bar-active progress-bar-primary"
                  : task.status === "error"
                    ? "progress-bar-error"
                    : "progress-bar-primary"
              }`}
              style={{
                width: `${task.total_length ? (task.completed_length / task.total_length) * 100 : 0}%`,
              }}
            />
          </div>
        </div>

        <div
          className="task-card-footer"
          role="presentation"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="task-footer-left">
            <span className={`task-status task-status-${task.status}`}>
              {task.status === "active"
                ? "下载中"
                : task.status === "queued"
                  ? "排队中"
                  : task.status === "error"
                    ? "失败"
                    : task.status}
            </span>
            {task.error && (
              <span className="text-danger text-sm" title={task.error}>
                {task.error}
              </span>
            )}
            {task.total_length > 0 && task.status !== "error" && (
              <span className="task-progress-text">
                {formatTaskProgressLabel(task)}
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
            {(task.status === "active" || task.status === "queued") && (
              <button
                type="button"
                className={`button secondary danger btn-task${isOperating ? " opacity-60" : ""}`}
                onClick={handleCancelClick}
                disabled={isOperating}
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
