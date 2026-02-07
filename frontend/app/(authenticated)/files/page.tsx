"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { useToast } from "@/components/Toast";
import PackTaskCard from "@/components/PackTaskCard";
import type { FileInfo, BrowseFileInfo, SpaceInfo } from "@/types";
import { List } from "react-window";
import { AutoSizer } from "react-virtualized-auto-sizer";

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleString();
}

type SortField = "name" | "size" | "created_at";
type SortOrder = "asc" | "desc";

export default function FilesPage() {
  const { showToast, showConfirm } = useToast();
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [space, setSpace] = useState<SpaceInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<number | null>(null);
  const [newName, setNewName] = useState("");

  // Sort state
  const [sortField, setSortField] = useState<SortField>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");

  // Search state
  const [toolbarSearchKeyword, setToolbarSearchKeyword] = useState("");
  const [showSearchModal, setShowSearchModal] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const searchModalInputRef = useRef<HTMLInputElement>(null);

  // Batch selection state
  const [selectedFiles, setSelectedFiles] = useState<Set<number>>(new Set());
  const [isBatchOperating, setIsBatchOperating] = useState(false);

  // BT folder browsing state
  const [browsingFile, setBrowsingFile] = useState<FileInfo | null>(null);
  const [browsePath, setBrowsePath] = useState<string[]>([]);
  const [browseContents, setBrowseContents] = useState<BrowseFileInfo[]>([]);
  const [browseLoading, setBrowseLoading] = useState(false);

  const [packTasksKey, setPackTasksKey] = useState(0);
  const [downloadingFile, setDownloadingFile] = useState<number | null>(null);

  // Pack dialog state
  const [packDialogOpen, setPackDialogOpen] = useState(false);
  const [packSize, setPackSize] = useState<number | null>(null);
  const [availableSpace, setAvailableSpace] = useState<number | null>(null);
  const [packOutputName, setPackOutputName] = useState("");
  const [packDeleteSource, setPackDeleteSource] = useState(false);
  const [packing, setPacking] = useState(false);
  const [packLoading, setPackLoading] = useState(false);

  useEffect(() => {
    if (showSearchModal || browsingFile) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [showSearchModal, browsingFile]);

  const loadFiles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listFiles();
      setFiles(response.files);
      setSpace(response.space);
      setSelectedFiles(new Set());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  // Focus search modal input when opened
  useEffect(() => {
    if (showSearchModal && searchModalInputRef.current) {
      searchModalInputRef.current.focus();
    }
  }, [showSearchModal]);

  // Keyboard shortcut for search (Cmd/Ctrl + F)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        openSearchModal();
      }
      if (e.key === "Escape" && showSearchModal) {
        closeSearchModal();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [showSearchModal, toolbarSearchKeyword]);

  // Sorted files (folders first, then by sort field)
  const sortedFiles = useMemo(() => {
    const sorted = [...files].sort((a, b) => {
      // Folders always come first
      if (a.is_directory && !b.is_directory) return -1;
      if (!a.is_directory && b.is_directory) return 1;

      // Then sort by field
      let cmp = 0;
      if (sortField === "name") {
        cmp = a.name.localeCompare(b.name);
      } else if (sortField === "size") {
        cmp = a.size - b.size;
      } else if (sortField === "created_at") {
        cmp = new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
      }
      return sortOrder === "asc" ? cmp : -cmp;
    });
    return sorted;
  }, [files, sortField, sortOrder]);

  // Search results for modal
  const searchResults = useMemo(() => {
    if (!searchKeyword.trim()) return [];
    const keyword = searchKeyword.toLowerCase();
    return files.filter((f) => f.name.toLowerCase().includes(keyword));
  }, [files, searchKeyword]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(sortOrder === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortOrder(field === "name" ? "asc" : "desc");
    }
  };

  const getSortIcon = (field: SortField) => {
    if (sortField !== field) return "↕";
    return sortOrder === "asc" ? "↑" : "↓";
  };

  // Search modal handlers
  const openSearchModal = () => {
    setSearchKeyword(toolbarSearchKeyword);
    setShowSearchModal(true);
  };

  const closeSearchModal = () => {
    setShowSearchModal(false);
  };

  const handleToolbarSearchKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      openSearchModal();
    }
  };

  const handleSearchModalKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      // Search is already active via searchResults memo
    }
  };

  // Sync modal input back to toolbar when closing
  const handleSearchModalInputChange = (value: string) => {
    setSearchKeyword(value);
    setToolbarSearchKeyword(value);
  };

  // Handle search result click
  const handleSearchResultClick = (file: FileInfo) => {
    closeSearchModal();
    if (file.is_directory) {
      openBrowse(file);
    }
  };

  const handleDelete = async (file: FileInfo) => {
    const confirmMsg = file.is_directory
      ? `确定要删除文件夹 "${file.name}" 吗？`
      : `确定要删除文件 "${file.name}" 吗？`;

    const confirmed = await showConfirm({
      title: "删除确认",
      message: confirmMsg,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    try {
      await api.deleteFile(file.id);
      loadFiles();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    }
  };

  const handleBatchDelete = async () => {
    if (selectedFiles.size === 0) {
      showToast("请先选择要删除的文件", "warning");
      return;
    }

    const selectedList = files.filter((f) => selectedFiles.has(f.id));
    const confirmed = await showConfirm({
      title: "批量删除",
      message: `确定要删除选中的 ${selectedList.length} 个文件吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      await Promise.all(selectedList.map((f) => api.deleteFile(f.id)));
      showToast(`已删除 ${selectedList.length} 个文件`, "success");
      loadFiles();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    } finally {
      setIsBatchOperating(false);
    }
  };

  const openPackDialog = async () => {
    if (selectedFiles.size === 0) {
      showToast("请先选择要打包的文件", "warning");
      return;
    }
    setPackDialogOpen(true);
    setPackLoading(true);
    setPackSize(null);
    setAvailableSpace(null);
    setPackOutputName("");
    try {
      const fileIds = Array.from(selectedFiles);
      const [sizeRes, spaceRes] = await Promise.all([
        api.calculatePackSize(fileIds),
        api.getAvailableSpace(),
      ]);
      setPackSize(sizeRes.total_size);
      setAvailableSpace(spaceRes.available);
    } catch (err) {
      showToast(`获取信息失败: ${(err as Error).message}`, "error");
      setPackDialogOpen(false);
    } finally {
      setPackLoading(false);
    }
  };

  const handlePackConfirm = async () => {
    setPacking(true);
    try {
      const fileIds = Array.from(selectedFiles);
      await api.createPackTask(fileIds, packOutputName || undefined, packDeleteSource);
      showToast("打包任务已创建", "success");
      setPackDialogOpen(false);
      setSelectedFiles(new Set());
      setPackOutputName("");
      setPackDeleteSource(false);
      setPackTasksKey((k) => k + 1);
    } catch (err) {
      showToast(`创建打包任务失败: ${(err as Error).message}`, "error");
    } finally {
      setPacking(false);
    }
  };

  const toggleFileSelection = useCallback((id: number) => {
    setSelectedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    if (selectedFiles.size === sortedFiles.length) {
      setSelectedFiles(new Set());
    } else {
      setSelectedFiles(new Set(sortedFiles.map((f) => f.id)));
    }
  }, [selectedFiles.size, sortedFiles]);

  const selectedSize = useMemo(() => {
    return files
      .filter((f) => selectedFiles.has(f.id))
      .reduce((sum, f) => sum + f.size, 0);
  }, [files, selectedFiles]);

  const handleRename = async (file: FileInfo) => {
    if (!newName.trim()) {
      showToast("请输入新名称", "warning");
      return;
    }

    try {
      await api.renameFile(file.id, newName.trim());
      setRenaming(null);
      setNewName("");
      loadFiles();
    } catch (err) {
      showToast(`重命名失败: ${(err as Error).message}`, "error");
    }
  };

  const startRename = (file: FileInfo) => {
    setRenaming(file.id);
    setNewName(file.name);
  };

  const cancelRename = () => {
    setRenaming(null);
    setNewName("");
  };

  const handleDownload = async (file: FileInfo, subpath?: string) => {
    setDownloadingFile(file.id);
    try {
      const url = api.downloadFileUrl(file.id, subpath);
      const a = document.createElement("a");
      a.href = url;
      a.download = subpath ? subpath.split("/").pop() || file.name : file.name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (err) {
      showToast(`下载失败: ${(err as Error).message}`, "error");
    } finally {
      setDownloadingFile(null);
    }
  };

  // BT folder browsing
  const openBrowse = async (file: FileInfo) => {
    setBrowsingFile(file);
    setBrowsePath([]);
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(file.id);
      setBrowseContents(contents);
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
      setBrowsingFile(null);
    } finally {
      setBrowseLoading(false);
    }
  };

  const navigateBrowse = async (name: string) => {
    if (!browsingFile) return;
    const newPath = [...browsePath, name];
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(browsingFile.id, newPath.join("/"));
      setBrowseContents(contents);
      setBrowsePath(newPath);
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
    } finally {
      setBrowseLoading(false);
    }
  };

  const navigateToPathIndex = async (index: number) => {
    if (!browsingFile) return;
    // index -1 means root
    const newPath = index < 0 ? [] : browsePath.slice(0, index + 1);
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(
        browsingFile.id,
        newPath.length > 0 ? newPath.join("/") : undefined
      );
      setBrowseContents(contents);
      setBrowsePath(newPath);
    } catch (err) {
      showToast(`导航失败: ${(err as Error).message}`, "error");
    } finally {
      setBrowseLoading(false);
    }
  };

  const closeBrowse = () => {
    setBrowsingFile(null);
    setBrowsePath([]);
    setBrowseContents([]);
  };

  const handlePackTaskComplete = useCallback(() => {
    loadFiles();
  }, [loadFiles]);

  // Space display helpers
  const getSpacePercentage = (space: SpaceInfo) => {
    const total = space.used + space.frozen + space.available;
    if (total === 0) return { used: 0, frozen: 0 };
    return {
      used: (space.used / total) * 100,
      frozen: (space.frozen / total) * 100,
    };
  };

  const getSpaceColor = (percentage: number) => {
    if (percentage >= 80) return "var(--danger)";
    if (percentage >= 50) return "var(--warning)";
    return "var(--success)";
  };

  return (
    <div className="glass-frame full-height animate-in">
      <div className="flex-between mb-7">
        <div>
          <h1 className="text-2xl">文件</h1>
          <p className="muted">管理您下载的文件</p>
        </div>
        <PackTaskCard key={packTasksKey} onTaskComplete={handlePackTaskComplete} />
      </div>

      {space && (
        <div className="card mb-6">
          <div className="flex-between mb-3">
            <div>
              <h3 className="stats-label">存储使用情况</h3>
              <div className="flex items-baseline gap-2">
                <span className="stats-value">{formatBytes(space.used)}</span>
                <span className="muted">
                  / {formatBytes(space.used + space.frozen + space.available)}
                </span>
              </div>
              {space.frozen > 0 && (
                <div className="text-sm muted mt-1">
                  已冻结: {formatBytes(space.frozen)} (下载中)
                </div>
              )}
            </div>
          </div>
          <div className="progress-container" style={{ position: "relative" }}>
            <div
              className="progress-bar"
              style={{
                width: `${getSpacePercentage(space).used + getSpacePercentage(space).frozen}%`,
                background: getSpaceColor(getSpacePercentage(space).used + getSpacePercentage(space).frozen),
              }}
            />
            {space.frozen > 0 && (
              <div
                style={{
                  position: "absolute",
                  left: `${getSpacePercentage(space).used}%`,
                  top: 0,
                  width: `${getSpacePercentage(space).frozen}%`,
                  height: "100%",
                  background: "repeating-linear-gradient(45deg, transparent, transparent 2px, rgba(255,255,255,0.3) 2px, rgba(255,255,255,0.3) 4px)",
                }}
              />
            )}
          </div>
        </div>
      )}

      {/* Toolbar - Always visible */}
      <div className="card filter-toolbar mb-4">
        {/* Path breadcrumb */}
        <div className="filter-group path-breadcrumb">
          <span className="file-icon">📁</span>
          <span className="text-sm font-medium">根目录</span>
          <span className="muted text-sm">({files.length} 项)</span>
        </div>

        {/* Sort select */}
        <div className="filter-group">
          <select
            className="select"
            value={`${sortField}-${sortOrder}`}
            onChange={(e) => {
              const [field, order] = e.target.value.split("-") as [SortField, SortOrder];
              setSortField(field);
              setSortOrder(order);
            }}
          >
            <option value="created_at-desc">时间 (最新)</option>
            <option value="created_at-asc">时间 (最早)</option>
            <option value="name-asc">名称 (A-Z)</option>
            <option value="name-desc">名称 (Z-A)</option>
            <option value="size-desc">大小 (最大)</option>
            <option value="size-asc">大小 (最小)</option>
          </select>
        </div>

        {/* Search input */}
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
            type="text"
            className="toolbar-search-input"
            placeholder="搜索文件名... (回车搜索)"
            value={toolbarSearchKeyword}
            onChange={(e) => setToolbarSearchKeyword(e.target.value)}
            onKeyDown={handleToolbarSearchKeyDown}
          />
          {toolbarSearchKeyword && (
            <button
              type="button"
              className="search-clear-btn"
              onClick={() => setToolbarSearchKeyword("")}
            >
              ✕
            </button>
          )}
        </div>

        {/* Batch operations */}
        <div className="filter-group ml-auto">
          {selectedFiles.size > 0 && (
            <>
              <span className="muted text-sm">
                已选 {selectedFiles.size} 项 ({formatBytes(selectedSize)})
              </span>
              <button
                type="button"
                className={`button secondary danger btn-sm${isBatchOperating ? " opacity-60" : ""}`}
                onClick={handleBatchDelete}
                disabled={isBatchOperating}
              >
                {isBatchOperating ? "删除中..." : "批量删除"}
              </button>
              <button
                type="button"
                className="button secondary btn-sm"
                onClick={openPackDialog}
                disabled={isBatchOperating}
              >
                打包下载
              </button>
            </>
          )}
          {sortedFiles.length > 0 && (
            <button
              type="button"
              className="button secondary btn-sm"
              onClick={toggleSelectAll}
            >
              {selectedFiles.size === sortedFiles.length && sortedFiles.length > 0
                ? "取消全选"
                : "全选"}
            </button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="card text-center py-8">
          <p className="muted">加载中...</p>
        </div>
      ) : error ? (
        <div className="card text-center py-8">
          <p className="text-danger">{error}</p>
        </div>
      ) : sortedFiles.length === 0 ? (
        <div className="card text-center py-8">
          <p className="muted">暂无文件</p>
        </div>
      ) : (
        <div className="card p-0 overflow-hidden file-table-wrapper" style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
          <div className="table-header" style={{ display: 'grid', gridTemplateColumns: '40px minmax(200px, 1fr) 120px 180px 200px', paddingRight: '16px' }}>
             <div className="table-cell text-left">
                  <input
                    type="checkbox"
                    checked={selectedFiles.size === sortedFiles.length && sortedFiles.length > 0}
                    onChange={toggleSelectAll}
                    className="checkbox-sm cursor-pointer"
                  />
                </div>
                <div
                  className="table-cell text-left sortable-header"
                  onClick={() => handleSort("name")}
                >
                  名称 <span className="sort-icon">{getSortIcon("name")}</span>
                </div>
                <div
                  className="table-cell text-right sortable-header"
                  onClick={() => handleSort("size")}
                >
                  大小 <span className="sort-icon">{getSortIcon("size")}</span>
                </div>
                <div
                  className="table-cell text-right sortable-header"
                  onClick={() => handleSort("created_at")}
                >
                  添加时间 <span className="sort-icon">{getSortIcon("created_at")}</span>
                </div>
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
                  rowHeight={60}
                  rowProps={{}}
                  rowComponent={({ index, style }) => {
                    const file = sortedFiles[index];
                    return (
                      <div style={style} key={file.id}>
                        <div 
                          className="table-row transition-bg" 
                          style={{ 
                            display: 'grid', 
                            gridTemplateColumns: '40px minmax(200px, 1fr) 120px 180px 200px',
                            height: '100%',
                            alignItems: 'center'
                          }}
                        >
                          <div className="table-cell">
                            <input
                              type="checkbox"
                              checked={selectedFiles.has(file.id)}
                              onChange={() => toggleFileSelection(file.id)}
                              className="checkbox-sm cursor-pointer"
                            />
                          </div>
                          <div className="table-cell" data-label="名称" style={{ overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
                            {renaming === file.id ? (
                              <div className="flex gap-2">
                                <input
                                  className="input py-1 px-3 text-base"
                                  value={newName}
                                  onChange={(e) => setNewName(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") handleRename(file);
                                    if (e.key === "Escape") cancelRename();
                                  }}
                                  autoFocus
                                  onClick={(e) => e.stopPropagation()}
                                />
                                <button
                                  className="button secondary btn-sm"
                                  onClick={(e) => { e.stopPropagation(); handleRename(file); }}
                                >
                                  ✓
                                </button>
                                <button
                                  className="button secondary btn-sm"
                                  onClick={(e) => { e.stopPropagation(); cancelRename(); }}
                                >
                                  ✕
                                </button>
                              </div>
                            ) : (
                              <div className="flex items-center gap-2">
                                <span className="file-icon">
                                  {file.is_directory ? "📁" : "📄"}
                                </span>
                                {file.is_directory ? (
                                  <button
                                    className="file-name-btn"
                                    onClick={() => openBrowse(file)}
                                  >
                                    {file.name}
                                  </button>
                                ) : (
                                  <span className="text-base truncate" title={file.name}>{file.name}</span>
                                )}
                              </div>
                            )}
                          </div>
                          <div className="table-cell text-right muted text-base" data-label="大小">
                            {formatBytes(file.size)}
                          </div>
                          <div className="table-cell text-right muted text-sm" data-label="添加时间">
                            {formatDate(file.created_at)}
                          </div>
                          <div className="table-cell text-right">
                            <div className="flex gap-2 flex-end">
                              {file.is_directory ? (
                                <button
                                  className="button secondary btn-sm"
                                  onClick={() => openBrowse(file)}
                                >
                                  浏览
                                </button>
                              ) : (
                                <button
                                  className="button secondary btn-sm"
                                  onClick={() => handleDownload(file)}
                                  disabled={downloadingFile === file.id}
                                >
                                  {downloadingFile === file.id ? "下载中..." : "下载"}
                                </button>
                              )}
                              <button
                                className="button secondary btn-sm"
                                onClick={() => startRename(file)}
                              >
                                重命名
                              </button>
                              <button
                                className="button secondary danger btn-sm"
                                onClick={() => handleDelete(file)}
                              >
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
        </div>
      )}

      {/* Search Modal */}
      {showSearchModal && (
        <div
          className="modal-overlay"
          onClick={closeSearchModal}
        >
          <div
            className="search-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="search-modal-header">
              <svg
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="search-modal-icon"
              >
                <circle cx="11" cy="11" r="8" />
                <path d="m21 21-4.35-4.35" />
              </svg>
              <input
                ref={searchModalInputRef}
                type="text"
                className="search-modal-input"
                placeholder="搜索文件名..."
                value={searchKeyword}
                onChange={(e) => handleSearchModalInputChange(e.target.value)}
                onKeyDown={handleSearchModalKeyDown}
              />
              {searchKeyword && (
                <button
                  className="search-modal-clear"
                  onClick={() => handleSearchModalInputChange("")}
                >
                  ✕
                </button>
              )}
            </div>

            <div className="search-modal-results">
              {searchKeyword.trim() === "" ? (
                <div className="search-modal-hint">
                  <p className="muted">输入关键词搜索文件</p>
                  <p className="muted text-sm">按 ESC 关闭</p>
                </div>
              ) : searchResults.length === 0 ? (
                <div className="search-modal-hint">
                  <p className="muted">未找到匹配的文件</p>
                </div>
              ) : (
                <div className="search-results-list">
                  {searchResults.map((file) => (
                    <div
                      key={file.id}
                      className="search-result-item"
                      onClick={() => handleSearchResultClick(file)}
                    >
                      <span className="file-icon">
                        {file.is_directory ? "📁" : "📄"}
                      </span>
                      <div className="search-result-info">
                        <span className="search-result-name">{file.name}</span>
                        <span className="search-result-meta">
                          {formatBytes(file.size)} · {formatDate(file.created_at)}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="search-modal-footer">
              <span className="muted text-sm">
                {searchResults.length > 0
                  ? `找到 ${searchResults.length} 个文件`
                  : "⌘F 打开搜索"}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* BT Folder Browser Modal */}
      {browsingFile && (
        <div className="modal-overlay" onClick={closeBrowse}>
          <div
            className="batch-modal-content"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: "800px", width: "90%" }}
          >
            <div className="modal-header">
              <h2 className="m-0">{browsingFile.name}</h2>
              <button
                type="button"
                onClick={closeBrowse}
                className="modal-close-btn"
              >
                ×
              </button>
            </div>

            {/* Clickable path breadcrumb */}
            <div className="card mb-4 py-3 px-4">
              <div className="path-breadcrumb-nav">
                <button
                  type="button"
                  className="path-segment"
                  onClick={() => navigateToPathIndex(-1)}
                >
                  📁 {browsingFile.name}
                </button>
                {browsePath.map((segment, index) => (
                  <span key={index} className="path-segment-wrapper">
                    <span className="path-separator">/</span>
                    <button
                      type="button"
                      className="path-segment"
                      onClick={() => navigateToPathIndex(index)}
                    >
                      {segment}
                    </button>
                  </span>
                ))}
              </div>
            </div>

            {browseLoading ? (
              <div className="text-center py-8">
                <p className="muted">加载中...</p>
              </div>
            ) : browseContents.length === 0 ? (
              <div className="text-center py-8">
                <p className="muted">文件夹为空</p>
              </div>
            ) : (
              <div
                className="card p-0 overflow-hidden"
                style={{ maxHeight: "400px", overflowY: "auto" }}
              >
                <table className="table">
                  <thead className="table-header">
                    <tr>
                      <th className="table-cell text-left">名称</th>
                      <th className="table-cell text-right">大小</th>
                      <th className="table-cell text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {browseContents.map((item) => (
                      <tr key={item.name} className="table-row transition-bg">
                        <td className="table-cell">
                          <div className="flex items-center gap-2">
                            <span className="file-icon">
                              {item.is_directory ? "📁" : "📄"}
                            </span>
                            {item.is_directory ? (
                              <button
                                className="file-name-btn"
                                onClick={() => navigateBrowse(item.name)}
                              >
                                {item.name}
                              </button>
                            ) : (
                              <span className="text-base">{item.name}</span>
                            )}
                          </div>
                        </td>
                        <td className="table-cell text-right muted text-base">
                          {item.is_directory ? "-" : formatBytes(item.size)}
                        </td>
                        <td className="table-cell text-right">
                          {item.is_directory ? (
                            <button
                              className="button secondary btn-sm"
                              onClick={() => navigateBrowse(item.name)}
                            >
                              打开
                            </button>
                          ) : (
                            <button
                              className="button secondary btn-sm"
                              onClick={() =>
                                handleDownload(
                                  browsingFile,
                                  [...browsePath, item.name].join("/")
                                )
                              }
                            >
                              下载
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Pack Dialog */}
      {packDialogOpen && (
        <div className="modal-overlay" onClick={() => !packing && setPackDialogOpen(false)}>
          <div
            className="batch-modal-content"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: "500px", width: "90%" }}
          >
            <div className="modal-header">
              <h2 className="m-0">打包下载</h2>
              <button
                type="button"
                onClick={() => !packing && setPackDialogOpen(false)}
                className="modal-close-btn"
                disabled={packing}
              >
                ×
              </button>
            </div>

            {packLoading ? (
              <div className="text-center py-8">
                <p className="muted">计算中...</p>
              </div>
            ) : (
              <div className="p-4">
                <div className="mb-4">
                  <p className="text-base mb-2">
                    已选择 <strong>{selectedFiles.size}</strong> 个文件
                  </p>
                  {packSize !== null && (
                    <p className="text-base mb-2">
                      预估大小: <strong>{formatBytes(packSize)}</strong>
                    </p>
                  )}
                  {availableSpace !== null && (
                    <p className="text-base mb-2">
                      可用空间: <strong>{formatBytes(availableSpace)}</strong>
                    </p>
                  )}
                  {packSize !== null && availableSpace !== null && packSize > availableSpace && (
                    <p className="text-danger text-sm">
                      空间不足，无法创建打包任务
                    </p>
                  )}
                </div>

                <div className="mb-4">
                  <label className="text-sm muted mb-1 block">
                    输出文件名 (可选)
                  </label>
                  <input
                    type="text"
                    className="input"
                    placeholder="默认自动生成"
                    value={packOutputName}
                    onChange={(e) => setPackOutputName(e.target.value)}
                    disabled={packing}
                  />
                </div>

                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="checkbox"
                    checked={packDeleteSource}
                    onChange={(e) => setPackDeleteSource(e.target.checked)}
                    disabled={packing}
                  />
                  <span>打包后删除源文件</span>
                </label>

                <div className="flex gap-3 flex-end">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => setPackDialogOpen(false)}
                    disabled={packing}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="button primary"
                    onClick={handlePackConfirm}
                    disabled={
                      packing ||
                      (packSize !== null && availableSpace !== null && packSize > availableSpace)
                    }
                  >
                    {packing ? "创建中..." : "确认打包"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}