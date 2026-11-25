"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { ApiRequestError } from "@/shared/api/client";
import type { TaskResponse, TorrentPreviewResponse } from "@/shared/api/types";
import { useMounted } from "@/shared/hooks/useMounted";
import { useToast } from "@/shared/components/toast-context";
import { BatchFailureDialog, type BatchFailure } from "@/shared/components/BatchFailureDialog";
import { PageHeader } from "@/shared/components/PageHeader";
import { taskCommands } from "@/shared/lib/commands";
import { useClipboard } from "@/shared/hooks/useClipboard";
import { useSelection } from "@/shared/hooks/useSelection";
import { useLocalState } from "@/shared/hooks/useLocalState";
import { useTaskWebSocket } from "@/shared/hooks/useTaskWebSocket";
import { sendTaskNotification } from "@/shared/notifications";
import { requestModalClose } from "@/shared/ui/modalClose";

import {
  cancelTasks,
  createTasks,
  fetchTasksAll,
  previewTorrent,
  retryTask,
} from "./api";
import {
  filterInFlightTasks,
  isCurrentVisibleStatus,
  isInFlightStatus,
  searchTasks,
  sortTasks,
  type LegacySortKey,
} from "./model";
import { StatsWidget } from "./components/StatsWidget";
import { AddTaskForm } from "./components/AddTaskForm";
import { BatchAddTasksDialog, type BatchFeedback } from "./components/BatchAddTasksDialog";
import { TaskToolbar } from "./components/TaskToolbar";
import { TaskList } from "./components/TaskList";
import { TorrentCreateWizard } from "./components/TorrentCreateWizard";

function upsertTaskById(tasks: TaskResponse[], task: TaskResponse): TaskResponse[] {
  const existingIndex = tasks.findIndex((item) => item.id === task.id);
  if (existingIndex === -1) {
    return [task, ...tasks];
  }
  const next = [...tasks];
  next[existingIndex] = task;
  return next;
}

function getTaskDisplayName(task: TaskResponse): string {
  return task.name || "未知文件";
}

const MAX_TORRENT_FILE_SIZE = 10 * 1024 * 1024;
const MAX_BATCH_TASKS = 30;

function parseBatchUris(value: string): string[] {
  return [...new Set(value.split("\n").map((line) => line.trim()).filter(Boolean))];
}

const NO_BATCH_FAILURES: { uri: string; reason: string }[] = [];

export function TasksPage() {
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [uri, setUri] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { selected: selectedTasks, toggle: toggleTaskSelection, setItemSelected, clear: clearSelection, toggleAll } = useSelection<number>();
  const copyUri = useClipboard();
  // C33：归档参考 老键名不回读，直接用 V2 白名单键（JSON 编码）。
  const filterState = useLocalState<string>("tasks.filter", "all");
  const filterStatus = filterState.value;
  const [searchKeyword, setSearchKeyword] = useState("");
  const [sortBy, setSortBy] = useState<LegacySortKey>("speed");
  const [showBatchAddModal, setShowBatchAddModal] = useState(false);
  const [isBatchAdding, setIsBatchAdding] = useState(false);
  const [cancelFailures, setCancelFailures] = useState<BatchFailure[]>([]);
  const [batchUris, setBatchUris] = useState("");
  const [batchFeedback, setBatchFeedback] = useState<BatchFeedback | null>(null);
  const [torrentWizard, setTorrentWizard] = useState<{
    torrentBase64: string;
    preview: TorrentPreviewResponse;
  } | null>(null);
  const mounted = useMounted();
  const uriInputRef = useRef<HTMLInputElement>(null);
  const torrentInputRef = useRef<HTMLInputElement>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const [operatingTaskIds, setOperatingTaskIds] = useState<Set<number>>(
    new Set(),
  );

  useEffect(() => taskCommands.subscribe((command) => {
    if (command === "add") {
      uriInputRef.current?.focus();
      return;
    }
    if (command === "batch") {
      setShowBatchAddModal(true);
      return;
    }
    torrentInputRef.current?.click();
  }), []);

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
  const tasksRef = useRef<TaskResponse[]>([]);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  useEffect(() => {
    fetchTasksAll()
      .then((currentTasks) => {
        setTasks(currentTasks);
      })
      .catch((err) => {
        showToast("加载任务失败: " + (err as Error).message, "error");
      });
  }, [showToast]);

  const handleTaskUpdate = useCallback((newTask: TaskResponse) => {
    const taskId = newTask.id;

    if (deletedTaskIds.has(taskId)) {
      return;
    }

    const oldTask = tasksRef.current.find((task) => task.id === taskId);
    if (oldTask) {
      const taskName = newTask.name || "下载任务";
      if (oldTask.status !== "completed" && newTask.status === "completed") {
        sendTaskNotification("completed", taskName, newTask.id, (_kind, text) => showToast(text, "error"));
        showToast(`${taskName} 下载完成`, "success");
      } else if (oldTask.status !== "failed" && newTask.status === "failed") {
        sendTaskNotification("failed", taskName, newTask.id, (_kind, text) => showToast(text, "error"));
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

  const refreshTasks = useCallback(async () => {
    const currentTasks = await fetchTasksAll();
    setTasks(() => currentTasks.filter((t) => !deletedTaskIds.has(t.id)));
  }, [deletedTaskIds]);

  // C28：断线补偿轮询由 V2 hook 内置（5s/隐藏态同频）；页面只提供权威重读。
  const pollFallback = useCallback(async () => {
    try {
      await refreshTasks();
      return true;
    } catch {
      return false;
    }
  }, [refreshTasks]);

  const { connected } = useTaskWebSocket(
    useCallback(
      (frame) => {
        if (frame.type === "task_update") handleTaskUpdate(frame.task);
      },
      [handleTaskUpdate],
    ),
    { pollFallback },
  );

  useEffect(() => {
    if (!connected) return;
    // 连上即权威重读一次（对齐 归档参考 onConnected 语义）。
    fetchTasksAll()
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
  }, [connected, showToast, deletedTaskIds]);

  const refreshAfterSubmit = useCallback(async () => {
    try {
      await refreshTasks();
    } catch {
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
        const outcome = await createTasks(uri);
        if (outcome.acceptedCount > 0) {
          accepted = true;
          setUri("");
          showToast("任务已提交", "success");
        } else {
          setError(outcome.failedItems[0]?.message ?? "提交失败");
        }
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 502) {
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
    [uri, isSubmitting, showToast, refreshAfterSubmit],
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

        const previewResponse = await previewTorrent(base64Content);
        setTorrentWizard({ torrentBase64: base64Content, preview: previewResponse });
      } catch (err) {
        const message = (err as Error).message;
        // 归档参考：重复种子的提示走 warning Toast，不占表单错误区。
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
    [showToast],
  );

  const handleTorrentCreated = useCallback((task: TaskResponse) => {
    if (isCurrentVisibleStatus(task.status)) {
      setTasks((prev) => upsertTaskById(prev, task));
    }
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

      const isFailedTask = task.status === "failed";
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
        const response = await cancelTasks([id]);
        const item = response.items[0];
        if (!item?.accepted) {
          showToast(
            (isFailedTask ? "删除" : "取消") + "失败：" + (item?.error?.message ?? "未知错误"),
            "error",
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
    [showConfirm, showToast, setItemSelected, deletedTaskIds],
  );

  const handleRetryTask = useCallback(
    async (task: TaskResponse) => {
      // C10：V2 无 retry_blocked_reason，不编造原因；不可重试时仅提示。
      if (task.retryable === false) {
        showToast("不可重试", "warning");
        return;
      }

      try {
        const newTask = await retryTask(task.id);
        if (isCurrentVisibleStatus(newTask.status)) {
          setTasks((prev) => upsertTaskById(prev, newTask));
        }
        showToast("已重新添加下载任务", "success");
      } catch (err) {
        showToast("重试失败：" + (err as Error).message, "error");
      }
    },
    [showToast],
  );

  const batchCancelTasks = useCallback(async () => {
    if (selectedTasks.size === 0 || isBatchOperating) return;

    const activeTasks = tasksRef.current.filter(
      (t) => selectedTasks.has(t.id) && isInFlightStatus(t.status),
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
      const ids = activeTasks.map((t) => t.id);
      const response = await cancelTasks(ids);
      // items 按请求顺序回位；accepted 项以 task.id 为准，缺 task 时按序号配对。
      const cancelledIds = new Set(
        response.items
          .map((item, index) => (item.accepted ? (item.task?.id ?? ids[index]) : null))
          .filter((value): value is number => value !== null),
      );
      cancelledIds.forEach((cancelledId) => deletedTaskIds.add(cancelledId));
      setTasks((prev) => prev.filter((t) => !cancelledIds.has(t.id)));
      clearSelection();
      if (response.rejected_count > 0) {
        const nameById = new Map(activeTasks.map((task) => [task.id, task.name]));
        setCancelFailures(
          response.items
            .map((item, index) => ({ item, id: item.task?.id ?? ids[index] }))
            .filter(({ item }) => !item.accepted)
            .map(({ item, id }) => ({
              label: item.task?.name ?? nameById.get(id) ?? `任务 ${id}`,
              reason: item.error?.message ?? "未知错误",
            })),
        );
        showToast(
          `已取消 ${response.accepted_count} 个任务，${response.rejected_count} 个取消失败`,
          "warning",
        );
      } else {
        showToast(`已取消 ${response.accepted_count} 个任务`, "success");
      }
    } catch (err) {
      showToast("批量取消失败：" + (err as Error).message, "error");
    } finally {
      setIsBatchOperating(false);
    }
  }, [selectedTasks, isBatchOperating, showConfirm, showToast, clearSelection, deletedTaskIds]);

  const batchAddTasks = useCallback(async (trigger: Element) => {
    if (isBatchAdding) return;
    const submittedUris = parseBatchUris(batchUris);

    if (submittedUris.length === 0) {
      showToast("请输入至少一个链接", "warning");
      return;
    }
    if (submittedUris.length > MAX_BATCH_TASKS) {
      showToast(`一次最多添加 ${MAX_BATCH_TASKS} 个任务`, "warning");
      return;
    }

    setIsBatchAdding(true);
    setBatchFeedback(null);
    let acceptedCount = 0;
    try {
      const outcome = await createTasks(submittedUris.join("\n"));
      acceptedCount = outcome.acceptedCount;

      if (outcome.rejectedCount === 0) {
        showToast(`已提交 ${outcome.acceptedCount} 个任务`, "success");
        setBatchUris("");
        requestModalClose(trigger);
      } else {
        setBatchFeedback({
          kind: "result",
          acceptedCount: outcome.acceptedCount,
          failures: outcome.failedItems.map((item) => ({ uri: item.uri, reason: item.message })),
        });
      }
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 502) {
        setBatchFeedback({
          kind: "error",
          message: "提交结果暂无法确认，请刷新任务列表",
        });
      } else {
        setBatchFeedback({ kind: "error", message: (err as Error).message });
      }
    } finally {
      setIsBatchAdding(false);
    }
    if (acceptedCount > 0) {
      await refreshAfterSubmit();
    }
  }, [batchUris, showToast, isBatchAdding, refreshAfterSubmit]);

  const batchFailures =
    batchFeedback?.kind === "result" ? batchFeedback.failures : NO_BATCH_FAILURES;

  const retryFailedBatch = useCallback(() => {
    setBatchUris(batchFailures.map((failure) => failure.uri).join("\n"));
    setBatchFeedback(null);
  }, [batchFailures]);

  const closeBatchAdd = useCallback(() => {
    setShowBatchAddModal(false);
    setBatchUris("");
    setBatchFeedback(null);
  }, []);

  const filteredTasks = useMemo(() => {
    let filtered = searchTasks(tasks, searchKeyword);
    if (filterStatus === "active") {
      filtered = filterInFlightTasks(filtered);
    }
    // 「当前任务」不再二次按状态过滤：失败任务由 WebSocket 单帧保留，刷新后才消失。
    return sortTasks(filtered, sortBy);
  }, [tasks, searchKeyword, filterStatus, sortBy]);

  const toggleSelectAll = useCallback(() => {
    toggleAll(filteredTasks.map((t) => t.id));
  }, [toggleAll, filteredTasks]);

  const hasActiveTasks = useMemo(
    () =>
      tasks.some(
        (t) => selectedTasks.has(t.id) && isInFlightStatus(t.status),
      ),
    [tasks, selectedTasks],
  );
  const activeTaskCount = useMemo(() => filterInFlightTasks(tasks).length, [tasks]);

  return (
    <>
      <div
        className="glass-frame full-height animate-in"
        aria-hidden={torrentWizard ? "true" : undefined}
      >
        <PageHeader title="任务" description="管理您的下载" />

        <StatsWidget />

        <AddTaskForm
          uri={uri}
          error={error}
          isSubmitting={isSubmitting}
          uriInputRef={uriInputRef}
          torrentInputRef={torrentInputRef}
          onUriChange={setUri}
          onSubmit={createTask}
          onTorrentUpload={handleTorrentUpload}
          onBatchAdd={() => setShowBatchAddModal(true)}
        />

        <section className="panel list-panel" aria-label="任务列表">
          <TaskToolbar
            selectedCount={selectedTasks.size}
            filteredCount={filteredTasks.length}
            currentCount={tasks.length}
            activeCount={activeTaskCount}
            filterStatus={filterStatus}
            sortBy={sortBy}
            searchKeyword={searchKeyword}
            hasActiveTasks={hasActiveTasks}
            isBatchOperating={isBatchOperating}
            onToggleSelectAll={toggleSelectAll}
            onBatchCancel={batchCancelTasks}
            onFilterStatusChange={filterState.setValue}
            onSortByChange={(value) => setSortBy(value as LegacySortKey)}
            onSearchKeywordChange={setSearchKeyword}
          />
          <TaskList
            filteredTasks={filteredTasks}
            selectedTasks={selectedTasks}
            operatingTaskIds={operatingTaskIds}
            onToggleSelection={toggleTaskSelection}
            onToggleSelectAll={toggleSelectAll}
            onCancel={cancelTask}
            onCopyUri={copyUri}
            onRetry={handleRetryTask}
          />
        </section>
      </div>

      {showBatchAddModal && (
          <BatchAddTasksDialog
            batchUris={batchUris}
            isBatchAdding={isBatchAdding}
            feedback={batchFeedback}
            onBatchUrisChange={setBatchUris}
            onSubmit={batchAddTasks}
            onCancel={closeBatchAdd}
            onRetryFailed={retryFailedBatch}
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
            document.body,
          )
        : null}
      <BatchFailureDialog failures={cancelFailures} onClose={() => setCancelFailures([])} />
    </>
  );
}
