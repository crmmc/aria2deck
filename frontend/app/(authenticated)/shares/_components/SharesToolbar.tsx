import { ToolbarGroup, ToolbarSearchInput, ToolbarShell } from "@/components/ui/Toolbar";
import type { ShareFilterStatus } from "../shareState";

type SharesToolbarProps = {
  selectedCount: number;
  filteredCount: number;
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
            <button
              type="button"
              className={`button secondary danger btn-sm${isOperating ? " opacity-60" : ""}`}
              onClick={onBatchDelete}
              disabled={isOperating}
            >
              删除选中
            </button>
          </>
        )}
        {hasRecords && (
          <button
            type="button"
            className={`button secondary btn-sm${isOperating ? " opacity-60" : ""}`}
            onClick={onRevokeAll}
            disabled={isOperating}
          >
            一键失效全部
          </button>
        )}
      </ToolbarGroup>

      <ToolbarGroup className="toolbar-select-group">
        <span className="muted text-sm">筛选:</span>
        <select
          aria-label="分享状态筛选"
          value={filterStatus}
          onChange={(e) => onFilterStatusChange(e.target.value as ShareFilterStatus)}
          className="select"
        >
          <option value="all">全部</option>
          <option value="active">活跃</option>
          <option value="expired">已过期</option>
          <option value="revoked">已失效</option>
        </select>
      </ToolbarGroup>

      <ToolbarGroup className="toolbar-search-group">
        <ToolbarSearchInput
          placeholder="搜索分享..."
          value={searchKeyword}
          onChange={onSearchKeywordChange}
          ariaLabel="搜索分享"
        />
      </ToolbarGroup>
    </ToolbarShell>
  );
}
