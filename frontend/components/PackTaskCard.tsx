"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { useToast } from "@/components/Toast";
import type { PackTask } from "@/types";

interface PackTaskCardProps {
  onTaskComplete?: () => void;
}

export default function PackTaskCard({ onTaskComplete }: PackTaskCardProps) {
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<PackTask[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [visible, setVisible] = useState(false);
  const hideTimerRef = useRef<NodeJS.Timeout | null>(null);
  const collapseTimerRef = useRef<NodeJS.Timeout | null>(null);
  const expandTimerRef = useRef<NodeJS.Timeout | null>(null);
  const timersRef = useRef<Set<NodeJS.Timeout>>(new Set());
  const taskStatusRef = useRef<Map<number, PackTask["status"]>>(new Map());
  const onTaskCompleteRef = useRef(onTaskComplete);
  onTaskCompleteRef.current = onTaskComplete;

  const loadTasks = useCallback(async () => {
    try {
      const data = await api.listPackTasks();
      const previousStatuses = taskStatusRef.current;
      const completedFromActive = data.some((task) => {
        const previousStatus = previousStatuses.get(task.id);
        return (
          task.status === "done" &&
          (previousStatus === "pending" || previousStatus === "packing")
        );
      });
      taskStatusRef.current = new Map(data.map((task) => [task.id, task.status]));
      setTasks(data);
      if (completedFromActive) {
        onTaskCompleteRef.current?.();
      }
    } catch (err) {
      console.error("Failed to load pack tasks:", err);
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    const hasActiveTasks = tasks.some(
      (t) => t.status === "pending" || t.status === "packing"
    );

    if (hasActiveTasks) {
      const interval = setInterval(() => {
        loadTasks();
      }, 2000);
      return () => clearInterval(interval);
    }
    return undefined;
  }, [tasks, loadTasks]);

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach(clearTimeout);
    };
  }, []);

  const handleMouseEnter = () => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      timersRef.current.delete(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    if (collapseTimerRef.current) {
      clearTimeout(collapseTimerRef.current);
      timersRef.current.delete(collapseTimerRef.current);
      collapseTimerRef.current = null;
    }
    setExpanded(true);
    const expandTimer = setTimeout(() => {
      setVisible(true);
      timersRef.current.delete(expandTimer);
    }, 10);
    expandTimerRef.current = expandTimer;
    timersRef.current.add(expandTimer);
  };

  const handleMouseLeave = () => {
    const hideTimer = setTimeout(() => {
      setVisible(false);
      timersRef.current.delete(hideTimer);
      const collapseTimer = setTimeout(() => {
        setExpanded(false);
        timersRef.current.delete(collapseTimer);
      }, 400);
      collapseTimerRef.current = collapseTimer;
      timersRef.current.add(collapseTimer);
    }, 1200);
    hideTimerRef.current = hideTimer;
    timersRef.current.add(hideTimer);
  };

  const activeTasks = tasks.filter(
    (t) => t.status === "pending" || t.status === "packing"
  );

  const terminalTasks = tasks.filter(
    (t) => t.status === "done" || t.status === "failed" || t.status === "cancelled"
  );

  const handleClearAll = async () => {
    try {
      const result = await api.clearPackTasks();
      showToast(`已清空 ${result.count} 条记录`, "success");
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
      await api.cancelPackTask(taskId);
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
      await api.deletePackTask(taskId);
      loadTasks();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "packing": return "var(--primary)";
      case "done": return "var(--success)";
      case "failed": return "var(--danger)";
      case "cancelled": return "var(--gray)";
      default: return "var(--warning)";
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case "pending": return "排队中...";
      case "packing": return "打包中";
      case "done": return "已完成";
      case "failed": return "失败";
      case "cancelled": return "已取消";
      default: return status;
    }
  };

  const getDisplayName = (task: PackTask) => {
    if (task.output_name) {
      return task.output_name;
    }
    if (task.folder_path.startsWith("[")) {
      try {
        const paths = JSON.parse(task.folder_path) as string[];
        return `${paths.length} 个文件`;
      } catch {
        return task.folder_path;
      }
    }
    return task.folder_path;
  };

  if (tasks.length === 0) return null;

  return (
    <div
      className="relative"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <button type="button" className="button secondary pack-task-btn">
        <span>打包任务</span>
        {activeTasks.length > 0 && (
          <span className="pack-task-badge">{activeTasks.length}</span>
        )}
      </button>

      {expanded && (
        <div className={`card pack-dropdown ${visible ? "pack-dropdown-visible" : "pack-dropdown-hidden"}`}>
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
                      style={{ width: `${task.progress}%` }}
                    />
                  </div>
                  <div className="flex-between">
                    <span className="muted text-xs">
                      {task.progress}% - 已预留: {formatBytes(task.reserved_space)}
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
                    输出: {formatBytes(task.output_size || 0)}
                    {task.delete_source && " · 已删除源文件"}
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
                  {task.error_message && (
                    <p className="text-xs text-danger mb-2">{task.error_message}</p>
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
        </div>
      )}
    </div>
  );
}
