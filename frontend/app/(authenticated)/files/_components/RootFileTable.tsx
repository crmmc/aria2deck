import type { KeyboardEvent, MouseEvent } from "react";
import { AutoSizer } from "react-virtualized-auto-sizer";
import { List } from "react-window";
import { formatBytes } from "@/lib/utils";
import type { FileInfo } from "@/types";
import type { SortField } from "./FileToolbar";

type RootFileTableProps = {
  isMobile: boolean;
  loading: boolean;
  error: string | null;
  sortedFiles: FileInfo[];
  selectedFiles: Set<number>;
  renaming: number | null;
  newName: string;
  downloadingFile: number | null;
  onSort: (field: SortField) => void;
  getSortIcon: (field: SortField) => string;
  onToggleSelectAll: () => void;
  onToggleFileSelection: (id: number) => void;
  onNewNameChange: (value: string) => void;
  onRename: (file: FileInfo) => void;
  onCancelRename: () => void;
  onStartRename: (file: FileInfo) => void;
  onEnterFolder: (file: FileInfo) => void;
  onDownload: (contentHash: string, fileName: string, fileId?: number, subpath?: string) => void;
  onShare: (file: FileInfo) => void;
  onDelete: (file: FileInfo) => void;
  formatDate: (date: string) => string;
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
  file: FileInfo;
  newName: string;
  onNewNameChange: (value: string) => void;
  onRename: (file: FileInfo) => void;
  onCancelRename: () => void;
  compact?: boolean;
}) {
  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") onRename(file);
    if (event.key === "Escape") onCancelRename();
  };

  return (
    <div className={`flex gap-2${compact ? " flex-1" : ""}`} style={compact ? { minWidth: 0 } : undefined}>
      <input
        className="input py-1 px-3 text-base"
        value={newName}
        onChange={(event) => onNewNameChange(event.target.value)}
        onKeyDown={handleKeyDown}
        ref={(element) => element?.focus()}
        aria-label="重命名文件"
        onClick={stopClick}
      />
      <button
        type="button"
        className="button secondary btn-sm"
        aria-label="确认重命名"
        onClick={(event) => { event.stopPropagation(); onRename(file); }}
      >
        ✓
      </button>
      <button
        type="button"
        className="button secondary btn-sm"
        aria-label="取消重命名"
        onClick={(event) => { event.stopPropagation(); onCancelRename(); }}
      >
        ✕
      </button>
    </div>
  );
}

function RootFileMobileList({
  sortedFiles,
  selectedFiles,
  renaming,
  newName,
  downloadingFile,
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
    <div style={{ maxHeight: "60vh", overflowY: "auto" }}>
      {sortedFiles.map((file) => (
        <div key={file.id} className="mobile-file-card">
          <div className="card-header">
            <input
              type="checkbox"
              checked={selectedFiles.has(file.id)}
              onChange={() => onToggleFileSelection(file.id)}
              className="checkbox-sm cursor-pointer"
              aria-label={`选择 ${file.name}`}
            />
            <div className="file-title">
              <span className="file-icon">
                {file.is_directory ? "📁" : "📄"}
              </span>
              {renaming === file.id ? (
                <RenameControls
                  file={file}
                  newName={newName}
                  onNewNameChange={onNewNameChange}
                  onRename={onRename}
                  onCancelRename={onCancelRename}
                  compact
                />
              ) : file.is_directory ? (
                <button
                  type="button"
                  className="mobile-name"
                  onClick={() => onEnterFolder(file)}
                >
                  {file.name}
                </button>
              ) : (
                <span className="mobile-name">{file.name}</span>
              )}
            </div>
          </div>
          <div className="card-meta">
            {formatBytes(file.size)} • {formatDate(file.created_at)}
          </div>
          <div className="card-actions">
            {file.is_directory ? (
              <button
                type="button"
                className="button secondary btn-sm"
                onClick={() => onEnterFolder(file)}
              >
                浏览
              </button>
            ) : (
              <button
                type="button"
                className="button secondary btn-sm"
                onClick={() => onDownload(file.content_hash, file.name, file.id)}
                disabled={downloadingFile === file.id}
              >
                {downloadingFile === file.id ? "下载中..." : "下载"}
              </button>
            )}
            <button type="button" className="button secondary btn-sm" onClick={() => onStartRename(file)}>
              重命名
            </button>
            <button type="button" className="button secondary btn-sm" onClick={() => onShare(file)}>
              分享
            </button>
            <button type="button" className="button secondary danger btn-sm" onClick={() => onDelete(file)}>
              删除
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function RootFileDesktopTable({
  sortedFiles,
  selectedFiles,
  renaming,
  newName,
  downloadingFile,
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
      <div className="table-header" style={{ display: "grid", gridTemplateColumns: "40px minmax(220px, 1fr) 120px 180px 300px", paddingRight: "16px" }}>
        <div className="table-cell text-left">
          <input
            type="checkbox"
            checked={selectedFiles.size === sortedFiles.length && sortedFiles.length > 0}
            onChange={onToggleSelectAll}
            className="checkbox-sm cursor-pointer"
            aria-label="全选"
          />
        </div>
        <button type="button" className="table-cell text-left sortable-header" onClick={() => onSort("name")}>
          名称 <span className="sort-icon">{getSortIcon("name")}</span>
        </button>
        <button type="button" className="table-cell text-right sortable-header" onClick={() => onSort("size")}>
          大小 <span className="sort-icon">{getSortIcon("size")}</span>
        </button>
        <button type="button" className="table-cell text-right sortable-header" onClick={() => onSort("created_at")}>
          添加时间 <span className="sort-icon">{getSortIcon("created_at")}</span>
        </button>
        <div className="table-cell text-right">操作</div>
      </div>

      <div style={{ flex: 1, minHeight: 320 }}>
        <AutoSizer renderProp={({ height, width }) => {
          const safeHeight = typeof height === "number" && height > 0 ? height : 400;
          const safeWidth = typeof width === "number" && width > 0 ? width : 1200;
          return (
            <List
              style={{ height: safeHeight, width: safeWidth }}
              rowCount={sortedFiles.length}
              rowHeight={80}
              rowProps={{}}
              rowComponent={({ index, style }) => {
                const file = sortedFiles[index];
                return (
                  <div style={style} key={file.id}>
                    <div
                      className="table-row transition-bg"
                      style={{
                        display: "grid",
                        gridTemplateColumns: "40px minmax(220px, 1fr) 120px 180px 300px",
                        height: "100%",
                        alignItems: "flex-start",
                        overflow: "hidden",
                      }}
                    >
                      <div className="table-cell" style={{ paddingTop: "20px" }}>
                        <input
                          type="checkbox"
                          checked={selectedFiles.has(file.id)}
                          onChange={() => onToggleFileSelection(file.id)}
                          className="checkbox-sm cursor-pointer"
                          aria-label="选择文件"
                        />
                      </div>
                      <div className="table-cell" data-label="名称" style={{ paddingTop: "14px", paddingBottom: "14px", overflow: "hidden" }}>
                        {renaming === file.id ? (
                          <RenameControls
                            file={file}
                            newName={newName}
                            onNewNameChange={onNewNameChange}
                            onRename={onRename}
                            onCancelRename={onCancelRename}
                          />
                        ) : (
                          <div className="flex items-center gap-2" style={{ minWidth: 0 }}>
                            <span className="file-icon" style={{ flexShrink: 0 }}>
                              {file.is_directory ? "📁" : "📄"}
                            </span>
                            {file.is_directory ? (
                              <button
                                type="button"
                                className="file-name-btn"
                                onClick={() => onEnterFolder(file)}
                                title={file.name}
                                style={{ wordBreak: "break-all", textAlign: "left", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}
                              >
                                {file.name}
                              </button>
                            ) : (
                              <span className="text-base" title={file.name} style={{ wordBreak: "break-all", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{file.name}</span>
                            )}
                          </div>
                        )}
                      </div>
                      <div className="table-cell text-right muted text-base" data-label="大小" style={{ paddingTop: "20px" }}>
                        {formatBytes(file.size)}
                      </div>
                      <div className="table-cell text-right muted text-sm" data-label="添加时间" style={{ paddingTop: "22px" }}>
                        {formatDate(file.created_at)}
                      </div>
                      <div className="table-cell text-right" style={{ paddingTop: "14px" }}>
                        <div className="flex gap-2 flex-end">
                          {file.is_directory ? (
                            <button type="button" className="button secondary btn-sm" onClick={() => onEnterFolder(file)}>
                              浏览
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="button secondary btn-sm"
                              onClick={() => onDownload(file.content_hash, file.name, file.id)}
                              disabled={downloadingFile === file.id}
                            >
                              {downloadingFile === file.id ? "下载中..." : "下载"}
                            </button>
                          )}
                          <button type="button" className="button secondary btn-sm" onClick={() => onStartRename(file)}>
                            重命名
                          </button>
                          <button type="button" className="button secondary btn-sm" onClick={() => onShare(file)}>
                            分享
                          </button>
                          <button type="button" className="button secondary danger btn-sm" onClick={() => onDelete(file)}>
                            删除
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                );
              }}
            />
          );
        }}
        />
      </div>
    </>
  );
}

export function RootFileTable(props: RootFileTableProps) {
  if (props.loading) {
    return (
      <div className="card text-center py-8">
        <p className="muted">加载中...</p>
      </div>
    );
  }

  if (props.error) {
    return (
      <div className="card text-center py-8">
        <p className="text-danger">{props.error}</p>
      </div>
    );
  }

  if (props.sortedFiles.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
        </div>
        <p className="font-medium mb-1">暂无文件</p>
        <p className="muted text-base">下载完成的文件将显示在这里</p>
      </div>
    );
  }

  return (
    <div className="card p-0 overflow-hidden file-table-wrapper" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {props.isMobile ? (
        <RootFileMobileList {...props} />
      ) : (
        <RootFileDesktopTable {...props} />
      )}
    </div>
  );
}
