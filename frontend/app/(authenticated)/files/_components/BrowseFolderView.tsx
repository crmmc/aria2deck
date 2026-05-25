import { AutoSizer } from "react-virtualized-auto-sizer";
import { List } from "react-window";
import { formatBytes } from "@/lib/utils";
import type { BrowseFileInfo } from "@/types";
import type { SortField } from "./FileToolbar";

type BrowseContext = {
  fileHash: string;
  fileName: string;
  path: string[];
};

type BrowseFolderViewProps = {
  isMobile: boolean;
  browseLoading: boolean;
  browseContext: BrowseContext;
  browseContents: BrowseFileInfo[];
  sortedBrowseContents: BrowseFileInfo[];
  selectedBrowseFiles: Set<string>;
  onSort: (field: SortField) => void;
  getSortIcon: (field: SortField) => string;
  onToggleAllBrowseFiles: () => void;
  onToggleBrowseFileSelection: (item: BrowseFileInfo) => void;
  onNavigateIntoSubfolder: (name: string) => void;
  onDownload: (contentHash: string, fileName: string, fileId?: number, subpath?: string) => void;
  onUnavailableDelete: () => void;
};

function getBrowseItemKey(context: BrowseContext, item: BrowseFileInfo) {
  return [...context.path, item.name].join("/");
}

export function BrowseFolderView({
  isMobile,
  browseLoading,
  browseContext,
  browseContents,
  sortedBrowseContents,
  selectedBrowseFiles,
  onSort,
  getSortIcon,
  onToggleAllBrowseFiles,
  onToggleBrowseFileSelection,
  onNavigateIntoSubfolder,
  onDownload,
  onUnavailableDelete,
}: BrowseFolderViewProps) {
  if (browseLoading) {
    return (
      <div className="card text-center py-8">
        <p className="muted">加载中...</p>
      </div>
    );
  }

  if (sortedBrowseContents.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          </svg>
        </div>
        <p className="font-medium mb-1">文件夹为空</p>
        <p className="muted text-base">此文件夹中没有文件</p>
      </div>
    );
  }

  return (
    <div className="card p-0 overflow-hidden file-table-wrapper" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {!isMobile && (
        <div className="table-header" style={{ display: "grid", gridTemplateColumns: "40px minmax(220px, 1fr) 120px 300px", paddingRight: "16px" }}>
          <div className="table-cell text-left">
            <input
              type="checkbox"
              checked={(() => {
                const allKeys = browseContents.map((item) => getBrowseItemKey(browseContext, item));
                return allKeys.length > 0 && allKeys.every((key) => selectedBrowseFiles.has(key));
              })()}
              onChange={onToggleAllBrowseFiles}
              className="checkbox-sm cursor-pointer"
              aria-label="全选"
            />
          </div>
          <button type="button"
            className="table-cell text-left sortable-header"
            onClick={() => onSort("name")}
          >
            名称 <span className="sort-icon">{getSortIcon("name")}</span>
          </button>
          <button type="button"
            className="table-cell text-right sortable-header"
            onClick={() => onSort("size")}
          >
            大小 <span className="sort-icon">{getSortIcon("size")}</span>
          </button>
          <div className="table-cell text-right">操作</div>
        </div>
      )}

      {isMobile ? (
        <div style={{ maxHeight: "60vh", overflowY: "auto" }}>
          {sortedBrowseContents.map((item) => {
            const itemKey = getBrowseItemKey(browseContext, item);
            return (
              <div key={item.name} className="mobile-file-card">
                <div className="card-header">
                  <input
                    type="checkbox"
                    checked={selectedBrowseFiles.has(itemKey)}
                    onChange={() => onToggleBrowseFileSelection(item)}
                    className="checkbox-sm cursor-pointer"
                    aria-label={`选择 ${item.name}`}
                  />
                  <div className="file-title">
                    <span className="file-icon">
                      {item.is_directory ? "📁" : "📄"}
                    </span>
                    {item.is_directory ? (
                      <button type="button"
                        className="mobile-name"
                        onClick={() => onNavigateIntoSubfolder(item.name)}
                      >
                        {item.name}
                      </button>
                    ) : (
                      <span className="mobile-name">{item.name}</span>
                    )}
                  </div>
                </div>
                <div className="card-meta">
                  {item.is_directory ? "-" : formatBytes(item.size)}
                </div>
                <div className="card-actions">
                  {item.is_directory ? (
                    <button type="button"
                      className="button secondary btn-sm"
                      onClick={() => onNavigateIntoSubfolder(item.name)}
                    >
                      打开
                    </button>
                  ) : (
                    <button type="button"
                      className="button secondary btn-sm"
                      onClick={() => {
                        onDownload(
                          browseContext.fileHash,
                          item.name,
                          undefined,
                          [...browseContext.path, item.name].join("/")
                        );
                      }}
                    >
                      下载
                    </button>
                  )}
                  <button type="button"
                    className="button secondary danger btn-sm"
                    onClick={onUnavailableDelete}
                  >
                    删除
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div style={{ flex: 1, minHeight: 320 }}>
          <AutoSizer renderProp={({ height, width }) => {
            const safeHeight = typeof height === "number" && height > 0 ? height : 400;
            const safeWidth = typeof width === "number" && width > 0 ? width : 1200;
            return (
              <List
                style={{ height: safeHeight, width: safeWidth }}
                rowCount={sortedBrowseContents.length}
                rowHeight={80}
                rowProps={{}}
                rowComponent={({ index, style }) => {
                  const item = sortedBrowseContents[index];
                  const itemKey = getBrowseItemKey(browseContext, item);
                  return (
                    <div style={style} key={item.name}>
                      <div
                        className="table-row transition-bg"
                        style={{
                          display: "grid",
                          gridTemplateColumns: "40px minmax(220px, 1fr) 120px 300px",
                          height: "100%",
                          alignItems: "flex-start",
                          overflow: "hidden",
                        }}
                      >
                        <div className="table-cell" style={{ paddingTop: "20px" }}>
                          <input
                            type="checkbox"
                            checked={selectedBrowseFiles.has(itemKey)}
                            onChange={() => onToggleBrowseFileSelection(item)}
                            className="checkbox-sm cursor-pointer"
                            aria-label="选择文件"
                          />
                        </div>
                        <div className="table-cell" data-label="名称" style={{ paddingTop: "14px", paddingBottom: "14px", overflow: "hidden" }}>
                          <div className="flex items-center gap-2" style={{ minWidth: 0 }}>
                            <span className="file-icon" style={{ flexShrink: 0 }}>
                              {item.is_directory ? "📁" : "📄"}
                            </span>
                            {item.is_directory ? (
                              <button type="button"
                                className="file-name-btn"
                                onClick={() => onNavigateIntoSubfolder(item.name)}
                                title={item.name}
                                style={{ wordBreak: "break-all", textAlign: "left", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}
                              >
                                {item.name}
                              </button>
                            ) : (
                              <span className="text-base" title={item.name} style={{ wordBreak: "break-all", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.name}</span>
                            )}
                          </div>
                        </div>
                        <div className="table-cell text-right muted text-base" data-label="大小" style={{ paddingTop: "20px" }}>
                          {item.is_directory ? "-" : formatBytes(item.size)}
                        </div>
                        <div className="table-cell text-right" style={{ paddingTop: "14px" }}>
                          <div className="flex gap-2 flex-end">
                            {item.is_directory ? (
                              <button type="button"
                                className="button secondary btn-sm"
                                onClick={() => onNavigateIntoSubfolder(item.name)}
                              >
                                打开
                              </button>
                            ) : (
                              <button type="button"
                                className="button secondary btn-sm"
                                onClick={() =>
                                  onDownload(
                                    browseContext.fileHash,
                                    item.name,
                                    undefined,
                                    [...browseContext.path, item.name].join("/")
                                  )}
                              >
                                下载
                              </button>
                            )}
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
      )}
    </div>
  );
}
