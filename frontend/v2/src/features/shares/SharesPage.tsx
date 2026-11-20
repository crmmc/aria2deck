"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { useToast } from "@/shared/components/toast-context";
import { useClipboard } from "@/shared/hooks/useClipboard";
import { deleteShares, fetchSharesAll, revokeAllShares, revokeShares, shareCopyText } from "./api";
import {
  filterRecords,
  initialSharesState,
  sharesReducer,
  sortShareRecords,
  toShareRecordView,
  type ShareRecordView,
} from "./model";
import { SharesToolbar } from "./components/SharesToolbar";
import { SharesList } from "./components/SharesList";

export function SharesPage() {
  const { showToast, showConfirm } = useToast();
  const [state, dispatch] = useReducer(sharesReducer, initialSharesState);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);

  const loadShares = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    dispatch({ type: "load_start" });
    try {
      // C21：顺序聚合全部 V2 页后再交给 reducer，保持 归档参考 无分页界面。
      const items = await fetchSharesAll();
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      dispatch({
        type: "load_success",
        records: sortShareRecords(items.map(toShareRecordView)),
      });
    } catch {
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      showToast("加载分享记录失败", "error");
    } finally {
      if (mountedRef.current && requestId === requestIdRef.current) {
        dispatch({ type: "load_finish" });
      }
    }
  }, [showToast]);

  useEffect(() => {
    mountedRef.current = true;
    loadShares();
    return () => {
      mountedRef.current = false;
    };
  }, [loadShares]);

  const copy = useClipboard();
  const copyLink = useCallback(
    (record: ShareRecordView) => {
      // C01：V2 不回传明文密码，有密码分享复制走 V2 安全格式（密码行占位提示）。
      void copy(shareCopyText({ code: record.shareCode, has_password: record.hasPassword }, null));
    },
    [copy],
  );

  const revokeShare = useCallback(
    async (id: number) => {
      try {
        const result = await revokeShares([id]);
        const first = result.results[0];
        if (result.rejected_count > 0 || !first?.ok) {
          showToast("操作失败：" + (first?.error?.message ?? "未知错误"), "error");
          return;
        }
        showToast("分享已失效", "success");
        if (mountedRef.current) loadShares();
      } catch (err) {
        showToast("操作失败：" + (err as Error).message, "error");
      }
    },
    [showToast, loadShares],
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
        const result = await deleteShares([id]);
        if (result.rejected_count > 0) {
          showToast("删除失败：" + (result.results[0]?.error?.message ?? "未知错误"), "error");
        } else {
          showToast("分享已删除", "success");
        }
        if (mountedRef.current) loadShares();
      } catch (err) {
        showToast("删除失败：" + (err as Error).message, "error");
      }
    },
    [showToast, showConfirm, loadShares],
  );

  const revokeAllSharesHandler = useCallback(async () => {
    // 界面不可达的 归档参考 防御分支：工具栏只在 hasRecords 时渲染该按钮，故豁免覆盖率。
    /* v8 ignore next 4 */
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
      await revokeAllShares();
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
    [state.records, state.searchKeyword, state.filterStatus],
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
      const result = await deleteShares(selectedList.map((r) => r.id));
      dispatch({ type: "clear_selected" });
      if (result.rejected_count > 0) {
        showToast(
          `已删除 ${result.accepted_count} 条分享记录，${result.rejected_count} 条删除失败`,
          "warning",
        );
      } else {
        showToast(`已删除 ${result.accepted_count} 条分享记录`, "success");
      }
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
        onRevokeAll={revokeAllSharesHandler}
        onFilterStatusChange={(status) =>
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
