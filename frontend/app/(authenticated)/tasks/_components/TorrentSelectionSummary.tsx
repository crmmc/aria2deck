import { formatBytes } from "@/lib/utils";

type TorrentSelectionSummaryProps = {
  selectedCount: number;
  selectedSize: number;
  totalCount: number;
};

export function TorrentSelectionSummary({
  selectedCount,
  selectedSize,
  totalCount,
}: TorrentSelectionSummaryProps) {
  return (
    <div className="torrent-summary-grid">
      <div className="torrent-summary-card">
        <span>已选文件</span>
        <strong>
          {selectedCount} / {totalCount}
        </strong>
      </div>
      <div className="torrent-summary-card">
        <span>已选大小</span>
        <strong>{formatBytes(selectedSize)}</strong>
      </div>
      <div className="torrent-summary-card">
        <span>文件总数</span>
        <strong>{totalCount}</strong>
      </div>
    </div>
  );
}
