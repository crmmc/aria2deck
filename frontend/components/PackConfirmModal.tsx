"use client";

import { useState, useEffect } from "react";
import { formatBytes } from "@/lib/utils";

interface PackConfirmModalProps {
  folderName: string;
  folderSize: number;
  availableSpace: number;
  onConfirm: (outputName: string) => void;
  onCancel: () => void;
  loading?: boolean;
  isMultiFile?: boolean;
  fileCount?: number;
}

export default function PackConfirmModal({
  folderName,
  folderSize,
  availableSpace,
  onConfirm,
  onCancel,
  loading = false,
  isMultiFile = false,
  fileCount = 1,
}: PackConfirmModalProps) {
  const canPack = folderSize <= availableSpace;
  const defaultName = isMultiFile ? "archive" : folderName.replace(/\.[^/.]+$/, "");
  const [outputName, setOutputName] = useState(defaultName);

  useEffect(() => {
    setOutputName(defaultName);
  }, [defaultName]);

  const handleConfirm = () => {
    const name = outputName.trim() || defaultName;
    onConfirm(name);
  };

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
      onClick={onCancel}
    >
      <div
        style={{
          width: 480,
          maxWidth: "90vw",
          padding: 24,
          background: "#ffffff",
          color: "#1a1a1a",
          borderRadius: 12,
          boxShadow: "0 8px 32px rgba(0,0,0,0.2)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 style={{ marginBottom: 16, color: "#1a1a1a" }}>
          {isMultiFile ? "确认打包文件" : "确认打包文件夹"}
        </h2>

        <div style={{ marginBottom: 16 }}>
          {isMultiFile ? (
            <p style={{ marginBottom: 8, color: "#1a1a1a" }}>
              <strong>选中文件:</strong> {fileCount} 个
            </p>
          ) : (
            <p style={{ marginBottom: 8, color: "#1a1a1a" }}>
              <strong>文件夹:</strong> {folderName}
            </p>
          )}
          <p style={{ marginBottom: 8, color: "#1a1a1a" }}>
            <strong>大小:</strong> {formatBytes(folderSize)}
          </p>
          <p style={{ marginBottom: 8, color: "#1a1a1a" }}>
            <strong>可用空间:</strong> {formatBytes(availableSpace)}
          </p>
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ display: "block", marginBottom: 8, fontWeight: 500, color: "#1a1a1a" }}>
            输出文件名
          </label>
          <input
            type="text"
            value={outputName}
            onChange={(e) => setOutputName(e.target.value)}
            placeholder={defaultName}
            style={{
              width: "100%",
              padding: "8px 12px",
              border: "1px solid #ccc",
              borderRadius: 6,
              fontSize: 14,
              color: "#1a1a1a",
              background: "#ffffff",
            }}
            disabled={loading}
          />
          <p style={{ fontSize: 12, marginTop: 4, color: "#666666" }}>
            扩展名将根据系统配置自动添加
          </p>
        </div>

        <div
          style={{
            padding: 12,
            background: "#fff8e6",
            border: "1px solid #ffcc00",
            borderRadius: 8,
            marginBottom: 16,
          }}
        >
          <p style={{ margin: 0, fontSize: 13, color: "#996600" }}>
            <strong>警告:</strong>
          </p>
          <ul style={{ margin: "8px 0 0 16px", fontSize: 13, color: "#996600" }}>
            <li>
              打包过程中将冻结 {formatBytes(folderSize)} 的空间
            </li>
            <li>
              打包成功后源文件将被<strong>删除</strong>
            </li>
            <li>只保留压缩包文件</li>
          </ul>
        </div>

        {!canPack && (
          <div
            style={{
              padding: 12,
              background: "#ffebee",
              border: "1px solid #ff3b30",
              borderRadius: 8,
              marginBottom: 16,
            }}
          >
            <p style={{ margin: 0, fontSize: 13, color: "#cc0000" }}>
              可用空间不足，无法执行此操作。
            </p>
          </div>
        )}

        <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
          <button
            className="button secondary"
            onClick={onCancel}
            disabled={loading}
            style={{ color: "#1a1a1a" }}
          >
            取消
          </button>
          <button
            className="button"
            onClick={handleConfirm}
            disabled={loading || !canPack || !outputName.trim()}
          >
            {loading ? "创建中..." : "确认打包"}
          </button>
        </div>
      </div>
    </div>
  );
}
