"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { Task } from "@/types";
import { useToast } from "@/components/Toast";
import AuthLayout from "@/components/AuthLayout";

function formatBytes(value?: number | null) {
  if (value === 0 || value == null || Number.isNaN(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = value;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

function getTaskDisplayName(task: Task): string {
  return task.name || "未知文件";
}

interface HistoryCardProps {
  task: Task;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (task: Task) => void;
}

function HistoryCard({
  task,
  isSelected,
  onToggleSelection,
  onCopyUri,
  onRetry,
}: HistoryCardProps) {
  const handleCardClick = () => {
    if (task.uri) {
      onCopyUri(task.uri);
    }
  };

  return (
    <div
      className="card"
      onClick={handleCardClick}
      style={{ cursor: task.uri ? "pointer" : "default" }}
    >
      <div className={`task-card-inner${isSelected ? " selected" : ""}`}>
        <div>
          <div className="space-between flex-start mb-3">
            <div className="task-card-header">
              <input
                type="checkbox"
                checked={isSelected}
                onChange={() => onToggleSelection(task.id)}
                onClick={(e) => e.stopPropagation()}
                className="checkbox-sm mt-2 cursor-pointer"
              />
              <div className="overflow-hidden flex-1">
                <h3 className="task-name" title={task.name || undefined}>
                  {getTaskDisplayName(task)}
                </h3>
                <div className="muted tabular-nums text-sm">
                  {formatBytes(task.total_length)}
                </div>
              </div>
            </div>
            <span
              className={`task-status task-status-${task.status}`}
              style={{ marginLeft: "auto" }}
            >
              {task.status === "complete" ? "已完成" : "失败"}
            </span>
          </div>

          {task.error && (
            <div className="text-danger text-sm mb-3" title={task.error}>
              {task.error}
            </div>
          )}
        </div>

        <div className="task-card-footer" onClick={(e) => e.stopPropagation()}>
          <div className="task-footer-left">
            <span className="muted text-sm">
              {new Date(task.created_at).toLocaleString()}
            </span>
          </div>

          <div className="task-footer-right">
            {task.uri && (
              <button
                className="button secondary btn-task"
                onClick={() => onCopyUri(task.uri!)}
                title="复制链接"
              >
                复制
              </button>
            )}
            {task.status === "error" && task.uri && (
              <button
                className="button secondary btn-task"
                onClick={() => onRetry(task)}
                title="重新下载"
              >
                重试
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function HistoryPage() {
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set());
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [isBatchOperating, setIsBatchOperating] = useState(false);

  useEffect(() => {
    loadHistory();
  }, []);

  async function loadHistory() {
    setLoading(true);
    try {
      const allTasks = await api.listTasks();
      // 只保留历史任务（已完成或失败）
      const historyTasks = allTasks.filter(
        (t) => t.status === "complete" || t.status === "error"
      );
      setTasks(historyTasks);
    } catch (err) {
      showToast("加载历史失败", "error");
    } finally {
      setLoading(false);
    }
  }

  function copyUri(uri: string) {
    navigator.clipboard
      .writeText(uri)
      .then(() => {
        showToast("链接已复制", "success");
      })
      .catch(() => {
        showToast("复制失败", "error");
      });
  }

  async function retryTask(task: Task) {
    if (!task.uri) return;

    try {
      await api.createTask(task.uri);
      showToast("已重新添加下载任务", "success");
    } catch (err) {
      showToast("重试失败：" + (err as Error).message, "error");
    }
  }

  async function batchDeleteHistory() {
    const selectedList = tasks.filter((t) => selectedTasks.has(t.id));
    if (selectedList.length === 0) {
      showToast("请先选择要删除的记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "删除历史",
      message: `确定要删除选中的 ${selectedList.length} 条历史记录吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await Promise.all(selectedList.map((t) => api.cancelTask(t.id)));
      setTasks((prev) => prev.filter((t) => !selectedTasks.has(t.id)));
      setSelectedTasks(new Set());
      showToast(`已删除 ${selectedList.length} 条历史记录`, "success");
    } catch (err) {
      showToast("删除失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }

  async function clearAllHistory() {
    if (tasks.length === 0) {
      showToast("没有历史记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "清空历史",
      message: `确定要清空全部 ${tasks.length} 条历史记录吗？`,
      confirmText: "清空",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await api.clearHistory();
      setTasks([]);
      setSelectedTasks(new Set());
      showToast(`已清空全部历史记录`, "success");
    } catch (err) {
      showToast("清空失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }

  function toggleTaskSelection(id: number) {
    setSelectedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function toggleSelectAll() {
    if (selectedTasks.size === filteredTasks.length) {
      setSelectedTasks(new Set());
    } else {
      setSelectedTasks(new Set(filteredTasks.map((t) => t.id)));
    }
  }

  const filteredTasks = useMemo(() => {
    let filtered = tasks;

    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter(
        (t) => t.name && t.name.toLowerCase().includes(keyword)
      );
    }

    if (filterStatus === "complete") {
      filtered = filtered.filter((t) => t.status === "complete");
    } else if (filterStatus === "error") {
      filtered = filtered.filter((t) => t.status === "error");
    }

    return filtered;
  }, [tasks, searchKeyword, filterStatus]);

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="space-between mb-7">
          <div>
            <h1 className="text-2xl">任务历史</h1>
            <p className="muted">查看已完成和失败的下载任务</p>
          </div>
        </div>

        <div className="card filter-toolbar">
          <div className="filter-group">
            <input
              type="text"
              placeholder="搜索任务..."
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
              className="search-input"
            />
          </div>

          <div className="filter-group">
            <span className="muted text-sm">筛选:</span>
            <select
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
              className="select"
            >
              <option value="all">全部</option>
              <option value="complete">已完成</option>
              <option value="error">失败</option>
            </select>
          </div>

          <div className="filter-group ml-auto">
            {selectedTasks.size > 0 && (
              <>
                <span className="muted text-sm">
                  已选 {selectedTasks.size} 项
                </span>
                <button
                  type="button"
                  className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                  onClick={batchDeleteHistory}
                  disabled={isBatchOperating}
                >
                  删除
                </button>
              </>
            )}
            {tasks.length > 0 && (
              <button
                type="button"
                className={`button secondary btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                onClick={clearAllHistory}
                disabled={isBatchOperating}
              >
                清空历史
              </button>
            )}
            <button
              type="button"
              className="button secondary btn-sm"
              onClick={toggleSelectAll}
            >
              {selectedTasks.size === filteredTasks.length &&
              filteredTasks.length > 0
                ? "取消全选"
                : "全选"}
            </button>
          </div>
        </div>

        <div className="task-list">
          {loading ? (
            <div className="empty-state">
              <p className="muted">加载中...</p>
            </div>
          ) : filteredTasks.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">
                <svg
                  width="48"
                  height="48"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <circle cx="12" cy="12" r="10" />
                  <polyline points="12 6 12 12 16 14" />
                </svg>
              </div>
              <p className="font-medium mb-1">暂无历史记录</p>
              <p className="muted text-base">
                完成的下载任务将显示在这里
              </p>
            </div>
          ) : (
            filteredTasks.map((task) => (
              <HistoryCard
                key={task.id}
                task={task}
                isSelected={selectedTasks.has(task.id)}
                onToggleSelection={toggleTaskSelection}
                onCopyUri={copyUri}
                onRetry={retryTask}
              />
            ))
          )}
        </div>
      </div>
    </AuthLayout>
  );
}
