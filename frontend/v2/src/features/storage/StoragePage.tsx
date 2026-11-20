"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useAuth } from "@/app/hooks";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { PaginationControls } from "@/shared/components/PaginationControls";
import { useToast } from "@/shared/components/toast-context";
import { useSelection } from "@/shared/hooks/useSelection";
import { formatBytes } from "@/shared/lib/format";
import type { StorageReferencingUserResponse } from "@/shared/api/types";

import { deleteStorageFiles, fetchReferencingUsers, fetchStorageFiles } from "./api";
import { firstRejectedMessage, toStorageFileView, type StorageFileView } from "./model";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;
const EMPTY_FILE_IDS: number[] = [];

/** 归档参考 存储管理页原文迁移（react-router 版）：表格/弹窗 DOM、防抖、requestId 守卫与批量反馈按 归档参考。 */
export function StoragePage() {
  const { user } = useAuth();
  const { showToast } = useToast();

  const [files, setFiles] = useState<StorageFileView[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [initialLoadDone, setInitialLoadDone] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  const [orphanOnly, setOrphanOnly] = useState(false);
  const { selected, selectedCount, setItemSelected, selectAll, clear } =
    useSelection<number>(EMPTY_FILE_IDS);
  const [deleting, setDeleting] = useState(false);
  const loading = !initialLoadDone;

  const [userModalFile, setUserModalFile] = useState<StorageFileView | null>(null);
  const [userModalUsers, setUserModalUsers] = useState<StorageReferencingUserResponse[]>([]);
  const [userModalLoading, setUserModalLoading] = useState(false);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);

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
    };
  }, []);

  useEffect(() => {
    if (user?.is_admin) {
      loadFiles();
    }
  }, [user, loadFiles]);

  // 搜索防抖：输入停止 300ms 后才触发后端查询；关键词真正变化时同步回到第 1 页，
  // 保证条件变化只发一笔"第 1 页 + 新条件"的请求（归档参考 交互语义）
  useEffect(() => {
    const timer = setTimeout(() => {
      const next = searchKeyword.trim();
      setDebouncedKeyword((prev) => {
        if (prev === next) return prev;
        setPage(1);
        return next;
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchKeyword]);

  const handlePageChange = useCallback(
    (nextPage: number) => {
      if (nextPage === page) return;
      requestIdRef.current += 1;
      clear();
      setPage(nextPage);
    },
    [page, clear],
  );

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      selectAll(files.map((f) => f.id));
    } else {
      clear();
    }
  };

  // 归档参考实现原文：点击即删，无确认框；202 只表示受理（C15 诚实文案）
  const handleDelete = async () => {
    if (selectedCount === 0) return;
    setDeleting(true);
    try {
      const res = await deleteStorageFiles(Array.from(selected));
      if (mountedRef.current) {
        showToast(`已受理删除 ${res.accepted_count} 个文件`, "success");
        const rejectedMessage = firstRejectedMessage(res);
        if (rejectedMessage !== null) {
          showToast(rejectedMessage, "error");
        }
        clear();
        const remainingTotal = Math.max(0, total - res.accepted_count);
        const targetPage = Math.min(
          page,
          Math.max(1, Math.ceil(remainingTotal / PAGE_SIZE)),
        );
        if (targetPage !== page) {
          setPage(targetPage);
        } else {
          void loadFiles();
        }
      }
    } catch {
      if (!mountedRef.current) return;
      showToast("删除失败", "error");
    } finally {
      if (mountedRef.current) setDeleting(false);
    }
  };

  const openUserModal = async (file: StorageFileView) => {
    setUserModalFile(file);
    setUserModalUsers([]);
    setUserModalLoading(true);
    try {
      const res = await fetchReferencingUsers(file.id);
      if (mountedRef.current) setUserModalUsers(res.items);
    } catch {
      if (!mountedRef.current) return;
      showToast("加载用户列表失败", "error");
    } finally {
      if (mountedRef.current) setUserModalLoading(false);
    }
  };

  if (user !== null && !user.is_admin) {
    return <Navigate to="/tasks" replace />;
  }
  if (!user?.is_admin) return null;
  if (loading) return null;

  return (
    <>
      <div className="glass-frame full-height animate-in">
        <div className="page-header">
          <h1 className="page-title">存储管理</h1>
          <p className="muted">管理系统存储文件</p>
        </div>

        <div className="card mb-5">
          <div className="flex gap-4 flex-wrap items-center">
            <input
              className="input"
              placeholder="搜索文件名..."
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
              style={{ width: 240 }}
              aria-label="搜索文件名"
            />
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={orphanOnly}
                onChange={(e) => {
                  requestIdRef.current += 1;
                  setOrphanOnly(e.target.checked);
                  setPage(1);
                  clear();
                }}
                aria-label="仅显示孤立文件"
              />
              <span className="text-base">仅显示孤立文件</span>
            </label>
            <button
              type="button"
              className="button"
              onClick={loadFiles}
            >
              刷新
            </button>
            <button
              type="button"
              className="button danger"
              onClick={handleDelete}
              disabled={selectedCount === 0 || deleting}
            >
              {deleting ? "删除中..." : `删除选中 (${selectedCount})`}
            </button>
          </div>
        </div>

        <div className="card p-0 overflow-hidden">
          <div style={{ overflowX: "auto" }}>
            <table className="table text-left" style={{ minWidth: 800 }}>
              <thead className="table-header">
                <tr>
                  <th className="table-cell" style={{ width: 40 }}>
                    <input
                      type="checkbox"
                      checked={files.length > 0 && selectedCount === files.length}
                      onChange={(e) => handleSelectAll(e.target.checked)}
                      aria-label="全选"
                    />
                  </th>
                  <th className="table-cell">文件名</th>
                  <th className="table-cell" style={{ width: 80 }}>大小</th>
                  <th className="table-cell" style={{ width: 100 }}>哈希</th>
                  <th className="table-cell" style={{ width: 70 }}>引用数</th>
                  <th className="table-cell" style={{ width: 160 }}>创建时间</th>
                  <th className="table-cell" style={{ width: 80 }}>状态</th>
                </tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.id} className="table-row" data-testid={`storage-row-${f.id}`}>
                    <td className="table-cell">
                      <input
                        type="checkbox"
                        checked={selected.has(f.id)}
                        onChange={(e) => setItemSelected(f.id, e.target.checked)}
                        aria-label={`选择 ${f.namesText === "" ? f.hashFull : f.namesText}`}
                      />
                    </td>
                    <td className="table-cell font-medium" title={f.namesText}>
                      {f.isDirectory ? "📁 " : ""}
                      {f.namesText === "" ? "—" : f.namesText}
                    </td>
                    <td className="table-cell">{formatBytes(f.size)}</td>
                    <td className="table-cell muted" title={f.hashFull}>
                      {f.hashShort}
                    </td>
                    <td className="table-cell">
                      <button
                        type="button"
                        className="button secondary btn-sm"
                        onClick={() => openUserModal(f)}
                        disabled={f.refCount === 0}
                      >
                        {f.refCount}
                      </button>
                    </td>
                    <td className="table-cell muted" suppressHydrationWarning>
                      {new Date(f.createdAtMs).toLocaleString()}
                    </td>
                    <td className="table-cell">
                      {f.onDisk ? (
                        <span className="badge active">存在</span>
                      ) : (
                        <span className="badge danger">缺失</span>
                      )}
                    </td>
                  </tr>
                ))}
                {files.length === 0 && (
                  <tr>
                    <td className="table-cell muted" colSpan={7} style={{ textAlign: "center" }}>
                      暂无存储文件
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <PaginationControls
            currentPage={page}
            pageSize={PAGE_SIZE}
            totalFiles={total}
            onPageChange={handlePageChange}
          />
        </div>
      </div>

      {userModalFile && (
        <ModalOverlay
          onClose={() => setUserModalFile(null)}
          ariaLabel="文件引用用户"
          contentClassName="modal-content max-w-400 animate-in"
        >
          <h3 className="mb-4">引用用户</h3>
          <p className="muted mb-4">{userModalFile.namesText}</p>
          {userModalLoading ? (
            <p className="muted">加载中...</p>
          ) : userModalUsers.length === 0 ? (
            <p className="muted">无引用用户</p>
          ) : (
            <ul className="mb-4">
              {userModalUsers.map((u) => (
                <li key={u.user_id} className="mb-2">
                  <span className="font-medium">{u.username}</span>
                  <span className="muted ml-2">({u.display_name})</span>
                </li>
              ))}
            </ul>
          )}
          <div className="flex flex-end">
            <button
              type="button"
              className="button secondary"
              onClick={() => setUserModalFile(null)}
            >
              关闭
            </button>
          </div>
        </ModalOverlay>
      )}
    </>
  );
}
