"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";

import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { api, taskWsUrl } from "@/lib/api";
import type { Task } from "@/types";
import { useToast } from "@/components/Toast";
import StatsWidget from "@/components/StatsWidget";
import AuthLayout from "@/components/AuthLayout";
import {
  sendTaskCompleteNotification,
  sendTaskErrorNotification,
} from "@/lib/notification";

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
  return task.name || task.uri;
}

interface SortableTaskCardProps {
  task: Task;
  isSelected: boolean;
  isRetrying: boolean;
  isOperating: boolean;
  onToggleSelection: (id: number) => void;
  onPause: (id: number) => void;
  onResume: (id: number) => void;
  onRemove: (id: number) => void;
  onRetry: (task: Task) => void;
  onNavigate: (id: number) => void;
}

function SortableTaskCard({
  task,
  isSelected,
  isRetrying,
  isOperating,
  onToggleSelection,
  onPause,
  onResume,
  onRemove,
  onRetry,
  onNavigate,
}: SortableTaskCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: task.gid || String(task.id), disabled: task.status !== "waiting" });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    zIndex: isDragging ? 1000 : "auto",
  };

  const canDrag = task.status === "waiting";

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="card"
      key={task.id}
      {...attributes}
    >
      <div
        className={`task-card-inner${isSelected ? " selected" : ""}`}
      >
        <div>
            <div
              className="space-between flex-start mb-3"
            >
            <div className="task-card-header">
              {canDrag && (
                <div
                  {...listeners}
                  className="drag-handle"
                  title="拖拽排序"
                >
                  ⋮⋮
                </div>
              )}
              <input
                type="checkbox"
                checked={isSelected}
                onChange={() => onToggleSelection(task.id)}
                onClick={(e) => e.stopPropagation()}
                className="checkbox-sm mt-2 cursor-pointer"
              />
              <div
                className="overflow-hidden flex-1"
                onClick={() => onNavigate(task.id)}
              >
                <h3
                  className="task-name"
                  title={task.name || task.uri}
                >
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
                    : task.status === "complete"
                      ? "progress-bar-success"
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
            <span
              className={`task-status task-status-${task.status}`}
            >
              {task.status === "active"
                ? "下载中"
                : task.status === "waiting"
                  ? "等待中"
                  : task.status === "paused"
                    ? "已暂停"
                    : task.status === "complete"
                      ? "已完成"
                      : task.status === "error"
                        ? "错误"
                        : task.status}
            </span>
            {task.total_length > 0 && task.status !== "complete" && (
              <span className="muted tabular-nums text-sm">
                {((task.completed_length / task.total_length) * 100).toFixed(1)}%
              </span>
            )}
            {task.status === "error" && task.error && (
              <span
                className="task-error-text"
                title={task.error}
              >
                {task.error}
              </span>
            )}
          </div>

          <div className="task-footer-right">
            {task.status === "active" || task.status === "waiting" ? (
              <button
                className={`button secondary btn-task${isOperating ? " opacity-60" : ""}`}
                onClick={() => onPause(task.id)}
                disabled={isOperating}
              >
                {isOperating ? "处理中..." : "暂停"}
              </button>
            ) : task.status === "paused" ? (
              <button
                className={`button secondary btn-task${isOperating ? " opacity-60" : ""}`}
                onClick={() => onResume(task.id)}
                disabled={isOperating}
              >
                {isOperating ? "处理中..." : "继续"}
              </button>
            ) : task.status === "error" ? (
              <button
                className={`button secondary btn-task${isRetrying ? " opacity-60" : ""}`}
                onClick={() => onRetry(task)}
                disabled={isRetrying}
              >
                {isRetrying ? "重试中..." : "重试"}
              </button>
            ) : null}

            <button
              className="button secondary danger btn-task"
              onClick={() => onRemove(task.id)}
            >
              删除
            </button>

            <Link
              className="button secondary btn-task btn-link"
              href={`/tasks/detail?id=${task.id}`}
              onClick={(e) => e.stopPropagation()}
            >
              详情
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function TasksPage() {
  const router = useRouter();
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [uri, setUri] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set());
  // 从 localStorage 恢复筛选/排序状态
  const [filterStatus, setFilterStatus] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("tasks_filterStatus") || "all";
    }
    return "all";
  });
  const [searchKeyword, setSearchKeyword] = useState("");
  const [sortBy, setSortBy] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("tasks_sortBy") || "time";
    }
    return "time";
  });
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("tasks_sortOrder");
      return saved === "asc" ? "asc" : "desc";
    }
    return "desc";
  });
  const [showBatchAddModal, setShowBatchAddModal] = useState(false);
  const [batchUris, setBatchUris] = useState("");
  const [deleteConfirmModal, setDeleteConfirmModal] = useState<{
    taskId: number;
    taskName: string;
    isComplete: boolean;
  } | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [retryingTaskIds, setRetryingTaskIds] = useState<Set<number>>(new Set());
  const [mounted, setMounted] = useState(false);
  const torrentInputRef = useRef<HTMLInputElement>(null);
  // 加载状态
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const [operatingTaskIds, setOperatingTaskIds] = useState<Set<number>>(new Set());

  // 已删除任务 ID 集合，用于过滤 WebSocket 推送的旧任务更新
  const deletedTaskIdsRef = useRef<Set<number>>(new Set());

  // 客户端挂载后才能使用 Portal
  useEffect(() => {
    setMounted(true);
  }, []);

  // 持久化筛选/排序状态到 localStorage
  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem("tasks_filterStatus", filterStatus);
    }
  }, [filterStatus]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem("tasks_sortBy", sortBy);
    }
  }, [sortBy]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem("tasks_sortOrder", sortOrder);
    }
  }, [sortOrder]);

  // DnD sensors
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 8, // 需要拖动 8px 才开始拖拽，防止误触
      },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  // 无闪烁更新：合并任务列表，保留顺序，移除已删除的任务
  const mergeTasks = useCallback((prev: Task[], fetched: Task[]): Task[] => {
    const fetchedMap = new Map(fetched.map((t) => [t.id, t]));
    const fetchedIds = new Set(fetched.map((t) => t.id));

    // 更新已存在的任务，移除已删除的任务
    const updated = prev
      .filter((t) => fetchedIds.has(t.id))
      .map((t) => fetchedMap.get(t.id) || t);

    // 添加新任务（在 fetched 中但不在 prev 中）
    const existingIds = new Set(prev.map((t) => t.id));
    const newTasks = fetched.filter((t) => !existingIds.has(t.id));

    return [...newTasks, ...updated];
  }, []);

  useEffect(() => {
    api
      .listTasks()
      .then(setTasks)
      .catch(() => null);
  }, []);

  // 定时轮询任务列表，无闪烁更新
  useEffect(() => {
    const pollInterval = setInterval(() => {
      api
        .listTasks()
        .then((fetched) => {
          setTasks((prev) => mergeTasks(prev, fetched));
          // 轮询成功后清理已删除集合，因为状态已同步
          deletedTaskIdsRef.current.clear();
        })
        .catch(() => null);
    }, 5000); // 每 5 秒轮询一次

    return () => clearInterval(pollInterval);
  }, [mergeTasks]);

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
          const taskId = payload.task.id;

          // 忽略已删除任务的更新（如重试后的旧任务）
          if (deletedTaskIdsRef.current.has(taskId)) {
            return;
          }

          setTasks((prev) => {
            const idx = prev.findIndex((task) => task.id === taskId);
            const oldTask = idx !== -1 ? prev[idx] : null;
            const newTask = payload.task;

            // 检测状态变化并发送通知
            if (oldTask) {
              const taskName = newTask.name || newTask.uri;
              if (oldTask.status !== "complete" && newTask.status === "complete") {
                sendTaskCompleteNotification(taskName, newTask.id);
              } else if (oldTask.status !== "error" && newTask.status === "error") {
                sendTaskErrorNotification(taskName, newTask.id);
              }
            }

            if (idx === -1) return [newTask, ...prev];
            const next = [...prev];
            next[idx] = newTask;
            return next;
          });
        } else if (payload.type === "notification") {
          // 服务端推送的通知（如任务因空间不足被取消）
          const level = payload.level === "error" ? "error"
                      : payload.level === "warning" ? "warning"
                      : "info";
          showToast(payload.message, level);
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
    if (isSubmitting) return;
    setError(null);
    setIsSubmitting(true);
    try {
      const task = await api.createTask(uri);
      setTasks((prev) => [task, ...prev]);
      setUri("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleTorrentUpload(
    event: React.ChangeEvent<HTMLInputElement>,
  ) {
    const file = event.target.files?.[0];
    if (!file) return;

    setError(null);

    // 检查文件类型
    if (!file.name.endsWith(".torrent")) {
      setError("请选择 .torrent 文件");
      return;
    }

    try {
      // 读取文件为 Base64
      const base64Content = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = reader.result as string;
          // 移除 data:application/x-bittorrent;base64, 前缀
          const base64 = result.split(",")[1];
          resolve(base64);
        };
        reader.onerror = () => reject(new Error("文件读取失败"));
        reader.readAsDataURL(file);
      });

      // 上传种子
      const task = await api.uploadTorrent(base64Content);
      setTasks((prev) => [task, ...prev]);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      // 重置 input，允许再次选择同一文件
      if (torrentInputRef.current) {
        torrentInputRef.current.value = "";
      }
    }
  }

  async function pauseTask(id: number) {
    if (operatingTaskIds.has(id)) return;
    setOperatingTaskIds((prev) => new Set(prev).add(id));
    try {
      await api.updateTaskStatus(id, "pause");
    } catch (err) {
      showToast("暂停失败：" + (err as Error).message, "error");
    } finally {
      setOperatingTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }

  async function resumeTask(id: number) {
    if (operatingTaskIds.has(id)) return;
    setOperatingTaskIds((prev) => new Set(prev).add(id));
    try {
      await api.updateTaskStatus(id, "resume");
    } catch (err) {
      showToast("继续失败：" + (err as Error).message, "error");
    } finally {
      setOperatingTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
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
      showToast("删除失败：" + (err as Error).message, "error");
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

    const confirmed = await showConfirm({
      title: "批量删除",
      message,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

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
      showToast("批量删除失败：" + (err as Error).message, "error");
    }
  }

  async function batchPauseTasks() {
    if (selectedTasks.size === 0 || isBatchOperating) return;
    const activeTasks = tasks.filter(
      (t) =>
        selectedTasks.has(t.id) &&
        (t.status === "active" || t.status === "waiting"),
    );
    if (activeTasks.length === 0) {
      showToast("没有可暂停的任务", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "批量暂停",
      message: `确定要暂停选中的 ${activeTasks.length} 个任务吗？`,
      confirmText: "暂停",
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await Promise.all(
        activeTasks.map((t) => api.updateTaskStatus(t.id, "pause")),
      );
      showToast(`已暂停 ${activeTasks.length} 个任务`, "success");
    } catch (err) {
      showToast("批量暂停失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }

  async function batchResumeTasks() {
    if (selectedTasks.size === 0 || isBatchOperating) return;
    const pausedTasks = tasks.filter(
      (t) => selectedTasks.has(t.id) && t.status === "paused",
    );
    if (pausedTasks.length === 0) {
      showToast("没有可继续的任务", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "批量继续",
      message: `确定要继续选中的 ${pausedTasks.length} 个任务吗？`,
      confirmText: "继续",
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await Promise.all(
        pausedTasks.map((t) => api.updateTaskStatus(t.id, "resume")),
      );
      showToast(`已继续 ${pausedTasks.length} 个任务`, "success");
    } catch (err) {
      showToast("批量继续失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }

  async function retryTask(task: Task) {
    // 种子任务无法重试
    if (task.uri === "[torrent]") {
      showToast("种子任务无法直接重试，请重新上传种子文件", "warning");
      return;
    }

    // 防止重复点击
    if (retryingTaskIds.has(task.id)) {
      return;
    }

    // 在调用 API 前就标记旧任务为已删除，防止 WebSocket 推送旧任务更新
    deletedTaskIdsRef.current.add(task.id);
    // 立即乐观移除旧任务
    setTasks((prev) => prev.filter((t) => t.id !== task.id));
    setRetryingTaskIds((prev) => new Set(prev).add(task.id));

    try {
      await api.retryTask(task.id);
      // 强制刷新列表，确保状态一致
      const tasks = await api.listTasks();
      setTasks(tasks);
      deletedTaskIdsRef.current.clear();
    } catch (err) {
      // 失败时从已删除集合中移除，允许后续重试
      deletedTaskIdsRef.current.delete(task.id);
      // 失败时恢复列表（通过刷新）
      const tasks = await api.listTasks();
      setTasks(tasks);
      showToast("重试失败：" + (err as Error).message, "error");
    } finally {
      setRetryingTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(task.id);
        return next;
      });
    }
  }

  async function batchRetryTasks() {
    if (selectedTasks.size === 0) return;

    // 筛选出可重试的 error 任务（排除种子任务）
    const errorTasks = tasks.filter(
      (t) =>
        selectedTasks.has(t.id) &&
        t.status === "error" &&
        t.uri !== "[torrent]",
    );

    if (errorTasks.length === 0) {
      showToast("没有可重试的任务（种子任务需重新上传）", "warning");
      return;
    }

    // 在调用 API 前就标记所有待重试任务为已删除，防止 WebSocket 推送旧任务更新
    errorTasks.forEach((t) => deletedTaskIdsRef.current.add(t.id));
    // 乐观移除旧任务
    const retriedIds = errorTasks.map((t) => t.id);
    setTasks((prev) => prev.filter((t) => !retriedIds.includes(t.id)));

    let successCount = 0;
    let failCount = 0;
    const failedIds: number[] = [];

    for (const task of errorTasks) {
      try {
        await api.retryTask(task.id);
        successCount++;
      } catch (err) {
        failCount++;
        failedIds.push(task.id);
        console.error(`Failed to retry task ${task.id}:`, err);
      }
    }

    // 失败的任务从已删除集合中移除
    failedIds.forEach((id) => deletedTaskIdsRef.current.delete(id));

    // 强制刷新列表，确保状态一致
    const refreshedTasks = await api.listTasks();
    setTasks(refreshedTasks);
    deletedTaskIdsRef.current.clear();

    if (failCount > 0) {
      showToast(`重试完成：成功 ${successCount} 个，失败 ${failCount} 个`, "warning");
    } else if (successCount > 0) {
      showToast(`成功重试 ${successCount} 个任务`, "success");
    }
  }

  async function batchAddTasks() {
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
      showToast(`添加完成：成功 ${successCount} 个，失败 ${failCount} 个`, "warning");
    } else {
      showToast(`成功添加 ${successCount} 个任务`, "success");
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

    // 关键词搜索
    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter(
        (t) =>
          (t.name && t.name.toLowerCase().includes(keyword)) ||
          (t.uri && t.uri.toLowerCase().includes(keyword))
      );
    }

    // 筛选
    if (filterStatus === "active") {
      filtered = filtered.filter(
        (t) => t.status === "active" || t.status === "waiting",
      );
    } else if (filterStatus === "complete") {
      filtered = filtered.filter((t) => t.status === "complete");
    } else if (filterStatus === "error") {
      filtered = filtered.filter((t) => t.status === "error");
    }

    // 排序
    // 排序（处理 null/undefined 值，确保稳定性）
    const sorted = [...filtered].sort((a, b) => {
      let comparison = 0;

      switch (sortBy) {
        case "speed":
          // null/undefined 视为 0
          comparison = (a.download_speed ?? 0) - (b.download_speed ?? 0);
          break;
        case "time":
          // 处理无效日期
          const timeA = a.created_at ? new Date(a.created_at).getTime() : 0;
          const timeB = b.created_at ? new Date(b.created_at).getTime() : 0;
          comparison = timeA - timeB;
          break;
        case "remaining":
          comparison = getRemainingTime(a) - getRemainingTime(b);
          break;
        default:
          comparison = 0;
      }

      // 主排序相同时，按 id 排序保证稳定性
      if (comparison === 0) {
        comparison = a.id - b.id;
      }

      return sortOrder === "asc" ? comparison : -comparison;
    });

    return sorted;
  }, [tasks, searchKeyword, filterStatus, sortBy, sortOrder]);

  async function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    // 找到任务（通过 gid 或 id 匹配）
    const activeTask = tasks.find((t) => (t.gid || String(t.id)) === active.id);
    if (!activeTask || activeTask.status !== "waiting") return;

    // 计算新位置
    const oldIndex = tasks.findIndex((t) => (t.gid || String(t.id)) === active.id);
    const newIndex = tasks.findIndex((t) => (t.gid || String(t.id)) === over.id);

    if (oldIndex === -1 || newIndex === -1) return;

    // 乐观更新 UI
    setTasks((prev) => arrayMove(prev, oldIndex, newIndex));

    // 调用 API
    try {
      await api.changeTaskPosition(activeTask.id, newIndex, "POS_SET");
    } catch (err) {
      // 失败回滚
      setTasks((prev) => arrayMove(prev, newIndex, oldIndex));
      console.error("调整位置失败:", err);
    }
  }

  return (
    <AuthLayout>
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
          {error ? (
            <div className="form-error">
              {error}
            </div>
          ) : null}
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
            <span className="muted text-sm">
              筛选:
            </span>
            <select
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
              className="select"
            >
              <option value="all">全部</option>
              <option value="active">进行中</option>
              <option value="complete">已完成</option>
              <option value="error">错误</option>
            </select>
          </div>

          <div className="filter-group">
            <span className="muted text-sm">
              排序:
            </span>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="select"
            >
              <option value="time">添加时间</option>
              <option value="speed">下载速度</option>
              <option value="remaining">剩余时间</option>
            </select>
            <button
              type="button"
              onClick={() => setSortOrder(sortOrder === "asc" ? "desc" : "asc")}
              className="sort-btn"
            >
              {sortOrder === "asc" ? "↑" : "↓"}
            </button>
          </div>

          <div className="filter-group ml-auto">
            {selectedTasks.size > 0 && (
              <>
                <span className="muted text-sm">
                  已选 {selectedTasks.size} 项
                </span>
                <button
                  type="button"
                  className={`button secondary btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                  onClick={batchPauseTasks}
                  disabled={isBatchOperating}
                >
                  暂停
                </button>
                <button
                  type="button"
                  className={`button secondary btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                  onClick={batchResumeTasks}
                  disabled={isBatchOperating}
                >
                  继续
                </button>
                <button
                  type="button"
                  className={`button secondary btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                  onClick={batchRetryTasks}
                  disabled={isBatchOperating}
                >
                  重试
                </button>
                <button
                  type="button"
                  className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                  onClick={batchDeleteTasks}
                  disabled={isBatchOperating}
                >
                  删除
                </button>
              </>
            )}
            <button
              type="button"
              className="button secondary btn-sm"
              onClick={toggleSelectAll}
            >
              {selectedTasks.size === filteredAndSortedTasks.length &&
              filteredAndSortedTasks.length > 0
                ? "取消全选"
                : "全选"}
            </button>
          </div>
        </div>

        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={filteredAndSortedTasks.map((t) => t.gid || String(t.id))}
            strategy={verticalListSortingStrategy}
          >
            <div className="task-list">
              {filteredAndSortedTasks.length === 0 && (
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
                  <p className="font-medium mb-1">
                    暂无任务
                  </p>
                  <p className="muted text-base">
                    点击上方的 "+" 添加下载任务
                  </p>
                </div>
              )}

              {filteredAndSortedTasks.map((task) => (
                <SortableTaskCard
                  key={task.gid || task.id}
                  task={task}
                  isSelected={selectedTasks.has(task.id)}
                  isRetrying={retryingTaskIds.has(task.id)}
                  isOperating={operatingTaskIds.has(task.id)}
                  onToggleSelection={toggleTaskSelection}
                  onPause={pauseTask}
                  onResume={resumeTask}
                  onRemove={removeTask}
                  onRetry={retryTask}
                  onNavigate={(id) => router.push(`/tasks/detail?id=${id}`)}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      </div>

      {mounted && showBatchAddModal && createPortal(
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

      {mounted && deleteConfirmModal && createPortal(
        <div
          className="modal-overlay"
          onClick={() => {
            setDeleteConfirmModal(null);
            setDeleteFiles(false);
          }}
        >
          <div
            className="delete-modal-content"
            onClick={(e) => e.stopPropagation()}
          >
              <div className="mb-4">
                <h2 className="m-0 mb-2">确认删除任务</h2>
                <p className="muted text-base m-0">
                  {deleteConfirmModal.taskName}
                </p>
              </div>

              {deleteConfirmModal.isComplete ? (
                <div className="mb-4">
                  <label
                    className={`delete-checkbox-option${deleteFiles ? " checked" : ""}`}
                  >
                    <input
                      type="checkbox"
                      checked={deleteFiles}
                      onChange={(e) => setDeleteFiles(e.target.checked)}
                      className="checkbox cursor-pointer"
                    />
                    <span className="text-base">同时删除下载的文件</span>
                  </label>
                </div>
              ) : (
                <div className="warning-box">
                  <p>
                    ⚠️ 此任务未完成，删除任务会同时删除未完成的文件
                  </p>
                </div>
              )}

              <div className="modal-footer">
                <button
                  type="button"
                  className="button secondary btn-task"
                  onClick={() => {
                    setDeleteConfirmModal(null);
                    setDeleteFiles(false);
                  }}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="button btn-danger"
                  onClick={confirmDeleteTask}
                >
                  确认删除
                </button>
              </div>
            </div>
          </div>,
        document.body
      )}
    </AuthLayout>
  );
}
