"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Icon } from "@/shared/components/Icon";
import { useToast } from "@/shared/components/toast-context";
import type { PackStatus, PackViewResponse } from "@/shared/api/types";
import { fileDownloadUrl } from "@/shared/api/urls";
import { triggerFileDownload } from "@/shared/lib/download";
import { cancelPack, clearPackRecords, deletePackRecord, fetchPacks } from "../api";
import { PackRecordsDrawer, type PackOperation } from "./PackRecordsDrawer";

type PackEntryButtonProps = {
  onTaskComplete?: () => void;
};

export function PackEntryButton({ onTaskComplete }: PackEntryButtonProps) {
  const { showToast, showConfirm } = useToast();
  const [tasks, setTasks] = useState<PackViewResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [operation, setOperation] = useState<PackOperation>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const taskStatusRef = useRef<Map<number, PackStatus>>(new Map());
  const onTaskCompleteRef = useRef(onTaskComplete);
  const requestIdRef = useRef(0);
  const mountedRef = useRef(false);

  useEffect(() => {
    onTaskCompleteRef.current = onTaskComplete;
  }, [onTaskComplete]);

  const loadTasks = useCallback(async (showLoading = false) => {
    if (!mountedRef.current) return;
    const requestId = ++requestIdRef.current;
    if (showLoading) setLoading(true);
    try {
      const data = await fetchPacks(50);
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      const previousStatuses = taskStatusRef.current;
      const completedFromActive = data.items.some((task) => {
        const previousStatus = previousStatuses.get(task.id);
        return task.status === "done" && (previousStatus === "pending" || previousStatus === "packing");
      });
      previousStatuses.clear();
      data.items.forEach((task) => previousStatuses.set(task.id, task.status));
      setTasks(data.items);
      setError(null);
      if (completedFromActive) onTaskCompleteRef.current?.();
    } catch (err) {
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      setError((err as Error).message);
    } finally {
      if (showLoading && mountedRef.current && requestId === requestIdRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void loadTasks(true);
    return () => {
      mountedRef.current = false;
      requestIdRef.current += 1;
    };
  }, [loadTasks]);

  const hasActiveTasks = tasks.some((task) => task.status === "pending" || task.status === "packing");
  useEffect(() => {
    if (!hasActiveTasks) return undefined;
    const interval = setInterval(() => {
      if (!document.hidden) void loadTasks();
    }, 2000);
    return () => clearInterval(interval);
  }, [hasActiveTasks, loadTasks]);

  const hasPackingTasks = tasks.some((task) => task.status === "packing");
  useEffect(() => {
    if (!open || !hasPackingTasks) return undefined;
    setNowMs(Date.now());
    const interval = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [open, hasPackingTasks]);

  const handleClear = async () => {
    setOperation("clear");
    try {
      const result = await clearPackRecords();
      showToast(`已清空 ${result.cleared_count} 条记录`, "success");
      await loadTasks();
    } catch (err) {
      showToast(`清空失败: ${(err as Error).message}`, "error");
    } finally {
      setOperation(null);
    }
  };

  const handleCancel = async (task: PackViewResponse) => {
    const confirmed = await showConfirm({
      title: "取消打包任务",
      message: "确定要取消此打包任务吗？",
      confirmText: "取消任务",
      danger: true,
    });
    if (!confirmed) return;
    setOperation(task.id);
    try {
      await cancelPack(task.id);
      await loadTasks();
    } catch (err) {
      showToast(`取消失败: ${(err as Error).message}`, "error");
    } finally {
      setOperation(null);
    }
  };

  const handleDelete = async (task: PackViewResponse) => {
    const confirmed = await showConfirm({
      title: "删除任务记录",
      message: "确定要删除此任务记录吗？",
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;
    setOperation(task.id);
    try {
      await deletePackRecord(task.id);
      await loadTasks();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    } finally {
      setOperation(null);
    }
  };

  const handleDownload = (task: PackViewResponse, output: NonNullable<PackViewResponse["output"]>) => {
    triggerFileDownload(fileDownloadUrl(output.user_file_id), output.display_name);
    showToast(`已触发打包文件下载: ${task.output_name}`, "success");
  };

  const activeCount = tasks.filter((task) => task.status === "pending" || task.status === "packing").length;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="button secondary btn-sm pack-entry-btn"
        onClick={() => setOpen(true)}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <Icon name="archive" className="icon-sm" />
        打包记录
        <span className="pack-entry-badge" hidden={activeCount === 0}>{activeCount}</span>
      </button>
      {open && (
        <PackRecordsDrawer
          tasks={tasks}
          loading={loading}
          error={error}
          nowMs={nowMs}
          operation={operation}
          trigger={buttonRef.current}
          onClose={() => setOpen(false)}
          onRetry={() => { void loadTasks(true); }}
          onClear={() => { void handleClear(); }}
          onCancel={(task) => { void handleCancel(task); }}
          onDelete={(task) => { void handleDelete(task); }}
          onDownload={handleDownload}
        />
      )}
    </>
  );
}
