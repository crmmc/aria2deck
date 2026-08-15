"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { TaskHistory } from "@/types";
import { useToast } from "@/components/Toast";
import { EmptyState } from "@/components/ui/EmptyState";
import { ToolbarGroup, ToolbarSearchInput, ToolbarShell } from "@/components/ui/Toolbar";
import { useClipboard } from "@/hooks/useClipboard";
import { useSelection } from "@/hooks/useSelection";
import { HistoryCard } from "./_components/HistoryCard";

export default function HistoryPage() {
  const { showToast, showConfirm } = useToast();
  const copyToClipboard = useClipboard();
  const [records, setRecords] = useState<TaskHistory[]>([]);
  const [loading, setLoading] = useState(true);
  const {
    selected: selectedRecords,
    selectedCount,
    setSelected: setSelectedRecords,
    toggle: toggleRecordSelection,
    toggleAll: toggleAllRecords,
  } = useSelection<number>();
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const mountedRef = useRef(true);

  const loadHistory = useCallback(async () => {
    if (!mountedRef.current) return;
    setLoading(true);
    try {
      const history = await api.listHistory();
      if (mountedRef.current) setRecords(history);
    } catch {
      if (!mountedRef.current) return;
      showToast("加载历史失败", "error");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    const mounted = mountedRef;
    mounted.current = true;
    loadHistory();
    return () => { mounted.current = false; };
  }, [loadHistory]);

  const copyUri = useCallback(
    (uri: string) => {
      void copyToClipboard(uri);
    },
    [copyToClipboard]
  );

  const deleteRecord = useCallback(
    async (record: TaskHistory) => {
      try {
        await api.deleteHistory(record.id);
        if (!mountedRef.current) return;
        setRecords((prev) => prev.filter((r) => r.id !== record.id));
        setSelectedRecords((prev) => {
          const next = new Set(prev);
          next.delete(record.id);
          return next;
        });
        showToast("已删除该条历史记录", "success");
      } catch (err) {
        showToast("删除失败：" + (err as Error).message, "error");
        if (mountedRef.current) loadHistory();
      }
    },
    [showToast, loadHistory]
  );

  const retryTask = useCallback(
    async (record: TaskHistory) => {
      if (record.retryable === false) {
        showToast(record.retry_blocked_reason || "不可重试", "warning");
        return;
      }

      try {
        await api.retryTask(record.id);
        showToast("已重新添加下载任务", "success");
      } catch (err) {
        showToast("重试失败：" + (err as Error).message, "error");
      }
    },
    [showToast]
  );

  async function batchDeleteHistory() {
    const selectedList = records.filter((r) => selectedRecords.has(r.id));
    if (selectedList.length === 0) {
      showToast("请先选择要删除的记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "删除历史",
      message: `确定要删除选中的 ${selectedList.length} 条历史记录吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;
    if (!mountedRef.current) return;

    setIsBatchOperating(true);
    const idsToDelete = new Set(selectedList.map((r) => r.id));
    try {
      await Promise.all(selectedList.map((r) => api.deleteHistory(r.id)));
      setRecords((prev) => prev.filter((r) => !idsToDelete.has(r.id)));
      setSelectedRecords(new Set());
      showToast(`已删除 ${selectedList.length} 条历史记录`, "success");
    } catch (err) {
      showToast("部分删除失败：" + (err as Error).message, "error");
      if (mountedRef.current) loadHistory();
    } finally {
      if (mountedRef.current) setIsBatchOperating(false);
    }
  }

  async function clearAllHistory() {
    if (records.length === 0) {
      showToast("没有历史记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "清空历史",
      message: `确定要清空全部 ${records.length} 条历史记录吗？`,
      confirmText: "清空",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await api.clearHistory();
      setRecords([]);
      setSelectedRecords(new Set());
      showToast(`已清空全部历史记录`, "success");
    } catch (err) {
      showToast("清空失败：" + (err as Error).message, "error");
    } finally {
      if (mountedRef.current) setIsBatchOperating(false);
    }
  }

  const filteredRecords = useMemo(() => {
    let filtered = records;

    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter((r) =>
        r.task_name.toLowerCase().includes(keyword)
      );
    }

    if (filterStatus === "completed") {
      filtered = filtered.filter((r) => r.result === "completed");
    } else if (filterStatus === "cancelled") {
      filtered = filtered.filter((r) => r.result === "cancelled");
    } else if (filterStatus === "failed") {
      filtered = filtered.filter((r) => r.result === "failed");
    }

    return filtered;
  }, [records, searchKeyword, filterStatus]);

  const toggleSelectAll = useCallback(() => {
    toggleAllRecords(filteredRecords.map((r) => r.id));
  }, [toggleAllRecords, filteredRecords]);

  return (
    <div className="glass-frame full-height animate-in">
      <div className="space-between mb-7">
        <div>
          <h1 className="text-2xl">任务历史</h1>
          <p className="muted">查看已完成、取消和失败的下载任务</p>
        </div>
      </div>

      <ToolbarShell>
        <ToolbarGroup className="toolbar-actions-group">
          <button type="button"
            className="button secondary btn-sm"
            onClick={toggleSelectAll}
          >
            {selectedCount === filteredRecords.length && filteredRecords.length > 0
              ? "取消全选"
              : "全选"}
          </button>
          {selectedCount > 0 && (
            <>
              <span className="muted text-sm">
                已选 {selectedCount} 项
              </span>
              <button type="button"
                className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                onClick={batchDeleteHistory}
                disabled={isBatchOperating}
              >
                删除
              </button>
            </>
          )}
          {records.length > 0 && (
            <button type="button"
              className={`button secondary btn-sm${isBatchOperating ? " opacity-60" : ""}`}
              onClick={clearAllHistory}
              disabled={isBatchOperating}
            >
              清空历史
            </button>
          )}
        </ToolbarGroup>

        <ToolbarGroup className="toolbar-select-group">
          <span className="muted text-sm">筛选:</span>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
            className="select"
            aria-label="筛选历史"
          >
            <option value="all">全部</option>
            <option value="completed">已完成</option>
            <option value="cancelled">已取消</option>
            <option value="failed">失败</option>
          </select>
        </ToolbarGroup>

        <ToolbarGroup className="toolbar-search-group">
          <ToolbarSearchInput
            ariaLabel="搜索历史"
            placeholder="搜索任务..."
            value={searchKeyword}
            onChange={setSearchKeyword}
          />
        </ToolbarGroup>
      </ToolbarShell>

      <div className="task-list">
        {loading ? (
          <div className="empty-state">
            <p className="muted">加载中...</p>
          </div>
        ) : filteredRecords.length === 0 ? (
          <EmptyState
            icon={
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
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
            }
            title="暂无历史记录"
            description="完成的下载任务将显示在这里"
          />
        ) : (
          <div className="card task-card-container">
            {filteredRecords.map((record) => (
              <HistoryCard
                key={record.id}
                record={record}
                isSelected={selectedRecords.has(record.id)}
                onToggleSelection={toggleRecordSelection}
                onCopyUri={copyUri}
                onRetry={retryTask}
                onDelete={deleteRecord}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
