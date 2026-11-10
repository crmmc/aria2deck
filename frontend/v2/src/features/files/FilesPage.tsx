import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Pagination,
} from "@/shared/components/ui";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { triggerBatchDownloads, triggerFileDownload } from "@/shared/lib/download";
import type {
  DirectoryBreadcrumbResponse,
  FileEntryItemResponse,
  FileItemResponse,
  FileSearchHitResponse,
  TaskQuotaResponse,
} from "@/shared/api/types";
import {
  ENTRY_PAGE_SIZE,
  ROOT_PAGE_SIZE,
  createShare,
  deleteFile,
  fetchEntries,
  fetchFiles,
  fileDownloadUrl,
  precomputePack,
  renameFile,
  searchFiles,
} from "./api";
import {
  Breadcrumbs,
  EntryTable,
  RootFileTable,
  SearchResults,
  SpaceUsageCard,
  type FileSort,
  type FileSortKey,
} from "./FileViews";
import { PackDialog, RenameDialog, ShareCreateDialog } from "./FilesDialogs";
import { PackTasksCard } from "./PackTasksCard";

interface BrowseContext {
  fileId: number;
  fileName: string;
  path: string;
}

const HIGHLIGHT_MS = 2500;
const SEARCH_DEBOUNCE_MS = 500;

export function FilesPage() {
  const { push } = useToast();
  // 根列表（服务端副本）
  const [files, setFiles] = useState<FileItemResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [space, setSpace] = useState<TaskQuotaResponse | null>(null);
  const [rootLoading, setRootLoading] = useState(true);
  const [rootError, setRootError] = useState<string | null>(null);
  const [rootPage, setRootPage] = useState(1);
  // 排序（F3：当前页客户端排序）
  const [rootSort, setRootSort] = useState<FileSort>(null);
  const [entrySort, setEntrySort] = useState<FileSort>(null);
  // 目录浏览（服务端副本 + 浏览上下文）
  const [browse, setBrowse] = useState<BrowseContext | null>(null);
  const [entries, setEntries] = useState<FileEntryItemResponse[]>([]);
  const [breadcrumbs, setBreadcrumbs] = useState<DirectoryBreadcrumbResponse[]>([]);
  const [entryTotal, setEntryTotal] = useState(0);
  const [entryPage, setEntryPage] = useState(1);
  const [entryLoading, setEntryLoading] = useState(false);
  const [entryError, setEntryError] = useState<string | null>(null);
  // 搜索（F4：查询按钮/回车立即触发；输入 500ms 防抖自动触发；全局/当前目录范围开关）
  const [keyword, setKeyword] = useState("");
  const debouncedKeyword = useDebouncedValue(keyword, SEARCH_DEBOUNCE_MS);
  const [searchScope, setSearchScope] = useState<"global" | "current">("global");
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [hits, setHits] = useState<FileSearchHitResponse[]>([]);
  const [truncated, setTruncated] = useState(false);
  // 选择与高亮
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [entrySelected, setEntrySelected] = useState<Set<string>>(new Set());
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [highlightName, setHighlightName] = useState<string | null>(null);
  // 弹窗与批量删除
  const [renameTarget, setRenameTarget] = useState<FileItemResponse | null>(null);
  const [shareTarget, setShareTarget] = useState<FileItemResponse | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<FileItemResponse | null>(null);
  const [confirmBatchDelete, setConfirmBatchDelete] = useState(false);
  const [packOpen, setPackOpen] = useState(false);
  const [packRefreshKey, setPackRefreshKey] = useState(0);
  const rootSeq = useRef(0);
  const entrySeq = useRef(0);
  const browseRef = useRef<BrowseContext | null>(null);
  browseRef.current = browse;
  const searchSeq = useRef(0);

  const loadRoot = useCallback(async () => {
    const seq = ++rootSeq.current;
    setRootLoading(true);
    setRootError(null);
    try {
      const response = await fetchFiles(rootPage, ROOT_PAGE_SIZE);
      if (seq !== rootSeq.current) return; // 丢弃过期响应。
      setFiles(response.items);
      setTotal(response.total);
      setSpace(response.space);
    } catch (caught) {
      if (seq !== rootSeq.current) return;
      setRootError(errorMessage(caught, "加载失败"));
    } finally {
      if (seq === rootSeq.current) setRootLoading(false);
    }
  }, [rootPage]);

  useEffect(() => {
    if (browse === null) void loadRoot();
  }, [browse, loadRoot]);

  const loadEntries = useCallback(
    async (context: BrowseContext, page: number) => {
      const seq = ++entrySeq.current;
      setEntryLoading(true);
      setEntryError(null);
      try {
        const response = await fetchEntries(context.fileId, context.path, page, ENTRY_PAGE_SIZE);
        if (seq !== entrySeq.current) return;
        setEntries(response.items);
        setBreadcrumbs(response.breadcrumbs);
        setEntryTotal(response.total);
        setEntryPage(page);
      } catch (caught) {
        if (seq !== entrySeq.current) return;
        setEntryError(errorMessage(caught, "加载目录失败"));
      } finally {
        if (seq === entrySeq.current) setEntryLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (browse !== null) void loadEntries(browse, entryPage);
  }, [browse, entryPage, loadEntries]);

  // F4 搜索执行器：范围=当前目录时限定在该目录内容（含子目录路径过滤）。
  // 连续相同的（关键词, 范围）直接跳过：查询按钮/回车/防抖三路触发不重复消耗限流额度。
  const lastSearchRef = useRef<{ term: string; scope: string }>({ term: "", scope: "global" });
  const runSearch = useCallback(
    async (term: string, scope: "global" | "current") => {
      const normalized = term.trim();
      const context = browseRef.current;
      const scopeKey = scope === "current" && context !== null ? "current" : "global";
      if (normalized === "") {
        searchSeq.current += 1;
        lastSearchRef.current = { term: "", scope: scopeKey };
        setHits([]);
        setTruncated(false);
        setSearchError(null);
        setSearchLoading(false);
        return;
      }
      if (
        lastSearchRef.current.term === normalized &&
        lastSearchRef.current.scope === scopeKey
      ) {
        return;
      }
      lastSearchRef.current = { term: normalized, scope: scopeKey };
      const scoped = scopeKey === "current" && context !== null;
      const seq = ++searchSeq.current;
      setSearchLoading(true);
      setSearchError(null);
      try {
        const response = await searchFiles(normalized, scoped ? context.fileId : null);
        if (seq !== searchSeq.current) return;
        const prefix = scoped && context.path !== "" ? `${context.path}/` : null;
        const scopedHits =
          prefix === null ? response.hits : response.hits.filter((hit) => hit.path.startsWith(prefix));
        setHits(scopedHits);
        setTruncated(response.truncated);
      } catch (caught) {
        if (seq !== searchSeq.current) return;
        setHits([]);
        setTruncated(false);
        setSearchError(errorMessage(caught, "搜索失败"));
      } finally {
        if (seq === searchSeq.current) setSearchLoading(false);
      }
    },
    [],
  );

  // 输入防抖自动搜索；按钮/回车立即触发同一执行器。
  useEffect(() => {
    void runSearch(debouncedKeyword, searchScope);
  }, [debouncedKeyword, searchScope, runSearch]);

  const flashHighlight = (apply: () => void, clear: () => void) => {
    apply();
    window.setTimeout(clear, HIGHLIGHT_MS);
  };

  const openBrowse = (fileId: number, fileName: string, path: string, highlight: string | null) => {
    setBrowse({ fileId, fileName, path });
    setEntryPage(1);
    setSelected(new Set());
    setEntrySelected(new Set());
    if (highlight !== null) {
      flashHighlight(() => setHighlightName(highlight), () => setHighlightName(null));
    }
  };

  // F5 定位：根文件不在当前页时逐页找到目标页再跳转（服务端命中不含页索引）。
  const locateRootFile = useCallback(
    async (hit: FileSearchHitResponse) => {
      const seq = ++rootSeq.current;
      const maxPage = Math.max(1, Math.ceil(total / ROOT_PAGE_SIZE));
      for (let page = 1; page <= maxPage; page += 1) {
        let response;
        try {
          response = await fetchFiles(page, ROOT_PAGE_SIZE);
        } catch (caught) {
          push("error", errorMessage(caught, "定位失败"));
          return;
        }
        if (seq !== rootSeq.current) return; // 用户已另行操作，放弃定位。
        if (response.items.some((file) => file.id === hit.user_file_id)) {
          if (page === rootPage) {
            flashHighlight(
              () => setHighlightId(hit.user_file_id),
              () => setHighlightId(null),
            );
          } else {
            setHighlightId(hit.user_file_id);
            window.setTimeout(() => setHighlightId(null), HIGHLIGHT_MS);
            setRootPage(page);
          }
          setBrowse(null);
          return;
        }
      }
      push("error", "定位失败：未在当前列表找到该文件");
    },
    [push, rootPage, total],
  );

  const handleLocate = (hit: FileSearchHitResponse) => {
    if (hit.path === "") {
      if (hit.kind === "directory") {
        openBrowse(hit.user_file_id, hit.display_name, "", null);
        return;
      }
      void locateRootFile(hit);
      return;
    }
    const segments = hit.path.split("/").filter(Boolean);
    const parent = segments.slice(0, -1).join("/");
    openBrowse(hit.user_file_id, hit.display_name, parent, segments[segments.length - 1] ?? null);
  };

  const handleRootDownload = (file: FileItemResponse) => {
    triggerFileDownload(fileDownloadUrl(file.id), file.display_name);
    push("info", "已触发下载");
  };

  const handleEntryDownload = (entry: FileEntryItemResponse) => {
    if (browse === null) return; // 目录行仅在浏览视图渲染，防御性保留。
    triggerFileDownload(fileDownloadUrl(browse.fileId, entry.path), entry.name);
    push("info", "已触发下载");
  };

  const handleBatchDownload = async () => {
    const selectedFiles = files.filter((file) => selected.has(file.id));
    if (selectedFiles.some((file) => file.kind === "directory")) {
      push("error", "选择包含文件夹，无法批量下载，请先打包后下载");
      return;
    }
    push("info", `已触发 ${selectedFiles.length} 个下载`);
    await triggerBatchDownloads(
      selectedFiles.map((file) => fileDownloadUrl(file.id)),
    );
  };

  // F7 目录内批量下载：仅文件条目可选中，iframe 间隔逐个触发。
  const handleEntryBatchDownload = async () => {
    if (browse === null || entrySelected.size === 0) return;
    push("info", `已触发 ${entrySelected.size} 个下载`);
    await triggerBatchDownloads(
      [...entrySelected].map((path) => fileDownloadUrl(browse.fileId, path)),
    );
  };

  // F6 批量删除：逐项删除 + 部分失败反馈 + 删空回页。
  const handleBatchDelete = async () => {
    setConfirmBatchDelete(false);
    const ids = [...selected];
    let deleted = 0;
    let failed = 0;
    for (const id of ids) {
      try {
        await deleteFile(id);
        deleted += 1;
      } catch {
        failed += 1;
      }
    }
    if (failed > 0) push("error", `已删除 ${deleted} 个文件，${failed} 个删除失败`);
    else push("success", `已删除 ${deleted} 个文件`);
    if (deleted > 0) setPackRefreshKey((current) => current + 1); // 整轮只重读一次打包卡。
    setSelected(new Set());
    const remaining = Math.max(0, total - deleted);
    const maxPage = Math.max(1, Math.ceil(remaining / ROOT_PAGE_SIZE));
    if (rootPage > maxPage) setRootPage(maxPage);
    else await loadRoot();
  };

  const handleRename = async (name: string) => {
    if (renameTarget === null) return;
    await renameFile(renameTarget.id, name);
    push("success", "已重命名");
    if (browse === null) await loadRoot();
  };

  const handleDelete = async () => {
    const target = deleteTarget;
    setDeleteTarget(null);
    if (target === null) return;
    try {
      await deleteFile(target.id);
      push("success", "已删除");
      setPackRefreshKey((current) => current + 1); // 删除后打包产物可能已不可用，重读常驻卡。
      // 删空末页回退：当前页只剩被删项时回上一页。
      const remaining = total - 1;
      const maxPage = Math.max(1, Math.ceil(remaining / ROOT_PAGE_SIZE));
      if (rootPage > maxPage) setRootPage(maxPage);
      else await loadRoot();
    } catch (caught) {
      push("error", errorMessage(caught, "删除失败"));
    }
  };

  const toggle = (id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleEntry = (path: string) => {
    setEntrySelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const toggleAllEntries = (checked: boolean) => {
    setEntrySelected(
      checked
        ? new Set(entries.filter((entry) => entry.kind === "file").map((entry) => entry.path))
        : new Set(),
    );
  };

  const breadcrumbNavigate = (path: string) => {
    if (browse === null) return;
    openBrowse(browse.fileId, browse.fileName, path, null);
  };

  const closePackDialog = () => {
    setPackOpen(false);
    setPackRefreshKey((current) => current + 1); // 弹窗关闭后由常驻卡接管进度。
  };

  return (
    <section className="page" aria-label="文件管理">
      <header className="page-header">
        <h1>文件</h1>
        {browse === null ? (
          <span className="header-meta">管理您下载的文件</span>
        ) : (
          <span className="header-meta">{browse.fileName}</span>
        )}
        <div className="header-actions">
          {browse !== null && (
            <button
              type="button"
              className="secondary-button"
              onClick={() => setBrowse(null)}
            >
              返回上级
            </button>
          )}
          {browse === null && selected.size > 0 && (
            <>
              <button type="button" className="secondary-button" onClick={() => setPackOpen(true)}>
                打包（{selected.size}）
              </button>
              <button
                type="button"
                className="secondary-button"
                onClick={() => void handleBatchDownload()}
              >
                批量下载
              </button>
              <button
                type="button"
                className="danger-button"
                onClick={() => setConfirmBatchDelete(true)}
              >
                批量删除（{selected.size}）
              </button>
              <button
                type="button"
                className="link-button"
                onClick={() => setSelected(new Set())}
              >
                清除选择
              </button>
            </>
          )}
          {browse !== null && entrySelected.size > 0 && (
            <>
              <button
                type="button"
                className="secondary-button"
                onClick={() => void handleEntryBatchDownload()}
              >
                批量下载（{entrySelected.size}）
              </button>
              <button
                type="button"
                className="link-button"
                onClick={() => setEntrySelected(new Set())}
              >
                清除选择
              </button>
            </>
          )}
          <label className="search-scope" title="范围开关：全局搜根列表与所有目录；当前目录仅搜正在浏览的目录">
            <input
              type="checkbox"
              checked={searchScope === "current"}
              disabled={browse === null}
              onChange={(event) =>
                setSearchScope(event.target.checked ? "current" : "global")
              }
              aria-label="仅搜索当前目录"
            />
            <span>当前目录</span>
          </label>
          <input
            type="search"
            className="files-search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void runSearch(keyword, searchScope);
            }}
            placeholder="搜索文件名…"
            aria-label="搜索文件"
          />
          <button
            type="button"
            className="secondary-button"
            onClick={() => void runSearch(keyword, searchScope)}
          >
            查询
          </button>
        </div>
      </header>

      {browse === null && <PackTasksCard refreshKey={packRefreshKey} onTaskComplete={() => void loadRoot()} />}
      {browse === null && space !== null && <SpaceUsageCard space={space} />}

      {keyword.trim() !== "" && (
        <div className="search-panel" aria-label="搜索结果">
          {searchLoading ? (
            <LoadingState label="搜索中…" />
          ) : searchError !== null ? (
            <ErrorState message={searchError} />
          ) : (
            <SearchResults hits={hits} truncated={truncated} onLocate={handleLocate} />
          )}
        </div>
      )}

      {browse === null ? (
        <>
          {rootLoading ? (
            <LoadingState />
          ) : rootError !== null ? (
            <ErrorState message={rootError} onRetry={() => void loadRoot()} />
          ) : files.length === 0 ? (
            <EmptyState title="暂无文件" hint="完成的下载会自动入库到文件列表。" />
          ) : (
            <>
              <RootFileTable
                files={files}
                selected={selected}
                highlightId={highlightId}
                sort={rootSort}
                onToggle={toggle}
                onToggleAll={(checked) =>
                  setSelected(checked ? new Set(files.map((file) => file.id)) : new Set())
                }
                onOpen={(file) => openBrowse(file.id, file.display_name, "", null)}
                onDownload={handleRootDownload}
                onRename={setRenameTarget}
                onShare={setShareTarget}
                onDelete={setDeleteTarget}
                onSort={(key: FileSortKey) =>
                  setRootSort((current) => {
                    if (current?.key !== key) return { key, order: "asc" };
                    if (current.order === "asc") return { key, order: "desc" };
                    return null;
                  })
                }
              />
              <Pagination
                page={rootPage}
                pageSize={ROOT_PAGE_SIZE}
                total={total}
                onChange={setRootPage}
              />
            </>
          )}
        </>
      ) : (
        <>
          <Breadcrumbs items={breadcrumbs} onNavigate={breadcrumbNavigate} />
          {entryLoading ? (
            <LoadingState />
          ) : entryError !== null ? (
            <ErrorState message={entryError} onRetry={() => void loadEntries(browse, entryPage)} />
          ) : entries.length === 0 ? (
            <EmptyState title="空目录" hint="该目录下没有内容。" />
          ) : (
            <>
              <EntryTable
                entries={entries}
                selected={entrySelected}
                highlightName={highlightName}
                sort={entrySort}
                onToggle={toggleEntry}
                onToggleAll={toggleAllEntries}
                onNavigate={(entry) =>
                  openBrowse(browse.fileId, browse.fileName, entry.path, null)
                }
                onDownload={handleEntryDownload}
                onSort={(key: FileSortKey) =>
                  setEntrySort((current) => {
                    if (current?.key !== key) return { key, order: "asc" };
                    if (current.order === "asc") return { key, order: "desc" };
                    return null;
                  })
                }
              />
              <Pagination
                page={entryPage}
                pageSize={ENTRY_PAGE_SIZE}
                total={entryTotal}
                onChange={setEntryPage}
              />
            </>
          )}
        </>
      )}

      {renameTarget !== null && (
        <RenameDialog
          initialName={renameTarget.display_name}
          onClose={() => setRenameTarget(null)}
          onConfirm={handleRename}
        />
      )}
      {shareTarget !== null && (
        <ShareCreateDialog
          fileName={shareTarget.display_name}
          onClose={() => setShareTarget(null)}
          onCreate={(options) => createShare(shareTarget.id, options)}
        />
      )}
      {deleteTarget !== null && (
        <ConfirmDialog
          title="删除文件"
          message={`确定删除「${deleteTarget.display_name}」？删除后不可恢复，正在使用的分享会一并撤销。`}
          confirmLabel="删除"
          danger
          onConfirm={() => void handleDelete()}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
      {confirmBatchDelete && (
        <ConfirmDialog
          title="批量删除"
          message={`确定要删除选中的 ${selected.size} 个文件吗？删除后不可恢复，相关分享会一并撤销。`}
          confirmLabel="删除"
          danger
          onConfirm={() => void handleBatchDelete()}
          onCancel={() => setConfirmBatchDelete(false)}
        />
      )}
      {packOpen && (
        <PackDialog
          selectedIds={[...selected]}
          onClose={closePackDialog}
          onPrecompute={precomputePack}
          onCreated={() => void loadRoot()}
        />
      )}
    </section>
  );
}
