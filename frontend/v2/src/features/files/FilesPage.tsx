"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { createPortal } from "react-dom";

import { useToast } from "@/shared/components/toast-context";
import { BatchFailureDialog, type BatchFailure } from "@/shared/components/BatchFailureDialog";
import { PaginationControls } from "@/shared/components/PaginationControls";
import { PageHeader } from "@/shared/components/PageHeader";
import { useIsMobile } from "@/shared/hooks/useIsMobile";
import { Icon, type SortDirection } from "@/shared/components/Icon";
import { useMounted } from "@/shared/hooks/useMounted";
import { useListSelection } from "@/shared/hooks/useListSelection";
import { fileDownloadUrl } from "@/shared/api/urls";
import { triggerBatchDownloads, triggerFileDownload } from "@/shared/lib/download";
import { formatBytes } from "@/shared/lib/format";
import { requestModalClose } from "@/shared/ui/modalClose";
import { batchDeleteFiles, createPack, fetchEntries, fetchFiles, precomputePack, renameFile, searchFiles } from "./api";
import {
  browseItemKey,
  formatFileDate,
  locateFilePage,
  matchesCondition,
  sortBrowseItems,
  sortFiles,
  toBrowseItemView,
  toFileView,
  toSearchHitView,
  type BrowseItemView,
  type FileView,
  type FilterCondition,
  type SearchHitView,
} from "./model";
import { BrowseFolderView } from "./components/BrowseFolderView";
import { SearchModal } from "./components/SearchModal";
import { FileToolbar, type BrowseContext, type SortField, type SortOrder } from "./components/FileToolbar";
import { PackDialog } from "./components/PackDialog";
import { RootFileTable } from "./components/RootFileTable";
import { PackEntryButton } from "./components/PackEntryButton";
import { CreateShareDialog } from "./components/CreateShareDialog";
import { FilterPopover } from "./components/FilterPopover";

const LOCATE_HIGHLIGHT_MS = 1800;
const BROWSE_PAGE_SIZE = 200;
const ROOT_PAGE_SIZE = 10;
const BATCH_DOWNLOAD_INTERVAL_MS = 1200;

export function FilesPage() {
  const { showToast, showConfirm } = useToast();
  const [files, setFiles] = useState<FileView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<number | null>(null);
  const [newName, setNewName] = useState("");

  // Sort state
  const [sortField, setSortField] = useState<SortField>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");

  // Search state（toolbar 是唯一输入源；弹窗只展示结果）
  const [toolbarSearchKeyword, setToolbarSearchKeyword] = useState("");
  const [showSearchModal, setShowSearchModal] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [searchResults, setSearchResults] = useState<SearchHitView[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchTruncated, setSearchTruncated] = useState(false);
  const [searchGlobal, setSearchGlobal] = useState(false);
  const [highlightUserFileId, setHighlightUserFileId] = useState<number | null>(null);
  const [highlightName, setHighlightName] = useState<string | null>(null);
  const pendingRootLocateRef = useRef<number | null>(null);
  const searchCloseTargetRef = useRef<Element | null>(null);
  const toolbarSearchInputRef = useRef<HTMLInputElement>(null);

  // Batch selection state
  const [isBatchOperating, setIsBatchOperating] = useState(false);
  const [batchFailures, setBatchFailures] = useState<BatchFailure[]>([]);

  // Folder browsing state（in-page navigation）
  const [browseContext, setBrowseContext] = useState<BrowseContext | null>(null);
  const [browseContents, setBrowseContents] = useState<BrowseItemView[]>([]);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [browsePage, setBrowsePage] = useState(1);
  const [browseTotal, setBrowseTotal] = useState(0);
  const browseRequestIdRef = useRef(0);
  const isInsideFolder = browseContext !== null;

  const [packTasksKey, setPackTasksKey] = useState(0);
  const [downloadingFile, setDownloadingFile] = useState<number | null>(null);

  // 本页条件筛选：条件只作用于当前页已加载数据，翻页保留、刷新不保留
  const [conditions, setConditions] = useState<FilterCondition[]>([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [filterClosing, setFilterClosing] = useState(false);
  const filterButtonRef = useRef<HTMLButtonElement>(null);

  // 排序与多选：选择集挂在筛选后的可见顺序上，范围选择与方向键扩缩都用同一数组算下标。
  const sortedFiles = useMemo(
    () => sortFiles(files, sortField, sortOrder),
    [files, sortField, sortOrder],
  );

  const visibleFiles = useMemo(
    () => sortedFiles.filter((file) => conditions.every((condition) => matchesCondition(file, condition))),
    [sortedFiles, conditions],
  );
  const activeFilterCount = conditions.filter((condition) => condition.value !== "").length;

  const sortedBrowseContents = useMemo(
    () => sortBrowseItems(browseContents, sortField, sortOrder),
    [browseContents, sortField, sortOrder],
  );

  const rootSelectionIds = useMemo(() => visibleFiles.map((file) => file.id), [visibleFiles]);
  const browseSelectionIds = useMemo(
    () => sortedBrowseContents.map((item) => browseItemKey(browseContext?.path ?? [], item.name)),
    [browseContext, sortedBrowseContents],
  );
  const rootSelection = useListSelection(rootSelectionIds);
  const browseSelection = useListSelection(browseSelectionIds);
  const selectedFiles = rootSelection.selected;
  const selectedBrowseFiles = browseSelection.selected;
  const {
    clear: clearRootSelection,
    toggle: toggleFileSelection,
    toggleAll: toggleRootSelectionAll,
    handleKeyDown: handleRootSelectionKeyDown,
    setSelected: setRootSelected,
  } = rootSelection;
  const {
    clear: clearBrowseSelection,
    toggle: toggleBrowseItemSelection,
    toggleAll: toggleBrowseSelectionAll,
    handleKeyDown: handleBrowseSelectionKeyDown,
  } = browseSelection;

  // 筛选/翻页导致可见集合变化时，剔除已不可见的选中项：隐藏项不参与任何批量操作
  useEffect(() => {
    setRootSelected((prev) => {
      if (prev.size === 0) return prev;
      const visibleIds = new Set(visibleFiles.map((file) => file.id));
      const next = new Set([...prev].filter((id) => visibleIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [visibleFiles, setRootSelected]);

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
  const [pageSize, setPageSize] = useState(ROOT_PAGE_SIZE);
  const [totalFiles, setTotalFiles] = useState(0);

  const closeSearchModal = useCallback(() => {
    searchCloseTargetRef.current = null;
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
      const response = await fetchFiles(page, size);
      const views = response.items.map(toFileView);
      setFiles(views);
      setTotalFiles(response.total);
      clearRootSelection();
      const pendingLocateId = pendingRootLocateRef.current;
      if (pendingLocateId !== null) {
        pendingRootLocateRef.current = null;
        if (views.some((f) => f.id === pendingLocateId)) {
          // 定位目标可能被筛选隐藏：先清条件再高亮，避免定位静默失败
          setConditions((prev) => (prev.length === 0 ? prev : []));
          setHighlightUserFileId(pendingLocateId);
          window.setTimeout(() => setHighlightUserFileId(null), LOCATE_HIGHLIGHT_MS);
          requestModalClose(searchCloseTargetRef.current);
          searchCloseTargetRef.current = null;
        } else {
          showToast("定位失败：未在当前列表找到该文件", "error");
        }
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [showToast, clearRootSelection, setConditions]);

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
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(sortOrder === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortOrder(field === "name" || field === "type" ? "asc" : "desc");
    }
  };

  const getSortIcon = (field: SortField): SortDirection => {
    if (sortField !== field) return "none";
    return sortOrder;
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
      // C08：V2 作用域参数只支持整个目录（file_id），不支持目录内子路径；
      // 非全局且在目录中时，按目录 id 作为作用域。
      const scopeFileId = !searchGlobal && browseContext ? browseContext.fileId : null;
      const response = await searchFiles(keyword, scopeFileId);
      setSearchResults(response.hits.map(toSearchHitView));
      setSearchTruncated(response.truncated);
    } catch (err) {
      setSearchResults([]);
      setSearchTruncated(false);
      setSearchError((err as Error).message);
    } finally {
      setSearchLoading(false);
    }
  }, [toolbarSearchKeyword, searchGlobal, browseContext, showToast]);

  // 目录浏览分页加载：requestId 守卫丢弃过期响应
  const loadBrowsePage = useCallback(
    async (fileId: number, path: string | undefined, page: number) => {
      const requestId = ++browseRequestIdRef.current;
      setBrowseLoading(true);
      try {
        const result = await fetchEntries(fileId, path ?? "", page, BROWSE_PAGE_SIZE);
        if (requestId !== browseRequestIdRef.current) return null;
        setBrowseContents(result.items.map(toBrowseItemView));
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
    [],
  );

  // 退出目录视图：作废在途目录请求并清空分页状态
  const clearBrowseState = useCallback(() => {
    browseRequestIdRef.current += 1;
    setBrowseContext(null);
    setBrowseContents([]);
    clearBrowseSelection();
    setBrowseTotal(0);
    setBrowsePage(1);
  }, [clearBrowseSelection]);

  const locateInsideFolder = useCallback(async (item: SearchHitView, trigger: Element) => {
    const segments = item.entryPath?.split("/").filter(Boolean) ?? [];
    const name = segments[segments.length - 1];
    const parentSegments = segments.slice(0, -1);
    const keepName = browseContext?.fileId === item.userFileId
      ? browseContext.fileName
      : item.path.split("/").filter(Boolean)[0] || item.name;
    setBrowsePage(1);
    try {
      const result = await loadBrowsePage(
        item.userFileId,
        parentSegments.length > 0 ? parentSegments.join("/") : undefined,
        1,
      );
      if (!result) return;
      setBrowseContext({ fileId: item.userFileId, fileName: keepName, path: parentSegments });
      clearRootSelection();
      clearBrowseSelection();
      if (!name || !result.items.some((c) => c.name === name)) {
        showToast("定位失败：未在文件夹中找到该文件", "error");
        return;
      }
      setHighlightName(name);
      window.setTimeout(() => setHighlightName(null), LOCATE_HIGHLIGHT_MS);
      requestModalClose(trigger);
    } catch (err) {
      showToast(`定位失败: ${(err as Error).message}`, "error");
    }
  }, [browseContext, showToast, loadBrowsePage, clearRootSelection, clearBrowseSelection]);

  const handleLocate = useCallback((item: SearchHitView, trigger: Element) => {
    searchCloseTargetRef.current = trigger;
    if (item.entryPath === null) {
      // C31：V2 无 root_index，逐页扫描定位目标页。
      const locate = async () => {
        const targetPage = await locateFilePage(
          (page) => fetchFiles(page, pageSize),
          item.userFileId,
          pageSize,
        );
        if (targetPage === null) {
          showToast("定位失败：未在当前列表找到该文件", "error");
          return;
        }
        if (browseContext) {
          clearBrowseState();
        }
        if (targetPage !== currentPage) {
          pendingRootLocateRef.current = item.userFileId;
          setCurrentPage(targetPage);
          return;
        }
        setConditions((prev) => (prev.length === 0 ? prev : []));
        setHighlightUserFileId(item.userFileId);
        window.setTimeout(() => setHighlightUserFileId(null), LOCATE_HIGHLIGHT_MS);
        requestModalClose(trigger);
      };
      void locate();
      return;
    }
    void locateInsideFolder(item, trigger);
  }, [browseContext, pageSize, currentPage, showToast, locateInsideFolder, clearBrowseState]);

  const handleToolbarSearchKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      void runSearch();
    }
  };

  const handleDelete = async (file: FileView) => {
    const confirmMsg = file.isDirectory
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
      const result = await batchDeleteFiles([file.id]);
      const item = result.results[0];
      if (!item?.accepted) {
        showToast(`删除失败：${item?.error?.message ?? "未知错误"}`, "error");
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

    const selectedList = visibleFiles.filter((f) => selectedFiles.has(f.id));
    const confirmed = await showConfirm({
      title: "批量删除",
      message: `确定要删除选中的 ${selectedList.length} 个文件吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    setIsBatchOperating(true);
    try {
      const result = await batchDeleteFiles(selectedList.map((f) => f.id));
      if (result.rejected_count > 0) {
        const nameById = new Map(selectedList.map((f) => [f.id, f.name]));
        setBatchFailures(
          result.results
            .filter((item) => !item.accepted)
            .map((item) => ({
              label: nameById.get(item.file_id) ?? `文件 ${item.file_id}`,
              reason: item.error?.message ?? "未知错误",
            })),
        );
        showToast(
          `已受理 ${result.accepted_count} 个文件，${result.rejected_count} 个删除失败`,
          "warning",
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
    const hasFolder = visibleFiles.some((f) => selectedFiles.has(f.id) && f.isDirectory);
    if (hasFolder) {
      showToast("当前选择包含文件夹，无法批量下载，请先打包后下载", "warning");
      return;
    }

    const selectedList = visibleFiles.filter((f) => selectedFiles.has(f.id));
    showToast(`正在发送已选 ${selectedList.length} 个文件的下载请求，请稍候...`, "info");

    // C24：归档参考 节奏 1200ms，显式传入。
    await triggerBatchDownloads(
      selectedList.map((file) => fileDownloadUrl(file.id)),
      BATCH_DOWNLOAD_INTERVAL_MS,
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
      const fileIds = visibleFiles.filter((f) => selectedFiles.has(f.id)).map((f) => f.id);
      const result = await precomputePack(fileIds);
      setPackSize(result.total_size);
      setAvailableSpace(result.available_bytes);
    } catch (err) {
      showToast(`获取信息失败: ${(err as Error).message}`, "error");
      requestModalClose(document.querySelector('dialog[aria-label="创建打包任务"]'));
    } finally {
      setPackLoading(false);
    }
  };

  const handlePackConfirm = async (trigger: Element) => {
    setPacking(true);
    try {
      const fileIds = visibleFiles.filter((f) => selectedFiles.has(f.id)).map((f) => f.id);
      // P1：归档参考实现的默认输出名由后端生成（base="archive" + "." + pack_format，默认 zip）。
      // V2 必填 output_name，前端按 归档参考规则复刻默认值。
      await createPack(fileIds, packOutputName || "archive.zip", packDeleteSource);
      showToast("打包任务已创建", "success");
      requestModalClose(trigger);
      clearRootSelection();
      setPackOutputName("");
      setPackDeleteSource(false);
      setPackTasksKey((k) => k + 1);
    } catch (err) {
      showToast(`创建打包任务失败: ${(err as Error).message}`, "error");
    } finally {
      setPacking(false);
    }
  };

  const toggleSelectAll = useCallback(() => {
    toggleRootSelectionAll();
  }, [toggleRootSelectionAll]);

  const selectedSize = useMemo(() => {
    return visibleFiles
      .filter((f) => selectedFiles.has(f.id))
      .reduce((sum, f) => sum + f.size, 0);
  }, [visibleFiles, selectedFiles]);

  const selectedBrowseSize = useMemo(() => {
    return browseContents
      .filter((item) => selectedBrowseFiles.has([...(browseContext?.path ?? []), item.name].join("/")))
      .reduce((sum, item) => sum + item.size, 0);
  }, [browseContents, browseContext, selectedBrowseFiles]);

  const handleRename = async (file: FileView) => {
    if (!newName.trim()) {
      showToast("请输入新名称", "warning");
      return;
    }

    try {
      await renameFile(file.id, newName.trim());
      setRenaming(null);
      setNewName("");
      loadFiles(currentPage, pageSize);
    } catch (err) {
      showToast(`重命名失败: ${(err as Error).message}`, "error");
    }
  };

  const startRename = (file: FileView) => {
    setRenaming(file.id);
    setNewName(file.name);
  };

  const cancelRename = () => {
    setRenaming(null);
    setNewName("");
  };

  const handleDownload = async (file: FileView, subpath?: string) => {
    setDownloadingFile(file.id);
    try {
      const downloadName = subpath ? subpath.split("/").pop() || file.name : file.name;
      triggerFileDownload(fileDownloadUrl(file.id, subpath), downloadName);
    } finally {
      setDownloadingFile(null);
    }
  };

  // Folder in-page navigation（导航事件内同步重置页码，保证只发一笔「第 1 页」请求）
  const enterFolder = async (file: FileView) => {
    setBrowseContext({ fileId: file.id, fileName: file.name, path: [] });
    clearRootSelection();
    clearBrowseSelection();
    setBrowsePage(1);
    setBrowseTotal(0);
    try {
      const result = await loadBrowsePage(file.id, undefined, 1);
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
      const result = await loadBrowsePage(browseContext.fileId, newPath.join("/"), 1);
      if (!result) return;
      setBrowseContext((prev) => (prev ? { ...prev, path: newPath } : prev));
      clearBrowseSelection();
    } catch (err) {
      showToast(`打开文件夹失败: ${(err as Error).message}`, "error");
    }
  };

  const navigateToBreadcrumb = async (index: number) => {
    if (!browseContext) return;
    const newPath = index < 0 ? [] : browseContext.path.slice(0, index + 1);
    setBrowsePage(1);
    try {
      const result = await loadBrowsePage(
        browseContext.fileId,
        newPath.length > 0 ? newPath.join("/") : undefined,
        1,
      );
      if (!result) return;
      setBrowseContext((prev) => (prev ? { ...prev, path: newPath } : prev));
      clearBrowseSelection();
    } catch (err) {
      showToast(`导航失败: ${(err as Error).message}`, "error");
    }
  };

  // 目录内翻页：换页后旧页选中项不可见，清空避免批量操作命中隐藏项
  const handleBrowsePageChange = useCallback(
    (page: number) => {
      if (!browseContext) return;
      const path = browseContext.path.length > 0 ? browseContext.path.join("/") : undefined;
      clearBrowseSelection();
      loadBrowsePage(browseContext.fileId, path, page).catch((err) => {
        showToast(`翻页失败: ${(err as Error).message}`, "error");
      });
    },
    [browseContext, loadBrowsePage, showToast, clearBrowseSelection],
  );

  const returnToRoot = () => {
    clearBrowseState();
  };

  // Browse file selection helpers
  const toggleAllBrowseFiles = useCallback(() => {
    toggleBrowseSelectionAll();
  }, [toggleBrowseSelectionAll]);

  const handleBrowseBatchDownload = async () => {
    if (!browseContext || selectedBrowseFiles.size === 0) return;
    const hasFolder = browseContents.some((item) => {
      const key = [...(browseContext.path ?? []), item.name].join("/");
      return item.isDirectory && selectedBrowseFiles.has(key);
    });
    if (hasFolder) {
      showToast("当前选择包含文件夹，无法批量下载，请仅选择文件", "warning");
      return;
    }

    const selectedPaths = Array.from(selectedBrowseFiles);
    showToast(`正在发送已选 ${selectedPaths.length} 个文件的下载请求...`, "info");

    await triggerBatchDownloads(
      selectedPaths.map((path) => fileDownloadUrl(browseContext.fileId, path)),
      BATCH_DOWNLOAD_INTERVAL_MS,
    );
    showToast(`已完成 ${selectedPaths.length} 个文件的下载触发`, "success");
  };

  const handlePackTaskComplete = useCallback(() => {
    loadFiles(currentPage, pageSize);
  }, [loadFiles, currentPage, pageSize]);

  const hasBulkSelection = isInsideFolder ? selectedBrowseFiles.size > 0 : selectedFiles.size > 0;

  return (
    <div className={`glass-frame full-height animate-in${hasBulkSelection ? " has-bulk" : ""}`}>
      <PageHeader
        title="文件"
        description="管理您下载的文件"
        actions={<PackEntryButton key={packTasksKey} onTaskComplete={handlePackTaskComplete} />}
      />

      <section className="panel list-panel file-list-panel" aria-label="文件列表">
        <FileToolbar
          isInsideFolder={isInsideFolder}
          browseContext={browseContext}
          browseContents={browseContents}
          sortField={sortField}
          sortOrder={sortOrder}
          toolbarSearchKeyword={toolbarSearchKeyword}
          searchGlobal={searchGlobal}
          searchLoading={searchLoading}
          searchInputRef={toolbarSearchInputRef}
          filterButtonRef={filterButtonRef}
          filterActiveCount={activeFilterCount}
          filterOpen={filterOpen}
          onToggleFilter={() => (filterOpen ? setFilterClosing(true) : setFilterOpen(true))}
          onReturnToRoot={returnToRoot}
          onNavigateToBreadcrumb={navigateToBreadcrumb}
          onSortFieldChange={setSortField}
          onSortOrderChange={setSortOrder}
          onToolbarSearchKeywordChange={setToolbarSearchKeyword}
          onToolbarSearchKeyDown={handleToolbarSearchKeyDown}
          onSearchGlobalChange={setSearchGlobal}
          onSearchSubmit={() => { void runSearch(); }}
        />
        {activeFilterCount > 0 && (
          <div className="filter-status">
            <span>已隐藏 {files.length - visibleFiles.length} / 本页 {files.length} 项</span>
            <button type="button" className="link-btn" onClick={() => setConditions([])}>清除筛选</button>
          </div>
        )}
        {isInsideFolder ? (
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
            onToggleBrowseFileSelection={toggleBrowseItemSelection}
            onSelectionKeyDown={handleBrowseSelectionKeyDown}
            onNavigateIntoSubfolder={navigateIntoSubfolder}
            onDownload={(subpath) => {
              void handleDownload({ id: browseContext.fileId, name: browseContext.fileName, size: 0, isDirectory: true, createdAtMs: 0 }, subpath);
            }}
            onUnavailableDelete={() => {
              showToast("文件夹内暂不支持在此页面单文件直接删除", "warning");
            }}
          />
        ) : (
          <RootFileTable
            isMobile={isMobile}
            loading={loading}
            error={error}
            sortedFiles={visibleFiles}
            filteredEmpty={files.length > 0 && visibleFiles.length === 0}
            onClearFilter={() => setConditions([])}
            selectedFiles={selectedFiles}
            renaming={renaming}
            newName={newName}
            downloadingFile={downloadingFile}
            highlightUserFileId={highlightUserFileId}
            onSort={handleSort}
            getSortIcon={getSortIcon}
            onToggleSelectAll={toggleSelectAll}
            onToggleFileSelection={toggleFileSelection}
            onSelectionKeyDown={handleRootSelectionKeyDown}
            onNewNameChange={setNewName}
            onRename={handleRename}
            onCancelRename={cancelRename}
            onStartRename={startRename}
            onEnterFolder={(file) => { void enterFolder(file); }}
            onDownload={(file) => { void handleDownload(file); }}
            onShare={(file) => setShareDialogFile({ id: file.id, name: file.name })}
            onDelete={(file) => { void handleDelete(file); }}
            formatDate={formatFileDate}
          />
        )}
      </section>

      {isInsideFolder ? (
        !browseLoading && browseTotal > BROWSE_PAGE_SIZE && (
          <PaginationControls
            currentPage={browsePage}
            pageSize={BROWSE_PAGE_SIZE}
            totalFiles={browseTotal}
            onPageChange={handleBrowsePageChange}
          />
        )
      ) : (
        <PaginationControls
          currentPage={currentPage}
          pageSize={pageSize}
          totalFiles={totalFiles}
          onPageChange={setCurrentPage}
          onPageSizeChange={setPageSize}
        />
      )}

      {hasBulkSelection && mounted && createPortal(
        <div className="file-bulk-layer">
          <div className="bulk-bar" role="region" aria-label={isInsideFolder ? "目录文件批量操作" : "文件批量操作"}>
            <span className="bulk-count">已选 <strong>{isInsideFolder ? selectedBrowseFiles.size : selectedFiles.size}</strong> 项</span>
            <span className="bulk-size">{formatBytes(isInsideFolder ? selectedBrowseSize : selectedSize)}</span>
            <span className="bulk-divider" />
            <button
              type="button"
              className="button secondary btn-sm"
              onClick={() => { void (isInsideFolder ? handleBrowseBatchDownload() : handleBatchDownload()); }}
              disabled={!isInsideFolder && isBatchOperating}
            >
              <Icon name="download" className="icon-sm" />批量下载
            </button>
            {!isInsideFolder && (
              <>
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={() => { void openPackDialog(); }}
                  disabled={isBatchOperating}
                >
                  <Icon name="archive" className="icon-sm" />打包
                </button>
                <button
                  type="button"
                  className="button danger btn-sm"
                  onClick={() => { void handleBatchDelete(); }}
                  disabled={isBatchOperating}
                >
                  <Icon name="trash" className="icon-sm" />{isBatchOperating ? "删除中..." : "删除"}
                </button>
              </>
            )}
            <button
              type="button"
              className="icon-btn"
              aria-label="取消选择"
              onClick={() => {
                if (isInsideFolder) clearBrowseSelection();
                else clearRootSelection();
              }}
            >
              <Icon name="x" className="icon-sm" />
            </button>
          </div>
        </div>,
        document.body,
      )}

      {/* Filter Popover */}
      {filterOpen && mounted && (
        <FilterPopover
          conditions={conditions}
          files={files}
          visibleCount={visibleFiles.length}
          trigger={filterButtonRef.current}
          closing={filterClosing}
          onChange={setConditions}
          onRequestClose={() => setFilterClosing(true)}
          onClosed={() => {
            setFilterOpen(false);
            setFilterClosing(false);
          }}
        />
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
        document.body,
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
          onConfirm={(trigger) => { void handlePackConfirm(trigger); }}
        />,
        document.body,
      )}
      {/* Share Dialog */}
      {shareDialogFile && (
        <CreateShareDialog
          userFileId={shareDialogFile.id}
          fileName={shareDialogFile.name}
          onClose={() => setShareDialogFile(null)}
        />
      )}
      <BatchFailureDialog failures={batchFailures} onClose={() => setBatchFailures([])} />
    </div>
  );
}
