"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api, ApiError } from "@/lib/api";
import type { Task, TorrentPreview } from "@/types";
import { useMounted } from "@/lib/useMounted";
import { useToast } from "@/components/Toast";
import { useClipboard } from "@/hooks/useClipboard";
import { useSelection } from "@/hooks/useSelection";
import StatsWidget from "@/components/StatsWidget";
import { useTaskWebSocket } from "@/hooks/useTaskWebSocket";
import {
  sendTaskCompleteNotification,
  sendTaskErrorNotification,
} from "@/lib/notification";

import { AddTaskForm } from "./_components/AddTaskForm";
import { BatchAddTasksDialog } from "./_components/BatchAddTasksDialog";
import { TaskToolbar } from "./_components/TaskToolbar";
import { TaskList } from "./_components/TaskList";
import { TorrentCreateWizard } from "./_components/TorrentCreateWizard";

function upsertTaskById(tasks: Task[], task: Task): Task[] {
  const existingIndex = tasks.findIndex((item) => item.id === task.id);
  if (existingIndex === -1) {
    return [task, ...tasks];
  }
  const next = [...tasks];
  next[existingIndex] = task;
  return next;
}

function getTaskDisplayName(task: Task): string {
  return task.name || "未知文件";
}

/** Tasks that stay on the current tasks page (aligned with backend is_current + error). */
function isCurrentVisibleStatus(status: string): boolean {
  return (
    status === "active" ||
    status === "queued" ||
    status === "waiting" ||
    status === "paused" ||
    status === "error"
  );
}

/** Live in-flight attempts that can still be cancelled. */
function isInFlightStatus(status: string): boolean {
  return (
    status === "active" ||
    status === "queued" ||
    status === "waiting" ||
    status === "paused"
  );
}

const MAX_TORRENT_FILE_SIZE = 10 * 1024 * 1024;
const MAX_BATCH_TASKS = 30;

function parseBatchUris(value: string): string[] {
  return [...new Set(value.split("\n").map((line) => line.trim()).filter(Boolean))];
}

export default function TasksPage() {
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [uri, setUri] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { selected: selectedTasks, toggle: toggleTaskSelection, setItemSelected, clear: clearSelection, toggleAll } = useSelection<number>();
  const copyUri = useClipboard();
  const [filterStatus, setFilterStatus] = useState<string>(() => {
    if (typeof window !== "undefined") {
      try {
        return localStorage.getItem("tasks_filterStatus") || "all";
      } catch (err) {
        console.warn("读取任务筛选条件失败", err);
      }
    }
    return "all";
  });
  const [searchKeyword, setSearchKeyword] = useState("");
  const [sortBy, setSortBy] = useState<string>("default");
  const [showBatchAddModal, setShowBatchAddModal] = useState(false);
  const [isBatchAdding, setIsBatchAdding] = useState(false);
  const [batchUris, setBatchUris] = useState("");
  const [torrentWizard, setTorrentWizard] = useState<{
    torrentBase64: string;
    preview: TorrentPreview;
  } | null>(null);
  const mounted = useMounted();
  const torrentInputRef = useRef<HTMLInputElement>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const [operatingTaskIds, setOperatingTaskIds] = useState<Set<number>>(
    new Set()
  );
  const wsConnectedRef = useRef(false);

  useEffect(() => {
    if (showBatchAddModal) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [showBatchAddModal]);

  const deletedTaskIdsRef = useRef<Set<number> | null>(null);
  if (deletedTaskIdsRef.current === null) {
    deletedTaskIdsRef.current = new Set();
  }
  const deletedTaskIds = deletedTaskIdsRef.current;
  const tasksRef = useRef<Task[]>([]);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      try {
        localStorage.setItem("tasks_filterStatus", filterStatus);
      } catch (err) {
        console.warn("保存任务筛选条件失败", err);
      }
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
    let isFetching = false;
    const pollInterval = setInterval(() => {
      if (wsConnectedRef.current) return;
      if (document.hidden || isFetching) return;

      isFetching = true;
      api
        .listTasks("active")
        .then((activeTasks) => {
          const activeMap = new Map(activeTasks.map((t) => [t.id, t]));

          const updatedActive = activeTasks.filter(
            (t) => !deletedTaskIds.has(t.id)
          );

          let needRefresh = false;
          const prevTasks = tasksRef.current;
          const prevInFlight = prevTasks.filter((t) => isInFlightStatus(t.status));
          for (const t of prevInFlight) {
            if (!activeMap.has(t.id) && !deletedTaskIds.has(t.id)) {
              needRefresh = true;
              break;
            }
          }

          const deletedIds = new Set(deletedTaskIds);
          deletedTaskIds.clear();
          setTasks((prev) => {
            const nonInFlight = prev.filter(
              (t) => !isInFlightStatus(t.status) && !deletedIds.has(t.id)
            );
            return [...updatedActive, ...nonInFlight];
          });

          if (needRefresh) {
            api
              .listTasks("current")
              .then((currentTasks) => {
                setTasks((prev) => {
                  const freshDeletedIds = new Set(deletedTaskIds);
                  return currentTasks.filter((t) => !freshDeletedIds.has(t.id));
                });
              })
              .catch((err: unknown) => {
                console.warn("刷新当前任务列表失败", err);
              });
          }
        })
        .catch((err: unknown) => {
          console.warn("轮询活动任务失败", err);
        })
        .finally(() => { isFetching = false; });
    }, 5000);

    return () => clearInterval(pollInterval);
  }, [deletedTaskIds]);

  const handleTaskUpdate = useCallback((newTask: Task) => {
    const taskId = newTask.id;

    if (deletedTaskIds.has(taskId)) {
      return;
    }

    const oldTask = tasksRef.current.find((task) => task.id === taskId);
    if (oldTask) {
      const taskName = newTask.name || "下载任务";
      if (oldTask.status !== "complete" && newTask.status === "complete") {
        sendTaskCompleteNotification(taskName, newTask.id);
        showToast(`${taskName} 下载完成`, "success");
      } else if (oldTask.status !== "error" && newTask.status === "error") {
        sendTaskErrorNotification(taskName, newTask.id);
        showToast(`${taskName} 下载失败`, "error");
      }
    }

    const isVisibleStatus = isCurrentVisibleStatus(newTask.status);

    setTasks((prev) => {
      const idx = prev.findIndex((task) => task.id === taskId);

      if (!isVisibleStatus) {
        if (idx === -1) return prev;
        const next = [...prev];
        next.splice(idx, 1);
        return next;
      }

      if (idx === -1) return [newTask, ...prev];
      const next = [...prev];
      next[idx] = newTask;
      return next;
    });
  }, [deletedTaskIds, showToast]);

  const handleNotification = useCallback(
    (message: string, level: "info" | "warning" | "error") => {
      showToast(message, level);
    },
    [showToast]
  );

  const handleWsConnected = useCallback(() => {
    wsConnectedRef.current = true;
    api
      .listTasks("current")
      .then((currentTasks) => {
        setTasks(() => {
          const deletedIds = deletedTaskIds;
          return currentTasks.filter((t) => !deletedIds.has(t.id));
        });
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : "未知错误";
        showToast(`同步任务状态失败: ${message}`, "warning");
      });
  }, [showToast]);

  const handleWsDisconnected = useCallback(() => {
    wsConnectedRef.current = false;
  }, []);

  useTaskWebSocket({
    onTaskUpdate: handleTaskUpdate,
    onNotification: handleNotification,
    onConnected: handleWsConnected,
    onDisconnected: handleWsDisconnected,
  });

  const refreshTasks = useCallback(async () => {
    const currentTasks = await api.listTasks("current");
    setTasks(() => currentTasks.filter((t) => !deletedTaskIds.has(t.id)));
  }, [deletedTaskIds]);

  const refreshAfterSubmit = useCallback(async () => {
    try {
      await refreshTasks();
    } catch (err) {
      showToast("任务已提交，但列表刷新失败，请手动刷新", "warning");
    }
  }, [refreshTasks, showToast]);

  const createTask = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (isSubmitting) return;
      setError(null);
      setIsSubmitting(true);
      let accepted = false;
      try {
        const result = await api.createTasks([{ uri }]);
        if (result.accepted_count > 0) {
          accepted = true;
          setUri("");
          showToast("任务已提交", "success");
        } else {
          setError(result.results.find((item) => !item.accepted)?.error ?? "提交失败");
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 502) {
          setError("提交结果暂无法确认，请刷新任务列表");
        } else {
          setError((err as Error).message);
        }
      } finally {
        setIsSubmitting(false);
      }
      if (accepted) {
        await refreshAfterSubmit();
      }
    },
    [uri, isSubmitting, showToast, refreshAfterSubmit]
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
      if (file.size > MAX_TORRENT_FILE_SIZE) {
        setError("种子文件过大，最大支持 10 MB");
        event.target.value = "";
        return;
      }

      try {
        const base64Content = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => {
            const result = reader.result;
            if (typeof result !== "string") {
              reject(new Error("文件读取结果无效"));
              return;
            }
            const splitIndex = result.indexOf(",");
            if (splitIndex < 0 || splitIndex === result.length - 1) {
              reject(new Error("文件编码格式无效"));
              return;
            }
            const base64 = result.slice(splitIndex + 1);
            resolve(base64);
          };
          reader.onerror = () => reject(new Error("文件读取失败"));
          reader.readAsDataURL(file);
        });

        const preview = await api.previewTorrent(base64Content);
        setTorrentWizard({ torrentBase64: base64Content, preview });
      } catch (err) {
        const message = (err as Error).message;
        if (message.includes("您已拥有此文件")) {
          showToast("您已拥有此文件", "warning");
          setError(null);
        } else {
          setError(message);
        }
      } finally {
        if (torrentInputRef.current) {
          torrentInputRef.current.value = "";
        }
      }
    },
    [showToast]
  );

  const handleTorrentCreated = useCallback((task: Task) => {
    if (isCurrentVisibleStatus(task.status)) {
      setTasks((prev) => upsertTaskById(prev, task));
    }
    setTorrentWizard(null);
    if (torrentInputRef.current) {
      torrentInputRef.current.value = "";
    }
  }, []);

  const handleTorrentWizardCancel = useCallback(() => {
    setTorrentWizard(null);
    if (torrentInputRef.current) {
      torrentInputRef.current.value = "";
    }
  }, []);

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
        const result = await api.cancelTasks([id]);
        const item = result.results[0];
        if (!item?.ok) {
          showToast(
            (isFailedTask ? "删除" : "取消") + "失败：" + (item?.error ?? "未知错误"),
            "error"
          );
          return;
        }
        deletedTaskIds.add(id);
        setTasks((prev) => prev.filter((t) => t.id !== id));
        setItemSelected(id, false);
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
    [showConfirm, showToast, setItemSelected, deletedTaskIds]
  );

  const retryTask = useCallback(
    async (task: Task) => {
      if (task.retryable === false) {
        showToast(task.retry_blocked_reason || "不可重试", "warning");
        return;
      }

      try {
        const newTask = await api.retryTask(task.id);
        if (isCurrentVisibleStatus(newTask.status)) {
          setTasks((prev) => upsertTaskById(prev, newTask));
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
      (t) => selectedTasks.has(t.id) && isInFlightStatus(t.status)
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
      const result = await api.cancelTasks(activeTasks.map((t) => t.id));
      const cancelledIds = new Set(
        result.results.filter((r) => r.ok).map((r) => r.task_id)
      );
      cancelledIds.forEach((id) => deletedTaskIds.add(id));
      setTasks((prev) => prev.filter((t) => !cancelledIds.has(t.id)));
      clearSelection();
      if (result.failed_count > 0) {
        showToast(
          `已取消 ${result.accepted_count} 个任务，${result.failed_count} 个取消失败`,
          "warning"
        );
      } else {
        showToast(`已取消 ${result.accepted_count} 个任务`, "success");
      }
    } catch (err) {
      showToast("批量取消失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }, [selectedTasks, isBatchOperating, showConfirm, showToast, clearSelection, deletedTaskIds]);

  const batchAddTasks = useCallback(async () => {
    if (isBatchAdding) return;
    const uris = parseBatchUris(batchUris);

    if (uris.length === 0) {
      showToast("请输入至少一个链接", "warning");
      return;
    }
    if (uris.length > MAX_BATCH_TASKS) {
      showToast(`一次最多添加 ${MAX_BATCH_TASKS} 个任务`, "warning");
      return;
    }

    setIsBatchAdding(true);
    setError(null);
    try {
      const result = await api.createTasks(uris.map((uri) => ({ uri })));

      if (result.failed_count === 0) {
        showToast(`已提交 ${result.accepted_count} 个任务`, "success");
        setBatchUris("");
        setShowBatchAddModal(false);
      } else if (result.accepted_count > 0) {
        showToast(
          `提交完成：成功${result.accepted_count}个，失败${result.failed_count}个`,
          "warning"
        );
        setBatchUris("");
        setShowBatchAddModal(false);
      } else if (result.results.length === 1) {
        setError(result.results[0]?.error ?? "提交失败");
        return;
      } else {
        const firstError = result.results.find((r) => r.error)?.error;
        setError(
          firstError
            ? `提交失败：${result.failed_count} 个任务均未成功（${firstError}）`
            : `提交失败：${result.failed_count} 个任务均未成功`
        );
        return;
      }
      await refreshAfterSubmit();
    } catch (err) {
      if (err instanceof ApiError && err.status === 502) {
        setError("提交结果暂无法确认，请刷新任务列表");
      } else {
        setError((err as Error).message);
      }
    } finally {
      setIsBatchAdding(false);
    }
  }, [batchUris, showToast, isBatchAdding, refreshAfterSubmit]);

  const closeBatchAdd = useCallback(() => {
    if (isBatchAdding) return;
    setShowBatchAddModal(false);
    setBatchUris("");
  }, [isBatchAdding]);

  const filteredTasks = useMemo(() => {
    let filtered = tasks;

    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter(
        (t) => t.name && t.name.toLowerCase().includes(keyword)
      );
    }

    if (filterStatus === "active") {
      filtered = filtered.filter((t) => isInFlightStatus(t.status));
    }

    if (sortBy === "speed") {
      filtered = filtered.toSorted((a, b) => b.download_speed - a.download_speed);
    } else if (sortBy === "progress") {
      const progress = (t: Task) => t.total_length > 0 ? t.completed_length / t.total_length : 0;
      filtered = filtered.toSorted((a, b) => progress(b) - progress(a));
    }

    return filtered;
  }, [tasks, searchKeyword, filterStatus, sortBy]);

  const toggleSelectAll = useCallback(() => {
    toggleAll(filteredTasks.map((t) => t.id));
  }, [toggleAll, filteredTasks]);

  const hasActiveTasks = useMemo(
    () =>
      tasks.some(
        (t) => selectedTasks.has(t.id) && isInFlightStatus(t.status)
      ),
    [tasks, selectedTasks]
  );

  return (
    <>
      <div
        className="glass-frame full-height animate-in"
        aria-hidden={torrentWizard ? "true" : undefined}
      >
        <div className="space-between mb-7">
          <div>
            <h1 className="text-2xl">任务</h1>
            <p className="muted">管理您的下载</p>
          </div>
        </div>

        <StatsWidget />

        <AddTaskForm
          uri={uri}
          error={error}
          isSubmitting={isSubmitting}
          torrentInputRef={torrentInputRef}
          onUriChange={setUri}
          onSubmit={createTask}
          onTorrentUpload={handleTorrentUpload}
          onBatchAdd={() => setShowBatchAddModal(true)}
        />

        <TaskToolbar
          selectedCount={selectedTasks.size}
          filteredCount={filteredTasks.length}
          filterStatus={filterStatus}
          sortBy={sortBy}
          searchKeyword={searchKeyword}
          hasActiveTasks={hasActiveTasks}
          isBatchOperating={isBatchOperating}
          onToggleSelectAll={toggleSelectAll}
          onBatchCancel={batchCancelTasks}
          onFilterStatusChange={setFilterStatus}
          onSortByChange={setSortBy}
          onSearchKeywordChange={setSearchKeyword}
        />

        <div className="task-list">
          <TaskList
            filteredTasks={filteredTasks}
            selectedTasks={selectedTasks}
            operatingTaskIds={operatingTaskIds}
            onToggleSelection={toggleTaskSelection}
            onCancel={cancelTask}
            onCopyUri={copyUri}
            onRetry={retryTask}
          />
        </div>
      </div>

      {showBatchAddModal && (
          <BatchAddTasksDialog
            batchUris={batchUris}
            isBatchAdding={isBatchAdding}
            onBatchUrisChange={setBatchUris}
            onSubmit={batchAddTasks}
            onCancel={closeBatchAdd}
          />
      )}
      {mounted && torrentWizard
        ? createPortal(
            <TorrentCreateWizard
              torrentBase64={torrentWizard.torrentBase64}
              preview={torrentWizard.preview}
              onCancel={handleTorrentWizardCancel}
              onCreated={handleTorrentCreated}
              onError={setError}
            />,
            document.body
          )
        : null}
    </>
  );
}
