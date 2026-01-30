"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "@/lib/api";
import type { Task } from "@/types";
import { useToast } from "@/components/Toast";
import StatsWidget from "@/components/StatsWidget";
import { useTaskWebSocket } from "@/hooks/useTaskWebSocket";
import {
  sendTaskCompleteNotification,
  sendTaskErrorNotification,
} from "@/lib/notification";

import { formatBytes } from "@/lib/utils";

function getTaskDisplayName(task: Task): string {
  return task.name || "未知文件";
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

const TaskCard = memo(function TaskCard({
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
    onCopyUri(task.uri!);
  }, [task.uri, onCopyUri]);

  const handleCancelClick = useCallback(() => {
    onCancel(task.id);
  }, [task.id, onCancel]);

  const handleRetryClick = useCallback(() => {
    onRetry(task);
  }, [task, onRetry]);

  return (
    <div
      className={`card${task.uri ? " cursor-pointer" : ""}`}
      onClick={handleCardClick}
    >
      <div className={`task-card-inner${isSelected ? " selected" : ""}`}>
        <div>
          <div className="space-between flex-start mb-3">
            <div className="task-card-header">
              <input
                type="checkbox"
                checked={isSelected}
                onChange={handleCheckboxChange}
                onClick={(e) => e.stopPropagation()}
                className="checkbox-sm mt-2 cursor-pointer"
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
            {task.total_length > 0 && task.status !== "error" && (
              <span className="muted tabular-nums text-sm">
                {((task.completed_length / task.total_length) * 100).toFixed(1)}%
              </span>
            )}
            {task.error && (
              <span className="text-danger text-sm" title={task.error}>
                {task.error}
              </span>
            )}
          </div>

          <div className="task-footer-right">
            {task.uri && (
              <button
                className="button secondary btn-task"
                onClick={handleCopyClick}
                title="复制链接"
              >
                复制
              </button>
            )}
            {task.status === "error" && task.uri && (
              <button
                className="button secondary btn-task"
                onClick={handleRetryClick}
                title="重新下载"
              >
                重试
              </button>
            )}
            {(task.status === "active" || task.status === "queued") && (
              <button
                className={`button secondary danger btn-task${isOperating ? " opacity-60" : ""}`}
                onClick={handleCancelClick}
                disabled={isOperating}
              >
                {isOperating ? "处理中..." : "取消"}
              </button>
            )}
            {task.status === "error" && (
              <button
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
    </div>
  );
});

export default function TasksPage() {
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [uri, setUri] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set());
  const [filterStatus, setFilterStatus] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("tasks_filterStatus") || "all";
    }
    return "all";
  });
  const [searchKeyword, setSearchKeyword] = useState("");
  const [showBatchAddModal, setShowBatchAddModal] = useState(false);
  const [batchUris, setBatchUris] = useState("");
  const [mounted, setMounted] = useState(false);
  const torrentInputRef = useRef<HTMLInputElement>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const [operatingTaskIds, setOperatingTaskIds] = useState<Set<number>>(
    new Set()
  );
  const [wsConnected, setWsConnected] = useState(false);

  const deletedTaskIdsRef = useRef<Set<number>>(new Set());
  const tasksRef = useRef<Task[]>([]);
  tasksRef.current = tasks;

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem("tasks_filterStatus", filterStatus);
    }
  }, [filterStatus]);

  useEffect(() => {
    api
      .listTasks("current")
      .then((currentTasks) => {
        setTasks(currentTasks);
      })
      .catch((err) => {
        showToast("加载任务失败: " + (err as Error).message, "error");
      });
  }, [showToast]);

  useEffect(() => {
    if (wsConnected) return;

    const pollInterval = setInterval(() => {
      api
        .listTasks("active")
        .then((activeTasks) => {
          setTasks((prev) => {
            // Keep failed tasks from previous state
            const failedTasks = prev.filter((t) => t.status === "error");
            const activeMap = new Map(activeTasks.map((t) => [t.id, t]));

            const updatedActive = activeTasks.filter(
              (t) => !deletedTaskIdsRef.current.has(t.id)
            );

            const prevActive = prev.filter(
              (t) => t.status === "active" || t.status === "queued"
            );
            for (const t of prevActive) {
              if (!activeMap.has(t.id) && !deletedTaskIdsRef.current.has(t.id)) {
                // Task disappeared, fetch current list to check if it failed
                api.listTasks("current").then((currentTasks) => {
                  setTasks(currentTasks);
                }).catch(() => {});
                break;
              }
            }

            deletedTaskIdsRef.current.clear();
            return [...updatedActive, ...failedTasks];
          });
        })
        .catch(() => {});
    }, 5000);

    return () => clearInterval(pollInterval);
  }, [wsConnected]);

  const handleTaskUpdate = useCallback((newTask: Task) => {
    const taskId = newTask.id;

    if (deletedTaskIdsRef.current.has(taskId)) {
      return;
    }

    // Only show active, queued, and error tasks
    const isActiveStatus =
      newTask.status === "active" || newTask.status === "queued";
    const isErrorStatus = newTask.status === "error";

    setTasks((prev) => {
      const idx = prev.findIndex((task) => task.id === taskId);
      const oldTask = idx !== -1 ? prev[idx] : null;

      if (oldTask) {
        const taskName = newTask.name || "下载任务";
        if (oldTask.status !== "complete" && newTask.status === "complete") {
          sendTaskCompleteNotification(taskName, newTask.id);
          showToast(`${taskName} 下载完成`, "success");
          // Remove completed task from list (it goes to history)
          if (idx !== -1) {
            const next = [...prev];
            next.splice(idx, 1);
            return next;
          }
          return prev;
        } else if (oldTask.status !== "error" && newTask.status === "error") {
          sendTaskErrorNotification(taskName, newTask.id);
          showToast(`${taskName} 下载失败`, "error");
        }
      }

      // Remove completed tasks from the list
      if (newTask.status === "complete") {
        if (idx !== -1) {
          const next = [...prev];
          next.splice(idx, 1);
          return next;
        }
        return prev;
      }

      // Keep active, queued, and error tasks
      if (!isActiveStatus && !isErrorStatus) {
        if (idx !== -1) {
          const next = [...prev];
          next.splice(idx, 1);
          return next;
        }
        return prev;
      }

      if (idx === -1) return [newTask, ...prev];
      const next = [...prev];
      next[idx] = newTask;
      return next;
    });
  }, [showToast]);

  const handleNotification = useCallback(
    (message: string, level: "info" | "warning" | "error") => {
      showToast(message, level);
    },
    [showToast]
  );

  const handleWsConnected = useCallback(() => {
    api.listTasks("current").then((currentTasks) => {
      setTasks(currentTasks);
    }).catch(() => {});
    setWsConnected(true);
  }, []);

  const handleWsDisconnected = useCallback(() => {
    setWsConnected(false);
  }, []);

  useTaskWebSocket({
    onTaskUpdate: handleTaskUpdate,
    onNotification: handleNotification,
    onConnected: handleWsConnected,
    onDisconnected: handleWsDisconnected,
  });

  const createTask = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (isSubmitting) return;
      setError(null);
      setIsSubmitting(true);
      try {
        const task = await api.createTask(uri);
        if (task.status === "active" || task.status === "queued") {
          setTasks((prev) => [task, ...prev]);
        }
        setUri("");
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setIsSubmitting(false);
      }
    },
    [uri, isSubmitting]
  );

  const handleTorrentUpload = useCallback(
    async (event: React.ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      if (!file) return;

      setError(null);

      if (!file.name.endsWith(".torrent")) {
        setError("请选择 .torrent 文件");
        return;
      }

      try {
        const base64Content = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => {
            const result = reader.result as string;
            const base64 = result.split(",")[1];
            resolve(base64);
          };
          reader.onerror = () => reject(new Error("文件读取失败"));
          reader.readAsDataURL(file);
        });

        const task = await api.uploadTorrent(base64Content);
        if (task.status === "active" || task.status === "queued") {
          setTasks((prev) => [task, ...prev]);
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        if (torrentInputRef.current) {
          torrentInputRef.current.value = "";
        }
      }
    },
    []
  );

  const cancelTask = useCallback(
    async (id: number) => {
      const task = tasksRef.current.find((t) => t.id === id);
      if (!task) return;

      const isFailedTask = task.status === "error";
      const confirmed = await showConfirm({
        title: isFailedTask ? "删除任务" : "取消下载",
        message: isFailedTask
          ? `确定要删除失败任务 "${getTaskDisplayName(task)}" 吗？`
          : `确定要取消下载 "${getTaskDisplayName(task)}" 吗？`,
        confirmText: isFailedTask ? "删除" : "取消下载",
        danger: true,
      });
      if (!confirmed) return;

      setOperatingTaskIds((prev) => {
        if (prev.has(id)) return prev;
        return new Set(prev).add(id);
      });

      try {
        await api.cancelTask(id);
        deletedTaskIdsRef.current.add(id);
        setTasks((prev) => prev.filter((t) => t.id !== id));
        setSelectedTasks((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      } catch (err) {
        showToast((isFailedTask ? "删除" : "取消") + "失败：" + (err as Error).message, "error");
      } finally {
        setOperatingTaskIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [showConfirm, showToast]
  );

  const retryTask = useCallback(
    async (task: Task) => {
      if (!task.uri) return;

      try {
        const newTask = await api.createTask(task.uri);
        if (newTask.status === "active" || newTask.status === "queued") {
          setTasks((prev) => [newTask, ...prev]);
        }
        showToast("已重新添加下载任务", "success");
      } catch (err) {
        showToast("重试失败：" + (err as Error).message, "error");
      }
    },
    [showToast]
  );

  const batchCancelTasks = useCallback(async () => {
    if (selectedTasks.size === 0 || isBatchOperating) return;

    const activeTasks = tasksRef.current.filter(
      (t) =>
        selectedTasks.has(t.id) &&
        (t.status === "active" || t.status === "queued")
    );
    if (activeTasks.length === 0) {
      showToast("没有可取消的任务", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "批量取消",
      message: `确定要取消选中的 ${activeTasks.length} 个任务吗？`,
      confirmText: "取消",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await Promise.all(activeTasks.map((t) => api.cancelTask(t.id)));
      const cancelledIds = new Set(activeTasks.map((t) => t.id));
      cancelledIds.forEach((id) => deletedTaskIdsRef.current.add(id));
      setTasks((prev) => prev.filter((t) => !cancelledIds.has(t.id)));
      setSelectedTasks(new Set());
      showToast(`已取消 ${activeTasks.length} 个任务`, "success");
    } catch (err) {
      showToast("批量取消失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }, [selectedTasks, isBatchOperating, showConfirm, showToast]);

  const batchDeleteFailed = useCallback(async () => {
    const failedTasks = tasksRef.current.filter(
      (t) => selectedTasks.has(t.id) && t.status === "error"
    );
    if (failedTasks.length === 0) {
      showToast("没有可删除的失败任务", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "删除失败任务",
      message: `确定要删除选中的 ${failedTasks.length} 个失败任务吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await Promise.all(failedTasks.map((t) => api.cancelTask(t.id)));
      const deletedIds = new Set(failedTasks.map((t) => t.id));
      deletedIds.forEach((id) => deletedTaskIdsRef.current.add(id));
      setTasks((prev) => prev.filter((t) => !deletedIds.has(t.id)));
      setSelectedTasks(new Set());
      showToast(`已删除 ${failedTasks.length} 个失败任务`, "success");
    } catch (err) {
      showToast("删除失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }, [selectedTasks, showConfirm, showToast]);

  const copyUri = useCallback(
    (uri: string) => {
      navigator.clipboard
        .writeText(uri)
        .then(() => {
          showToast("链接已复制", "success");
        })
        .catch(() => {
          showToast("复制失败", "error");
        });
    },
    [showToast]
  );

  const batchAddTasks = useCallback(async () => {
    const uris = batchUris
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);

    if (uris.length === 0) {
      showToast("请输入至少一个链接", "warning");
      return;
    }

    setError(null);
    let successCount = 0;
    let failCount = 0;

    for (const uri of uris) {
      try {
        const task = await api.createTask(uri);
        if (task.status === "active" || task.status === "queued") {
          setTasks((prev) => [task, ...prev]);
        }
        successCount++;
      } catch (err) {
        failCount++;
        console.error(`Failed to add ${uri}:`, err);
      }
    }

    setBatchUris("");
    setShowBatchAddModal(false);

    if (failCount > 0) {
      showToast(
        `添加完成：成功 ${successCount} 个，失败 ${failCount} 个`,
        "warning"
      );
    } else {
      showToast(`成功添加 ${successCount} 个任务`, "success");
    }
  }, [batchUris, showToast]);

  const toggleTaskSelection = useCallback((id: number) => {
    setSelectedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const filteredTasks = useMemo(() => {
    let filtered = tasks;

    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter(
        (t) => t.name && t.name.toLowerCase().includes(keyword)
      );
    }

    if (filterStatus === "active") {
      filtered = filtered.filter(
        (t) => t.status === "active" || t.status === "queued"
      );
    } else if (filterStatus === "error") {
      filtered = filtered.filter((t) => t.status === "error");
    }

    return filtered;
  }, [tasks, searchKeyword, filterStatus]);

  const toggleSelectAll = useCallback(() => {
    if (selectedTasks.size === filteredTasks.length) {
      setSelectedTasks(new Set());
    } else {
      setSelectedTasks(new Set(filteredTasks.map((t) => t.id)));
    }
  }, [selectedTasks.size, filteredTasks]);

  const hasActiveTasks = useMemo(
    () =>
      tasks.some(
        (t) =>
          selectedTasks.has(t.id) &&
          (t.status === "active" || t.status === "queued")
      ),
    [tasks, selectedTasks]
  );

  const hasFailedTasks = useMemo(
    () =>
      tasks.some(
        (t) => selectedTasks.has(t.id) && t.status === "error"
      ),
    [tasks, selectedTasks]
  );

  return (
    <>
      <div className="glass-frame full-height animate-in">
        <div className="space-between mb-7">
          <div>
            <h1 className="text-2xl">任务</h1>
            <p className="muted">管理您的下载</p>
          </div>
        </div>

        <StatsWidget />

        <div className="card add-task-card">
          <form onSubmit={createTask} className="add-task-form">
            <input
              className="input add-task-input"
              placeholder="粘贴磁力链接、HTTP 或 FTP URL..."
              value={uri}
              onChange={(event) => setUri(event.target.value)}
              required
            />
            <input
              type="file"
              ref={torrentInputRef}
              accept=".torrent"
              onChange={handleTorrentUpload}
              className="hidden"
            />
            <button
              className={`button flex-shrink-0 shadow-none${isSubmitting ? " opacity-60" : ""}`}
              type="submit"
              disabled={isSubmitting}
            >
              {isSubmitting ? "添加中..." : "+ 添加任务"}
            </button>
            <button
              className="button secondary flex-shrink-0 shadow-none"
              type="button"
              onClick={() => setShowBatchAddModal(true)}
            >
              批量添加
            </button>
            <button
              className="button secondary flex-shrink-0 shadow-none"
              type="button"
              onClick={() => torrentInputRef.current?.click()}
              title="上传种子文件"
            >
              上传种子
            </button>
          </form>
          {error ? <div className="form-error">{error}</div> : null}
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
              <option value="all">当前任务</option>
              <option value="active">进行中</option>
              <option value="error">失败</option>
            </select>
          </div>

          <div className="filter-group ml-auto">
            {selectedTasks.size > 0 && (
              <>
                <span className="muted text-sm">
                  已选 {selectedTasks.size} 项
                </span>
                {hasActiveTasks && (
                  <button
                    type="button"
                    className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                    onClick={batchCancelTasks}
                    disabled={isBatchOperating}
                  >
                    取消下载
                  </button>
                )}
                {hasFailedTasks && (
                  <button
                    type="button"
                    className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                    onClick={batchDeleteFailed}
                    disabled={isBatchOperating}
                  >
                    删除失败
                  </button>
                )}
              </>
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
          {filteredTasks.length === 0 && (
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
                  <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
                  <polyline points="13 2 13 9 20 9" />
                </svg>
              </div>
              <p className="font-medium mb-1">暂无活动任务</p>
              <p className="muted text-base">
                添加新任务开始下载，已完成的文件请前往文件页面查看
              </p>
            </div>
          )}

          {filteredTasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              isSelected={selectedTasks.has(task.id)}
              isOperating={operatingTaskIds.has(task.id)}
              onToggleSelection={toggleTaskSelection}
              onCancel={cancelTask}
              onCopyUri={copyUri}
              onRetry={retryTask}
            />
          ))}
        </div>
      </div>

      {mounted &&
        showBatchAddModal &&
        createPortal(
          <div
            className="modal-overlay"
            onClick={() => setShowBatchAddModal(false)}
          >
            <div
              className="batch-modal-content"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="modal-header">
                <h2 className="m-0">批量添加任务</h2>
                <button
                  type="button"
                  onClick={() => setShowBatchAddModal(false)}
                  className="modal-close-btn"
                >
                  ×
                </button>
              </div>

              <p className="muted text-sm mb-3">
                每行输入一个链接，支持磁力链接、HTTP 或 FTP URL
              </p>

              <textarea
                value={batchUris}
                onChange={(e) => setBatchUris(e.target.value)}
                placeholder="magnet:?xt=urn:btih:...&#10;https://example.com/file1.zip&#10;https://example.com/file2.zip"
                className="batch-textarea"
              />

              <div className="modal-footer">
                <button
                  type="button"
                  className="button secondary btn-task"
                  onClick={() => {
                    setShowBatchAddModal(false);
                    setBatchUris("");
                  }}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="button btn-task"
                  onClick={batchAddTasks}
                >
                  添加任务
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}
    </>
  );
}
