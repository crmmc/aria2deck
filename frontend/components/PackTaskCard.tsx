"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import type { PackTask } from "@/types";

interface PackTaskCardProps {
  onTaskComplete?: () => void;
}

export default function PackTaskCard({ onTaskComplete }: PackTaskCardProps) {
  const [tasks, setTasks] = useState<PackTask[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [visible, setVisible] = useState(false);
  const hideTimerRef = useRef<NodeJS.Timeout | null>(null);

  // Load tasks
  const loadTasks = useCallback(async () => {
    try {
      const data = await api.listPackTasks();
      setTasks(data);
    } catch (err) {
      console.error("Failed to load pack tasks:", err);
    }
  }, []);

  // Initial load
  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  // Poll for updates when there are active tasks
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

  // Check for task completion and trigger callback
  useEffect(() => {
    const hasJustCompleted = tasks.some((t) => t.status === "done");
    if (hasJustCompleted && onTaskComplete) {
      onTaskComplete();
    }
  }, [tasks, onTaskComplete]);

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
      }
    };
  }, []);

  const handleMouseEnter = () => {
    // 清除隐藏定时器
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    setExpanded(true);
    // 短暂延迟后显示（触发淡入动画）
    setTimeout(() => setVisible(true), 10);
  };

  const handleMouseLeave = () => {
    // 延迟 1.2s 后隐藏
    hideTimerRef.current = setTimeout(() => {
      setVisible(false);
      // 等待淡出动画完成后再移除 DOM
      setTimeout(() => setExpanded(false), 400);
    }, 1200);
  };

  const activeTasks = tasks.filter(
    (t) => t.status === "pending" || t.status === "packing"
  );

  const handleCancel = async (taskId: number) => {
    if (!confirm("确定要取消此打包任务吗?")) return;
    try {
      await api.cancelPackTask(taskId);
      loadTasks();
    } catch (err) {
      alert(`取消失败: ${(err as Error).message}`);
    }
  };

  const handleDelete = async (taskId: number) => {
    if (!confirm("确定要删除此任务记录吗?")) return;
    try {
      await api.cancelPackTask(taskId);
      loadTasks();
    } catch (err) {
      alert(`删除失败: ${(err as Error).message}`);
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "packing":
        return "#0071e3";
      case "done":
        return "#34c759";
      case "failed":
        return "#ff3b30";
      case "cancelled":
        return "#8e8e93";
      default:
        return "#ff9500";
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case "pending":
        return "排队中...";
      case "packing":
        return "打包中";
      case "done":
        return "已完成";
      case "failed":
        return "失败";
      case "cancelled":
        return "已取消";
      default:
        return status;
    }
  };

  // 获取显示名称（处理多文件 JSON 格式）
  const getDisplayName = (task: PackTask) => {
    // 如果有 output_path，从中提取文件名
    if (task.output_path) {
      const parts = task.output_path.split("/");
      return parts[parts.length - 1];
    }
    // 如果 folder_path 是 JSON 数组格式，说明是多文件打包
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
      style={{ position: "relative" }}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <button
        className="button secondary"
        style={{
          padding: "8px 16px",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span>打包任务</span>
        {activeTasks.length > 0 && (
          <span
            style={{
              background: "#0071e3",
              color: "white",
              borderRadius: 10,
              padding: "2px 8px",
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            {activeTasks.length}
          </span>
        )}
      </button>

      {expanded && (
        <div
          className="card"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: 8,
            width: 360,
            maxHeight: 400,
            overflowY: "auto",
            zIndex: 100,
            padding: 0,
            opacity: visible ? 1 : 0,
            transform: visible ? "translateY(0)" : "translateY(-8px)",
            transition: "opacity 0.4s ease, transform 0.4s ease",
          }}
        >
          {tasks.map((task) => (
            <div
              key={task.id}
              style={{
                padding: 16,
                borderBottom: "1px solid rgba(0,0,0,0.05)",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                <span style={{ fontWeight: 500, fontSize: 14 }}>
                  {getDisplayName(task)}
                </span>
                <span
                  style={{
                    fontSize: 12,
                    color: getStatusColor(task.status),
                    fontWeight: 600,
                  }}
                >
                  {getStatusText(task.status)}
                </span>
              </div>

              {(task.status === "pending" || task.status === "packing") && (
                <>
                  <div
                    style={{
                      height: 4,
                      background: "rgba(0,0,0,0.05)",
                      borderRadius: 2,
                      marginBottom: 8,
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${task.progress}%`,
                        background: "#0071e3",
                        transition: "width 0.3s ease",
                      }}
                    />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {task.progress}% - 已预留: {formatBytes(task.reserved_space)}
                    </span>
                    <button
                      className="button secondary danger"
                      style={{ padding: "4px 12px", fontSize: 12 }}
                      onClick={() => handleCancel(task.id)}
                    >
                      取消
                    </button>
                  </div>
                </>
              )}

              {task.status === "done" && (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    输出: {formatBytes(task.output_size || 0)}
                  </span>
                  <div style={{ display: "flex", gap: 8 }}>
                    <a
                      className="button secondary"
                      style={{ padding: "4px 12px", fontSize: 12, textDecoration: "none" }}
                      href={api.downloadPackResult(task.id)}
                      download
                    >
                      下载
                    </a>
                    <button
                      className="button secondary danger"
                      style={{ padding: "4px 12px", fontSize: 12 }}
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
                    <p style={{ margin: "0 0 8px 0", fontSize: 12, color: "#ff3b30" }}>
                      {task.error_message}
                    </p>
                  )}
                  <div style={{ display: "flex", justifyContent: "flex-end" }}>
                    <button
                      className="button secondary"
                      style={{ padding: "4px 12px", fontSize: 12 }}
                      onClick={() => handleDelete(task.id)}
                    >
                      删除
                    </button>
                  </div>
                </div>
              )}

              {task.status === "cancelled" && (
                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  <button
                    className="button secondary"
                    style={{ padding: "4px 12px", fontSize: 12 }}
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
