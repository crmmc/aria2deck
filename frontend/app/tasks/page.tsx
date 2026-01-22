"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { api, taskWsUrl } from "@/lib/api";
import type { Task } from "@/types";
import StatsWidget from "@/components/StatsWidget";
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

export default function TasksPage() {
  const router = useRouter();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [uri, setUri] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set());
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [sortBy, setSortBy] = useState<string>("time");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [showBatchAddModal, setShowBatchAddModal] = useState(false);
  const [batchUris, setBatchUris] = useState("");
  const [deleteConfirmModal, setDeleteConfirmModal] = useState<{
    taskId: number;
    taskName: string;
    isComplete: boolean;
  } | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);

  useEffect(() => {
    api
      .listTasks()
      .then(setTasks)
      .catch(() => null);
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let pingInterval: ReturnType<typeof setInterval>;

    function connect() {
      ws = new WebSocket(taskWsUrl());

      ws.onopen = () => {
        pingInterval = setInterval(() => {
          if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
        }, 15000);
      };

      ws.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type === "task_update") {
          setTasks((prev) => {
            const idx = prev.findIndex((task) => task.id === payload.task.id);
            if (idx === -1) return [payload.task, ...prev];
            const next = [...prev];
            next[idx] = payload.task;
            return next;
          });
        }
      };

      ws.onerror = () => {
        ws?.close();
      };

      ws.onclose = () => {
        clearInterval(pingInterval);
        reconnectTimeout = setTimeout(connect, 3000);
      };
    }

    connect();

    return () => {
      clearTimeout(reconnectTimeout);
      clearInterval(pingInterval);
      ws?.close();
    };
  }, []);

  async function createTask(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      const task = await api.createTask(uri);
      setTasks((prev) => [task, ...prev]);
      setUri("");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function pauseTask(id: number) {
    try {
      await api.updateTaskStatus(id, "pause");
    } catch (err) {
      console.error(err);
    }
  }

  async function resumeTask(id: number) {
    try {
      await api.updateTaskStatus(id, "resume");
    } catch (err) {
      console.error(err);
    }
  }

  async function removeTask(id: number) {
    const task = tasks.find((t) => t.id === id);
    if (!task) return;

    // 显示删除确认对话框
    setDeleteConfirmModal({
      taskId: id,
      taskName: getTaskDisplayName(task),
      isComplete: task.status === "complete",
    });
    setDeleteFiles(false);
  }

  async function confirmDeleteTask() {
    if (!deleteConfirmModal) return;

    try {
      await api.deleteTask(deleteConfirmModal.taskId, deleteFiles);
      setTasks((prev) =>
        prev.filter((t) => t.id !== deleteConfirmModal.taskId),
      );
      setSelectedTasks((prev) => {
        const next = new Set(prev);
        next.delete(deleteConfirmModal.taskId);
        return next;
      });
      setDeleteConfirmModal(null);
      setDeleteFiles(false);
    } catch (err) {
      console.error(err);
      alert("删除失败：" + (err as Error).message);
    }
  }

  async function batchDeleteTasks() {
    if (selectedTasks.size === 0) return;

    // 检查是否有未完成的任务
    const selectedTasksList = tasks.filter((t) => selectedTasks.has(t.id));
    const hasIncompleteTasks = selectedTasksList.some(
      (t) => t.status !== "complete",
    );

    const message = hasIncompleteTasks
      ? `确定要删除选中的 ${selectedTasks.size} 个任务吗？\n\n警告：删除任务会同时删除任务相关联的文件！`
      : `确定要删除选中的 ${selectedTasks.size} 个任务吗？`;

    if (!confirm(message)) return;

    try {
      // 批量删除时，未完成的任务会删除文件
      await Promise.all(
        Array.from(selectedTasks).map((id) => {
          const task = tasks.find((t) => t.id === id);
          const shouldDeleteFiles = task ? task.status !== "complete" : false;
          return api.deleteTask(id, shouldDeleteFiles);
        }),
      );
      setTasks((prev) => prev.filter((t) => !selectedTasks.has(t.id)));
      setSelectedTasks(new Set());
    } catch (err) {
      console.error(err);
      alert("批量删除失败：" + (err as Error).message);
    }
  }

  async function batchPauseTasks() {
    if (selectedTasks.size === 0) return;
    const activeTasks = tasks.filter(
      (t) =>
        selectedTasks.has(t.id) &&
        (t.status === "active" || t.status === "waiting"),
    );
    if (activeTasks.length === 0) {
      alert("没有可暂停的任务");
      return;
    }
    try {
      await Promise.all(
        activeTasks.map((t) => api.updateTaskStatus(t.id, "pause")),
      );
    } catch (err) {
      console.error(err);
    }
  }

  async function batchResumeTasks() {
    if (selectedTasks.size === 0) return;
    const pausedTasks = tasks.filter(
      (t) => selectedTasks.has(t.id) && t.status === "paused",
    );
    if (pausedTasks.length === 0) {
      alert("没有可继续的任务");
      return;
    }
    try {
      await Promise.all(
        pausedTasks.map((t) => api.updateTaskStatus(t.id, "resume")),
      );
    } catch (err) {
      console.error(err);
    }
  }

  async function batchAddTasks() {
    const uris = batchUris
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);

    if (uris.length === 0) {
      alert("请输入至少一个链接");
      return;
    }

    setError(null);
    let successCount = 0;
    let failCount = 0;

    for (const uri of uris) {
      try {
        const task = await api.createTask(uri);
        setTasks((prev) => [task, ...prev]);
        successCount++;
      } catch (err) {
        failCount++;
        console.error(`Failed to add ${uri}:`, err);
      }
    }

    setBatchUris("");
    setShowBatchAddModal(false);

    if (failCount > 0) {
      alert(`添加完成：成功 ${successCount} 个，失败 ${failCount} 个`);
    } else {
      alert(`成功添加 ${successCount} 个任务`);
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
    if (selectedTasks.size === filteredAndSortedTasks.length) {
      setSelectedTasks(new Set());
    } else {
      setSelectedTasks(new Set(filteredAndSortedTasks.map((t) => t.id)));
    }
  }

  function getRemainingTime(task: Task): number {
    if (task.status !== "active" || task.download_speed === 0) return Infinity;
    const remaining = task.total_length - task.completed_length;
    return remaining / task.download_speed;
  }

  const filteredAndSortedTasks = useMemo(() => {
    let filtered = tasks;

    // 筛选
    if (filterStatus === "active") {
      filtered = tasks.filter(
        (t) => t.status === "active" || t.status === "waiting",
      );
    } else if (filterStatus === "complete") {
      filtered = tasks.filter((t) => t.status === "complete");
    } else if (filterStatus === "error") {
      filtered = tasks.filter((t) => t.status === "error");
    }

    // 排序
    const sorted = [...filtered].sort((a, b) => {
      let comparison = 0;

      switch (sortBy) {
        case "speed":
          comparison = a.download_speed - b.download_speed;
          break;
        case "time":
          comparison =
            new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
          break;
        case "remaining":
          comparison = getRemainingTime(a) - getRemainingTime(b);
          break;
        default:
          comparison = 0;
      }

      return sortOrder === "asc" ? comparison : -comparison;
    });

    return sorted;
  }, [tasks, filterStatus, sortBy, sortOrder]);

  const activeCount = useMemo(
    () => tasks.filter((task) => task.status === "active").length,
    [tasks],
  );

  function getStatusClass(status: string) {
    if (status === "active") return "badge active";
    if (status === "complete") return "badge complete";
    if (status === "error") return "badge error";
    return "badge";
  }

  function getTaskDisplayName(task: Task): string {
    if (task.name) return task.name;
    // 如果没有名称，显示 URI，过长则截断
    const uri = task.uri;
    if (uri.length > 60) {
      return uri.substring(0, 57) + "...";
    }
    return uri;
  }

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="space-between" style={{ marginBottom: 32 }}>
          <div>
            <h1 style={{ fontSize: "28px" }}>任务</h1>
            <p className="muted">管理您的下载</p>
          </div>
        </div>

        <StatsWidget />

        <div
          className="card"
          style={{
            marginBottom: 16,
            padding: "8px",
            background: "rgba(255,255,255,0.9)",
          }}
        >
          <form onSubmit={createTask} style={{ display: "flex", gap: "8px" }}>
            <input
              className="input"
              style={{
                border: "none",
                background: "transparent",
                padding: "12px 16px",
              }}
              placeholder="粘贴磁力链接、HTTP 或 FTP URL..."
              value={uri}
              onChange={(event) => setUri(event.target.value)}
              required
            />
            <button
              className="button"
              type="submit"
              style={{ flexShrink: 0, boxShadow: "none" }}
            >
              + 添加任务
            </button>
            <button
              className="button secondary"
              type="button"
              onClick={() => setShowBatchAddModal(true)}
              style={{ flexShrink: 0, boxShadow: "none" }}
            >
              批量添加
            </button>
          </form>
          {error ? (
            <div
              style={{
                padding: "0 16px 12px",
                color: "#ff3b30",
                fontSize: "13px",
              }}
            >
              {error}
            </div>
          ) : null}
        </div>

        {/* 筛选和排序工具栏 */}
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
              <option value="active">进行中</option>
              <option value="complete">已完成</option>
              <option value="error">错误</option>
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
              <option value="time">添加时间</option>
              <option value="speed">下载速度</option>
              <option value="remaining">剩余时间</option>
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

          <div style={{ marginLeft: "auto", display: "flex", gap: "8px" }}>
            {selectedTasks.size > 0 && (
              <>
                <span className="muted" style={{ fontSize: "13px" }}>
                  已选 {selectedTasks.size} 项
                </span>
                <button
                  type="button"
                  className="button secondary"
                  onClick={batchPauseTasks}
                  style={{ padding: "6px 12px", fontSize: "13px" }}
                >
                  暂停
                </button>
                <button
                  type="button"
                  className="button secondary"
                  onClick={batchResumeTasks}
                  style={{ padding: "6px 12px", fontSize: "13px" }}
                >
                  继续
                </button>
                <button
                  type="button"
                  className="button secondary danger"
                  onClick={batchDeleteTasks}
                  style={{ padding: "6px 12px", fontSize: "13px" }}
                >
                  删除
                </button>
              </>
            )}
            <button
              type="button"
              className="button secondary"
              onClick={toggleSelectAll}
              style={{ padding: "6px 12px", fontSize: "13px" }}
            >
              {selectedTasks.size === filteredAndSortedTasks.length &&
              filteredAndSortedTasks.length > 0
                ? "取消全选"
                : "全选"}
            </button>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
          {filteredAndSortedTasks.map((task) => (
            <div
              className="card"
              key={task.id}
              style={{
                display: "flex",
                flexDirection: "column",
                justifyContent: "space-between",
                cursor: "pointer",
                transition: "transform 0.2s ease, box-shadow 0.2s ease",
                border: selectedTasks.has(task.id)
                  ? "2px solid #0071e3"
                  : "1px solid rgba(0,0,0,0.1)",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.transform = "translateY(-2px)";
                e.currentTarget.style.boxShadow = "0 4px 12px rgba(0,0,0,0.1)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.transform = "translateY(0)";
                e.currentTarget.style.boxShadow = "none";
              }}
            >
              <div>
                <div
                  className="space-between"
                  style={{ alignItems: "flex-start", marginBottom: 12 }}
                >
                  <div
                    style={{
                      display: "flex",
                      gap: "12px",
                      alignItems: "flex-start",
                      overflow: "hidden",
                      flex: 1,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selectedTasks.has(task.id)}
                      onChange={() => toggleTaskSelection(task.id)}
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        marginTop: "4px",
                        cursor: "pointer",
                        width: "16px",
                        height: "16px",
                      }}
                    />
                    <div
                      style={{ overflow: "hidden", flex: 1 }}
                      onClick={() => router.push(`/tasks/detail?id=${task.id}`)}
                    >
                      <h3
                        style={{
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          fontSize: "16px",
                          marginBottom: "4px",
                        }}
                        title={task.name || task.uri}
                      >
                        {getTaskDisplayName(task)}
                      </h3>
                      <div className="muted" style={{ fontSize: "13px" }}>
                        {formatBytes(task.completed_length)} /{" "}
                        {formatBytes(task.total_length)}
                      </div>
                    </div>
                  </div>
                  <span className={getStatusClass(task.status)}>
                    {task.status}
                  </span>
                </div>

                {/* Progress Bar */}
                <div
                  style={{
                    height: "6px",
                    background: "rgba(0,0,0,0.05)",
                    borderRadius: "3px",
                    marginBottom: "12px",
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${task.total_length ? (task.completed_length / task.total_length) * 100 : 0}%`,
                      background:
                        task.status === "error"
                          ? "#ff3b30"
                          : task.status === "complete"
                            ? "#34c759"
                            : "#0071e3",
                      transition: "width 0.3s ease",
                    }}
                  />
                </div>

                {/* 左下角速度显示 */}
                {task.status === "active" && task.download_speed > 0 && (
                  <div
                    className="muted"
                    style={{
                      fontSize: "12px",
                      marginBottom: "8px",
                      display: "flex",
                      alignItems: "center",
                      gap: "4px",
                    }}
                  >
                    <span style={{ color: "#34c759" }}>↓</span>
                    <span>{formatBytes(task.download_speed)}/s</span>
                  </div>
                )}
              </div>

              <div
                style={{
                  display: "flex",
                  gap: "8px",
                  marginTop: "auto",
                  justifyContent: "flex-end",
                }}
                onClick={(e) => e.stopPropagation()}
              >
                {task.status === "active" || task.status === "waiting" ? (
                  <button
                    className="button secondary"
                    style={{
                      padding: "8px 16px",
                      fontSize: "13px",
                      minWidth: "64px",
                    }}
                    onClick={() => pauseTask(task.id)}
                  >
                    暂停
                  </button>
                ) : task.status === "paused" ? (
                  <button
                    className="button secondary"
                    style={{
                      padding: "8px 16px",
                      fontSize: "13px",
                      minWidth: "64px",
                    }}
                    onClick={() => resumeTask(task.id)}
                  >
                    继续
                  </button>
                ) : null}

                <button
                  className="button secondary danger"
                  style={{
                    padding: "8px 16px",
                    fontSize: "13px",
                    minWidth: "64px",
                  }}
                  onClick={() => removeTask(task.id)}
                >
                  删除
                </button>

                <Link
                  className="button secondary"
                  style={{
                    padding: "8px 16px",
                    textAlign: "center",
                    fontSize: "13px",
                    minWidth: "64px",
                    textDecoration: "none",
                  }}
                  href={`/tasks/detail?id=${task.id}`}
                  onClick={(e) => e.stopPropagation()}
                >
                  详情
                </Link>
              </div>
            </div>
          ))}
        </div>

        {/* 批量添加任务弹窗 */}
        {showBatchAddModal && (
          <div
            style={{
              position: "fixed",
              top: 0,
              left: 0,
              right: 0,
              bottom: 0,
              background: "rgba(0,0,0,0.5)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              zIndex: 1000,
            }}
            onClick={() => setShowBatchAddModal(false)}
          >
            <div
              className="card"
              style={{
                width: "90%",
                maxWidth: "600px",
                maxHeight: "80vh",
                display: "flex",
                flexDirection: "column",
                background: "white",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "16px",
                }}
              >
                <h2 style={{ margin: 0 }}>批量添加任务</h2>
                <button
                  type="button"
                  onClick={() => setShowBatchAddModal(false)}
                  style={{
                    background: "none",
                    border: "none",
                    fontSize: "24px",
                    cursor: "pointer",
                    color: "#666",
                  }}
                >
                  ×
                </button>
              </div>

              <p
                className="muted"
                style={{ fontSize: "13px", marginBottom: "12px" }}
              >
                每行输入一个链接，支持磁力链接、HTTP 或 FTP URL
              </p>

              <textarea
                value={batchUris}
                onChange={(e) => setBatchUris(e.target.value)}
                placeholder="magnet:?xt=urn:btih:...&#10;https://example.com/file1.zip&#10;https://example.com/file2.zip"
                style={{
                  flex: 1,
                  minHeight: "300px",
                  padding: "12px",
                  border: "1px solid rgba(0,0,0,0.1)",
                  borderRadius: "6px",
                  fontSize: "13px",
                  fontFamily: "monospace",
                  resize: "vertical",
                  marginBottom: "16px",
                }}
              />

              <div
                style={{
                  display: "flex",
                  gap: "8px",
                  justifyContent: "flex-end",
                }}
              >
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setShowBatchAddModal(false);
                    setBatchUris("");
                  }}
                  style={{ padding: "8px 16px" }}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="button"
                  onClick={batchAddTasks}
                  style={{ padding: "8px 16px" }}
                >
                  添加任务
                </button>
              </div>
            </div>
          </div>
        )}

        {/* 删除确认对话框 */}
        {deleteConfirmModal && (
          <div
            style={{
              position: "fixed",
              top: 0,
              left: 0,
              right: 0,
              bottom: 0,
              background: "rgba(0,0,0,0.5)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              zIndex: 1000,
            }}
            onClick={() => {
              setDeleteConfirmModal(null);
              setDeleteFiles(false);
            }}
          >
            <div
              className="card"
              style={{
                width: "90%",
                maxWidth: "500px",
                background: "white",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <div style={{ marginBottom: "16px" }}>
                <h2 style={{ margin: 0, marginBottom: "8px" }}>确认删除任务</h2>
                <p className="muted" style={{ fontSize: "14px", margin: 0 }}>
                  {deleteConfirmModal.taskName}
                </p>
              </div>

              {deleteConfirmModal.isComplete ? (
                <div style={{ marginBottom: "16px" }}>
                  <label
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "8px",
                      cursor: "pointer",
                      padding: "12px",
                      border: "1px solid rgba(0,0,0,0.1)",
                      borderRadius: "6px",
                      background: deleteFiles
                        ? "rgba(0,113,227,0.05)"
                        : "transparent",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={deleteFiles}
                      onChange={(e) => setDeleteFiles(e.target.checked)}
                      style={{
                        width: "18px",
                        height: "18px",
                        cursor: "pointer",
                      }}
                    />
                    <span style={{ fontSize: "14px" }}>同时删除下载的文件</span>
                  </label>
                </div>
              ) : (
                <div
                  style={{
                    marginBottom: "16px",
                    padding: "12px",
                    background: "rgba(255, 59, 48, 0.1)",
                    borderRadius: "6px",
                    border: "1px solid rgba(255, 59, 48, 0.3)",
                  }}
                >
                  <p
                    style={{
                      margin: 0,
                      color: "#ff3b30",
                      fontSize: "14px",
                      fontWeight: 500,
                    }}
                  >
                    ⚠️ 此任务未完成，删除任务会同时删除未完成的文件
                  </p>
                </div>
              )}

              <div
                style={{
                  display: "flex",
                  gap: "8px",
                  justifyContent: "flex-end",
                }}
              >
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setDeleteConfirmModal(null);
                    setDeleteFiles(false);
                  }}
                  style={{ padding: "8px 16px" }}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="button danger"
                  onClick={confirmDeleteTask}
                  style={{ padding: "8px 16px", background: "#ff3b30" }}
                >
                  确认删除
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </AuthLayout>
  );
}
