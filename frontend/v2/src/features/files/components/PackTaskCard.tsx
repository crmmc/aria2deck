"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { createPortal } from "react-dom";

import { useMounted } from "@/shared/hooks/useMounted";
import { formatBytes } from "@/shared/lib/format";
import { useToast } from "@/shared/components/toast-context";
import { cancelPack, clearPackRecords, deletePackRecord, fetchPacks } from "../api";
import type { PackViewResponse } from "@/shared/api/types";

function getStatusColor(status: string): string {
  switch (status) {
    case "packing": return "var(--primary)";
    case "done": return "var(--success)";
    case "failed": return "var(--danger)";
    case "cancelled": return "var(--gray)";
    default: return "var(--warning)";
  }
}

function getStatusText(status: string): string {
  switch (status) {
    case "pending": return "排队中...";
    case "packing": return "打包中";
    case "done": return "已完成";
    case "failed": return "失败";
    case "cancelled": return "已取消";
    default: return status;
  }
}

/** C11：V2 无 folder_path 源描述；output_name 由后端生成，兜底不编造来源。 */
function getDisplayName(task: PackViewResponse): string {
  if (task.output_name) return task.output_name;
  return "打包任务";
}

function getStepText(step: string | null): string {
  switch (step) {
    case "validating": return "校验";
    case "compressing": return "压缩";
    case "verifying": return "验收";
    default: return "处理中";
  }
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) {
    return `${minutes}分${remainingSeconds.toString().padStart(2, "0")}秒`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours}小时${(minutes % 60).toString().padStart(2, "0")}分${remainingSeconds.toString().padStart(2, "0")}秒`;
}

function getTimingText(task: PackViewResponse, nowMs: number): string {
  const startedMs = task.step_started_at_ms;
  const stepProgress = task.step_progress;
  if (
    startedMs === null
    || !Number.isFinite(stepProgress)
    || stepProgress <= 0
    || stepProgress > 100
  ) {
    return `${getStepText(task.step)} · 已用 -- / 预计剩余 --`;
  }
  const elapsed = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
  if (elapsed <= 0) {
    return `${getStepText(task.step)} · 已用 -- / 预计剩余 --`;
  }
  const eta = Math.round(elapsed * (100 - stepProgress) / stepProgress);
  return `${getStepText(task.step)} · 已用 ${formatDuration(elapsed)} / 预计剩余 ${formatDuration(Math.max(0, eta))}`;
}

interface DropdownPosition {
  top: number;
  right: number;
}

interface PackTaskCardProps {
  onTaskComplete?: () => void;
}

export function PackTaskCard({ onTaskComplete }: PackTaskCardProps) {
  const { showToast, showConfirm } = useToast();
  const mounted = useMounted();
  const [tasks, setTasks] = useState<PackViewResponse[]>([]);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [expanded, setExpanded] = useState(false);
  const [visible, setVisible] = useState(false);
  const [position, setPosition] = useState<DropdownPosition | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const taskStatusRef = useRef<Map<number, PackViewResponse["status"]> | null>(null);
  if (taskStatusRef.current === null) {
    taskStatusRef.current = new Map();
  }
  const taskStatus = taskStatusRef.current;
  const onTaskCompleteRef = useRef(onTaskComplete);

  useEffect(() => {
    onTaskCompleteRef.current = onTaskComplete;
  }, [onTaskComplete]);

  const loadTasks = useCallback(async () => {
    try {
      const data = await fetchPacks(50);
      const previousStatuses = taskStatus;
      const completedFromActive = data.items.some((task) => {
        const previousStatus = previousStatuses.get(task.id);
        return (
          task.status === "done" &&
          (previousStatus === "pending" || previousStatus === "packing")
        );
      });
      taskStatus.clear();
      data.items.forEach((task) => taskStatus.set(task.id, task.status));
      setTasks(data.items);
      if (completedFromActive) {
        onTaskCompleteRef.current?.();
      }
    } catch (err) {
      console.error("Failed to load pack tasks:", err);
    }
  }, [taskStatus]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    const hasActiveTasks = tasks.some(
      (t) => t.status === "pending" || t.status === "packing",
    );

    if (hasActiveTasks) {
      const interval = setInterval(() => {
        if (!document.hidden) loadTasks();
      }, 2000);
      return () => clearInterval(interval);
    }
    return undefined;
  }, [tasks, loadTasks]);

  useEffect(() => {
    if (!tasks.some((task) => task.status === "packing")) return undefined;
    setNowMs(Date.now());
    const interval = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [tasks]);

  const collapse = useCallback(() => {
    setVisible(false);
    const timer = setTimeout(() => {
      setExpanded(false);
      setPosition(null);
    }, 400);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!expanded) return undefined;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") collapse();
    };
    const handleMouseDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        buttonRef.current?.contains(target) ||
        panelRef.current?.contains(target)
      ) {
        return;
      }
      collapse();
    };
    const handleReposition = (e: Event) => {
      if (panelRef.current?.contains(e.target as Node)) return;
      collapse();
    };
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handleMouseDown);
    window.addEventListener("resize", handleReposition, true);
    window.addEventListener("scroll", handleReposition, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("resize", handleReposition, true);
      window.removeEventListener("scroll", handleReposition, true);
    };
  }, [expanded, collapse]);

  const clearPendingClose = useCallback(() => {
    if (closeTimerRef.current !== null) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const expand = useCallback(() => {
    clearPendingClose();
    if (expanded) return;
    const rect = buttonRef.current?.getBoundingClientRect();
    setPosition(
      rect
        ? { top: rect.bottom + 8, right: window.innerWidth - rect.right }
        : { top: 0, right: 0 },
    );
    setExpanded(true);
    requestAnimationFrame(() => setVisible(true));
  }, [expanded, clearPendingClose]);

  const scheduleClose = useCallback(() => {
    clearPendingClose();
    closeTimerRef.current = setTimeout(() => {
      closeTimerRef.current = null;
      collapse();
    }, 200);
  }, [collapse, clearPendingClose]);

  useEffect(() => () => clearPendingClose(), [clearPendingClose]);

  const handleToggle = () => {
    if (expanded) {
      clearPendingClose();
      collapse();
      return;
    }
    expand();
  };

  const activeTasks = useMemo(
    () => tasks.filter((t) => t.status === "pending" || t.status === "packing"),
    [tasks],
  );

  const terminalTasks = useMemo(
    () => tasks.filter((t) => t.status === "done" || t.status === "failed" || t.status === "cancelled"),
    [tasks],
  );

  const handleClearAll = async () => {
    try {
      const result = await clearPackRecords();
      showToast(`已清空 ${result.cleared_count} 条记录`, "success");
      loadTasks();
    } catch (err) {
      showToast(`清空失败: ${(err as Error).message}`, "error");
    }
  };

  const handleCancel = async (taskId: number) => {
    const confirmed = await showConfirm({
      title: "取消打包任务",
      message: "确定要取消此打包任务吗？",
      confirmText: "取消任务",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await cancelPack(taskId);
      loadTasks();
    } catch (err) {
      showToast(`取消失败: ${(err as Error).message}`, "error");
    }
  };

  const handleDelete = async (taskId: number) => {
    const confirmed = await showConfirm({
      title: "删除任务记录",
      message: "确定要删除此任务记录吗？",
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await deletePackRecord(taskId);
      loadTasks();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    }
  };

  if (tasks.length === 0) return null;

  return (
    <div className="relative">
      <button
        type="button"
        className="button secondary pack-task-btn"
        ref={buttonRef}
        onClick={handleToggle}
        onMouseEnter={expand}
        onMouseLeave={scheduleClose}
        aria-expanded={expanded}
        aria-haspopup="true"
      >
        <span>打包任务</span>
        {activeTasks.length > 0 && (
          <span className="pack-task-badge">{activeTasks.length}</span>
        )}
      </button>

      {expanded && mounted && position && createPortal(
        <div
          ref={panelRef}
          className={`pack-dropdown ${visible ? "pack-dropdown-visible" : "pack-dropdown-hidden"}`}
          style={{ top: position.top, right: position.right }}
          onMouseEnter={clearPendingClose}
          onMouseLeave={scheduleClose}
        >
          {terminalTasks.length > 0 && (
            <div className="pack-dropdown-header">
              <button type="button"
                className="button secondary btn-sm"
                onClick={handleClearAll}
              >
                清空已完成
              </button>
            </div>
          )}
          {tasks.map((task) => (
            <div key={task.id} className="pack-task-item">
              <div className="pack-task-header">
                <span className="pack-task-name">{getDisplayName(task)}</span>
                <span className="pack-task-status" style={{ color: getStatusColor(task.status) }}>
                  {getStatusText(task.status)}
                </span>
              </div>

              {(task.status === "pending" || task.status === "packing") && (
                <>
                  <div className="pack-progress-bar">
                    <div
                      className="pack-progress-fill"
                      style={{ width: `${task.step_progress}%` }}
                    />
                  </div>
                  <div className="flex-between">
                    <span className="muted text-xs">
                      {task.status === "pending" ? "排队中" : getTimingText(task, nowMs)}
                    </span>
                    <button type="button"
                      className="button secondary danger btn-sm"
                      onClick={() => handleCancel(task.id)}
                    >
                      取消
                    </button>
                  </div>
                </>
              )}

              {task.status === "done" && (
                <div className="flex-between">
                  <span className="muted text-xs">
                    {task.output === null
                      ? "产物已删除"
                      : `输出: ${formatBytes(task.output.size)}${task.delete_source ? " · 已删除源文件" : ""}`}
                  </span>
                  <button type="button"
                    className="button secondary danger btn-sm"
                    onClick={() => handleDelete(task.id)}
                  >
                    删除
                  </button>
                </div>
              )}

              {task.status === "failed" && (
                <div>
                  {task.error_code && (
                    <p className="text-xs text-danger mb-2">{task.error_code}</p>
                  )}
                  <div className="flex flex-end">
                    <button type="button"
                      className="button secondary btn-sm"
                      onClick={() => handleDelete(task.id)}
                    >
                      删除
                    </button>
                  </div>
                </div>
              )}

              {task.status === "cancelled" && (
                <div className="flex flex-end">
                  <button type="button"
                    className="button secondary btn-sm"
                    onClick={() => handleDelete(task.id)}
                  >
                    删除
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}
