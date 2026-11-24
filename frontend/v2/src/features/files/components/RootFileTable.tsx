import { type KeyboardEvent, type MouseEvent } from "react";
import { formatBytes } from "@/shared/lib/format";
import { Icon, SortIcon, type SortDirection } from "@/shared/components/Icon";
import type { FileView } from "../model";
import type { SortField } from "./FileToolbar";

type RootFileTableProps = {
  isMobile: boolean;
  loading: boolean;
  error: string | null;
  sortedFiles: FileView[];
  selectedFiles: Set<number>;
  renaming: number | null;
  newName: string;
  downloadingFile: number | null;
  highlightUserFileId?: number | null;
  onSort: (field: SortField) => void;
  getSortIcon: (field: SortField) => SortDirection;
  onToggleSelectAll: () => void;
  onToggleFileSelection: (id: number) => void;
  onNewNameChange: (value: string) => void;
  onRename: (file: FileView) => void;
  onCancelRename: () => void;
  onStartRename: (file: FileView) => void;
  onEnterFolder: (file: FileView) => void;
  onDownload: (file: FileView) => void;
  onShare: (file: FileView) => void;
  onDelete: (file: FileView) => void;
  formatDate: (ms: number) => string;
};

type RootFileListProps = Omit<RootFileTableProps, "isMobile" | "loading" | "error">;

function stopClick(event: MouseEvent) {
  event.stopPropagation();
}

function RenameControls({
  file,
  newName,
  onNewNameChange,
  onRename,
  onCancelRename,
  compact = false,
}: {
  file: FileView;
  newName: string;
  onNewNameChange: (value: string) => void;
  onRename: (file: FileView) => void;
  onCancelRename: () => void;
  compact?: boolean;
}) {
  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") onRename(file);
    if (event.key === "Escape") onCancelRename();
  };

  return (
    <div className={`rename-controls flex gap-2${compact ? " flex-1 compact" : ""}`}>
      <input
        className="input py-1 px-3 text-base"
        value={newName}
        onChange={(event) => onNewNameChange(event.target.value)}
        onKeyDown={handleKeyDown}
        ref={(element) => element?.focus()}
        aria-label="重命名文件"
        onClick={stopClick}
      />
      <button type="button" className="icon-btn" aria-label="确认重命名" onClick={(event) => { event.stopPropagation(); onRename(file); }}>
        <Icon name="check" className="icon-sm" />
      </button>
      <button type="button" className="icon-btn" aria-label="取消重命名" onClick={(event) => { event.stopPropagation(); onCancelRename(); }}>
        <Icon name="x" className="icon-sm" />
      </button>
    </div>
  );
}

function FileMark({ directory }: { directory: boolean }) {
  return <span className={`file-mark${directory ? " folder" : ""}`}><Icon name={directory ? "folder" : "file"} /></span>;
}

function RootFileMobileList({
  sortedFiles,
  selectedFiles,
  renaming,
  newName,
  downloadingFile,
  highlightUserFileId,
  onToggleFileSelection,
  onNewNameChange,
  onRename,
  onCancelRename,
  onStartRename,
  onEnterFolder,
  onDownload,
  onShare,
  onDelete,
  formatDate,
}: RootFileListProps) {
  return (
    <div className="mobile-file-list">
      {sortedFiles.map((file) => {
        const isHighlighted = highlightUserFileId != null && file.id === highlightUserFileId;
        return (
          <div key={file.id} className={`mobile-file-card${isHighlighted ? " file-locate-highlight" : ""}`} aria-current={isHighlighted ? "true" : undefined}>
            <div className="card-header">
              <input type="checkbox" checked={selectedFiles.has(file.id)} onChange={() => onToggleFileSelection(file.id)} className="checkbox-sm cursor-pointer" aria-label={`选择 ${file.name}`} />
              <div className="file-title">
                <FileMark directory={file.isDirectory} />
                {renaming === file.id ? (
                  <RenameControls file={file} newName={newName} onNewNameChange={onNewNameChange} onRename={onRename} onCancelRename={onCancelRename} compact />
                ) : file.isDirectory ? (
                  <button type="button" className="mobile-name" onClick={() => onEnterFolder(file)}>{file.name}</button>
                ) : <span className="mobile-name">{file.name}</span>}
              </div>
            </div>
            <div className="card-meta">{formatBytes(file.size)} • {formatDate(file.createdAtMs)}</div>
            <div className="card-actions">
              {file.isDirectory ? (
                <button type="button" className="button secondary btn-sm" onClick={() => onEnterFolder(file)}>浏览</button>
              ) : (
                <button type="button" className="button secondary btn-sm" onClick={() => onDownload(file)} disabled={downloadingFile === file.id}>
                  {downloadingFile === file.id ? "下载中..." : "下载"}
                </button>
              )}
              <button type="button" className="button secondary btn-sm" onClick={() => onStartRename(file)}>重命名</button>
              <button type="button" className="button secondary btn-sm" onClick={() => onShare(file)}>分享</button>
              <button type="button" className="button secondary danger btn-sm" onClick={() => onDelete(file)}>删除</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RootFileDesktopTable({
  sortedFiles,
  selectedFiles,
  renaming,
  newName,
  downloadingFile,
  highlightUserFileId,
  onSort,
  getSortIcon,
  onToggleSelectAll,
  onToggleFileSelection,
  onNewNameChange,
  onRename,
  onCancelRename,
  onStartRename,
  onEnterFolder,
  onDownload,
  onShare,
  onDelete,
  formatDate,
}: RootFileListProps) {
  return (
    <>
      <div className="table-header file-table-head">
        <input type="checkbox" checked={selectedFiles.size === sortedFiles.length && sortedFiles.length > 0} onChange={onToggleSelectAll} className="checkbox-sm cursor-pointer" aria-label="全选" />
        <button type="button" className="table-cell text-left sortable-header" onClick={() => onSort("name")}>名称 <SortIcon direction={getSortIcon("name")} /></button>
        <button type="button" className="table-cell text-left sortable-header" onClick={() => onSort("size")}>大小 <SortIcon direction={getSortIcon("size")} /></button>
        <button type="button" className="table-cell text-left sortable-header" onClick={() => onSort("created_at")}>添加时间 <SortIcon direction={getSortIcon("created_at")} /></button>
        <div className="table-cell text-right">操作</div>
      </div>
      <div className="file-rows">
        {sortedFiles.map((file) => {
          const isHighlighted = highlightUserFileId != null && file.id === highlightUserFileId;
          const selected = selectedFiles.has(file.id);
          return (
            <article
              key={file.id}
              data-file-id={file.id}
              className={`table-row file-row${selected ? " selected" : ""}${isHighlighted ? " file-locate-highlight" : ""}`}
              aria-current={isHighlighted ? "true" : undefined}
            >
              <input type="checkbox" checked={selected} onChange={() => onToggleFileSelection(file.id)} className="checkbox-sm cursor-pointer" aria-label="选择文件" />
              <div className="task-main">
                <FileMark directory={file.isDirectory} />
                <div className="file-name">
                  {renaming === file.id ? (
                    <RenameControls file={file} newName={newName} onNewNameChange={onNewNameChange} onRename={onRename} onCancelRename={onCancelRename} />
                  ) : (
                    <>
                      {file.isDirectory ? (
                        <button type="button" className="file-name-btn" onClick={() => onEnterFolder(file)} title={file.name}>{file.name}</button>
                      ) : <strong title={file.name}>{file.name}</strong>}
                      <div className="file-meta"><span>{file.isDirectory ? "文件夹" : "文件"}</span></div>
                    </>
                  )}
                </div>
              </div>
              <span className="file-size" data-label="大小">{formatBytes(file.size)}</span>
              <span className="file-date" data-label="添加时间">{formatDate(file.createdAtMs)}</span>
              <div className="row-actions">
                {file.isDirectory ? (
                  <button type="button" className="icon-btn" onClick={() => onEnterFolder(file)} title="浏览" aria-label="浏览"><Icon name="chevron-right" className="icon-sm" /></button>
                ) : (
                  <button type="button" className="icon-btn" onClick={() => onDownload(file)} disabled={downloadingFile === file.id} title="下载" aria-label={downloadingFile === file.id ? "下载中..." : "下载"}><Icon name="download" className="icon-sm" /></button>
                )}
                <button type="button" className="icon-btn" onClick={() => onStartRename(file)} title="重命名" aria-label="重命名"><Icon name="edit" className="icon-sm" /></button>
                <button type="button" className="icon-btn" onClick={() => onShare(file)} title="分享" aria-label="分享"><Icon name="share" className="icon-sm" /></button>
                <button type="button" className="icon-btn danger" onClick={() => onDelete(file)} title="删除" aria-label="删除"><Icon name="trash" className="icon-sm" /></button>
              </div>
            </article>
          );
        })}
      </div>
    </>
  );
}

export function RootFileTable(props: RootFileTableProps) {
  if (props.loading) return <div className="card text-center py-8"><p className="muted">加载中...</p></div>;
  if (props.error) return <div className="card text-center py-8"><p className="text-danger">{props.error}</p></div>;
  if (props.sortedFiles.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-icon"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg></div>
        <p className="font-medium mb-1">暂无文件</p>
        <p className="muted text-base">下载完成的文件将显示在这里</p>
      </div>
    );
  }
  return (
    <div className="card p-0 overflow-hidden file-table-wrapper" data-testid="file-list">
      {props.isMobile ? <RootFileMobileList {...props} /> : <RootFileDesktopTable {...props} />}
    </div>
  );
}
