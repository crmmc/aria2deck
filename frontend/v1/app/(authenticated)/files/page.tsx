"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { triggerDownloadsSequentially } from "@/lib/download-trigger";
import { formatBytes } from "@/lib/utils";
import { useIsMobile } from "@/hooks/useIsMobile";
import { useMounted } from "@/lib/useMounted";
import { useToast } from "@/components/Toast";
import PackTaskCard from "@/components/PackTaskCard";
import CreateShareDialog from "@/components/CreateShareDialog";
import type {
  FileInfo,
  BrowseFileInfo,
  BrowsePageResponse,
  SpaceInfo,
  FileSearchItem,
} from "@/types";
import { BrowseFolderView } from "./_components/BrowseFolderView";
import { SearchModal } from "./_components/SearchModal";
import { FileToolbar, type SortField, type SortOrder } from "./_components/FileToolbar";
import { PackDialog } from "./_components/PackDialog";
import { PaginationControls } from "@/components/ui/PaginationControls";
import { RootFileTable } from "./_components/RootFileTable";
import { SpaceUsageCard } from "./_components/SpaceUsageCard";

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  const y = date.getFullYear();
  const m = date.getMonth() + 1;
  const d = date.getDate();
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${y}/${m}/${d} ${hh}:${mm}`;
}

const LOCATE_HIGHLIGHT_MS = 1800;
// 目录浏览分页页大小（与后端 BROWSE_DEFAULT_PAGE_SIZE 保持一致）
const BROWSE_PAGE_SIZE = 200;

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

  // Search state (toolbar is the only input source; modal only shows results)
  const [toolbarSearchKeyword, setToolbarSearchKeyword] = useState("");
  const [showSearchModal, setShowSearchModal] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [searchResults, setSearchResults] = useState<FileSearchItem[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchTruncated, setSearchTruncated] = useState(false);
  const [searchGlobal, setSearchGlobal] = useState(false);
  const [highlightUserFileId, setHighlightUserFileId] = useState<number | null>(null);
  const [highlightName, setHighlightName] = useState<string | null>(null);
  const pendingRootLocateRef = useRef<number | null>(null);
  const toolbarSearchInputRef = useRef<HTMLInputElement>(null);

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
  const [browsePage, setBrowsePage] = useState(1);
  const [browseTotal, setBrowseTotal] = useState(0);
  const browseRequestIdRef = useRef(0);
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

  const closeSearchModal = useCallback(() => {
    setShowSearchModal(false);
  }, []);

  const isMobile = useIsMobile();
  const mounted = useMounted();

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

  // 搜索定位：高亮行渲染后滚动到可视区域中央
  useEffect(() => {
    if (highlightUserFileId == null) return;
    const frame = requestAnimationFrame(() => {
      document
        .querySelector(`[data-file-id="${highlightUserFileId}"]`)
        ?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [highlightUserFileId]);

  const loadFiles = useCallback(async (page: number, size: number) => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listFiles(page, size);
      setFiles(response.files);
      setSpace(response.space);
      setTotalFiles(response.total);
      setSelectedFiles(new Set());
      const pendingLocateId = pendingRootLocateRef.current;
      if (pendingLocateId !== null) {
        pendingRootLocateRef.current = null;
        if (response.files.some((f) => f.id === pendingLocateId)) {
          setHighlightUserFileId(pendingLocateId);
          window.setTimeout(() => setHighlightUserFileId(null), LOCATE_HIGHLIGHT_MS);
          setShowSearchModal(false);
        } else {
          showToast("定位失败：未在当前列表找到该文件", "error");
        }
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    loadFiles(currentPage, pageSize);
  }, [currentPage, pageSize, loadFiles]);

  // Keyboard shortcut for search (Cmd/Ctrl + F)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        toolbarSearchInputRef.current?.focus();
      }
      if (e.key === "Escape" && showSearchModal) {
        closeSearchModal();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [showSearchModal, closeSearchModal]);

  // Sorted files (folders first, then by sort field)
  const sortedFiles = useMemo(() => {
    return files.toSorted((a, b) => {
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
  }, [files, sortField, sortOrder]);

  // Sorted browse contents (inside folder)
  const sortedBrowseContents = useMemo(() => {
    return browseContents.toSorted((a, b) => {
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

  const runSearch = useCallback(async () => {
    const keyword = toolbarSearchKeyword.trim();
    if (!keyword) {
      showToast("请输入关键词", "warning");
      return;
    }
    setSearchKeyword(keyword);
    setShowSearchModal(true);
    setSearchLoading(true);
    setSearchError(null);
    try {
      const params: { q: string; scopeContentHash?: string; scopePath?: string } = { q: keyword };
      if (!searchGlobal && browseContext) {
        params.scopeContentHash = browseContext.fileHash;
        if (browseContext.path.length > 0) {
          params.scopePath = browseContext.path.join("/");
        }
      }
      const response = await api.searchFiles(params);
      setSearchResults(response.items);
      setSearchTruncated(response.truncated);
    } catch (err) {
      setSearchResults([]);
      setSearchTruncated(false);
      setSearchError((err as Error).message);
    } finally {
      setSearchLoading(false);
    }
  }, [toolbarSearchKeyword, searchGlobal, browseContext, showToast]);

  // 目录浏览分页加载：requestId 守卫丢弃过期响应（并发导航/翻页竞态）
  const loadBrowsePage = useCallback(
    async (
      fileHash: string,
      path: string | undefined,
      page: number
    ): Promise<BrowsePageResponse | null> => {
      const requestId = ++browseRequestIdRef.current;
      setBrowseLoading(true);
      try {
        const result = await api.browseFile(fileHash, path, page, BROWSE_PAGE_SIZE);
        if (requestId !== browseRequestIdRef.current) return null; // 已有更新的请求，丢弃本响应
        setBrowseContents(result.items);
        setBrowseTotal(result.total);
        setBrowsePage(page);
        return result;
      } catch (err) {
        if (requestId !== browseRequestIdRef.current) return null;
        throw err;
      } finally {
        if (requestId === browseRequestIdRef.current) setBrowseLoading(false);
      }
    },
    []
  );

  // 退出目录视图：作废在途目录请求并清空分页状态
  const clearBrowseState = useCallback(() => {
    browseRequestIdRef.current += 1;
    setBrowseContext(null);
    setBrowseContents([]);
    setSelectedBrowseFiles(new Set());
    setBrowseTotal(0);
    setBrowsePage(1);
  }, []);

  const locateInsideFolder = useCallback(async (item: FileSearchItem) => {
    const segments = item.entry_path?.split("/").filter(Boolean) ?? [];
    const name = segments[segments.length - 1];
    const parentSegments = segments.slice(0, -1);
    const keepName = browseContext?.fileHash === item.content_hash
      ? browseContext.fileName
      : item.path.split("/").filter(Boolean)[0] || item.name;
    setBrowsePage(1);
    try {
      const result = await loadBrowsePage(
        item.content_hash,
        parentSegments.length > 0 ? parentSegments.join("/") : undefined,
        1
      );
      if (!result) return;
      setBrowseContext({ fileHash: item.content_hash, fileName: keepName, path: parentSegments });
      setSelectedBrowseFiles(new Set());
      if (!name || !result.items.some((c) => c.name === name)) {
        showToast("定位失败：未在文件夹中找到该文件", "error");
        return;
      }
      setHighlightName(name);
      window.setTimeout(() => setHighlightName(null), LOCATE_HIGHLIGHT_MS);
      setShowSearchModal(false);
    } catch (err) {
      showToast(`定位失败: ${(err as Error).message}`, "error");
    }
  }, [browseContext, showToast, loadBrowsePage]);

  const handleLocate = useCallback((item: FileSearchItem) => {
    if (item.entry_path == null) {
      const targetPage = Math.floor(item.root_index / pageSize) + 1;
      if (browseContext) {
        clearBrowseState();
      }
      if (targetPage !== currentPage) {
        pendingRootLocateRef.current = item.user_file_id;
        setCurrentPage(targetPage);
        return;
      }
      if (files.some((f) => f.id === item.user_file_id)) {
        setHighlightUserFileId(item.user_file_id);
        window.setTimeout(() => setHighlightUserFileId(null), LOCATE_HIGHLIGHT_MS);
        setShowSearchModal(false);
      } else {
        showToast("定位失败：未在当前列表找到该文件", "error");
      }
      return;
    }
    void locateInsideFolder(item);
  }, [browseContext, pageSize, currentPage, files, showToast, locateInsideFolder, clearBrowseState]);

  const handleToolbarSearchKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      void runSearch();
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
      const result = await api.deleteFiles([file.content_hash]);
      const item = result.results[0];
      if (!item?.ok) {
        showToast(`删除失败：${item?.error ?? "未知错误"}`, "error");
        return;
      }
      loadFiles(currentPage, pageSize);
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
      const result = await api.deleteFiles(
        selectedList.map((f) => f.content_hash)
      );
      if (result.failed_count > 0) {
        showToast(
          `已受理 ${result.accepted_count} 个文件，${result.failed_count} 个删除失败`,
          "warning"
        );
      } else {
        showToast(`已删除 ${result.accepted_count} 个文件`, "success");
      }
      // 删除后当前页可能已空，计算应回到哪一页
      const remainingTotal = totalFiles - result.accepted_count;
      const maxPage = Math.max(1, Math.ceil(remainingTotal / pageSize));
      const targetPage = Math.min(currentPage, maxPage);
      if (targetPage !== currentPage) {
        setCurrentPage(targetPage);
      }
      loadFiles(targetPage, pageSize);
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

    const selectedList = files.filter(f => selectedFiles.has(f.id));
    showToast(`正在发送已选 ${selectedList.length} 个文件的下载请求，请稍候...`, "info");

    await triggerDownloadsSequentially(
      selectedList.map((file) => api.downloadFileUrl(file.content_hash)),
    );
    showToast(`已完成 ${selectedList.length} 个文件的下载触发`, "success");
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
      loadFiles(currentPage, pageSize);
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

  // Folder in-page navigation（导航事件内同步重置页码，保证只发一笔「第 1 页」请求）
  const enterFolder = async (file: FileInfo) => {
    setBrowseContext({ fileHash: file.content_hash, fileName: file.name, path: [] });
    setSelectedBrowseFiles(new Set());
    setBrowsePage(1);
    setBrowseTotal(0);
    try {
      const result = await loadBrowsePage(file.content_hash, undefined, 1);
      if (!result) return;
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
      setBrowseContext(null);
    }
  };

  const navigateIntoSubfolder = async (name: string) => {
    if (!browseContext) return;
    const newPath = [...browseContext.path, name];
    setBrowsePage(1);
    try {
      const result = await loadBrowsePage(browseContext.fileHash, newPath.join("/"), 1);
      if (!result) return;
      setBrowseContext(prev => prev ? { ...prev, path: newPath } : prev);
      setSelectedBrowseFiles(new Set());
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
    }
  };

  const navigateToBreadcrumb = async (index: number) => {
    if (!browseContext) return;
    // index -1 means root of the folder
    const newPath = index < 0 ? [] : browseContext.path.slice(0, index + 1);
    setBrowsePage(1);
    try {
      const result = await loadBrowsePage(
        browseContext.fileHash,
        newPath.length > 0 ? newPath.join("/") : undefined,
        1
      );
      if (!result) return;
      setBrowseContext(prev => prev ? { ...prev, path: newPath } : prev);
      setSelectedBrowseFiles(new Set());
    } catch (err) {
      showToast(`导航失败: ${(err as Error).message}`, "error");
    }
  };

  // 目录内翻页：换页后旧页选中项不可见，清空避免批量操作命中隐藏项（与根列表翻页一致）
  const handleBrowsePageChange = useCallback(
    (page: number) => {
      if (!browseContext) return;
      const path = browseContext.path.length > 0 ? browseContext.path.join("/") : undefined;
      setSelectedBrowseFiles(new Set());
      loadBrowsePage(browseContext.fileHash, path, page).catch((err) => {
        showToast(`翻页失败: ${(err as Error).message}`, "error");
      });
    },
    [browseContext, loadBrowsePage, showToast]
  );

  const returnToRoot = () => {
    clearBrowseState();
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

    const selectedPaths = Array.from(selectedBrowseFiles);
    showToast(`正在发送已选 ${selectedPaths.length} 个文件的下载请求...`, "info");

    await triggerDownloadsSequentially(
      selectedPaths.map((path) => api.downloadFileUrl(browseContext.fileHash, path)),
    );
    showToast(`已完成 ${selectedPaths.length} 个文件的下载触发`, "success");
  };

  const handlePackTaskComplete = useCallback(() => {
    loadFiles(currentPage, pageSize);
  }, [loadFiles, currentPage, pageSize]);

  return (
    <div className="glass-frame full-height animate-in">
      <div className="flex-between mb-7">
        <div>
          <h1 className="text-2xl">文件</h1>
          <p className="muted">管理您下载的文件</p>
        </div>
        <PackTaskCard key={packTasksKey} onTaskComplete={handlePackTaskComplete} />
      </div>

      {space && <SpaceUsageCard space={space} />}

      {/* Toolbar - Always visible */}
      <FileToolbar
        isInsideFolder={isInsideFolder}
        browseContext={browseContext}
        browseContents={browseContents}
        selectedBrowseFiles={selectedBrowseFiles}
        selectedFiles={selectedFiles}
        selectedSize={selectedSize}
        sortedFilesLength={sortedFiles.length}
        sortField={sortField}
        sortOrder={sortOrder}
        toolbarSearchKeyword={toolbarSearchKeyword}
        searchGlobal={searchGlobal}
        searchLoading={searchLoading}
        searchInputRef={toolbarSearchInputRef}
        isBatchOperating={isBatchOperating}
        onReturnToRoot={returnToRoot}
        onNavigateToBreadcrumb={navigateToBreadcrumb}
        onSortFieldChange={setSortField}
        onSortOrderChange={setSortOrder}
        onToolbarSearchKeywordChange={setToolbarSearchKeyword}
        onToolbarSearchKeyDown={handleToolbarSearchKeyDown}
        onSearchGlobalChange={setSearchGlobal}
        onSearchSubmit={() => { void runSearch(); }}
        onToggleAllBrowseFiles={toggleAllBrowseFiles}
        onBrowseBatchDownload={handleBrowseBatchDownload}
        onToggleSelectAll={toggleSelectAll}
        onBatchDownload={handleBatchDownload}
        onBatchDelete={handleBatchDelete}
        onOpenPackDialog={openPackDialog}
      />

      {/* Folder contents table (inside folder) */}
      {isInsideFolder ? (
        <>
          <BrowseFolderView
            isMobile={isMobile}
            browseLoading={browseLoading}
            browseContext={browseContext}
            browseContents={browseContents}
            sortedBrowseContents={sortedBrowseContents}
            selectedBrowseFiles={selectedBrowseFiles}
            highlightName={highlightName}
            onSort={handleSort}
            getSortIcon={getSortIcon}
            onToggleAllBrowseFiles={toggleAllBrowseFiles}
            onToggleBrowseFileSelection={toggleBrowseFileSelection}
            onNavigateIntoSubfolder={navigateIntoSubfolder}
            onDownload={handleDownload}
            onUnavailableDelete={() => {
              showToast("文件夹内暂不支持在此页面单文件直接删除", "warning");
            }}
          />
          {!browseLoading && browseTotal > BROWSE_PAGE_SIZE && (
            <PaginationControls
              currentPage={browsePage}
              pageSize={BROWSE_PAGE_SIZE}
              totalFiles={browseTotal}
              onPageChange={handleBrowsePageChange}
            />
          )}
        </>
      ) : (
        /* Root file table */
          <>
          <RootFileTable
            isMobile={isMobile}
            loading={loading}
            error={error}
            sortedFiles={sortedFiles}
            selectedFiles={selectedFiles}
            renaming={renaming}
            newName={newName}
            downloadingFile={downloadingFile}
            highlightUserFileId={highlightUserFileId}
            onSort={handleSort}
            getSortIcon={getSortIcon}
            onToggleSelectAll={toggleSelectAll}
            onToggleFileSelection={toggleFileSelection}
            onNewNameChange={setNewName}
            onRename={handleRename}
            onCancelRename={cancelRename}
            onStartRename={startRename}
            onEnterFolder={enterFolder}
            onDownload={handleDownload}
            onShare={(file) => setShareDialogFile({ id: file.id, name: file.name })}
            onDelete={handleDelete}
            formatDate={formatDate}
          />
          <PaginationControls
            currentPage={currentPage}
            pageSize={pageSize}
            totalFiles={totalFiles}
            onPageChange={setCurrentPage}
            onPageSizeChange={setPageSize}
          />
          </>
      )}

      {/* Search result modal */}
      {showSearchModal && mounted && createPortal(
        <SearchModal
          keyword={searchKeyword}
          results={searchResults}
          loading={searchLoading}
          error={searchError}
          truncated={searchTruncated}
          onLocate={handleLocate}
          onClose={closeSearchModal}
        />,
        document.body
      )}

      {/* Pack Dialog */}
      {packDialogOpen && mounted && createPortal(
        <PackDialog
          selectedCount={selectedFiles.size}
          packSize={packSize}
          availableSpace={availableSpace}
          packOutputName={packOutputName}
          packDeleteSource={packDeleteSource}
          packLoading={packLoading}
          packing={packing}
          onClose={() => setPackDialogOpen(false)}
          onOutputNameChange={setPackOutputName}
          onDeleteSourceChange={setPackDeleteSource}
          onConfirm={handlePackConfirm}
        />,
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
