import type { RefObject } from "react";
import { Icon } from "@/shared/components/Icon";
import type { BrowseItemView } from "../model";

export type SortField = "name" | "size" | "created_at";
export type SortOrder = "asc" | "desc";

export type BrowseContext = {
  fileId: number;
  fileName: string;
  path: string[];
};

type FileToolbarProps = {
  isInsideFolder: boolean;
  browseContext: BrowseContext | null;
  browseContents: BrowseItemView[];
  sortField: SortField;
  sortOrder: SortOrder;
  toolbarSearchKeyword: string;
  searchGlobal: boolean;
  searchLoading: boolean;
  searchInputRef?: RefObject<HTMLInputElement>;
  onReturnToRoot: () => void;
  onNavigateToBreadcrumb: (index: number) => void;
  onSortFieldChange: (field: SortField) => void;
  onSortOrderChange: (order: SortOrder) => void;
  onToolbarSearchKeywordChange: (value: string) => void;
  onToolbarSearchKeyDown: (e: React.KeyboardEvent) => void;
  onSearchGlobalChange: (value: boolean) => void;
  onSearchSubmit: () => void;
};

export function FileToolbar({
  isInsideFolder,
  browseContext,
  browseContents,
  sortField,
  sortOrder,
  toolbarSearchKeyword,
  searchGlobal,
  searchLoading,
  searchInputRef,
  onReturnToRoot,
  onNavigateToBreadcrumb,
  onSortFieldChange,
  onSortOrderChange,
  onToolbarSearchKeywordChange,
  onToolbarSearchKeyDown,
  onSearchGlobalChange,
  onSearchSubmit,
}: FileToolbarProps) {
  return (
    <div className="card filter-toolbar file-toolbar mb-4">
      <div className="filter-group path-breadcrumb">
        {isInsideFolder ? (
          <>
            <button
              type="button"
              className="path-segment"
              onClick={onReturnToRoot}
            >
              <span className="file-icon"><Icon name="folder" /></span>
              <span className="text-sm font-medium">根目录</span>
            </button>
            <span className="path-separator">/</span>
            <button
              type="button"
              className="path-segment"
              onClick={() => onNavigateToBreadcrumb(-1)}
            >
              {browseContext!.fileName}
            </button>
            {browseContext!.path.map((segment, index) => (
              <span key={browseContext!.path.slice(0, index + 1).join("/")} className="path-segment-wrapper">
                <span className="path-separator">/</span>
                <button
                  type="button"
                  className="path-segment"
                  onClick={() => onNavigateToBreadcrumb(index)}
                >
                  {segment}
                </button>
              </span>
            ))}
            <span className="muted text-sm">({browseContents.length} 项)</span>
          </>
        ) : (
          <>
            <span className="file-icon"><Icon name="folder" /></span>
            <span className="text-sm font-medium">根目录</span>
          </>
        )}
      </div>

      <div className="search-sort-row">
        <div className="filter-group sort-group">
          <select
            className="select"
            value={`${sortField}-${sortOrder}`}
            onChange={(e) => {
              const [field, order] = e.target.value.split("-") as [SortField, SortOrder];
              onSortFieldChange(field);
              onSortOrderChange(order);
            }}
            aria-label="排序方式"
          >
            <option value="created_at-desc">时间 (最新)</option>
            <option value="created_at-asc">时间 (最早)</option>
            <option value="name-asc">名称 (A-Z)</option>
            <option value="name-desc">名称 (Z-A)</option>
            <option value="size-desc">大小 (最大)</option>
            <option value="size-asc">大小 (最小)</option>
          </select>
        </div>

        <div className="filter-group search-input-group">
          <div className="search-field">
            <Icon name="search" className="search-input-icon" />
            <input
              ref={searchInputRef}
              type="text"
              className="toolbar-search-input"
              placeholder="输入文件名，点查询或按回车"
              value={toolbarSearchKeyword}
              onChange={(e) => onToolbarSearchKeywordChange(e.target.value)}
              onKeyDown={onToolbarSearchKeyDown}
              aria-label="搜索文件"
            />
            {toolbarSearchKeyword && (
              <button
                type="button"
                className="search-clear-btn"
                onClick={() => onToolbarSearchKeywordChange("")}
                aria-label="清除搜索"
              >
                <Icon name="x" className="icon-sm" />
              </button>
            )}
          </div>
          <label className="search-global-toggle">
            <input
              type="checkbox"
              checked={searchGlobal}
              onChange={(e) => onSearchGlobalChange(e.target.checked)}
              aria-label="全局"
            />
            全局
          </label>
          <button
            type="button"
            className="button secondary btn-sm"
            onClick={onSearchSubmit}
            disabled={searchLoading}
          >
            {searchLoading ? "查询中..." : "查询"}
          </button>
        </div>
      </div>
    </div>
  );
}
