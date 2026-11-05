import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import { ConfirmDialog, EmptyState, ErrorState, LoadingState, Pagination } from "@/shared/components/ui";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { formatBytes } from "@/shared/lib/format";
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
import { Breadcrumbs, EntryTable, RootFileTable, SearchResults } from "./FileViews";
import { PackDialog, RenameDialog, ShareCreateDialog } from "./FilesDialogs";

interface BrowseContext {
  fileId: number;
  fileName: string;
  path: string;
}

const HIGHLIGHT_MS = 2500;

export function FilesPage() {
  const { push } = useToast();
  // 根列表（服务端副本）
  const [files, setFiles] = useState<FileItemResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [space, setSpace] = useState<TaskQuotaResponse | null>(null);
  const [rootLoading, setRootLoading] = useState(true);
  const [rootError, setRootError] = useState<string | null>(null);
  const [rootPage, setRootPage] = useState(1);
  // 目录浏览（服务端副本 + 浏览上下文）
  const [browse, setBrowse] = useState<BrowseContext | null>(null);
  const [entries, setEntries] = useState<FileEntryItemResponse[]>([]);
  const [breadcrumbs, setBreadcrumbs] = useState<DirectoryBreadcrumbResponse[]>([]);
  const [entryTotal, setEntryTotal] = useState(0);
  const [entryPage, setEntryPage] = useState(1);
  const [entryLoading, setEntryLoading] = useState(false);
  const [entryError, setEntryError] = useState<string | null>(null);
  // 搜索（防抖 + 结果面板）
  const [keyword, setKeyword] = useState("");
  const debouncedKeyword = useDebouncedValue(keyword, 300);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [hits, setHits] = useState<FileSearchHitResponse[]>([]);
  const [truncated, setTruncated] = useState(false);
  // 选择与高亮
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [highlightName, setHighlightName] = useState<string | null>(null);
  // 弹窗
  const [renameTarget, setRenameTarget] = useState<FileItemResponse | null>(null);
  const [shareTarget, setShareTarget] = useState<FileItemResponse | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<FileItemResponse | null>(null);
  const [packOpen, setPackOpen] = useState(false);
  const rootSeq = useRef(0);
  const entrySeq = useRef(0);
  const browseRef = useRef<BrowseContext | null>(null);
  browseRef.current = browse;

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

  // 防抖搜索：关键词或范围变化时重查；空白则收起面板。
  useEffect(() => {
    const term = debouncedKeyword.trim();
    if (term === "") {
      setHits([]);
      setTruncated(false);
      setSearchError(null);
      setSearchLoading(false);
      return;
    }
    let cancelled = false;
    setSearchLoading(true);
    setSearchError(null);
    searchFiles(term, browseRef.current?.fileId ?? null)
      .then((response) => {
        if (cancelled) return;
        setHits(response.hits);
        setTruncated(response.truncated);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setHits([]);
        setTruncated(false);
        setSearchError(errorMessage(caught, "搜索失败"));
      })
      .finally(() => {
        if (!cancelled) setSearchLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedKeyword, browse]);

  const flashHighlight = (apply: () => void, clear: () => void) => {
    apply();
    window.setTimeout(clear, HIGHLIGHT_MS);
  };

  const openBrowse = (fileId: number, fileName: string, path: string, highlight: string | null) => {
    setBrowse({ fileId, fileName, path });
    setEntryPage(1);
    setSelected(new Set());
    if (highlight !== null) {
      flashHighlight(() => setHighlightName(highlight), () => setHighlightName(null));
    }
  };

  const handleLocate = (hit: FileSearchHitResponse) => {
    if (hit.path === "") {
      if (hit.kind === "directory") {
        openBrowse(hit.user_file_id, hit.display_name, "", null);
        return;
      }
      // 根文件：回根列表第 1 页并高亮（当前页未包含时提示）。
      setBrowse(null);
      setRootPage(1);
      if (files.some((file) => file.id === hit.user_file_id)) {
        flashHighlight(
          () => setHighlightId(hit.user_file_id),
          () => setHighlightId(null),
        );
      } else {
        push("info", "已定位到根列表，该文件不在第 1 页");
      }
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

  const breadcrumbNavigate = (path: string) => {
    if (browse === null) return;
    openBrowse(browse.fileId, browse.fileName, path, null);
  };

  const spaceText =
    space === null
      ? ""
      : `已用 ${formatBytes(space.used_bytes)} · 可用 ${formatBytes(space.available_bytes)}`;

  return (
    <section className="page" aria-label="文件管理">
      <header className="page-header">
        <h1>文件</h1>
        {browse === null ? (
          <span className="header-meta">{spaceText}</span>
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
                className="link-button"
                onClick={() => setSelected(new Set())}
              >
                清除选择
              </button>
            </>
          )}
          <label className="search-field">
            <span className="visually-hidden">搜索文件</span>
            <input
              type="search"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索文件名…"
              aria-label="搜索文件"
            />
          </label>
        </div>
      </header>

      {debouncedKeyword.trim() !== "" && (
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
                onToggle={toggle}
                onToggleAll={(checked) =>
                  setSelected(checked ? new Set(files.map((file) => file.id)) : new Set())
                }
                onOpen={(file) => openBrowse(file.id, file.display_name, "", null)}
                onDownload={handleRootDownload}
                onRename={setRenameTarget}
                onShare={setShareTarget}
                onDelete={setDeleteTarget}
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
                highlightName={highlightName}
                onNavigate={(entry) =>
                  openBrowse(browse.fileId, browse.fileName, entry.path, null)
                }
                onDownload={handleEntryDownload}
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
      {packOpen && (
        <PackDialog
          selectedIds={[...selected]}
          onClose={() => setPackOpen(false)}
          onPrecompute={precomputePack}
          onCreated={() => void loadRoot()}
        />
      )}
    </section>
  );
}
