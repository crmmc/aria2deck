import { ToolbarGroup, ToolbarSearchInput, ToolbarShell } from "@/components/ui/Toolbar";

type TaskToolbarProps = {
  selectedCount: number;
  filteredCount: number;
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
  return (
    <ToolbarShell>
      <ToolbarGroup className="toolbar-actions-group">
        <button
          type="button"
          className="button secondary btn-sm"
          onClick={onToggleSelectAll}
        >
          {selectedCount === filteredCount && filteredCount > 0
            ? "取消全选"
            : "全选"}
        </button>
        {selectedCount > 0 && (
          <>
            <span className="muted text-sm">
              已选 {selectedCount} 项
            </span>
            {hasActiveTasks && (
              <button
                type="button"
                className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                onClick={onBatchCancel}
                disabled={isBatchOperating}
              >
                取消下载
              </button>
            )}
          </>
        )}
      </ToolbarGroup>

      <ToolbarGroup className="toolbar-select-group">
        <span className="muted text-sm">筛选:</span>
        <select
          value={filterStatus}
          onChange={(e) => onFilterStatusChange(e.target.value)}
          className="select"
          aria-label="筛选任务"
        >
          <option value="all">当前任务</option>
          <option value="active">进行中</option>
        </select>
      </ToolbarGroup>

      <ToolbarGroup className="toolbar-select-group">
        <span className="muted text-sm">排序:</span>
        <select
          value={sortBy}
          onChange={(e) => onSortByChange(e.target.value)}
          className="select"
          aria-label="排序方式"
        >
          <option value="default">默认</option>
          <option value="speed">下载速度</option>
          <option value="progress">下载进度</option>
        </select>
      </ToolbarGroup>

      <ToolbarGroup className="toolbar-search-group">
        <ToolbarSearchInput
          placeholder="搜索任务..."
          value={searchKeyword}
          onChange={onSearchKeywordChange}
          ariaLabel="搜索任务"
        />
      </ToolbarGroup>
    </ToolbarShell>
  );
}
