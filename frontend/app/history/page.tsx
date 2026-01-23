"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
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
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState<string>("all");
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
          t.status === "stopped",
      );
      setTasks(historyTasks);
      setLoading(false);
    } catch (err) {
      router.push("/login");
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("确定要删除此历史记录吗？")) return;
    try {
      await api.deleteTask(id);
      setTasks(tasks.filter((t) => t.id !== id));
    } catch (err) {
      alert("删除失败");
    }
  }

  async function handleClearAll() {
    if (!confirm("确定要清空所有历史记录吗？此操作不可恢复！")) return;
    try {
      const result = await api.clearHistory();
      alert(`已清空 ${result.count} 条历史记录`);
      setTasks([]);
    } catch (err) {
      alert("清空失败");
    }
  }

  async function handleRetry(task: Task) {
    // 种子任务无法重试
    if (task.uri === "[torrent]") {
      alert("种子任务无法直接重试，请重新上传种子文件");
      return;
    }

    // 立即乐观移除旧任务
    setTasks((prev) => prev.filter((t) => t.id !== task.id));

    try {
      await api.retryTask(task.id);
      // 强制刷新列表，确保状态一致
      const allTasks = await api.listTasks();
      const historyTasks = allTasks.filter(
        (t) =>
          t.status === "complete" ||
          t.status === "error" ||
          t.status === "stopped",
      );
      setTasks(historyTasks);
      alert("任务已重新开始，请前往任务页面查看进度");
    } catch (err) {
      // 失败时恢复列表（通过刷新）
      const allTasks = await api.listTasks();
      const historyTasks = allTasks.filter(
        (t) =>
          t.status === "complete" ||
          t.status === "error" ||
          t.status === "stopped",
      );
      setTasks(historyTasks);
      alert("重试失败：" + (err as Error).message);
    }
  }

  const filteredAndSortedTasks = useMemo(() => {
    let filtered = tasks;

    // 筛选
    if (filterStatus !== "all") {
      filtered = tasks.filter((t) => t.status === filterStatus);
    }

    // 排序
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
  }, [tasks, filterStatus, sortBy, sortOrder]);

  if (loading) return null;

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="space-between" style={{ marginBottom: 32 }}>
          <div>
            <h1 style={{ marginBottom: 8 }}>任务历史</h1>
            <p className="muted">查看已完成、错误或已停止的任务。</p>
          </div>
          {tasks.length > 0 && (
            <button
              className="button secondary danger"
              onClick={handleClearAll}
              style={{ height: "fit-content" }}
            >
              清空历史
            </button>
          )}
        </div>

        {/* 筛选和排序工具栏 */}
        {tasks.length > 0 && (
          <div
            className="card"
            style={{
              marginBottom: 16,
              padding: "12px 16px",
              display: "flex",
              gap: "16px",
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <span className="muted" style={{ fontSize: "13px" }}>
                筛选:
              </span>
              <select
                value={filterStatus}
                onChange={(e) => setFilterStatus(e.target.value)}
                style={{
                  padding: "6px 12px",
                  fontSize: "13px",
                  border: "1px solid rgba(0,0,0,0.1)",
                  borderRadius: "6px",
                  background: "white",
                  cursor: "pointer",
                }}
              >
                <option value="all">全部</option>
                <option value="complete">已完成</option>
                <option value="error">错误</option>
                <option value="stopped">已停止</option>
              </select>
            </div>

            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <span className="muted" style={{ fontSize: "13px" }}>
                排序:
              </span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                style={{
                  padding: "6px 12px",
                  fontSize: "13px",
                  border: "1px solid rgba(0,0,0,0.1)",
                  borderRadius: "6px",
                  background: "white",
                  cursor: "pointer",
                }}
              >
                <option value="time">完成时间</option>
                <option value="size">文件大小</option>
              </select>
              <button
                type="button"
                onClick={() => setSortOrder(sortOrder === "asc" ? "desc" : "asc")}
                style={{
                  padding: "6px 12px",
                  fontSize: "13px",
                  border: "1px solid rgba(0,0,0,0.1)",
                  borderRadius: "6px",
                  background: "white",
                  cursor: "pointer",
                }}
              >
                {sortOrder === "asc" ? "↑" : "↓"}
              </button>
            </div>

            <div className="muted" style={{ marginLeft: "auto", fontSize: "13px" }}>
              共 {filteredAndSortedTasks.length} 条记录
            </div>
          </div>
        )}

        {filteredAndSortedTasks.length === 0 ? (
          <div
            className="card"
            style={{ textAlign: "center", padding: "48px 0" }}
          >
            <p className="muted">暂无历史记录。</p>
          </div>
        ) : (
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: 14,
              }}
            >
              <thead
                style={{
                  background: "rgba(0,0,0,0.03)",
                  borderBottom: "1px solid rgba(0,0,0,0.05)",
                }}
              >
                <tr>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "left",
                      fontWeight: 600,
                      color: "var(--muted)",
                    }}
                  >
                    任务名称
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "left",
                      fontWeight: 600,
                      color: "var(--muted)",
                      width: 100,
                    }}
                  >
                    状态
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "right",
                      fontWeight: 600,
                      color: "var(--muted)",
                      width: 120,
                    }}
                  >
                    大小
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "right",
                      fontWeight: 600,
                      color: "var(--muted)",
                      width: 160,
                    }}
                  >
                    完成时间
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "right",
                      fontWeight: 600,
                      color: "var(--muted)",
                      width: 130,
                    }}
                  >
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {filteredAndSortedTasks.map((task) => (
                  <tr
                    key={task.id}
                    style={{ borderBottom: "1px solid rgba(0,0,0,0.05)" }}
                  >
                    <td style={{ padding: "12px 16px" }}>
                      <div style={{ fontWeight: 500, marginBottom: 4 }}>
                        {task.name || "未命名任务"}
                      </div>
                      <div
                        className="muted"
                        style={{
                          fontSize: 12,
                          wordBreak: "break-all",
                          maxWidth: 400,
                        }}
                      >
                        {task.uri}
                      </div>
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <span
                        className={`badge ${
                          task.status === "complete"
                            ? "complete"
                            : task.status === "error"
                              ? "error"
                              : ""
                        }`}
                      >
                        {task.status === "complete"
                          ? "已完成"
                          : task.status === "error"
                            ? "错误"
                            : "已停止"}
                      </span>
                    </td>
                    <td
                      style={{
                        padding: "12px 16px",
                        textAlign: "right",
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      {formatBytes(task.total_length)}
                    </td>
                    <td
                      style={{
                        padding: "12px 16px",
                        textAlign: "right",
                        fontSize: 13,
                        color: "var(--muted)",
                      }}
                    >
                      {formatDate(task.updated_at)}
                    </td>
                    <td style={{ padding: "12px 16px", textAlign: "right" }}>
                      <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end", flexShrink: 0 }}>
                        {task.status === "error" && (
                          <button
                            className="button secondary"
                            onClick={() => handleRetry(task)}
                            style={{
                              padding: "4px 12px",
                              fontSize: 12,
                              height: 28,
                              whiteSpace: "nowrap",
                            }}
                          >
                            重试
                          </button>
                        )}
                        <button
                          className="button secondary danger"
                          onClick={() => handleDelete(task.id)}
                          style={{
                            padding: "4px 12px",
                            fontSize: 12,
                            height: 28,
                            whiteSpace: "nowrap",
                          }}
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
