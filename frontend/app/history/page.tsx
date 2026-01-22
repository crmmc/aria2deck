"use client";

import { useEffect, useState } from "react";
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

        {tasks.length === 0 ? (
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
                      width: 100,
                    }}
                  >
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
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
                      <button
                        className="button secondary danger"
                        onClick={() => handleDelete(task.id)}
                        style={{
                          padding: "4px 12px",
                          fontSize: 12,
                          height: 28,
                        }}
                      >
                        删除
                      </button>
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
