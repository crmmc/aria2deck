"use client";

import { useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { ModalOverlay } from "@/components/ModalOverlay";
import type { ShareLink } from "@/types";

interface CreateShareDialogProps {
  userFileId: number;
  fileName: string;
  onClose: () => void;
  onCreated?: (share: ShareLink) => void;
}

const EXPIRE_OPTIONS = [
  { label: "1 小时", value: 3600 },
  { label: "1 天", value: 86400 },
  { label: "7 天", value: 604800 },
  { label: "30 天", value: 2592000 },
  { label: "永久", value: 0 },
];

export default function CreateShareDialog({
  userFileId,
  fileName,
  onClose,
  onCreated,
}: CreateShareDialogProps) {
  const { showToast } = useToast();
  const [password, setPassword] = useState("");
  const [expiresIn, setExpiresIn] = useState(604800); // 默认7天
  const [maxDownloads, setMaxDownloads] = useState("");
  const [creating, setCreating] = useState(false);
  const [createdShare, setCreatedShare] = useState<ShareLink | null>(null);
  const handleCreate = async () => {
    setCreating(true);
    try {
      const share = await api.createShare({
        user_file_id: userFileId,
        password: password || undefined,
        expires_in: expiresIn || undefined,
        max_downloads: maxDownloads ? parseInt(maxDownloads, 10) : undefined,
      });
      setCreatedShare(share);
      onCreated?.(share);
      showToast("分享创建成功", "success");
    } catch (err) {
      showToast(`创建失败: ${(err as Error).message}`, "error");
    } finally {
      setCreating(false);
    }
  };
  const shareUrl = createdShare && typeof window !== "undefined"
    ? `${window.location.origin}/s/${createdShare.share_code}`
    : "";
  const copyLink = () => {
    navigator.clipboard.writeText(shareUrl).then(
      () => showToast("链接已复制", "success"),
      () => showToast("复制失败", "error"),
    );
  };

  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;

  return createPortal(
    <ModalOverlay
      onClose={onClose}
      contentClassName="modal-content max-w-400"
    >
        <div className="modal-header">
          <h3 className="modal-title">创建分享</h3>
          <button type="button" className="modal-close-btn" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <p className="text-sm muted mb-3">文件: {fileName}</p>
          {createdShare ? (
            <div>
              <div className="mb-3">
                <label className="label" htmlFor="share-link">分享链接</label>
                <div className="flex gap-2">
                  <input
                    id="share-link"
                    type="text"
                    className="input flex-1"
                    value={shareUrl}
                    readOnly
                  />
                  <button
                    type="button"
                    className="button primary"
                    onClick={copyLink}
                  >
                    复制
                  </button>
                </div>
              </div>
              {createdShare.has_password && (
                <p className="text-sm muted">密码: {password}</p>
              )}
              <button
                type="button"
                className="button secondary mt-3"
                style={{ width: "100%" }}
                onClick={onClose}
              >
                关闭
              </button>
            </div>
          ) : (
            <div>
              <div className="mb-3">
                <label className="label" htmlFor="share-expires">有效期</label>
                <select
                  id="share-expires"
                  className="select"
                  style={{ width: "100%" }}
                  value={expiresIn}
                  onChange={(e) => setExpiresIn(Number(e.target.value))}
                >
                  {EXPIRE_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="mb-3">
                <label className="label" htmlFor="share-password">密码（可选）</label>
                <input
                  id="share-password"
                  type="text"
                  className="input"
                  style={{ width: "100%" }}
                  placeholder="留空则无需密码"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  maxLength={100}
                />
              </div>
              <div className="mb-3">
                <label className="label" htmlFor="share-max-downloads">下载次数限制（可选）</label>
                <input
                  id="share-max-downloads"
                  type="number"
                  className="input"
                  style={{ width: "100%" }}
                  placeholder="留空则不限"
                  value={maxDownloads}
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === "") {
                      setMaxDownloads(val);
                      return;
                    }
                    const num = Number(val);
                    if (Number.isInteger(num) && num >= 1 && num <= 10000) {
                      setMaxDownloads(val);
                    }
                  }}
                  min={1}
                  max={10000}
                />
              </div>
              <button
                type="button"
                className={`button primary${creating ? " opacity-60" : ""}`}
                style={{ width: "100%" }}
                onClick={handleCreate}
                disabled={creating}
              >
                {creating ? "创建中..." : "创建分享"}
              </button>
            </div>
          )}
        </div>
    </ModalOverlay>,
    document.body
  );
}