"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";
import { useToast } from "@/components/Toast";
import { api } from "@/lib/api";
import type { StoredFileInfo, FileUserInfo } from "@/types";

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) {
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }
  if (bytes >= 1024 * 1024) {
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }
  if (bytes >= 1024) {
    return (bytes / 1024).toFixed(2) + " KB";
  }
  return bytes + " B";
}

export default function StoragePage() {
  const { user } = useAuth();
  const router = useRouter();
  const { showToast } = useToast();

  const [files, setFiles] = useState<StoredFileInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [orphanOnly, setOrphanOnly] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [deleting, setDeleting] = useState(false);

  const [userModalFile, setUserModalFile] = useState<StoredFileInfo | null>(null);
  const [userModalUsers, setUserModalUsers] = useState<FileUserInfo[]>([]);
  const [userModalLoading, setUserModalLoading] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    if (user && !user.is_admin) {
      router.replace("/tasks");
    }
  }, [user, router]);

  const initialLoadDone = useRef(false);

  const loadFiles = useCallback(async () => {
    if (!initialLoadDone.current) setLoading(true);
    try {
      const res = await api.listStoredFiles(search || undefined, orphanOnly);
      if (!mountedRef.current) return;
      setFiles(res.files);
      initialLoadDone.current = true;
    } catch {
      if (!mountedRef.current) return;
      showToast("加载存储文件失败", "error");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [search, orphanOnly, showToast]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (user?.is_admin) {
      loadFiles();
    }
  }, [user, loadFiles]);

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelected(new Set(files.map((f) => f.id)));
    } else {
      setSelected(new Set());
    }
  };

  const handleSelect = (id: number, checked: boolean) => {
    const newSet = new Set(selected);
    if (checked) {
      newSet.add(id);
    } else {
      newSet.delete(id);
    }
    setSelected(newSet);
  };

  const handleDelete = async () => {
    if (selected.size === 0) return;
    setDeleting(true);
    try {
      const res = await api.bulkDeleteStoredFiles(Array.from(selected));
      if (!mountedRef.current) return;
      showToast(`已删除 ${res.deleted_count} 个文件`, "success");
      if (res.errors.length > 0) {
        showToast(res.errors[0], "error");
      }
      setSelected(new Set());
      loadFiles();
    } catch {
      if (!mountedRef.current) return;
      showToast("删除失败", "error");
    } finally {
      if (mountedRef.current) setDeleting(false);
    }
  };

  const openUserModal = async (file: StoredFileInfo) => {
    setUserModalFile(file);
    setUserModalUsers([]);
    setUserModalLoading(true);
    try {
      const res = await api.getFileUsers(file.id);
      if (!mountedRef.current) return;
      setUserModalUsers(res.users);
    } catch {
      if (!mountedRef.current) return;
      showToast("加载用户列表失败", "error");
    } finally {
      if (mountedRef.current) setUserModalLoading(false);
    }
  };

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
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ width: 240 }}
            />
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={orphanOnly}
                onChange={(e) => setOrphanOnly(e.target.checked)}
              />
              <span className="text-base">仅显示孤立文件</span>
            </label>
            <button
              className="button"
              onClick={loadFiles}
            >
              刷新
            </button>
            <button
              className="button danger"
              onClick={handleDelete}
              disabled={selected.size === 0 || deleting}
            >
              {deleting ? "删除中..." : `删除选中 (${selected.size})`}
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
                      checked={files.length > 0 && selected.size === files.length}
                      onChange={(e) => handleSelectAll(e.target.checked)}
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
                <tr key={f.id} className="table-row">
                  <td className="table-cell">
                    <input
                      type="checkbox"
                      checked={selected.has(f.id)}
                      onChange={(e) => handleSelect(f.id, e.target.checked)}
                    />
                  </td>
                  <td className="table-cell font-medium" title={f.original_name}>
                    {f.is_directory ? "📁 " : ""}{f.original_name}
                  </td>
                  <td className="table-cell">{formatSize(f.size)}</td>
                  <td className="table-cell muted" title={f.content_hash}>
                    {f.content_hash.slice(0, 8)}...
                  </td>
                  <td className="table-cell">
                    <button
                      className="button secondary btn-sm"
                      onClick={() => openUserModal(f)}
                      disabled={f.ref_count === 0}
                    >
                      {f.ref_count}
                    </button>
                  </td>
                  <td className="table-cell muted">
                    {new Date(f.created_at).toLocaleString()}
                  </td>
                  <td className="table-cell">
                    {f.exists_on_disk ? (
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
        </div>
      </div>

      {userModalFile && (
        <div className="modal-overlay" onClick={() => setUserModalFile(null)}>
          <div
            className="modal-content max-w-400 animate-in"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="mb-4">引用用户</h3>
            <p className="muted mb-4">{userModalFile.original_name}</p>
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
                className="button secondary"
                onClick={() => setUserModalFile(null)}
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
