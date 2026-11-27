import { useEffect, useRef, type KeyboardEvent } from "react";
import { formatBytes } from "@/shared/lib/format";
import { isShiftClick } from "@/shared/lib/selection";
import { Icon, SortIcon, type SortDirection } from "@/shared/components/Icon";
import { browseItemKey, type BrowseItemView } from "../model";
import type { SortField, BrowseContext } from "./FileToolbar";

type BrowseFolderViewProps = {
  isMobile: boolean;
  browseLoading: boolean;
  browseContext: BrowseContext;
  browseContents: BrowseItemView[];
  sortedBrowseContents: BrowseItemView[];
  selectedBrowseFiles: Set<string>;
  highlightName?: string | null;
  onSort: (field: SortField) => void;
  getSortIcon: (field: SortField) => SortDirection;
  onToggleAllBrowseFiles: () => void;
  onToggleBrowseFileSelection: (itemKey: string, shiftKey: boolean) => void;
  onSelectionKeyDown: (event: KeyboardEvent<HTMLElement>) => void;
  onNavigateIntoSubfolder: (name: string) => void;
  onDownload: (subpath: string) => void;
  onUnavailableDelete: () => void;
};

export function BrowseFolderView({
  isMobile,
  browseLoading,
  browseContext,
  browseContents,
  sortedBrowseContents,
  selectedBrowseFiles,
  highlightName,
  onSort,
  getSortIcon,
  onToggleAllBrowseFiles,
  onToggleBrowseFileSelection,
  onSelectionKeyDown,
  onNavigateIntoSubfolder,
  onDownload,
  onUnavailableDelete,
}: BrowseFolderViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!highlightName) return;
    const frame = requestAnimationFrame(() => {
      containerRef.current?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [highlightName, sortedBrowseContents]);

  if (browseLoading) return <div className="card text-center py-8"><p className="muted">加载中...</p></div>;
  if (sortedBrowseContents.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-icon"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg></div>
        <p className="font-medium mb-1">文件夹为空</p>
        <p className="muted text-base">此文件夹中没有文件</p>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="card p-0 overflow-hidden file-table-wrapper"
      data-testid="file-list"
      onKeyDown={onSelectionKeyDown}
    >
      {!isMobile && (
        <div className="table-header file-table-head browse-file-head">
          <input
            type="checkbox"
            checked={(() => {
              const allKeys = browseContents.map((item) => browseItemKey(browseContext.path, item.name));
              return allKeys.length > 0 && allKeys.every((key) => selectedBrowseFiles.has(key));
            })()}
            onChange={onToggleAllBrowseFiles}
            className="checkbox-sm cursor-pointer"
            aria-label="全选"
          />
          <button type="button" className="table-cell text-left sortable-header" onClick={() => onSort("name")}>名称 <SortIcon direction={getSortIcon("name")} /></button>
          <button type="button" className="table-cell text-left sortable-header" onClick={() => onSort("size")}>大小 <SortIcon direction={getSortIcon("size")} /></button>
          <div className="table-cell text-right">操作</div>
        </div>
      )}

      {isMobile ? (
        <div className="mobile-file-list">
          {sortedBrowseContents.map((item, index) => {
            const itemKey = browseItemKey(browseContext.path, item.name);
            const isHighlighted = highlightName != null && item.name === highlightName;
            return (
              <div key={item.name} className={`mobile-file-card${isHighlighted ? " file-locate-highlight" : ""}`} aria-current={isHighlighted ? "true" : undefined}>
                <div className="card-header">
                  <input type="checkbox" checked={selectedBrowseFiles.has(itemKey)} onChange={(event) => onToggleBrowseFileSelection(itemKey, isShiftClick(event.nativeEvent))} data-select-index={index} className="checkbox-sm cursor-pointer" aria-label={`选择 ${item.name}`} />
                  <div className="file-title">
                    <span className={`file-mark${item.isDirectory ? " folder" : ""}`}><Icon name={item.isDirectory ? "folder" : "file"} /></span>
                    {item.isDirectory ? (
                      <button type="button" className="mobile-name" onClick={() => onNavigateIntoSubfolder(item.name)}>{item.name}</button>
                    ) : <span className="mobile-name">{item.name}</span>}
                  </div>
                </div>
                <div className="card-meta">{item.isDirectory ? "-" : formatBytes(item.size)}</div>
                <div className="card-actions">
                  {item.isDirectory ? (
                    <button type="button" className="button secondary btn-sm" onClick={() => onNavigateIntoSubfolder(item.name)}>打开</button>
                  ) : (
                    <button type="button" className="button secondary btn-sm" onClick={() => onDownload([...browseContext.path, item.name].join("/"))}>下载</button>
                  )}
                  <button type="button" className="button secondary danger btn-sm" onClick={onUnavailableDelete}>删除</button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="file-rows">
          {sortedBrowseContents.map((item, index) => {
            const itemKey = browseItemKey(browseContext.path, item.name);
            const isHighlighted = highlightName != null && item.name === highlightName;
            const selected = selectedBrowseFiles.has(itemKey);
            return (
              <article key={item.name} className={`table-row file-row browse-file-row${selected ? " selected" : ""}${isHighlighted ? " file-locate-highlight" : ""}`} aria-current={isHighlighted ? "true" : undefined}>
                <input type="checkbox" checked={selected} onChange={(event) => onToggleBrowseFileSelection(itemKey, isShiftClick(event.nativeEvent))} data-select-index={index} className="checkbox-sm cursor-pointer" aria-label="选择文件" />
                <div className="task-main">
                  <span className={`file-mark${item.isDirectory ? " folder" : ""}`}><Icon name={item.isDirectory ? "folder" : "file"} /></span>
                  <div className="file-name">
                    {item.isDirectory ? (
                      <button type="button" className="file-name-btn" onClick={() => onNavigateIntoSubfolder(item.name)} title={item.name}>{item.name}</button>
                    ) : <strong title={item.name}>{item.name}</strong>}
                    <div className="file-meta"><span>{item.isDirectory ? "文件夹" : "文件"}</span></div>
                  </div>
                </div>
                <span className="file-size" data-label="大小">{item.isDirectory ? "-" : formatBytes(item.size)}</span>
                <div className="row-actions">
                  {item.isDirectory ? (
                    <button type="button" className="icon-btn" onClick={() => onNavigateIntoSubfolder(item.name)} title="打开" aria-label="打开"><Icon name="chevron-right" className="icon-sm" /></button>
                  ) : (
                    <button type="button" className="icon-btn" onClick={() => onDownload([...browseContext.path, item.name].join("/"))} title="下载" aria-label="下载"><Icon name="download" className="icon-sm" /></button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
