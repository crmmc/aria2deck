"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useAuth } from "@/shared/auth/hooks";
import { errorMessage } from "@/shared/api/client";
import { DialogShell } from "@/shared/components/DialogShell";
import { EmptyState } from "@/shared/components/EmptyState";
import { Icon } from "@/shared/components/Icon";
import { PageHeader } from "@/shared/components/PageHeader";
import { PaginationControls } from "@/shared/components/PaginationControls";
import { ToolbarSearchInput } from "@/shared/components/Toolbar";
import { useToast } from "@/shared/components/toast-context";
import { BatchFailureDialog, type BatchFailure } from "@/shared/components/BatchFailureDialog";
import { useSelection } from "@/shared/hooks/useSelection";
import { formatAdminBytes, formatAdminDateTime } from "@/shared/lib/format";
import { requestModalClose } from "@/shared/ui/modalClose";
import type { StorageReferencingUserResponse } from "@/shared/api/types";

import { deleteStorageFiles, fetchReferencingUsers, fetchStorageFiles } from "./api";
import { firstRejectedMessage, toStorageFileView, type StorageFileView } from "./model";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;
const EMPTY_FILE_IDS: number[] = [];

export function StoragePage() {
  const { user } = useAuth();
  const { showToast } = useToast();

  const [files, setFiles] = useState<StorageFileView[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [initialLoadDone, setInitialLoadDone] = useState(false);
  const [batchFailures, setBatchFailures] = useState<BatchFailure[]>([]);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  const [orphanOnly, setOrphanOnly] = useState(false);
  const { selected, setItemSelected, selectAll, clear } = useSelection<number>(EMPTY_FILE_IDS);
  const [deleteTargetIds, setDeleteTargetIds] = useState<number[] | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [userModalFile, setUserModalFile] = useState<StorageFileView | null>(null);
  const [userModalUsers, setUserModalUsers] = useState<StorageReferencingUserResponse[]>([]);
  const [userModalLoading, setUserModalLoading] = useState(false);
  const [userModalError, setUserModalError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);
  const referenceRequestIdRef = useRef(0);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const userModalCloseRef = useRef<HTMLButtonElement>(null);
  const deleteReturnRef = useRef<HTMLButtonElement>(null);
  const searchSettledRef = useRef(true);

  const currentPageSelectedCount = files.reduce(
    (count, file) => count + (selected.has(file.id) ? 1 : 0),
    0,
  );
  const allCurrentPageSelected = files.length > 0 && currentPageSelectedCount === files.length;
  const loading = !initialLoadDone;
  const filtersActive = debouncedKeyword !== "" || orphanOnly;
  const searchSettled = searchKeyword.trim() === debouncedKeyword;
  searchSettledRef.current = searchSettled;

  const loadFiles = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    try {
      const res = await fetchStorageFiles(page, PAGE_SIZE, debouncedKeyword, orphanOnly);
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      const lastPage = Math.max(1, Math.ceil(res.total / res.page_size));
      if (res.page > lastPage) {
        setPage(lastPage);
        return;
      }
      setFiles(res.items.map(toStorageFileView));
      setTotal(res.total);
      clear();
    } catch {
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      showToast("加载存储文件失败", "error");
    } finally {
      if (mountedRef.current && requestId === requestIdRef.current) {
        setInitialLoadDone(true);
      }
    }
  }, [page, debouncedKeyword, orphanOnly, clear, showToast]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestIdRef.current += 1;
      referenceRequestIdRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (user?.is_admin && searchSettledRef.current) {
      void loadFiles();
    }
  }, [user, loadFiles]);

  useEffect(() => {
    const timer = setTimeout(() => {
      const next = searchKeyword.trim();
      setDebouncedKeyword((previous) => previous === next ? previous : next);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchKeyword]);

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = currentPageSelectedCount > 0 && !allCurrentPageSelected;
    }
  }, [allCurrentPageSelected, currentPageSelectedCount]);

  useEffect(() => {
    if (userModalFile) userModalCloseRef.current?.focus();
  }, [userModalFile]);

  useEffect(() => {
    if (deleteTargetIds !== null) deleteReturnRef.current?.focus();
  }, [deleteTargetIds]);

  const handleSearchChange = (value: string) => {
    clear();
    if (value.trim() !== debouncedKeyword) {
      requestIdRef.current += 1;
      setPage(1);
    }
    setSearchKeyword(value);
  };

  const handleOrphanToggle = () => {
    requestIdRef.current += 1;
    setDebouncedKeyword(searchKeyword.trim());
    setOrphanOnly((previous) => !previous);
    setPage(1);
    clear();
  };

  const handleResetFilters = () => {
    requestIdRef.current += 1;
    setSearchKeyword("");
    setDebouncedKeyword("");
    setOrphanOnly(false);
    setPage(1);
    clear();
  };

  const handlePageChange = (nextPage: number) => {
    requestIdRef.current += 1;
    clear();
    setPage(nextPage);
  };

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      selectAll(files.map((file) => file.id));
      return;
    }
    clear();
  };

  const openDeleteDialog = () => {
    setDeleteTargetIds(files.filter((file) => selected.has(file.id)).map((file) => file.id));
  };

  const handleDelete = async (trigger: Element, targetIds: number[]) => {
    setDeleting(true);
    try {
      const res = await deleteStorageFiles(targetIds);
      if (!mountedRef.current) return;
      const nameById = new Map(files.map((file) => [file.id, file.namesText]));
      const failures = res.results
        .filter((item) => !item.accepted)
        .map((item) => ({
          label: nameById.get(item.content_id) ?? `内容 ${item.content_id}`,
          reason: item.error?.message ?? "未知错误",
        }));
      if (res.accepted_count === 0) {
        setBatchFailures(failures.length > 0 ? failures : [{ label: "删除存储内容", reason: firstRejectedMessage(res) ?? "删除失败" }]);
        showToast(firstRejectedMessage(res) ?? "删除失败", "error");
        return;
      }

      showToast(`已受理删除 ${res.accepted_count} 个文件`, "success");
      if (failures.length > 0) setBatchFailures(failures);
      clear();
      requestModalClose(trigger);
      const remainingTotal = Math.max(0, total - res.accepted_count);
      const targetPage = Math.min(page, Math.max(1, Math.ceil(remainingTotal / PAGE_SIZE)));
      if (targetPage !== page) {
        setPage(targetPage);
      } else {
        void loadFiles();
      }
    } catch (caught) {
      if (!mountedRef.current) return;
      showToast(errorMessage(caught, "删除失败"), "error");
    } finally {
      if (mountedRef.current) setDeleting(false);
    }
  };

  const closeUserModal = useCallback(() => {
    referenceRequestIdRef.current += 1;
    setUserModalFile(null);
  }, []);

  const openUserModal = async (file: StorageFileView) => {
    const requestId = ++referenceRequestIdRef.current;
    setUserModalFile(file);
    setUserModalUsers([]);
    setUserModalError(null);
    setUserModalLoading(true);
    try {
      const res = await fetchReferencingUsers(file.id);
      if (mountedRef.current && requestId === referenceRequestIdRef.current) {
        setUserModalUsers(res.items);
      }
    } catch (caught) {
      if (!mountedRef.current || requestId !== referenceRequestIdRef.current) return;
      const message = errorMessage(caught, "加载用户列表失败");
      setUserModalError(message);
      showToast(message, "error");
    } finally {
      if (mountedRef.current && requestId === referenceRequestIdRef.current) {
        setUserModalLoading(false);
      }
    }
  };

  if (user !== null && !user.is_admin) {
    return <Navigate to="/tasks" replace />;
  }
  if (!user?.is_admin) return null;
  if (loading) return null;

  return (
    <>
      <div className="glass-frame">
        <PageHeader title="存储管理" description="查看内容引用、孤立文件与磁盘状态" />

        <section className="panel admin-table-panel" aria-label="存储文件列表">
          <div className="toolbar admin-storage-toolbar">
            <label className="toolbar-label" htmlFor="storage-search">搜索内容哈希或存储路径</label>
            <ToolbarSearchInput
              id="storage-search"
              value={searchKeyword}
              placeholder="搜索内容哈希或存储路径"
              ariaLabel="搜索内容哈希或存储路径"
              clearAriaLabel="清空内容哈希或存储路径搜索"
              onChange={handleSearchChange}
            />
            <div className="toolbar-spacer" />
            <button
              type="button"
              className="button secondary btn-sm toolbar-toggle"
              aria-pressed={orphanOnly}
              onClick={handleOrphanToggle}
            >
              仅显示孤立文件
            </button>
            <button
              type="button"
              className="button secondary btn-sm"
              onClick={() => {
                clear();
                void loadFiles();
              }}
            >
              <Icon name="refresh" className="icon-sm" />刷新
            </button>
            <button
              type="button"
              className="button danger btn-sm"
              onClick={openDeleteDialog}
              disabled={currentPageSelectedCount === 0 || deleting}
            >
              <Icon name="trash" className="icon-sm" />删除选中({currentPageSelectedCount})
            </button>
          </div>

          <div className="admin-table-scroll">
            <table className="admin-table admin-storage-table">
              <thead>
                <tr>
                  <th className="admin-check-cell">
                    <input
                      ref={selectAllRef}
                      type="checkbox"
                      checked={allCurrentPageSelected}
                      disabled={files.length === 0}
                      onChange={(event) => handleSelectAll(event.target.checked)}
                      aria-label="选择当前页筛选结果"
                    />
                  </th>
                  <th>文件名</th>
                  <th>大小</th>
                  <th>内容哈希</th>
                  <th>引用数</th>
                  <th>创建时间</th>
                  <th>磁盘状态</th>
                </tr>
              </thead>
              <tbody>
                {files.map((file) => {
                  const displayName = file.namesText || "—";
                  return (
                    <tr
                      key={file.id}
                      className={selected.has(file.id) ? "selected" : undefined}
                      data-testid={`storage-row-${file.id}`}
                    >
                      <td className="admin-check-cell">
                        <input
                          type="checkbox"
                          checked={selected.has(file.id)}
                          onChange={(event) => setItemSelected(file.id, event.target.checked)}
                          aria-label={`选择文件 ${displayName}`}
                        />
                      </td>
                      <td className="admin-name-cell" title={displayName}>{displayName}</td>
                      <td>{formatAdminBytes(file.size)}</td>
                      <td>
                        <span className="admin-hash mono" title={file.hashFull}>{file.hashShort}</span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="button secondary btn-sm admin-ref-button"
                          onClick={() => void openUserModal(file)}
                          disabled={file.refCount === 0}
                          aria-label={`查看 ${displayName} 的 ${file.refCount} 条引用`}
                        >
                          {file.refCount} 条引用
                        </button>
                      </td>
                      <td suppressHydrationWarning>{formatAdminDateTime(file.createdAtMs)}</td>
                      <td>
                        <span className={`status-badge ${file.onDisk ? "status-success" : "status-danger"}`}>
                          <Icon name={file.onDisk ? "check" : "alert"} className="icon-sm" />
                          {file.onDisk ? "存在" : "缺失"}
                        </span>
                      </td>
                    </tr>
                  );
                })}
                {files.length === 0 && (
                  <tr>
                    <td colSpan={7}>
                      <EmptyState
                        icon={<Icon name="search" className="icon-lg" />}
                        title="没有匹配的存储内容"
                        description="调整内容哈希或存储路径搜索或孤立文件筛选后再试。"
                      >
                        <button type="button" className="button secondary" onClick={handleResetFilters}>
                          重置筛选
                        </button>
                      </EmptyState>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="admin-table-footer admin-storage-footer">
            <span className="admin-result-count">
              {filtersActive ? `筛选出 ${total} 条` : `共 ${total} 条`}
            </span>
            <span>已选当前页 {currentPageSelectedCount} 项 · 批量选择只作用于当前页当前筛选结果</span>
            <PaginationControls
              currentPage={page}
              pageSize={PAGE_SIZE}
              totalFiles={total}
              onPageChange={handlePageChange}
            />
          </div>
        </section>
      </div>

      {userModalFile && (
        <DialogShell
          onClose={closeUserModal}
          ariaLabel="引用用户"
          contentClassName="modal-content admin-dialog-content"
        >
          <div className="dialog-shell">
            <div className="dialog-head">
              <h2>引用用户</h2>
              <button
                className="icon-btn"
                type="button"
                aria-label="关闭对话框"
                onClick={(event) => requestModalClose(event.currentTarget)}
              >
                <Icon name="x" />
              </button>
            </div>
            <div className="dialog-body">
              <p className="dialog-message admin-reference-message">
                {userModalFile.namesText || "未命名内容"} 被以下用户引用：
              </p>
              {userModalLoading ? (
                <p className="muted" role="status">加载中...</p>
              ) : userModalError !== null ? (
                <p className="field-error admin-reference-error" role="alert">{userModalError}</p>
              ) : userModalUsers.length === 0 ? (
                <p className="muted">无引用用户</p>
              ) : (
                <div className="admin-reference-list">
                  {userModalUsers.map((reference) => (
                    <div
                      className="admin-reference-row"
                      key={`${reference.user_id}:${reference.display_name}:${reference.referenced_at_ms}`}
                    >
                      <span className="avatar" aria-hidden="true">
                        {reference.username.slice(0, 2).toUpperCase()}
                      </span>
                      <div className="admin-reference-copy">
                        <strong>{reference.username}</strong>
                        <span>用户 ID {reference.user_id} · {reference.display_name}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="dialog-actions">
              <button
                ref={userModalCloseRef}
                type="button"
                className="button secondary"
                onClick={(event) => requestModalClose(event.currentTarget)}
              >
                关闭
              </button>
            </div>
          </div>
        </DialogShell>
      )}

      {deleteTargetIds !== null && (
        <DialogShell
          onClose={() => setDeleteTargetIds(null)}
          ariaLabel="删除存储文件"
          contentClassName="modal-content admin-dialog-content"
          closeDisabled={deleting}
        >
          <div className="dialog-shell">
            <div className="dialog-head">
              <h2>删除存储文件</h2>
              <button
                className="icon-btn"
                type="button"
                aria-label="关闭对话框"
                disabled={deleting}
                onClick={(event) => requestModalClose(event.currentTarget)}
              >
                <Icon name="x" />
              </button>
            </div>
            <div className="dialog-body">
              <p className="dialog-message">
                确定删除当前筛选结果中选中的 {deleteTargetIds.length} 个文件吗？
              </p>
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="button secondary"
                ref={deleteReturnRef}
                disabled={deleting}
                onClick={(event) => requestModalClose(event.currentTarget)}
              >
                返回
              </button>
              <button
                type="button"
                className="button danger"
                disabled={deleting}
                onClick={(event) => void handleDelete(event.currentTarget, deleteTargetIds)}
              >
                {deleting ? "删除中..." : "删除文件"}
              </button>
            </div>
          </div>
        </DialogShell>
      )}
      <BatchFailureDialog failures={batchFailures} onClose={() => setBatchFailures([])} />
    </>
  );
}
