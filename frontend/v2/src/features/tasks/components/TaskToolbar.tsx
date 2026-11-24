import { useMemo } from "react";

import { Icon } from "@/shared/components/Icon";
import { SegmentedControl } from "@/shared/components/SegmentedControl";
import { ToolbarSearchInput, ToolbarShell } from "@/shared/components/Toolbar";

type TaskToolbarProps = {
  selectedCount: number;
  filteredCount: number;
  currentCount: number;
  activeCount: number;
  filterStatus: string;
  sortBy: string;
  searchKeyword: string;
  hasActiveTasks: boolean;
  isBatchOperating: boolean;
  onToggleSelectAll: () => void;
  onBatchCancel: () => void;
  onFilterStatusChange: (status: string) => void;
  onSortByChange: (sort: string) => void;
  onSearchKeywordChange: (keyword: string) => void;
};

export function TaskToolbar({
  selectedCount,
  filteredCount,
  currentCount,
  activeCount,
  filterStatus,
  sortBy,
  searchKeyword,
  hasActiveTasks,
  isBatchOperating,
  onToggleSelectAll,
  onBatchCancel,
  onFilterStatusChange,
  onSortByChange,
  onSearchKeywordChange,
}: TaskToolbarProps) {
  const isBulk = selectedCount > 0;
  const options = useMemo(
    () => [
      { value: "all", label: "当前任务", count: currentCount },
      { value: "active", label: "进行中", count: activeCount },
    ],
    [activeCount, currentCount],
  );

  return (
    <ToolbarShell className={isBulk ? "is-bulk" : ""}>
      <div className="toolbar-default" hidden={isBulk}>
        <SegmentedControl
          ariaLabel="任务筛选"
          options={options}
          value={filterStatus}
          onChange={onFilterStatusChange}
        />
        <div className="toolbar-spacer" />
        <label className="toolbar-label" htmlFor="task-search">搜索</label>
        <ToolbarSearchInput
          id="task-search"
          placeholder="搜索任务"
          value={searchKeyword}
          onChange={onSearchKeywordChange}
          ariaLabel="搜索任务"
          clearAriaLabel="清空任务搜索"
        />
        <label className="toolbar-label" htmlFor="task-sort">排序</label>
        <div className="select-wrap">
          <select
            id="task-sort"
            value={sortBy}
            onChange={(event) => onSortByChange(event.target.value)}
            className="select"
            aria-label="任务排序"
          >
            <option value="default">最近更新</option>
            <option value="speed">下载速度</option>
            <option value="progress">下载进度</option>
          </select>
          <Icon name="chevron-down" />
        </div>
      </div>
      <div className="toolbar-bulk" hidden={!isBulk}>
        <span className="bulk-count">已选 <strong>{selectedCount}</strong> 个任务</span>
        <div className="bulk-actions">
          <button
            type="button"
            className="button secondary btn-sm"
            onClick={onToggleSelectAll}
          >
            {selectedCount === filteredCount && filteredCount > 0 ? "取消全选" : "全选"}
          </button>
          {hasActiveTasks && (
            <button
              type="button"
              className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
              onClick={onBatchCancel}
              disabled={isBatchOperating}
            >
              <Icon name="trash" className="icon-sm" />
              取消下载
            </button>
          )}
        </div>
      </div>
    </ToolbarShell>
  );
}
