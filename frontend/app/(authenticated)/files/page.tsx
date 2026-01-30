"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { useToast } from "@/components/Toast";
import PackTaskCard from "@/components/PackTaskCard";
import type { FileInfo, BrowseFileInfo, SpaceInfo } from "@/types";

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleString();
}

export default function FilesPage() {
  const { showToast, showConfirm } = useToast();
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [space, setSpace] = useState<SpaceInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<number | null>(null);
  const [newName, setNewName] = useState("");

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

  const loadFiles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listFiles();
      setFiles(response.files);
      setSpace(response.space);
      // Clear selection when files reload
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
    if (selectedFiles.size === files.length) {
      setSelectedFiles(new Set());
    } else {
      setSelectedFiles(new Set(files.map((f) => f.id)));
    }
  }, [selectedFiles.size, files]);

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

  const navigateBrowseUp = async () => {
    if (!browsingFile || browsePath.length === 0) return;
    const newPath = browsePath.slice(0, -1);
    setBrowseLoading(true);
    try {
      const contents = await api.browseFile(
        browsingFile.id,
        newPath.length > 0 ? newPath.join("/") : undefined
      );
      setBrowseContents(contents);
      setBrowsePath(newPath);
    } catch (err) {
      showToast(`返回上级失败: ${(err as Error).message}`, "error");
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
            {/* Used space */}
            <div
              className="progress-bar"
              style={{
                width: `${getSpacePercentage(space).used + getSpacePercentage(space).frozen}%`,
                background: getSpaceColor(getSpacePercentage(space).used + getSpacePercentage(space).frozen),
              }}
            />
            {/* Frozen space overlay - show as striped pattern on top of used+frozen bar */}
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

      {/* Batch operation toolbar */}
      {files.length > 0 && (
        <div className="card filter-toolbar mb-4">
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
              </>
            )}
            <button
              type="button"
              className="button secondary btn-sm"
              onClick={toggleSelectAll}
            >
              {selectedFiles.size === files.length && files.length > 0
                ? "取消全选"
                : "全选"}
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="card text-center py-8">
          <p className="muted">加载中...</p>
        </div>
      ) : error ? (
        <div className="card text-center py-8">
          <p className="text-danger">{error}</p>
        </div>
      ) : files.length === 0 ? (
        <div className="card text-center py-8">
          <p className="muted">暂无文件</p>
        </div>
      ) : (
        <div className="card p-0 overflow-hidden">
          <table className="table">
            <thead className="table-header">
              <tr>
                <th className="table-cell text-left" style={{ width: "40px" }}>
                  <input
                    type="checkbox"
                    checked={selectedFiles.size === files.length && files.length > 0}
                    onChange={toggleSelectAll}
                    className="checkbox-sm cursor-pointer"
                  />
                </th>
                <th className="table-cell text-left">名称</th>
                <th className="table-cell text-right">大小</th>
                <th className="table-cell text-right">添加时间</th>
                <th className="table-cell text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {files.map((file) => (
                <tr key={file.id} className="table-row transition-bg">
                  <td className="table-cell">
                    <input
                      type="checkbox"
                      checked={selectedFiles.has(file.id)}
                      onChange={() => toggleFileSelection(file.id)}
                      className="checkbox-sm cursor-pointer"
                    />
                  </td>
                  <td className="table-cell">
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
                        />
                        <button
                          className="button secondary btn-sm"
                          onClick={() => handleRename(file)}
                        >
                          ✓
                        </button>
                        <button
                          className="button secondary btn-sm"
                          onClick={cancelRename}
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
                          <span className="text-base">{file.name}</span>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="table-cell text-right muted text-base">
                    {formatBytes(file.size)}
                  </td>
                  <td className="table-cell text-right muted text-sm">
                    {formatDate(file.created_at)}
                  </td>
                  <td className="table-cell text-right">
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
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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

            <div className="card mb-4 py-3 px-4">
              <div className="flex items-center gap-2">
                <button
                  className="button secondary btn-sm"
                  onClick={closeBrowse}
                >
                  🏠 根目录
                </button>
                {browsePath.length > 0 && (
                  <>
                    <span className="muted">/</span>
                    <span className="text-base">{browsePath.join("/")}</span>
                    <span className="ml-auto" />
                    <button
                      className="button secondary btn-sm"
                      onClick={navigateBrowseUp}
                    >
                      ← 返回
                    </button>
                  </>
                )}
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
    </div>
  );
}
