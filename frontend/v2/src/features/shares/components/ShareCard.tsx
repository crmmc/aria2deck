import { memo, useCallback } from "react";

import { formatBytes } from "@/shared/lib/format";
import { Icon } from "@/shared/components/Icon";
import { getEffectiveStatus, type ShareRecordView } from "../model";

interface ShareCardProps {
  record: ShareRecordView;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyLink: (record: ShareRecordView) => void;
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
  const handleCardClick = useCallback(() => onCopyLink(record), [record, onCopyLink]);
  const handleCheckboxChange = useCallback(() => onToggleSelection(record.id), [record.id, onToggleSelection]);
  const handleCopyClick = useCallback(() => onCopyLink(record), [record, onCopyLink]);
  const handleRevokeClick = useCallback(() => onRevoke(record.id), [record.id, onRevoke]);
  const handleDeleteClick = useCallback(() => onDelete(record.id), [record.id, onDelete]);

  const currentStatus = getEffectiveStatus(record);
  const statusText = currentStatus === "active" ? "活跃" : currentStatus === "expired" ? "已过期" : "已失效";
  const statusClass = currentStatus === "active" ? "task-status-complete" : currentStatus === "expired" ? "task-status-cancelled" : "task-status-error";

  return (
    <article className={`task-card-inner record-row share-card-item${isSelected ? " selected" : ""}`}>
      <input
        type="checkbox"
        aria-label={`选择分享 ${record.fileName}`}
        checked={isSelected}
        onChange={handleCheckboxChange}
        className="checkbox-sm cursor-pointer"
      />
      <button
        type="button"
        className="share-card-copy-area task-main"
        onClick={handleCardClick}
        aria-label={`打开分享 ${record.fileName}`}
      >
        <span className="file-mark"><Icon name="file" /></span>
        <span className="task-name">
          <strong className="share-card-title" title={record.fileName}>{record.fileName}</strong>
          <span className="task-meta muted tabular-nums text-sm share-card-details">
            <span>{formatBytes(record.fileSize)}</span>
            <span className="share-code" title={record.hasPassword ? "有密码" : undefined}>
              <Icon name={record.hasPassword ? "lock" : "link"} className="icon-sm" />
              {record.shareCode}
            </span>
            <span>
              下载 {record.downloadCount}{record.maxDownloads != null && record.maxDownloads > 0 ? ` / ${record.maxDownloads}` : ""} 次
            </span>
            <span suppressHydrationWarning>创建于 {new Date(record.createdAtMs).toLocaleString()}</span>
          </span>
        </span>
      </button>
      <span className={`task-status status-badge ${statusClass}`}>
        <Icon name={currentStatus === "active" ? "check" : currentStatus === "expired" ? "clock" : "x"} className="icon-sm" />
        {statusText}
      </span>
      <span className="record-time" suppressHydrationWarning>
        {record.expiresAtMs !== null ? `至 ${new Date(record.expiresAtMs).toLocaleString()}` : "永久有效"}
      </span>
      <div className="task-footer-right record-actions">
        <button type="button" className="icon-btn" onClick={handleCopyClick} title="复制链接" aria-label="复制链接">
          <Icon name="copy" className="icon-sm" />
        </button>
        {currentStatus === "active" && (
          <button type="button" className="icon-btn" onClick={handleRevokeClick} title="失效" aria-label="失效">
            <Icon name="x" className="icon-sm" />
          </button>
        )}
        <button type="button" className="icon-btn danger" onClick={handleDeleteClick} title="删除" aria-label="删除">
          <Icon name="trash" className="icon-sm" />
        </button>
      </div>
    </article>
  );
});
