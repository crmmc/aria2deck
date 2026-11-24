import { useMemo } from "react";

import { SegmentedControl } from "@/shared/components/SegmentedControl";
import { ToolbarSearchInput, ToolbarShell } from "@/shared/components/Toolbar";
import type { ShareFilterStatus } from "../model";

type SharesToolbarProps = {
  selectedCount: number;
  filteredCount: number;
  filterCounts: Record<ShareFilterStatus, number>;
  filterStatus: ShareFilterStatus;
  searchKeyword: string;
  isOperating: boolean;
  hasRecords: boolean;
  onToggleSelectAll: () => void;
  onBatchDelete: () => void;
  onRevokeAll: () => void;
  onFilterStatusChange: (status: ShareFilterStatus) => void;
  onSearchKeywordChange: (keyword: string) => void;
};

export function SharesToolbar({
  selectedCount,
  filteredCount,
  filterCounts,
  filterStatus,
  searchKeyword,
  isOperating,
  hasRecords,
  onToggleSelectAll,
  onBatchDelete,
  onRevokeAll,
  onFilterStatusChange,
  onSearchKeywordChange,
}: SharesToolbarProps) {
  const isBulk = selectedCount > 0;
  const options = useMemo(
    () => [
      { value: "all" as const, label: "全部", count: filterCounts.all },
      { value: "active" as const, label: "活跃", count: filterCounts.active },
      { value: "expired" as const, label: "已过期", count: filterCounts.expired },
      { value: "revoked" as const, label: "已失效", count: filterCounts.revoked },
    ],
    [filterCounts],
  );
  const revokeAllButton = hasRecords ? (
    <button
      type="button"
      className={`button secondary btn-sm${isOperating ? " opacity-60" : ""}`}
      onClick={onRevokeAll}
      disabled={isOperating}
    >
      一键失效全部
    </button>
  ) : null;

  return (
    <ToolbarShell className={isBulk ? "is-bulk" : ""}>
      <div className="toolbar-default" hidden={isBulk}>
        <SegmentedControl
          ariaLabel="分享筛选"
          options={options}
          value={filterStatus}
          onChange={onFilterStatusChange}
        />
        {revokeAllButton}
        <div className="toolbar-spacer" />
        <label className="toolbar-label" htmlFor="share-search">搜索</label>
        <ToolbarSearchInput
          id="share-search"
          placeholder="搜索文件或提取码"
          value={searchKeyword}
          onChange={onSearchKeywordChange}
          ariaLabel="搜索分享"
        />
      </div>
      <div className="toolbar-bulk" hidden={!isBulk}>
        <span className="bulk-count">已选 <strong>{selectedCount}</strong> 个分享</span>
        <div className="bulk-actions">
          <button
            type="button"
            className="button secondary btn-sm"
            onClick={onToggleSelectAll}
          >
            {selectedCount === filteredCount && filteredCount > 0 ? "取消全选" : "全选"}
          </button>
          <button
            type="button"
            className={`button secondary danger btn-sm${isOperating ? " opacity-60" : ""}`}
            onClick={onBatchDelete}
            disabled={isOperating}
          >
            删除选中
          </button>
          {revokeAllButton}
        </div>
      </div>
    </ToolbarShell>
  );
}
