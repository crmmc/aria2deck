"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import AuthLayout from "@/components/AuthLayout";
import type { FileInfo, QuotaResponse } from "@/types";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = bytes;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

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

  const loadFiles = async (path?: string) => {
    setLoading(true);
    setError(null);
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
  };

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

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <div className="space-between" style={{ marginBottom: 32 }}>
          <div>
            <h1 style={{ fontSize: "28px" }}>文件</h1>
            <p className="muted">管理您下载的文件</p>
          </div>
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
                    }}
                    onMouseEnter={(e) =>
                      (e.currentTarget.style.background = "rgba(0,0,0,0.02)")
                    }
                    onMouseLeave={(e) =>
                      (e.currentTarget.style.background = "transparent")
                    }
                  >
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
                        {!file.is_dir && (
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
      </div>
    </AuthLayout>
  );
}
