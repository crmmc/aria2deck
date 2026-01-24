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
    <div className="modal-overlay" onClick={onCancel}>
      <div className="pack-modal-content" onClick={(e) => e.stopPropagation()}>
        <h2 className="pack-modal-title">
          {isMultiFile ? "确认打包文件" : "确认打包文件夹"}
        </h2>

        <div className="mb-4">
          {isMultiFile ? (
            <p className="pack-info-item">
              <strong>选中文件:</strong> {fileCount} 个
            </p>
          ) : (
            <p className="pack-info-item">
              <strong>文件夹:</strong> {folderName}
            </p>
          )}
          <p className="pack-info-item">
            <strong>大小:</strong> {formatBytes(folderSize)}
          </p>
          <p className="pack-info-item">
            <strong>可用空间:</strong> {formatBytes(availableSpace)}
          </p>
        </div>

        <div className="mb-4">
          <label className="form-label font-medium" style={{ color: "#1a1a1a" }}>
            输出文件名
          </label>
          <input
            type="text"
            value={outputName}
            onChange={(e) => setOutputName(e.target.value)}
            placeholder={defaultName}
            className="input"
            style={{ color: "#1a1a1a", background: "#ffffff" }}
            disabled={loading}
          />
          <p className="text-xs mt-1" style={{ color: "#666666" }}>
            扩展名将根据系统配置自动添加
          </p>
        </div>

        <div className="pack-warning-box">
          <p className="pack-warning-text"><strong>警告:</strong></p>
          <ul className="pack-warning-list">
            <li>打包过程中将冻结 {formatBytes(folderSize)} 的空间</li>
            <li>打包成功后源文件将被<strong>删除</strong></li>
            <li>只保留压缩包文件</li>
          </ul>
        </div>

        {!canPack && (
          <div className="pack-error-box">
            <p className="pack-error-text">可用空间不足，无法执行此操作。</p>
          </div>
        )}

        <div className="flex gap-3 flex-end">
          <button
            className="button secondary btn-text-dark"
            onClick={onCancel}
            disabled={loading}
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
