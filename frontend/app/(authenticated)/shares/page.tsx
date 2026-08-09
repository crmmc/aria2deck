"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useClipboard } from "@/hooks/useClipboard";
import { sharesReducer, initialSharesState, filterRecords } from "./shareState";
import type { ShareFilterStatus } from "./shareState";
import { SharesToolbar } from "./_components/SharesToolbar";
import { SharesList } from "./_components/SharesList";

export default function SharesPage() {
  const { showToast, showConfirm } = useToast();
  const [state, dispatch] = useReducer(sharesReducer, initialSharesState);
  const mountedRef = useRef(true);

  const loadShares = useCallback(async () => {
    dispatch({ type: "load_start" });
    try {
      const shares = await api.listShares();
      if (mountedRef.current) dispatch({ type: "load_success", records: shares });
    } catch {
      if (!mountedRef.current) return;
      showToast("加载分享记录失败", "error");
    } finally {
      if (mountedRef.current) dispatch({ type: "load_finish" });
    }
  }, [showToast]);

  useEffect(() => {
    mountedRef.current = true;
    loadShares();
    return () => { mountedRef.current = false; };
  }, [loadShares]);

  const copy = useClipboard();
  const copyLink = useCallback(
    (shareCode: string, password?: string | null) => {
      const base = `${window.location.origin}/s/${shareCode}`;
      const link = password ? `${base}?password=${encodeURIComponent(password)}` : base;
      copy(link);
    },
    [copy]
  );

  const revokeShare = useCallback(
    async (id: number) => {
      try {
        await api.revokeShare(id);
        showToast("分享已失效", "success");
        if (mountedRef.current) loadShares();
      } catch (err) {
        showToast("操作失败：" + (err as Error).message, "error");
      }
    },
    [showToast, loadShares]
  );

  const deleteShare = useCallback(
    async (id: number) => {
      const confirmed = await showConfirm({
        title: "删除分享",
        message: "确定要删除这条分享记录吗？",
        confirmText: "删除",
        danger: true,
      });
      if (!confirmed) return;

      try {
        await api.deleteShare(id);
        showToast("分享已删除", "success");
        if (mountedRef.current) loadShares();
      } catch (err) {
        showToast("删除失败：" + (err as Error).message, "error");
      }
    },
    [showToast, showConfirm, loadShares]
  );

  const revokeAllShares = useCallback(async () => {
    if (state.records.length === 0) {
      showToast("没有分享记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "一键失效全部",
      message: "确定要让所有活跃的分享链接失效吗？此操作不可恢复。",
      confirmText: "确定失效",
      danger: true,
    });
    if (!confirmed) return;

    dispatch({ type: "set_operating", operating: true });
    try {
      await api.revokeAllShares();
      showToast("已让所有分享失效", "success");
      if (mountedRef.current) loadShares();
    } catch (err) {
      showToast("操作失败：" + (err as Error).message, "error");
    } finally {
      if (mountedRef.current) dispatch({ type: "set_operating", operating: false });
    }
  }, [state.records.length, showToast, showConfirm, loadShares]);

  const toggleRecordSelection = useCallback((id: number) => {
    dispatch({ type: "toggle_selected", id });
  }, []);

  const filteredRecords = useMemo(
    () => filterRecords(state.records, state.searchKeyword, state.filterStatus),
    [state.records, state.searchKeyword, state.filterStatus]
  );

  const toggleSelectAll = useCallback(() => {
    if (state.selectedIds.size === filteredRecords.length) {
      dispatch({ type: "clear_selected" });
    } else {
      dispatch({ type: "set_selected", ids: filteredRecords.map((r) => r.id) });
    }
  }, [state.selectedIds.size, filteredRecords]);

  const batchDeleteShares = useCallback(async () => {
    const selectedList = state.records.filter((r) => state.selectedIds.has(r.id));
    if (selectedList.length === 0) {
      showToast("请先选择要删除的记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "删除分享",
      message: `确定要删除选中的 ${selectedList.length} 条分享记录吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    dispatch({ type: "set_operating", operating: true });
    try {
      await Promise.all(selectedList.map((r) => api.deleteShare(r.id)));
      dispatch({ type: "clear_selected" });
      showToast(`已删除 ${selectedList.length} 条分享记录`, "success");
    } catch (err) {
      showToast("部分删除失败：" + (err as Error).message, "error");
    } finally {
      if (mountedRef.current) {
        dispatch({ type: "set_operating", operating: false });
        loadShares();
      }
    }
  }, [state.records, state.selectedIds, showToast, showConfirm, loadShares]);

  return (
    <div className="glass-frame full-height animate-in">
      <div className="space-between mb-7">
        <div>
          <h1 className="text-2xl">分享管理</h1>
          <p className="muted">管理你的文件分享链接</p>
        </div>
      </div>

      <SharesToolbar
        selectedCount={state.selectedIds.size}
        filteredCount={filteredRecords.length}
        filterStatus={state.filterStatus}
        searchKeyword={state.searchKeyword}
        isOperating={state.isOperating}
        hasRecords={state.records.length > 0}
        onToggleSelectAll={toggleSelectAll}
        onBatchDelete={batchDeleteShares}
        onRevokeAll={revokeAllShares}
        onFilterStatusChange={(status: ShareFilterStatus) =>
          dispatch({ type: "set_filter_status", status })
        }
        onSearchKeywordChange={(keyword: string) =>
          dispatch({ type: "set_search_keyword", keyword })
        }
      />

      <div className="task-list">
        <SharesList
          loading={state.loading}
          filteredRecords={filteredRecords}
          selectedIds={state.selectedIds}
          onToggleSelection={toggleRecordSelection}
          onCopyLink={copyLink}
          onRevoke={revokeShare}
          onDelete={deleteShare}
        />
      </div>
    </div>
  );
}
