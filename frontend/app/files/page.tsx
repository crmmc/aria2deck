"use client";

import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import AuthLayout from "@/components/AuthLayout";
import PackConfirmModal from "@/components/PackConfirmModal";
import PackTaskCard from "@/components/PackTaskCard";
import type { FileInfo, QuotaResponse } from "@/types";

function formatDate(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  return date.toLocaleString();
}

export default function FilesPage() {
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [currentPath, setCurrentPath] = useState("");
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [quota, setQuota] = useState<QuotaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  // Selection state
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());

  // Pack states
  const [packingFolder, setPackingFolder] = useState<FileInfo | null>(null);
  const [packAvailableSpace, setPackAvailableSpace] = useState<number>(0);
  const [packLoading, setPackLoading] = useState(false);
  const [packTasksKey, setPackTasksKey] = useState(0);
  const [calculatingSize, setCalculatingSize] = useState(false);

  // Multi-file pack states
  const [packingMulti, setPackingMulti] = useState(false);
  const [multiPackSize, setMultiPackSize] = useState(0);
  const [multiPackPaths, setMultiPackPaths] = useState<string[]>([]);

  const loadFiles = useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    setSelectedFiles(new Set()); // Clear selection when navigating
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

    if (!confirm(confirmMsg)) return;

    try {
      await api.deleteFile(file.path);
      loadFiles(currentPath);
      loadQuota();
    } catch (err) {
      alert(`删除失败: ${(err as Error).message}`);
    }
  };

  const handleRename = async (file: FileInfo) => {
    if (!newName.trim()) {
      alert("请输入新名称");
      return;
    }

    try {
      await api.renameFile(file.path, newName.trim());
      setRenaming(null);
      setNewName("");
      loadFiles(currentPath);
    } catch (err) {
      alert(`重命名失败: ${(err as Error).message}`);
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
    if (percentage >= 80) return "#ff3b30";
    if (percentage >= 50) return "#ff9500";
    return "#34c759";
  };

  // Selection handlers
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

  // Pack handlers
  const handleStartPack = async (file: FileInfo) => {
    setCalculatingSize(true);
    try {
      // Get folder size and available space from server in single request
      const space = await api.getPackAvailableSpace(file.path);

      if (!space.folder_size) {
        alert("无法计算文件夹大小或文件夹为空");
        return;
      }

      setPackAvailableSpace(space.user_available);
      setPackingFolder({ ...file, size: space.folder_size });
    } catch (err) {
      alert(`检查文件夹失败: ${(err as Error).message}`);
    } finally {
      setCalculatingSize(false);
    }
  };

  // Multi-file pack handler
  const handleStartMultiPack = async () => {
    if (selectedFiles.size === 0) return;

    setCalculatingSize(true);
    try {
      const paths = Array.from(selectedFiles);
      const result = await api.calculateFilesSize(paths);

      if (result.total_size === 0) {
        alert("选中的文件为空");
        return;
      }

      setMultiPackPaths(paths);
      setMultiPackSize(result.total_size);
      setPackAvailableSpace(result.user_available);
      setPackingMulti(true);
    } catch (err) {
      alert(`计算大小失败: ${(err as Error).message}`);
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
      alert(`创建打包任务失败: ${(err as Error).message}`);
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
      alert(`创建打包任务失败: ${(err as Error).message}`);
    } finally {
      setPackLoading(false);
    }
  };

  const handlePackTaskComplete = useCallback(() => {
    loadFiles(currentPath);
    loadQuota();
  }, [currentPath, loadFiles]);

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="space-between" style={{ marginBottom: 32 }}>
          <div>
            <h1 style={{ fontSize: "28px" }}>文件</h1>
            <p className="muted">管理您下载的文件</p>
          </div>
          <PackTaskCard key={packTasksKey} onTaskComplete={handlePackTaskComplete} />
        </div>

        {/* Quota Widget */}
        {quota && (
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="space-between" style={{ marginBottom: 12 }}>
              <div>
                <h3
                  className="muted"
                  style={{ fontSize: 13, textTransform: "uppercase" }}
                >
                  存储使用情况
                </h3>
                <div
                  style={{ display: "flex", alignItems: "baseline", gap: 8 }}
                >
                  <span style={{ fontSize: 24, fontWeight: 600 }}>
                    {formatBytes(quota.used)}
                  </span>
                  <span className="muted">
                    / {formatBytes(quota.total)} ({quota.percentage.toFixed(1)}
                    %)
                  </span>
                </div>
              </div>
            </div>
            <div
              style={{
                height: 6,
                background: "rgba(0,0,0,0.05)",
                borderRadius: 3,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${quota.percentage}%`,
                  background: getQuotaColor(quota.percentage),
                  transition: "width 0.5s ease, background 0.3s ease",
                }}
              />
            </div>
          </div>
        )}

        {/* Breadcrumb Navigation */}
        <div
          className="card"
          style={{ marginBottom: 24, padding: "12px 16px" }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button
              className="button secondary"
              style={{ padding: "6px 12px", fontSize: "13px" }}
              onClick={() => handleNavigate("")}
            >
              🏠 主页
            </button>
            {currentPath && (
              <>
                <span className="muted">/</span>
                <span style={{ fontSize: "14px" }}>{currentPath}</span>
              </>
            )}
            {parentPath !== null && (
              <>
                <span style={{ marginLeft: "auto" }} />
                <button
                  className="button secondary"
                  style={{ padding: "6px 12px", fontSize: "13px" }}
                  onClick={() => handleNavigate(parentPath)}
                >
                  ← 返回
                </button>
              </>
            )}
          </div>
        </div>

        {/* Selection Action Bar */}
        {selectedFiles.size > 0 && (
          <div
            className="card"
            style={{
              marginBottom: 16,
              padding: "12px 16px",
              display: "flex",
              alignItems: "center",
              gap: 16,
              background: "rgba(0, 122, 255, 0.05)",
              border: "1px solid rgba(0, 122, 255, 0.2)",
            }}
          >
            <span style={{ fontWeight: 500 }}>
              已选中 {selectedFiles.size} 项
            </span>
            <button
              className="button"
              style={{ padding: "6px 16px", fontSize: "13px" }}
              onClick={handleStartMultiPack}
              disabled={calculatingSize}
            >
              {calculatingSize ? "计算中..." : "打包下载"}
            </button>
            <button
              className="button secondary"
              style={{ padding: "6px 12px", fontSize: "13px" }}
              onClick={clearSelection}
            >
              取消选择
            </button>
          </div>
        )}

        {/* File List */}
        {loading ? (
          <div className="card" style={{ textAlign: "center", padding: 48 }}>
            <p className="muted">加载中...</p>
          </div>
        ) : error ? (
          <div className="card" style={{ textAlign: "center", padding: 48 }}>
            <p style={{ color: "#ff3b30" }}>{error}</p>
          </div>
        ) : files.length === 0 ? (
          <div className="card" style={{ textAlign: "center", padding: 48 }}>
            <p className="muted">暂无文件</p>
          </div>
        ) : (
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr
                  style={{
                    background: "rgba(0,0,0,0.02)",
                    borderBottom: "1px solid rgba(0,0,0,0.1)",
                  }}
                >
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "center",
                      width: 40,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selectedFiles.size === files.length && files.length > 0}
                      onChange={toggleSelectAll}
                      style={{ cursor: "pointer" }}
                    />
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "left",
                      fontSize: "13px",
                      fontWeight: 600,
                      color: "var(--muted)",
                    }}
                  >
                    名称
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "right",
                      fontSize: "13px",
                      fontWeight: 600,
                      color: "var(--muted)",
                    }}
                  >
                    大小
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "right",
                      fontSize: "13px",
                      fontWeight: 600,
                      color: "var(--muted)",
                    }}
                  >
                    修改时间
                  </th>
                  <th
                    style={{
                      padding: "12px 16px",
                      textAlign: "right",
                      fontSize: "13px",
                      fontWeight: 600,
                      color: "var(--muted)",
                    }}
                  >
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {files.map((file) => (
                  <tr
                    key={file.path}
                    style={{
                      borderBottom: "1px solid rgba(0,0,0,0.05)",
                      transition: "background 0.2s",
                      background: selectedFiles.has(file.path) ? "rgba(0, 122, 255, 0.05)" : "transparent",
                    }}
                    onMouseEnter={(e) => {
                      if (!selectedFiles.has(file.path)) {
                        e.currentTarget.style.background = "rgba(0,0,0,0.02)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!selectedFiles.has(file.path)) {
                        e.currentTarget.style.background = "transparent";
                      }
                    }}
                  >
                    <td style={{ padding: "12px 16px", textAlign: "center" }}>
                      <input
                        type="checkbox"
                        checked={selectedFiles.has(file.path)}
                        onChange={() => toggleSelectFile(file.path)}
                        style={{ cursor: "pointer" }}
                      />
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      {renaming === file.path ? (
                        <div style={{ display: "flex", gap: 8 }}>
                          <input
                            className="input"
                            style={{ padding: "6px 12px", fontSize: "14px" }}
                            value={newName}
                            onChange={(e) => setNewName(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") handleRename(file);
                              if (e.key === "Escape") cancelRename();
                            }}
                            autoFocus
                          />
                          <button
                            className="button secondary"
                            style={{ padding: "6px 12px", fontSize: "13px" }}
                            onClick={() => handleRename(file)}
                          >
                            ✓
                          </button>
                          <button
                            className="button secondary"
                            style={{ padding: "6px 12px", fontSize: "13px" }}
                            onClick={cancelRename}
                          >
                            ✕
                          </button>
                        </div>
                      ) : (
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                          }}
                        >
                          <span style={{ fontSize: "18px" }}>
                            {file.is_dir ? "📁" : "📄"}
                          </span>
                          {file.is_dir ? (
                            <button
                              style={{
                                background: "none",
                                border: "none",
                                color: "var(--primary)",
                                cursor: "pointer",
                                fontSize: "14px",
                                fontWeight: 500,
                                padding: 0,
                              }}
                              onClick={() => handleNavigate(file.path)}
                            >
                              {file.name}
                            </button>
                          ) : (
                            <span style={{ fontSize: "14px" }}>
                              {file.name}
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td
                      style={{
                        padding: "12px 16px",
                        textAlign: "right",
                        fontSize: "14px",
                        color: "var(--muted)",
                      }}
                    >
                      {file.is_dir ? "-" : formatBytes(file.size)}
                    </td>
                    <td
                      style={{
                        padding: "12px 16px",
                        textAlign: "right",
                        fontSize: "13px",
                        color: "var(--muted)",
                      }}
                    >
                      {formatDate(file.modified_at)}
                    </td>
                    <td style={{ padding: "12px 16px", textAlign: "right" }}>
                      <div
                        style={{
                          display: "flex",
                          gap: 8,
                          justifyContent: "flex-end",
                        }}
                      >
                        {file.is_dir ? (
                          <button
                            className="button secondary"
                            style={{ padding: "6px 12px", fontSize: "13px" }}
                            onClick={() => handleStartPack(file)}
                            disabled={calculatingSize}
                          >
                            {calculatingSize ? "计算中..." : "打包下载"}
                          </button>
                        ) : (
                          <a
                            className="button secondary"
                            style={{ padding: "6px 12px", fontSize: "13px" }}
                            href={api.downloadFile(file.path)}
                            download
                          >
                            下载
                          </a>
                        )}
                        <button
                          className="button secondary"
                          style={{ padding: "6px 12px", fontSize: "13px" }}
                          onClick={() => startRename(file)}
                        >
                          重命名
                        </button>
                        <button
                          className="button secondary danger"
                          style={{ padding: "6px 12px", fontSize: "13px" }}
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

        {/* Pack Confirm Modal - Single Folder */}
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

        {/* Pack Confirm Modal - Multi Files */}
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
