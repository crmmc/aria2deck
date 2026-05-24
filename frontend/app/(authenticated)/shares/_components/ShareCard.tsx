import { memo, useCallback } from "react";
import type { ShareLink } from "@/types";
import { formatBytes } from "@/lib/utils";
import { getEffectiveStatus } from "../shareState";

interface ShareCardProps {
  record: ShareLink;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyLink: (shareCode: string) => void;
  onRevoke: (id: number) => void;
  onDelete: (id: number) => void;
}

export const ShareCard = memo(function ShareCard({
  record,
  isSelected,
  onToggleSelection,
  onCopyLink,
  onRevoke,
  onDelete,
}: ShareCardProps) {
  const handleCardClick = useCallback(() => {
    onCopyLink(record.share_code);
  }, [record.share_code, onCopyLink]);

  const handleCheckboxChange = useCallback(() => {
    onToggleSelection(record.id);
  }, [record.id, onToggleSelection]);

  const handleCopyClick = useCallback(() => {
    onCopyLink(record.share_code);
  }, [record.share_code, onCopyLink]);

  const handleRevokeClick = useCallback(() => {
    onRevoke(record.id);
  }, [record.id, onRevoke]);

  const handleDeleteClick = useCallback(() => {
    onDelete(record.id);
  }, [record.id, onDelete]);

  const currentStatus = getEffectiveStatus(record);

  const statusText =
    currentStatus === "active"
      ? "活跃"
      : currentStatus === "expired"
        ? "已过期"
        : "已失效";

  const statusClass =
    currentStatus === "active"
      ? "task-status-complete"
      : currentStatus === "expired"
        ? "task-status-cancelled"
        : "task-status-error";

  return (
    <div className={`task-card-inner share-card-item${isSelected ? " selected" : ""}`}>
      <div className="share-card-main-row">
        <input
          type="checkbox"
          aria-label={`选择分享 ${record.file_name}`}
          checked={isSelected}
          onChange={handleCheckboxChange}
          className="checkbox-sm mt-2 cursor-pointer"
        />
        <button
          type="button"
          className="share-card-copy-area"
          onClick={handleCardClick}
          aria-label={`打开分享 ${record.file_name}`}
        >
          <span className="share-card-copy-header">
            <span className="overflow-hidden flex-1">
              <span className="task-name share-card-title" title={record.file_name}>
                {record.file_name} {record.has_password && <span className="muted text-sm ml-1" title="有密码">🔒</span>}
              </span>
              <span className="muted tabular-nums text-sm share-card-line">
                {formatBytes(record.file_size)}
              </span>
            </span>
            <span className={`task-status ${statusClass}`} style={{ marginLeft: "auto" }}>
              {statusText}
            </span>
          </span>
          <span className="text-sm mb-3 muted share-card-details">
            <span>提取码: {record.share_code}</span>
            <span>
              下载次数: {record.download_count}
              {record.max_downloads != null && record.max_downloads > 0 ? ` / ${record.max_downloads}` : ""}
            </span>
            {record.expires_at && (
              <span suppressHydrationWarning>
                过期时间: {new Date(record.expires_at).toLocaleString()}
              </span>
            )}
          </span>
        </button>
      </div>
      <div className="task-card-footer">
        <div className="task-footer-left">
          <span className="muted text-sm" suppressHydrationWarning>
            创建于 {new Date(record.created_at).toLocaleString()}
          </span>
        </div>
        <div className="task-footer-right">
          <button type="button" className="button secondary btn-sm" onClick={handleCopyClick} title="复制链接">
            复制链接
          </button>
          {currentStatus === "active" && (
            <button type="button" className="button secondary btn-sm" onClick={handleRevokeClick} title="失效">
              失效
            </button>
          )}
          <button type="button" className="button secondary danger btn-sm" onClick={handleDeleteClick} title="删除">
            删除
          </button>
        </div>
      </div>
    </div>
  );
});
