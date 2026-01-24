"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import AuthLayout from "@/components/AuthLayout";
import type { Task } from "@/types";

function formatBytes(value: number) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = value;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

function formatDate(dateStr: string) {
  const date = new Date(dateStr);
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function HistoryPage() {
  const router = useRouter();
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [sortBy, setSortBy] = useState<string>("time");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");

  useEffect(() => {
    loadHistory();
  }, []);

  async function loadHistory() {
    try {
      await api.me();
      const allTasks = await api.listTasks();
      const historyTasks = allTasks.filter(
        (t) =>
          t.status === "complete" ||
          t.status === "error" ||
          t.status === "stopped" ||
          t.status === "removed",
      );
      setTasks(historyTasks);
      setLoading(false);
    } catch {
      router.push("/login");
    }
  }

  async function handleDelete(id: number) {
    const confirmed = await showConfirm({
      title: "删除历史记录",
      message: "确定要删除此历史记录吗？",
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api.deleteTask(id);
      setTasks(tasks.filter((t) => t.id !== id));
    } catch {
      showToast("删除失败", "error");
    }
  }

  async function handleClearAll() {
    const confirmed = await showConfirm({
      title: "清空历史记录",
      message: "确定要清空所有历史记录吗？此操作不可恢复！",
      confirmText: "清空",
      danger: true,
    });
    if (!confirmed) return;
    try {
      const result = await api.clearHistory();
      showToast(`已清空 ${result.count} 条历史记录`, "success");
      setTasks([]);
    } catch {
      showToast("清空失败", "error");
    }
  }

  async function handleRetry(task: Task) {
    if (task.uri === "[torrent]") {
      showToast("种子任务无法直接重试，请重新上传种子文件", "warning");
      return;
    }

    setTasks((prev) => prev.filter((t) => t.id !== task.id));

    try {
      await api.retryTask(task.id);
      const allTasks = await api.listTasks();
      const historyTasks = allTasks.filter(
        (t) =>
          t.status === "complete" ||
          t.status === "error" ||
          t.status === "stopped" ||
          t.status === "removed",
      );
      setTasks(historyTasks);
      showToast("任务已重新开始，请前往任务页面查看进度", "success");
    } catch (err) {
      const allTasks = await api.listTasks();
      const historyTasks = allTasks.filter(
        (t) =>
          t.status === "complete" ||
          t.status === "error" ||
          t.status === "stopped" ||
          t.status === "removed",
      );
      setTasks(historyTasks);
      showToast("重试失败：" + (err as Error).message, "error");
    }
  }

  const filteredAndSortedTasks = useMemo(() => {
    let filtered = tasks;

    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter(
        (t) =>
          (t.name && t.name.toLowerCase().includes(keyword)) ||
          (t.uri && t.uri.toLowerCase().includes(keyword))
      );
    }

    if (filterStatus !== "all") {
      filtered = filtered.filter((t) => t.status === filterStatus);
    }

    const sorted = [...filtered].sort((a, b) => {
      let comparison = 0;

      switch (sortBy) {
        case "time":
          comparison =
            new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime();
          break;
        case "size":
          comparison = a.total_length - b.total_length;
          break;
        default:
          comparison = 0;
      }

      return sortOrder === "asc" ? comparison : -comparison;
    });

    return sorted;
  }, [tasks, searchKeyword, filterStatus, sortBy, sortOrder]);

  if (loading) return null;

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="flex-between mb-7">
          <div>
            <h1 className="mb-2">任务历史</h1>
            <p className="muted">查看已完成、错误或已停止的任务。</p>
          </div>
          {tasks.length > 0 && (
            <button
              className="button secondary danger"
              onClick={handleClearAll}
            >
              清空历史
            </button>
          )}
        </div>

        {tasks.length > 0 && (
          <div className="card toolbar mb-4">
            <div className="flex gap-2 items-center">
              <input
                type="text"
                placeholder="搜索任务..."
                value={searchKeyword}
                onChange={(e) => setSearchKeyword(e.target.value)}
                className="search-input"
              />
            </div>

            <div className="flex gap-2 items-center">
              <span className="muted text-sm">筛选:</span>
              <select
                value={filterStatus}
                onChange={(e) => setFilterStatus(e.target.value)}
                className="select"
              >
                <option value="all">全部</option>
                <option value="complete">已完成</option>
                <option value="error">错误</option>
                <option value="stopped">已停止</option>
                <option value="removed">已删除</option>
              </select>
            </div>

            <div className="flex gap-2 items-center">
              <span className="muted text-sm">排序:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="select"
              >
                <option value="time">完成时间</option>
                <option value="size">文件大小</option>
              </select>
              <button
                type="button"
                onClick={() => setSortOrder(sortOrder === "asc" ? "desc" : "asc")}
                className="sort-btn"
              >
                {sortOrder === "asc" ? "↑" : "↓"}
              </button>
            </div>

            <div className="muted ml-auto text-sm">
              共 {filteredAndSortedTasks.length} 条记录
            </div>
          </div>
        )}

        {filteredAndSortedTasks.length === 0 ? (
          <div className="card text-center py-8">
            <p className="muted">暂无历史记录。</p>
          </div>
        ) : (
          <div className="card p-0 overflow-hidden">
            <table className="table">
              <thead className="table-header">
                <tr>
                  <th className="table-cell text-left">任务名称</th>
                  <th className="table-cell text-left" style={{ width: 100 }}>状态</th>
                  <th className="table-cell text-right" style={{ width: 120 }}>大小</th>
                  <th className="table-cell text-right" style={{ width: 160 }}>完成时间</th>
                  <th className="table-cell text-right" style={{ width: 130 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredAndSortedTasks.map((task) => (
                  <tr key={task.id} className="table-row">
                    <td className="table-cell">
                      <div
                        className="font-medium mb-1 truncate"
                        style={{ maxWidth: 400 }}
                        title={task.name || task.uri}
                      >
                        {task.name || "未命名任务"}
                      </div>
                      <div
                        className="muted text-xs truncate"
                        style={{ maxWidth: 400 }}
                        title={task.uri}
                      >
                        {task.uri}
                      </div>
                    </td>
                    <td className="table-cell">
                      <span
                        className={`badge ${
                          task.status === "complete"
                            ? "complete"
                            : task.status === "error"
                              ? "error"
                              : task.status === "removed"
                                ? "removed"
                                : ""
                        }`}
                      >
                        {task.status === "complete"
                          ? "已完成"
                          : task.status === "error"
                            ? "错误"
                            : task.status === "removed"
                              ? "已删除"
                              : "已停止"}
                      </span>
                    </td>
                    <td className="table-cell text-right tabular-nums">
                      {formatBytes(task.total_length)}
                    </td>
                    <td className="table-cell text-right text-sm muted">
                      {formatDate(task.updated_at)}
                    </td>
                    <td className="table-cell text-right">
                      <div className="flex gap-2 flex-end flex-shrink-0">
                        {task.uri !== "[torrent]" && (
                          <button
                            className="button secondary btn-sm"
                            onClick={() => {
                              navigator.clipboard.writeText(task.uri);
                            }}
                            title="复制下载链接"
                          >
                            复制
                          </button>
                        )}
                        {task.status === "error" && (
                          <button
                            className="button secondary btn-sm"
                            onClick={() => handleRetry(task)}
                          >
                            重试
                          </button>
                        )}
                        <button
                          className="button secondary danger btn-sm"
                          onClick={() => handleDelete(task.id)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </AuthLayout>
  );
}
