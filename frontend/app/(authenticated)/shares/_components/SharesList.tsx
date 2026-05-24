import type { ShareLink } from "@/types";
import { ShareCard } from "./ShareCard";

type SharesListProps = {
  loading: boolean;
  filteredRecords: ShareLink[];
  selectedIds: Set<number>;
  onToggleSelection: (id: number) => void;
  onCopyLink: (shareCode: string) => void;
  onRevoke: (id: number) => void;
  onDelete: (id: number) => void;
};

export function SharesList({
  loading,
  filteredRecords,
  selectedIds,
  onToggleSelection,
  onCopyLink,
  onRevoke,
  onDelete,
}: SharesListProps) {
  if (loading) {
    return (
      <div className="empty-state">
        <p className="muted">加载中...</p>
      </div>
    );
  }

  if (filteredRecords.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-icon">
          <svg
            width="48"
            height="48"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="18" cy="5" r="3" />
            <circle cx="6" cy="12" r="3" />
            <circle cx="18" cy="19" r="3" />
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
            <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
          </svg>
        </div>
        <p className="font-medium mb-1">暂无分享记录</p>
        <p className="muted text-base">你创建的文件分享链接将显示在这里</p>
      </div>
    );
  }

  return (
    <div className="card task-card-container">
      {filteredRecords.map((record) => (
        <ShareCard
          key={record.id}
          record={record}
          isSelected={selectedIds.has(record.id)}
          onToggleSelection={onToggleSelection}
          onCopyLink={onCopyLink}
          onRevoke={onRevoke}
          onDelete={onDelete}
        />
      ))}
    </div>
  );
}
