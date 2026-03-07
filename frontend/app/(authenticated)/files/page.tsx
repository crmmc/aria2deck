"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { useToast } from "@/components/Toast";
import PackTaskCard from "@/components/PackTaskCard";
import CreateShareDialog from "@/components/CreateShareDialog";
import type { FileInfo, BrowseFileInfo, SpaceInfo } from "@/types";
import { List } from "react-window";
import { AutoSizer } from "react-virtualized-auto-sizer";

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  const y = date.getFullYear();
  const m = date.getMonth() + 1;
  const d = date.getDate();
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${y}/${m}/${d} ${hh}:${mm}`;
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

  // Folder browsing state (in-page navigation)
  const [browseContext, setBrowseContext] = useState<{
    fileHash: string;
    fileName: string;
    path: string[];
  } | null>(null);
  const [browseContents, setBrowseContents] = useState<BrowseFileInfo[]>([]);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [selectedBrowseFiles, setSelectedBrowseFiles] = useState<Set<string>>(new Set());
  const isInsideFolder = browseContext !== null;

  const [packTasksKey, setPackTasksKey] = useState(0);
  const [downloadingFile, setDownloadingFile] = useState<number | null>(null);

  // Share dialog state
  const [shareDialogFile, setShareDialogFile] = useState<{ id: number; name: string } | null>(null);
  // Pack dialog state
  const [packDialogOpen, setPackDialogOpen] = useState(false);
  const [packSize, setPackSize] = useState<number | null>(null);
  const [availableSpace, setAvailableSpace] = useState<number | null>(null);
  const [packOutputName, setPackOutputName] = useState("");
  const [packDeleteSource, setPackDeleteSource] = useState(false);
  const [packing, setPacking] = useState(false);
  const [packLoading, setPackLoading] = useState(false);

  // Pagination state
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [totalFiles, setTotalFiles] = useState(0);


  // Mobile detection
  const [isMobile, setIsMobile] = useState(false);
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
    const check = () => setIsMobile(window.innerWidth < 768);
    check();
    window.addEventListener('resize', check);
    return () => window.removeEventListener('resize', check);
  }, []);

  useEffect(() => {
    if (showSearchModal) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [showSearchModal]);

  const loadFiles = useCallback(async (page?: number, size?: number) => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listFiles(page ?? currentPage, size ?? pageSize);
      setFiles(response.files);
      setSpace(response.space);
      setTotalFiles(response.total);
      setSelectedFiles(new Set());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [currentPage, pageSize]);

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
        if (browseContext) return; // Disable search inside folder
        e.preventDefault();
        openSearchModal();
      }
      if (e.key === "Escape" && showSearchModal) {
        closeSearchModal();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [showSearchModal, toolbarSearchKeyword, browseContext]);

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

  // Sorted browse contents (inside folder)
  const sortedBrowseContents = useMemo(() => {
    return [...browseContents].sort((a, b) => {
      // Directories always first
      if (a.is_directory && !b.is_directory) return -1;
      if (!a.is_directory && b.is_directory) return 1;

      let cmp = 0;
      // created_at not available in BrowseFileInfo, fallback to name
      const effectiveField = sortField === "created_at" ? "name" : sortField;
      if (effectiveField === "name") {
        cmp = a.name.localeCompare(b.name);
      } else if (effectiveField === "size") {
        cmp = a.size - b.size;
      }
      return sortOrder === "asc" ? cmp : -cmp;
    });
  }, [browseContents, sortField, sortOrder]);

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

  // Sync modal input back to toolbar when closing
  const handleSearchModalInputChange = (value: string) => {
    setSearchKeyword(value);
    setToolbarSearchKeyword(value);
  };

  // Handle search result click
  const handleSearchResultClick = (file: FileInfo) => {
    closeSearchModal();
    if (file.is_directory) {
      enterFolder(file);
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
      await api.deleteFile(file.content_hash);
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
      await Promise.all(selectedList.map((f) => api.deleteFile(f.content_hash)));
      showToast(`已删除 ${selectedList.length} 个文件`, "success");
      loadFiles();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    } finally {
      setIsBatchOperating(false);
    }
  };

  const handleBatchDownload = async () => {
    if (selectedFiles.size === 0) return;
    const hasFolder = files.some((f) => selectedFiles.has(f.id) && f.is_directory);
    if (hasFolder) {
      showToast("当前选择包含文件夹，无法批量下载，请先打包后下载", "warning");
      return;
    }
    for (const fileId of selectedFiles) {
      const file = files.find(f => f.id === fileId);
      if (!file) continue;
      const url = api.downloadFileUrl(file.content_hash);
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      await new Promise((r) => setTimeout(r, 300));
    }
    showToast(`已开始下载 ${selectedFiles.size} 个文件`, "success");
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
      await api.renameFile(file.content_hash, newName.trim());
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

  const handleDownload = async (contentHash: string, fileName: string, fileId?: number, subpath?: string) => {
    setDownloadingFile(fileId ?? null);
    try {
      const downloadName = subpath ? subpath.split("/").pop() || fileName : fileName;
      const url = api.downloadFileUrl(contentHash, subpath);
      const a = document.createElement("a");
      a.href = url;
      a.download = downloadName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (err) {
      showToast(`下载失败: ${(err as Error).message}`, "error");
    } finally {
      setDownloadingFile(null);
    }
  };

  // Folder in-page navigation
  const enterFolder = async (file: FileInfo) => {
    setBrowseContext({ fileHash: file.content_hash, fileName: file.name, path: [] });
    setSelectedBrowseFiles(new Set());
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(file.content_hash);
      setBrowseContents(contents);
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
      setBrowseContext(null);
    } finally {
      setBrowseLoading(false);
    }
  };

  const navigateIntoSubfolder = async (name: string) => {
    if (!browseContext) return;
    const newPath = [...browseContext.path, name];
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(browseContext.fileHash, newPath.join("/"));
      setBrowseContents(contents);
      setBrowseContext({ ...browseContext, path: newPath });
      setSelectedBrowseFiles(new Set());
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
    } finally {
      setBrowseLoading(false);
    }
  };

  const navigateToBreadcrumb = async (index: number) => {
    if (!browseContext) return;
    // index -1 means root of the folder
    const newPath = index < 0 ? [] : browseContext.path.slice(0, index + 1);
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(
        browseContext.fileHash,
        newPath.length > 0 ? newPath.join("/") : undefined
      );
      setBrowseContents(contents);
      setBrowseContext({ ...browseContext, path: newPath });
      setSelectedBrowseFiles(new Set());
    } catch (err) {
      showToast(`导航失败: ${(err as Error).message}`, "error");
    } finally {
      setBrowseLoading(false);
    }
  };

  const returnToRoot = () => {
    setBrowseContext(null);
    setBrowseContents([]);
    setSelectedBrowseFiles(new Set());
  };

  // Browse file selection helpers
  const toggleBrowseFileSelection = (item: BrowseFileInfo) => {
    const key = [...(browseContext?.path ?? []), item.name].join("/");
    setSelectedBrowseFiles((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const toggleAllBrowseFiles = () => {
    const keys = browseContents.map((f) => [...(browseContext?.path ?? []), f.name].join("/"));
    const allSelected = keys.length > 0 && keys.every((k) => selectedBrowseFiles.has(k));
    if (allSelected) {
      setSelectedBrowseFiles(new Set());
    } else {
      setSelectedBrowseFiles(new Set(keys));
    }
  };

  const handleBrowseBatchDownload = async () => {
    if (!browseContext || selectedBrowseFiles.size === 0) return;
    // 检查选中项是否包含文件夹
    const hasFolder = browseContents.some((item) => {
      const key = [...(browseContext.path ?? []), item.name].join("/");
      return item.is_directory && selectedBrowseFiles.has(key);
    });
    if (hasFolder) {
      showToast("当前选择包含文件夹，无法批量下载，请仅选择文件", "warning");
      return;
    }
    for (const path of selectedBrowseFiles) {
      const url = api.downloadFileUrl(browseContext.fileHash, path);
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      await new Promise((r) => setTimeout(r, 300));
    }
    showToast(`已开始下载 ${selectedBrowseFiles.size} 个文件`, "success");
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
          {isInsideFolder ? (
            <>
              <button
                type="button"
                className="path-segment"
                onClick={returnToRoot}
              >
                <span className="file-icon">📁</span>
                <span className="text-sm font-medium">根目录</span>
              </button>
              <span className="path-separator">/</span>
              <button
                type="button"
                className="path-segment"
                onClick={() => navigateToBreadcrumb(-1)}
              >
                {browseContext!.fileName}
              </button>
              {browseContext!.path.map((segment, index) => (
                <span key={index} className="path-segment-wrapper">
                  <span className="path-separator">/</span>
                  <button
                    type="button"
                    className="path-segment"
                    onClick={() => navigateToBreadcrumb(index)}
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

        {/* Search and Sort Row */}
        <div className="search-sort-row">
          {/* Sort select */}
          <div className="filter-group sort-group">
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
          <div className={`filter-group search-input-group${isInsideFolder ? " opacity-40 pointer-events-none" : ""}`}>
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
              placeholder={isMobile ? "搜索文件..." : "搜索文件名... (回车搜索)"}
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
                    onClick={handleBrowseBatchDownload}
                  >
                    批量下载
                  </button>
                </>
              )}
              {browseContents.length > 0 && (
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={toggleAllBrowseFiles}
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
                    onClick={handleBatchDownload}
                    disabled={isBatchOperating}
                  >
                    批量下载
                  </button>
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
                    打包
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
            </>
          )}
          </div>
        </div>
      </div>

      {/* Folder contents table (inside folder) */}
      {isInsideFolder ? (
        browseLoading ? (
          <div className="card text-center py-8">
            <p className="muted">加载中...</p>
          </div>
        ) : sortedBrowseContents.length === 0 ? (
          <div className="card text-center py-8">
            <p className="muted">文件夹为空</p>
          </div>
        ) : (
          <div className="card p-0 overflow-hidden file-table-wrapper" style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
            {!isMobile && (
              <div className="table-header" style={{ display: 'grid', gridTemplateColumns: '40px minmax(220px, 1fr) 120px 300px', paddingRight: '16px' }}>
                <div className="table-cell text-left">
                  <input
                    type="checkbox"
                    checked={(() => {
                      const allKeys = browseContents
                        .map((f) => [...(browseContext?.path ?? []), f.name].join("/"));
                      return allKeys.length > 0 && allKeys.every((k) => selectedBrowseFiles.has(k));
                    })()}
                    onChange={toggleAllBrowseFiles}
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
                <div className="table-cell text-right">操作</div>
              </div>
            )}

            {isMobile ? (
              <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
                {sortedBrowseContents.map((item) => {
                  const itemKey = [...(browseContext?.path ?? []), item.name].join("/");
                  return (
                    <div key={item.name} className="mobile-file-card">
                      <div className="card-header">
                        <input
                          type="checkbox"
                          checked={selectedBrowseFiles.has(itemKey)}
                          onChange={() => toggleBrowseFileSelection(item)}
                          className="checkbox-sm cursor-pointer"
                        />
                        <div className="file-title">
                          <span className="file-icon">
                            {item.is_directory ? "📁" : "📄"}
                          </span>
                          {item.is_directory ? (
                            <button
                              className="mobile-name"
                              onClick={() => navigateIntoSubfolder(item.name)}
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
                          <button
                            className="button secondary btn-sm"
                            onClick={() => navigateIntoSubfolder(item.name)}
                          >
                            打开
                          </button>
                        ) : (
                          <button
                            className="button secondary btn-sm"
                            onClick={() => {
                              handleDownload(
                                browseContext!.fileHash,
                                item.name,
                                undefined,
                                [...browseContext!.path, item.name].join("/")
                              );
                            }}
                          >
                            下载
                          </button>
                        )}
                        <button
                          className="button secondary danger btn-sm"
                          onClick={() => {
                            showToast("文件夹内暂不支持在此页面单文件直接删除", "warning");
                          }}
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
                        const itemKey = [...(browseContext?.path ?? []), item.name].join("/");
                        return (
                          <div style={style} key={item.name}>
                            <div
                              className="table-row transition-bg"
                              style={{
                                display: 'grid',
                                gridTemplateColumns: '40px minmax(220px, 1fr) 120px 300px',
                                height: '100%',
                                alignItems: 'flex-start'
                              }}
                            >
                              <div className="table-cell" style={{ paddingTop: '20px' }}>
                                <input
                                  type="checkbox"
                                  checked={selectedBrowseFiles.has(itemKey)}
                                  onChange={() => toggleBrowseFileSelection(item)}
                                  className="checkbox-sm cursor-pointer"
                                />
                              </div>
                              <div className="table-cell" data-label="名称" style={{ wordBreak: 'break-all', paddingTop: '14px', paddingBottom: '14px' }}>
                                <div className="flex items-center gap-2">
                                  <span className="file-icon">
                                    {item.is_directory ? "📁" : "📄"}
                                  </span>
                                  {item.is_directory ? (
                                    <button
                                      className="file-name-btn"
                                      onClick={() => navigateIntoSubfolder(item.name)}
                                      style={{ wordBreak: 'break-all', textAlign: 'left' }}
                                    >
                                      {item.name}
                                    </button>
                                  ) : (
                                    <span className="text-base" style={{ wordBreak: 'break-all' }}>{item.name}</span>
                                  )}
                                </div>
                              </div>
                              <div className="table-cell text-right muted text-base" data-label="大小" style={{ paddingTop: '20px' }}>
                                {item.is_directory ? "-" : formatBytes(item.size)}
                              </div>
                              <div className="table-cell text-right" style={{ paddingTop: '14px' }}>
                                <div className="flex gap-2 flex-end">
                                  {item.is_directory ? (
                                    <button
                                      className="button secondary btn-sm"
                                      onClick={() => navigateIntoSubfolder(item.name)}
                                    >
                                      打开
                                    </button>
                                  ) : (
                                    <button
                                      className="button secondary btn-sm"
                                      onClick={() =>
                                        handleDownload(
                                          browseContext!.fileHash,
                                          item.name,
                                          undefined,
                                          [...browseContext!.path, item.name].join("/")
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
        )
      ) : (
        /* Root file table */
        loading ? (
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
          <>
          <div className="card p-0 overflow-hidden file-table-wrapper" style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
            {!isMobile && (
              <div className="table-header" style={{ display: 'grid', gridTemplateColumns: '40px minmax(220px, 1fr) 120px 180px 300px', paddingRight: '16px' }}>
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
            )}

            {isMobile ? (
              <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
                {sortedFiles.map((file) => (
                  <div key={file.id} className="mobile-file-card">
                    <div className="card-header">
                      <input
                        type="checkbox"
                        checked={selectedFiles.has(file.id)}
                        onChange={() => toggleFileSelection(file.id)}
                        className="checkbox-sm cursor-pointer"
                      />
                      <div className="file-title">
                        <span className="file-icon">
                          {file.is_directory ? "📁" : "📄"}
                        </span>
                        {renaming === file.id ? (
                          <div className="flex gap-2 flex-1" style={{ minWidth: 0 }}>
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
                          file.is_directory ? (
                            <button
                              className="mobile-name"
                              onClick={() => enterFolder(file)}
                            >
                              {file.name}
                            </button>
                          ) : (
                            <span className="mobile-name">{file.name}</span>
                          )
                        )}
                      </div>
                    </div>
                    <div className="card-meta">
                      {formatBytes(file.size)} • {formatDate(file.created_at)}
                    </div>
                    <div className="card-actions">
                      {file.is_directory ? (
                        <button
                          className="button secondary btn-sm"
                          onClick={() => enterFolder(file)}
                        >
                          浏览
                        </button>
                      ) : (
                        <button
                          className="button secondary btn-sm"
                          onClick={() => handleDownload(file.content_hash, file.name, file.id)}
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
                        className="button secondary btn-sm"
                        onClick={() => setShareDialogFile({ id: file.id, name: file.name })}
                      >
                        分享
                      </button>
                      <button
                        className="button secondary danger btn-sm"
                        onClick={() => handleDelete(file)}
                      >
                        删除
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
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
                                display: 'grid',
                                gridTemplateColumns: '40px minmax(220px, 1fr) 120px 180px 300px',
                                height: '100%',
                                alignItems: 'flex-start'
                              }}
                            >
                              <div className="table-cell" style={{ paddingTop: '20px' }}>
                                <input
                                  type="checkbox"
                                  checked={selectedFiles.has(file.id)}
                                  onChange={() => toggleFileSelection(file.id)}
                                  className="checkbox-sm cursor-pointer"
                                />
                              </div>
                              <div className="table-cell" data-label="名称" style={{ wordBreak: 'break-all', paddingTop: '14px', paddingBottom: '14px' }}>
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
                                        onClick={() => enterFolder(file)}
                                        style={{ wordBreak: 'break-all', textAlign: 'left' }}
                                      >
                                        {file.name}
                                      </button>
                                    ) : (
                                      <span className="text-base" style={{ wordBreak: 'break-all' }}>{file.name}</span>
                                    )}
                                  </div>
                                )}
                              </div>
                              <div className="table-cell text-right muted text-base" data-label="大小" style={{ paddingTop: '20px' }}>
                                {formatBytes(file.size)}
                              </div>
                              <div className="table-cell text-right muted text-sm" data-label="添加时间" style={{ paddingTop: '22px' }}>
                                {formatDate(file.created_at)}
                              </div>
                              <div className="table-cell text-right" style={{ paddingTop: '14px' }}>
                                <div className="flex gap-2 flex-end">
                                  {file.is_directory ? (
                                    <button
                                      className="button secondary btn-sm"
                                      onClick={() => enterFolder(file)}
                                    >
                                      浏览
                                    </button>
                                  ) : (
                                    <button
                                      className="button secondary btn-sm"
                                      onClick={() => handleDownload(file.content_hash, file.name, file.id)}
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
                                    className="button secondary btn-sm"
                                    onClick={() => setShareDialogFile({ id: file.id, name: file.name })}
                                  >
                                    分享
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
            )}
          </div>
          {/* Pagination */}
          {/* Pagination */}
          {(() => {
            const totalPages = Math.max(1, Math.ceil(totalFiles / pageSize));
            // 生成当前页附近的页码（最多显示 5 个）
            const pages: number[] = [];
            let start = Math.max(1, currentPage - 2);
            const end = Math.min(totalPages, start + 4);
            start = Math.max(1, end - 4);
            for (let i = start; i <= end; i++) pages.push(i);
            return (
              <div className="flex items-center justify-end gap-2 py-3 px-2">
                <select
                  className="select-sm"
                  value={pageSize}
                  onChange={(e) => {
                    const s = Number(e.target.value);
                    setPageSize(s);
                    setCurrentPage(1);
                    loadFiles(1, s);
                  }}
                >
                  {[10, 20, 30, 50, 100].map((n) => (
                    <option key={n} value={n}>{n} 条/页</option>
                  ))}
                </select>
                <span className="text-sm muted" style={{ marginLeft: 4 }}>
                  共 {totalFiles} 项
                </span>
                <div className="flex items-center gap-0" style={{ marginLeft: 8 }}>
                  <button
                    className="button secondary btn-sm"
                    style={{ borderRadius: '4px 0 0 4px' }}
                    disabled={currentPage <= 1}
                    onClick={() => { setCurrentPage(p => p - 1); loadFiles(currentPage - 1); }}
                  >
                    ‹
                  </button>
                  {pages.map((p) => (
                    <button
                      key={p}
                      className={`button btn-sm ${p === currentPage ? 'primary' : 'secondary'}`}
                      style={{ borderRadius: 0, minWidth: 32 }}
                      onClick={() => { if (p !== currentPage) { setCurrentPage(p); loadFiles(p); } }}
                    >
                      {p}
                    </button>
                  ))}
                  <button
                    className="button secondary btn-sm"
                    style={{ borderRadius: '0 4px 4px 0' }}
                    disabled={currentPage >= totalPages}
                    onClick={() => { setCurrentPage(p => p + 1); loadFiles(currentPage + 1); }}
                  >
                    ›
                  </button>
                </div>
              </div>
            );
          })()}
          </>
        )
      )}

      {/* Search Modal */}
      {showSearchModal && mounted && createPortal(
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
        </div>,
        document.body
      )}

      {/* Pack Dialog */}
      {packDialogOpen && mounted && createPortal(
        <div className="modal-overlay" onClick={() => !packing && setPackDialogOpen(false)}>
          <div
            className="batch-modal-content"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: "500px", width: "90%" }}
          >
            <div className="modal-header">
              <h2 className="m-0">打包</h2>
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
                    maxLength={200}
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
        </div>,
        document.body
      )}
      {/* Share Dialog */}
      {shareDialogFile && (
        <CreateShareDialog
          userFileId={shareDialogFile.id}
          fileName={shareDialogFile.name}
          onClose={() => setShareDialogFile(null)}
        />
      )}
    </div>
  );
}
