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

  const loadTasks = useCallback(async () => {
    try {
      const data = await api.listPackTasks();
      setTasks(data);
    } catch (err) {
      console.error("Failed to load pack tasks:", err);
    }
  }, []);

  useEffect(() => {
    loadTasks();
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
  }, [tasks, loadTasks]);

  useEffect(() => {
    const hasJustCompleted = tasks.some((t) => t.status === "done");
    if (hasJustCompleted && onTaskComplete) {
      onTaskComplete();
    }
  }, [tasks, onTaskComplete]);

  useEffect(() => {
    return () => {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
      }
    };
  }, []);

  const handleMouseEnter = () => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    setExpanded(true);
    setTimeout(() => setVisible(true), 10);
  };

  const handleMouseLeave = () => {
    hideTimerRef.current = setTimeout(() => {
      setVisible(false);
      setTimeout(() => setExpanded(false), 400);
    }, 1200);
  };

  const activeTasks = tasks.filter(
    (t) => t.status === "pending" || t.status === "packing"
  );

  const handleDownload = async (task: PackTask) => {
    try {
      const response = await fetch(api.downloadPackResult(task.id), {
        credentials: "include",
      });
      if (!response.ok) {
        if (response.status === 404) {
          showToast("打包文件已被删除", "error");
          loadTasks();
          return;
        }
        const data = await response.json().catch(() => ({}));
        showToast(`下载失败: ${data.detail || response.statusText}`, "error");
        return;
      }
      const contentDisposition = response.headers.get("content-disposition");
      let filename = "download";
      if (contentDisposition) {
        const match = contentDisposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';\n]+)/i);
        if (match) {
          filename = decodeURIComponent(match[1]);
        }
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    } catch (err) {
      showToast(`下载失败: ${(err as Error).message}`, "error");
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
      await api.cancelPackTask(taskId);
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
    if (task.output_path) {
      const parts = task.output_path.split("/");
      return parts[parts.length - 1];
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
      <button className="button secondary pack-task-btn">
        <span>打包任务</span>
        {activeTasks.length > 0 && (
          <span className="pack-task-badge">{activeTasks.length}</span>
        )}
      </button>

      {expanded && (
        <div className={`card pack-dropdown ${visible ? "pack-dropdown-visible" : "pack-dropdown-hidden"}`}>
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
                    <button
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
                  </span>
                  <div className="flex gap-2">
                    <button
                      className="button secondary btn-sm"
                      onClick={() => handleDownload(task)}
                    >
                      下载
                    </button>
                    <button
                      className="button secondary danger btn-sm"
                      onClick={() => handleDelete(task.id)}
                    >
                      删除
                    </button>
                  </div>
                </div>
              )}

              {task.status === "failed" && (
                <div>
                  {task.error_message && (
                    <p className="text-xs text-danger mb-2">{task.error_message}</p>
                  )}
                  <div className="flex flex-end">
                    <button
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
                  <button
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
