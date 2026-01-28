"use client";

import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { useToast } from "@/components/Toast";
import AuthLayout from "@/components/AuthLayout";
import PackConfirmModal from "@/components/PackConfirmModal";
import PackTaskCard from "@/components/PackTaskCard";
import type { FileInfo, QuotaResponse } from "@/types";

function formatDate(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  return date.toLocaleString();
}

export default function FilesPage() {
  const { showToast, showConfirm } = useToast();
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [currentPath, setCurrentPath] = useState("");
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [quota, setQuota] = useState<QuotaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());

  const [packingFolder, setPackingFolder] = useState<FileInfo | null>(null);
  const [packAvailableSpace, setPackAvailableSpace] = useState<number>(0);
  const [packLoading, setPackLoading] = useState(false);
  const [packTasksKey, setPackTasksKey] = useState(0);
  const [calculatingSize, setCalculatingSize] = useState(false);

  const [packingMulti, setPackingMulti] = useState(false);
  const [multiPackSize, setMultiPackSize] = useState(0);
  const [multiPackPaths, setMultiPackPaths] = useState<string[]>([]);
  const [downloadingFile, setDownloadingFile] = useState<string | null>(null);

  const loadFiles = useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    setSelectedFiles(new Set());
    try {
      const response = await api.listFiles(path);
      setFiles(response.files);
      setCurrentPath(response.current_path);
      setParentPath(response.parent_path);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadQuota = async () => {
    try {
      const quotaData = await api.getQuota();
      setQuota(quotaData);
    } catch (err) {
      console.error("Failed to load quota:", err);
    }
  };

  useEffect(() => {
    loadFiles();
    loadQuota();
  }, []);

  const handleNavigate = (path: string) => {
    loadFiles(path);
  };

  const handleDelete = async (file: FileInfo) => {
    const confirmMsg = file.is_dir
      ? `确定要删除文件夹 "${file.name}" 及其所有内容吗？`
      : `确定要删除文件 "${file.name}" 吗？`;

    const confirmed = await showConfirm({
      title: "删除确认",
      message: confirmMsg,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    try {
      await api.deleteFile(file.path);
      loadFiles(currentPath);
      loadQuota();
    } catch (err) {
      showToast(`删除失败: ${(err as Error).message}`, "error");
    }
  };

  const handleRename = async (file: FileInfo) => {
    if (!newName.trim()) {
      showToast("请输入新名称", "warning");
      return;
    }

    try {
      await api.renameFile(file.path, newName.trim());
      setRenaming(null);
      setNewName("");
      loadFiles(currentPath);
    } catch (err) {
      showToast(`重命名失败: ${(err as Error).message}`, "error");
    }
  };

  const startRename = (file: FileInfo) => {
    setRenaming(file.path);
    setNewName(file.name);
  };

  const cancelRename = () => {
    setRenaming(null);
    setNewName("");
  };

  const getQuotaColor = (percentage: number) => {
    if (percentage >= 80) return "var(--danger)";
    if (percentage >= 50) return "var(--warning)";
    return "var(--success)";
  };

  const toggleSelectFile = (path: string) => {
    setSelectedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedFiles.size === files.length) {
      setSelectedFiles(new Set());
    } else {
      setSelectedFiles(new Set(files.map((f) => f.path)));
    }
  };

  const clearSelection = () => {
    setSelectedFiles(new Set());
  };

  const handleStartPack = async (file: FileInfo) => {
    setCalculatingSize(true);
    try {
      const space = await api.getPackAvailableSpace(file.path);

      if (!space.folder_size) {
        showToast("无法计算文件夹大小或文件夹为空", "warning");
        return;
      }

      setPackAvailableSpace(space.user_available);
      setPackingFolder({ ...file, size: space.folder_size });
    } catch (err) {
      showToast(`检查文件夹失败: ${(err as Error).message}`, "error");
    } finally {
      setCalculatingSize(false);
    }
  };

  const handleStartMultiPack = async () => {
    if (selectedFiles.size === 0) return;

    setCalculatingSize(true);
    try {
      const paths = Array.from(selectedFiles);
      const result = await api.calculateFilesSize(paths);

      if (result.total_size === 0) {
        showToast("选中的文件为空", "warning");
        return;
      }

      setMultiPackPaths(paths);
      setMultiPackSize(result.total_size);
      setPackAvailableSpace(result.user_available);
      setPackingMulti(true);
    } catch (err) {
      showToast(`计算大小失败: ${(err as Error).message}`, "error");
    } finally {
      setCalculatingSize(false);
    }
  };

  const handleConfirmPack = async (outputName: string) => {
    if (!packingFolder) return;

    setPackLoading(true);
    try {
      await api.createPackTask(packingFolder.path, outputName);
      setPackingFolder(null);
      setPackTasksKey((k) => k + 1);
    } catch (err) {
      showToast(`创建打包任务失败: ${(err as Error).message}`, "error");
    } finally {
      setPackLoading(false);
    }
  };

  const handleConfirmMultiPack = async (outputName: string) => {
    setPackLoading(true);
    try {
      await api.createPackTaskMulti(multiPackPaths, outputName);
      setPackingMulti(false);
      setMultiPackPaths([]);
      setSelectedFiles(new Set());
      setPackTasksKey((k) => k + 1);
    } catch (err) {
      showToast(`创建打包任务失败: ${(err as Error).message}`, "error");
    } finally {
      setPackLoading(false);
    }
  };

  const handlePackTaskComplete = useCallback(() => {
    loadFiles(currentPath);
    loadQuota();
  }, [currentPath, loadFiles]);

  const handleDownload = async (file: FileInfo) => {
    setDownloadingFile(file.path);
    try {
      const { token } = await api.getDownloadToken(file.path);
      const url = api.downloadFileUrl(token);
      // 创建临时链接并触发下载
      const a = document.createElement("a");
      a.href = url;
      a.download = file.name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (err) {
      showToast(`获取下载链接失败: ${(err as Error).message}`, "error");
    } finally {
      setDownloadingFile(null);
    }
  };

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="flex-between mb-7">
          <div>
            <h1 className="text-2xl">文件</h1>
            <p className="muted">管理您下载的文件</p>
          </div>
          <PackTaskCard key={packTasksKey} onTaskComplete={handlePackTaskComplete} />
        </div>

        {quota && (
          <div className="card mb-6">
            <div className="flex-between mb-3">
              <div>
                <h3 className="stats-label">存储使用情况</h3>
                <div className="flex items-baseline gap-2">
                  <span className="stats-value">{formatBytes(quota.used)}</span>
                  <span className="muted">
                    / {formatBytes(quota.total)} ({quota.percentage.toFixed(1)}%)
                  </span>
                </div>
              </div>
            </div>
            <div className="progress-container">
              <div
                className="progress-bar"
                style={{
                  width: `${quota.percentage}%`,
                  background: getQuotaColor(quota.percentage),
                }}
              />
            </div>
          </div>
        )}

        <div className="card mb-6 py-3 px-4">
          <div className="flex items-center gap-2">
            <button
              className="button secondary btn-sm"
              onClick={() => handleNavigate("")}
            >
              🏠 主页
            </button>
            {currentPath && (
              <>
                <span className="muted">/</span>
                <span className="text-base">{currentPath}</span>
              </>
            )}
            {parentPath !== null && (
              <>
                <span className="ml-auto" />
                <button
                  className="button secondary btn-sm"
                  onClick={() => handleNavigate(parentPath)}
                >
                  ← 返回
                </button>
              </>
            )}
          </div>
        </div>

        {selectedFiles.size > 0 && (
          <div className="card selection-bar mb-4 py-3 px-4 flex items-center gap-4">
            <span className="font-medium">已选中 {selectedFiles.size} 项</span>
            <button
              className="button btn-sm"
              onClick={handleStartMultiPack}
              disabled={calculatingSize}
            >
              {calculatingSize ? "计算中..." : "打包下载"}
            </button>
            <button
              className="button secondary btn-sm"
              onClick={clearSelection}
            >
              取消选择
            </button>
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
                  <th className="table-cell text-center" style={{ width: 40 }}>
                    <input
                      type="checkbox"
                      checked={selectedFiles.size === files.length && files.length > 0}
                      onChange={toggleSelectAll}
                      className="cursor-pointer"
                    />
                  </th>
                  <th className="table-cell text-left">名称</th>
                  <th className="table-cell text-right">大小</th>
                  <th className="table-cell text-right">修改时间</th>
                  <th className="table-cell text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {files.map((file) => (
                  <tr
                    key={file.path}
                    className={`table-row transition-bg ${selectedFiles.has(file.path) ? "selection-bar" : ""}`}
                  >
                    <td className="table-cell text-center">
                      <input
                        type="checkbox"
                        checked={selectedFiles.has(file.path)}
                        onChange={() => toggleSelectFile(file.path)}
                        className="cursor-pointer"
                      />
                    </td>
                    <td className="table-cell">
                      {renaming === file.path ? (
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
                          <span className="file-icon">{file.is_dir ? "📁" : "📄"}</span>
                          {file.is_dir ? (
                            <button
                              className="file-name-btn"
                              onClick={() => handleNavigate(file.path)}
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
                      {file.is_dir ? "-" : formatBytes(file.size)}
                    </td>
                    <td className="table-cell text-right muted text-sm">
                      {formatDate(file.modified_at)}
                    </td>
                    <td className="table-cell text-right">
                      <div className="flex gap-2 flex-end">
                        {file.is_dir ? (
                          <button
                            className="button secondary btn-sm"
                            onClick={() => handleStartPack(file)}
                            disabled={calculatingSize}
                          >
                            {calculatingSize ? "计算中..." : "打包下载"}
                          </button>
                        ) : (
                          <button
                            className="button secondary btn-sm"
                            onClick={() => handleDownload(file)}
                            disabled={downloadingFile === file.path}
                          >
                            {downloadingFile === file.path ? "获取中..." : "下载"}
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

        {packingFolder && (
          <PackConfirmModal
            folderName={packingFolder.name}
            folderSize={packingFolder.size || 0}
            availableSpace={packAvailableSpace}
            onConfirm={handleConfirmPack}
            onCancel={() => setPackingFolder(null)}
            loading={packLoading}
          />
        )}

        {packingMulti && (
          <PackConfirmModal
            folderName="多文件打包"
            folderSize={multiPackSize}
            availableSpace={packAvailableSpace}
            onConfirm={handleConfirmMultiPack}
            onCancel={() => {
              setPackingMulti(false);
              setMultiPackPaths([]);
            }}
            loading={packLoading}
            isMultiFile
            fileCount={multiPackPaths.length}
          />
        )}
      </div>
    </AuthLayout>
  );
}
