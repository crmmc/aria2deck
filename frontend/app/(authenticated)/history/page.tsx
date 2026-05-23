"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { TaskHistory } from "@/types";
import { useToast } from "@/components/Toast";
import { formatBytes } from "@/lib/utils";

interface HistoryCardProps {
  record: TaskHistory;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (record: TaskHistory) => void;
}

const HistoryCard = memo(function HistoryCard({
  record,
  isSelected,
  onToggleSelection,
  onCopyUri,
  onRetry,
}: HistoryCardProps) {
  const handleCardClick = useCallback(() => {
    if (record.uri) {
      onCopyUri(record.uri);
    }
  }, [record.uri, onCopyUri]);

  const handleCheckboxChange = useCallback(() => {
    onToggleSelection(record.id);
  }, [record.id, onToggleSelection]);

  const handleCopyClick = useCallback(() => {
    onCopyUri(record.uri!);
  }, [record.uri, onCopyUri]);

  const handleRetryClick = useCallback(() => {
    onRetry(record);
  }, [record, onRetry]);

  const statusText =
    record.result === "completed"
      ? "已完成"
      : record.result === "cancelled"
        ? "已取消"
        : "失败";

  const statusClass =
    record.result === "completed"
      ? "task-status-complete"
      : record.result === "cancelled"
        ? "task-status-cancelled"
        : "task-status-error";

  const interactiveProps = record.uri
    ? {
        role: "button",
        tabIndex: 0,
        onClick: handleCardClick,
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            handleCardClick();
          }
        },
        "aria-label": `复制历史任务链接 ${record.task_name}`,
      }
    : {};

  return (
    <div
      className={`task-card-inner${isSelected ? " selected" : ""}${record.uri ? " cursor-pointer" : ""}`}
      {...interactiveProps}
    >
      <div>
        <div className="space-between flex-start mb-3">
          <div className="task-card-header">
            <input
              type="checkbox"
              aria-label={`选择历史记录 ${record.task_name}`}
              checked={isSelected}
              onChange={handleCheckboxChange}
              onClick={(e) => e.stopPropagation()}
              className="checkbox-sm mt-2 cursor-pointer"
            />
            <div className="overflow-hidden flex-1">
              <h3 className="task-name" title={record.task_name}>
                {record.task_name}
              </h3>
              <div className="muted tabular-nums text-sm">
                {formatBytes(record.total_length)}
              </div>
            </div>
          </div>
          <span
            className={`task-status ${statusClass}`}
            style={{ marginLeft: "auto" }}
          >
            {statusText}
          </span>
        </div>

        {record.reason && (
          <div
            className={`text-sm mb-3 ${record.result === "failed" ? "text-danger" : "muted"}`}
            title={record.reason}
          >
            {record.reason}
          </div>
        )}
      </div>

      <div className="task-card-footer" role="presentation" onClick={(e) => e.stopPropagation()}>
        <div className="task-footer-left">
          <span className="muted text-sm" suppressHydrationWarning>
            {new Date(record.finished_at).toLocaleString()}
          </span>
        </div>

        <div className="task-footer-right">
          {record.uri && (
            <button type="button"
              className="button secondary btn-task"
              onClick={handleCopyClick}
              title="复制链接"
            >
              复制
            </button>
          )}
          {record.result === "failed" && record.uri && (
            <button type="button"
              className="button secondary btn-task"
              onClick={handleRetryClick}
              title="重新下载"
            >
              重试
            </button>
          )}
        </div>
      </div>
    </div>
  );
});

export default function HistoryPage() {
  const { showToast, showConfirm } = useToast();
  const [records, setRecords] = useState<TaskHistory[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedRecords, setSelectedRecords] = useState<Set<number>>(
    new Set()
  );
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const mountedRef = useRef(true);

  const loadHistory = useCallback(async () => {
    if (!mountedRef.current) return;
    setLoading(true);
    try {
      const history = await api.listHistory();
      if (!mountedRef.current) return;
      setRecords(history);
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

  const retryTask = useCallback(
    async (record: TaskHistory) => {
      if (!record.uri) return;

      try {
        await api.createTask(record.uri);
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

  const toggleRecordSelection = useCallback((id: number) => {
    setSelectedRecords((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

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
    if (selectedRecords.size === filteredRecords.length) {
      setSelectedRecords(new Set());
    } else {
      setSelectedRecords(new Set(filteredRecords.map((r) => r.id)));
    }
  }, [selectedRecords.size, filteredRecords]);

  return (
    <div className="glass-frame full-height animate-in">
      <div className="space-between mb-7">
        <div>
          <h1 className="text-2xl">任务历史</h1>
          <p className="muted">查看已完成、取消和失败的下载任务</p>
        </div>
      </div>

      <div className="card filter-toolbar inline-filter-toolbar">
        <div className="filter-group toolbar-actions-group">
          <button
            type="button"
            className="button secondary btn-sm"
            onClick={toggleSelectAll}
          >
            {selectedRecords.size === filteredRecords.length &&
            filteredRecords.length > 0
              ? "取消全选"
              : "全选"}
          </button>
          {selectedRecords.size > 0 && (
            <>
              <span className="muted text-sm">
                已选 {selectedRecords.size} 项
              </span>
              <button
                type="button"
                className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                onClick={batchDeleteHistory}
                disabled={isBatchOperating}
              >
                删除
              </button>
            </>
          )}
          {records.length > 0 && (
            <button
              type="button"
              className={`button secondary btn-sm${isBatchOperating ? " opacity-60" : ""}`}
              onClick={clearAllHistory}
              disabled={isBatchOperating}
            >
              清空历史
            </button>
          )}
        </div>

        <div className="filter-group toolbar-select-group">
          <span className="muted text-sm">筛选:</span>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
            className="select"
          >
            <option value="all">全部</option>
            <option value="completed">已完成</option>
            <option value="cancelled">已取消</option>
            <option value="failed">失败</option>
          </select>
        </div>

        <div className="filter-group toolbar-search-group">
          <input
            type="text"
            aria-label="搜索历史任务"
            placeholder="搜索任务..."
            value={searchKeyword}
            onChange={(e) => setSearchKeyword(e.target.value)}
            className="search-input"
          />
        </div>
      </div>

      <div className="task-list">
        {loading ? (
          <div className="empty-state">
            <p className="muted">加载中...</p>
          </div>
        ) : filteredRecords.length === 0 ? (
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
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
            </div>
            <p className="font-medium mb-1">暂无历史记录</p>
            <p className="muted text-base">完成的下载任务将显示在这里</p>
          </div>
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
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
