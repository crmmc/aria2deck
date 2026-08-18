import type { RefObject } from "react";
import { formatBytes } from "@/lib/utils";
import type { BrowseFileInfo } from "@/types";

type SortField = "name" | "size" | "created_at";
type SortOrder = "asc" | "desc";

type FileToolbarProps = {
  isInsideFolder: boolean;
  browseContext: { fileHash: string; fileName: string; path: string[] } | null;
  browseContents: BrowseFileInfo[];
  selectedBrowseFiles: Set<string>;
  selectedFiles: Set<number>;
  selectedSize: number;
  sortedFilesLength: number;
  sortField: SortField;
  sortOrder: SortOrder;
  toolbarSearchKeyword: string;
  searchGlobal: boolean;
  searchLoading: boolean;
  searchInputRef?: RefObject<HTMLInputElement>;
  isBatchOperating: boolean;
  onReturnToRoot: () => void;
  onNavigateToBreadcrumb: (index: number) => void;
  onSortFieldChange: (field: SortField) => void;
  onSortOrderChange: (order: SortOrder) => void;
  onToolbarSearchKeywordChange: (value: string) => void;
  onToolbarSearchKeyDown: (e: React.KeyboardEvent) => void;
  onSearchGlobalChange: (value: boolean) => void;
  onSearchSubmit: () => void;
  onToggleAllBrowseFiles: () => void;
  onBrowseBatchDownload: () => void;
  onToggleSelectAll: () => void;
  onBatchDownload: () => void;
  onBatchDelete: () => void;
  onOpenPackDialog: () => void;
};

export function FileToolbar({
  isInsideFolder,
  browseContext,
  browseContents,
  selectedBrowseFiles,
  selectedFiles,
  selectedSize,
  sortedFilesLength,
  sortField,
  sortOrder,
  toolbarSearchKeyword,
  searchGlobal,
  searchLoading,
  searchInputRef,
  isBatchOperating,
  onReturnToRoot,
  onNavigateToBreadcrumb,
  onSortFieldChange,
  onSortOrderChange,
  onToolbarSearchKeywordChange,
  onToolbarSearchKeyDown,
  onSearchGlobalChange,
  onSearchSubmit,
  onToggleAllBrowseFiles,
  onBrowseBatchDownload,
  onToggleSelectAll,
  onBatchDownload,
  onBatchDelete,
  onOpenPackDialog,
}: FileToolbarProps) {
  return (
    <div className="card filter-toolbar mb-4">
      <div className="filter-group path-breadcrumb">
        {isInsideFolder ? (
          <>
            <button
              type="button"
              className="path-segment"
              onClick={onReturnToRoot}
            >
              <span className="file-icon">📁</span>
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
            <span className="file-icon">📁</span>
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
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="search-input-icon"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="m21 21-4.35-4.35" />
          </svg>
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
              ✕
            </button>
          )}
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

        <div className="filter-group batch-actions">
          {isInsideFolder ? (
            <>
              {selectedBrowseFiles.size > 0 && (
                <>
                  <span className="muted text-sm">
                    已选 {selectedBrowseFiles.size} 项
                  </span>
                  <button
                    type="button"
                    className="button secondary btn-sm"
                    onClick={onBrowseBatchDownload}
                  >
                    批量下载
                  </button>
                </>
              )}
              {browseContents.length > 0 && (
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={onToggleAllBrowseFiles}
                >
                  {(() => {
                    const allKeys = browseContents
                      .map((f) => [...(browseContext?.path ?? []), f.name].join("/"));
                    return allKeys.length > 0 && allKeys.every((k) => selectedBrowseFiles.has(k))
                      ? "取消全选"
                      : "全选";
                  })()}
                </button>
              )}
            </>
          ) : (
            <>
              {selectedFiles.size > 0 && (
                <>
                  <span className="muted text-sm">
                    已选 {selectedFiles.size} 项 ({formatBytes(selectedSize)})
                  </span>
                  <button
                    type="button"
                    className="button secondary btn-sm"
                    onClick={onBatchDownload}
                    disabled={isBatchOperating}
                  >
                    批量下载
                  </button>
                  <button
                    type="button"
                    className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                    onClick={onBatchDelete}
                    disabled={isBatchOperating}
                  >
                    {isBatchOperating ? "删除中..." : "批量删除"}
                  </button>
                  <button
                    type="button"
                    className="button secondary btn-sm"
                    onClick={onOpenPackDialog}
                    disabled={isBatchOperating}
                  >
                    打包
                  </button>
                </>
              )}
              {sortedFilesLength > 0 && (
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={onToggleSelectAll}
                >
                  {selectedFiles.size === sortedFilesLength && sortedFilesLength > 0
                    ? "取消全选"
                    : "全选"}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export type { SortField, SortOrder };
