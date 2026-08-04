"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "@/lib/api";
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

const MAX_TORRENT_FILE_SIZE = 10 * 1024 * 1024;
const MAX_BATCH_TASKS = 30;
const BATCH_TASK_CONCURRENCY = 3;

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
  const batchAddControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      batchAddControllerRef.current?.abort();
      batchAddControllerRef.current = null;
    };
  }, []);

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
          const prevActive = prevTasks.filter(
            (t) => t.status === "active" || t.status === "queued"
          );
          for (const t of prevActive) {
            if (!activeMap.has(t.id) && !deletedTaskIds.has(t.id)) {
              needRefresh = true;
              break;
            }
          }

          const deletedIds = new Set(deletedTaskIds);
          deletedTaskIds.clear();
          setTasks((prev) => {
            const nonActive = prev.filter(
              (t) => t.status !== "active" && t.status !== "queued" && !deletedIds.has(t.id)
            );
            return [...updatedActive, ...nonActive];
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

    const isVisibleStatus =
      newTask.status === "active" ||
      newTask.status === "queued" ||
      newTask.status === "error";

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

  const createTask = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (isSubmitting) return;
      setError(null);
      setIsSubmitting(true);
      try {
        const task = await api.createTask(uri);
        if (task.status === "active" || task.status === "queued") {
          setTasks((prev) => upsertTaskById(prev, task));
        }
        setUri("");
      } catch (err) {
        const message = (err as Error).message;
        if (message.includes("您已拥有此文件")) {
          showToast("您已拥有此文件", "warning");
          setError(null);
        } else {
          setError(message);
        }
      } finally {
        setIsSubmitting(false);
      }
    },
    [uri, isSubmitting, showToast]
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
    if (task.status === "active" || task.status === "queued") {
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
        await api.cancelTask(id);
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
      if (!task.uri) return;

      try {
        const newTask = await api.createTask(task.uri);
        if (newTask.status === "active" || newTask.status === "queued") {
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
      cancelledIds.forEach((id) => deletedTaskIds.add(id));
      setTasks((prev) => prev.filter((t) => !cancelledIds.has(t.id)));
      clearSelection();
      showToast(`已取消 ${activeTasks.length} 个任务`, "success");
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

    const controller = new AbortController();
    batchAddControllerRef.current = controller;
    setIsBatchAdding(true);
    setError(null);
    let nextIndex = 0;
    let successCount = 0;
    let failCount = 0;
    const createdTasks: Task[] = [];

    const worker = async () => {
      while (!controller.signal.aborted) {
        const index = nextIndex++;
        if (index >= uris.length) return;
        try {
          const task = await api.createTask(uris[index], controller.signal);
          if (controller.signal.aborted) return;
          createdTasks.push(task);
          successCount++;
        } catch {
          if (controller.signal.aborted) return;
          failCount++;
        }
      }
    };

    try {
      await Promise.all(
        Array.from(
          { length: Math.min(BATCH_TASK_CONCURRENCY, uris.length) },
          () => worker()
        )
      );
      if (
        controller.signal.aborted ||
        batchAddControllerRef.current !== controller
      ) return;

      const visibleTasks = createdTasks.filter(
        (task) => task.status === "active" || task.status === "queued"
      );
      if (visibleTasks.length > 0) {
        setTasks((prev) =>
          visibleTasks.reduce(upsertTaskById, prev)
        );
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
    } finally {
      if (batchAddControllerRef.current === controller) {
        batchAddControllerRef.current = null;
        setIsBatchAdding(false);
      }
    }
  }, [batchUris, showToast, isBatchAdding]);

  const cancelBatchAdd = useCallback(() => {
    batchAddControllerRef.current?.abort();
    batchAddControllerRef.current = null;
    setIsBatchAdding(false);
    setShowBatchAddModal(false);
    setBatchUris("");
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
        (t) =>
          selectedTasks.has(t.id) &&
          (t.status === "active" || t.status === "queued")
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
            onCancel={cancelBatchAdd}
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
