"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "@/shared/components/toast-context";
import { EmptyState } from "@/shared/components/EmptyState";
import { PaginationControls } from "@/shared/components/PaginationControls";
import { ToolbarGroup, ToolbarSearchInput, ToolbarShell } from "@/shared/components/Toolbar";
import { useClipboard } from "@/shared/hooks/useClipboard";
import { useSelection } from "@/shared/hooks/useSelection";

import { deleteHistory, fetchHistory, retryFromHistory } from "./api";
import { sortHistoryRecords, toHistoryRecordView, type HistoryRecordView } from "./model";
import { HistoryCard } from "./components/HistoryCard";

const DEFAULT_PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;

export function HistoryPage() {
  const { showToast, showConfirm } = useToast();
  const copyToClipboard = useClipboard();
  const [records, setRecords] = useState<HistoryRecordView[]>([]);
  const [total, setTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
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
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);

  const loadHistory = useCallback(async () => {
    if (!mountedRef.current) return;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    try {
      const result = await fetchHistory(
        filterStatus === "all" ? null : (filterStatus as "completed" | "cancelled" | "failed"),
        debouncedKeyword,
        currentPage,
        pageSize,
      );
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      setRecords(sortHistoryRecords(result.items.map(toHistoryRecordView)));
      // 新一页/新条件的结果成为当前数据时，清空残留选中，避免工具栏显示旧页计数、批量操作命中空列表
      setSelectedRecords(new Set());
      setTotal(result.total);
    } catch (err) {
      // 归档参考语义：401 等加载失败即使随后守卫跳转卸载本页也要提示（ToastHost 在路由外）。
      showToast("加载历史失败: " + ((err as Error)?.message ?? ""), "error");
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
    } finally {
      if (mountedRef.current && requestId === requestIdRef.current) setLoading(false);
    }
  }, [currentPage, pageSize, filterStatus, debouncedKeyword, showToast, setSelectedRecords]);

  useEffect(() => {
    const mounted = mountedRef;
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // 搜索防抖：输入停止 300ms 后才触发后端查询；关键词真正变化时同步回到第 1 页，
  // 保证条件变化只发一笔"第 1 页 + 新条件"的请求（归档参考 原逻辑）
  useEffect(() => {
    const timer = setTimeout(() => {
      const next = searchKeyword.trim();
      setDebouncedKeyword((prev) => {
        if (prev === next) return prev;
        setCurrentPage(1);
        return next;
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchKeyword]);

  const copyUri = useCallback(
    (uri: string) => {
      void copyToClipboard(uri);
    },
    [copyToClipboard],
  );

  const deleteRecord = useCallback(
    async (record: HistoryRecordView) => {
      try {
        const result = await deleteHistory([record.id]);
        const first = result.items[0];
        if (result.rejected_count > 0 || !first?.accepted) {
          showToast("删除失败：" + (first?.error?.message ?? "未知错误"), "error");
          if (mountedRef.current) loadHistory();
          return;
        }
        if (!mountedRef.current) return;
        setSelectedRecords((prev) => {
          const next = new Set(prev);
          next.delete(record.id);
          return next;
        });
        showToast("已删除该条历史记录", "success");
        // 当前页删空且非第 1 页时回退到上一页（页码变化会触发重新拉取）
        if (records.length === 1 && currentPage > 1) {
          setCurrentPage((page) => page - 1);
        } else {
          loadHistory();
        }
      } catch (err) {
        showToast("删除失败：" + (err as Error).message, "error");
        if (mountedRef.current) loadHistory();
      }
    },
    [showToast, loadHistory, setSelectedRecords, records.length, currentPage],
  );

  const retryRecord = useCallback(
    async (record: HistoryRecordView) => {
      // C10：V2 无 retry_blocked_reason，不编造原因；不可重试时仅提示。
      if (record.retryable === false) {
        showToast("不可重试", "warning");
        return;
      }

      try {
        await retryFromHistory(record.id);
        showToast("已重新添加下载任务", "success");
      } catch (err) {
        showToast("重试失败：" + (err as Error).message, "error");
      }
    },
    [showToast],
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
    try {
      const ids = selectedList.map((r) => r.id);
      const result = await deleteHistory(ids);
      // items 按请求顺序回位；accepted 项以 task.id 为准，缺 task 时按序号配对。
      const deletedIds = new Set(
        result.items
          .map((item, index) => (item.accepted ? (item.task?.id ?? ids[index]) : null))
          .filter((value): value is number => value !== null),
      );
      setSelectedRecords(new Set());
      if (result.rejected_count > 0) {
        showToast(
          `已删除 ${result.accepted_count} 条，${result.rejected_count} 条删除失败`,
          "warning",
        );
      } else {
        showToast(`已删除 ${result.accepted_count} 条历史记录`, "success");
      }
      // 当前页被删空且非第 1 页时回退到上一页（页码变化会触发重新拉取）
      const remaining = records.filter((r) => !deletedIds.has(r.id));
      if (remaining.length === 0 && currentPage > 1) {
        setCurrentPage((page) => page - 1);
      } else {
        loadHistory();
      }
    } catch (err) {
      showToast("删除失败：" + (err as Error).message, "error");
      if (mountedRef.current) loadHistory();
    } finally {
      if (mountedRef.current) setIsBatchOperating(false);
    }
  }

  const toggleSelectAll = useCallback(() => {
    toggleAllRecords(records.map((r) => r.id));
  }, [toggleAllRecords, records]);

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
            {selectedCount === records.length && records.length > 0
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
        </ToolbarGroup>

        <ToolbarGroup className="toolbar-select-group">
          <span className="muted text-sm">筛选:</span>
          <select
            value={filterStatus}
            onChange={(e) => {
              setFilterStatus(e.target.value);
              setCurrentPage(1);
            }}
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
        ) : records.length === 0 ? (
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
            {records.map((record) => (
              <HistoryCard
                key={record.id}
                record={record}
                isSelected={selectedRecords.has(record.id)}
                onToggleSelection={toggleRecordSelection}
                onCopyUri={copyUri}
                onRetry={retryRecord}
                onDelete={deleteRecord}
              />
            ))}
          </div>
        )}
      </div>

      {total > 0 && (
        <PaginationControls
          currentPage={currentPage}
          pageSize={pageSize}
          totalFiles={total}
          onPageChange={setCurrentPage}
          onPageSizeChange={setPageSize}
        />
      )}
    </div>
  );
}
