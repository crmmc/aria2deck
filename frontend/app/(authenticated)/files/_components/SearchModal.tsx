import { ModalOverlay } from "@/components/ModalOverlay";
import { formatBytes } from "@/lib/utils";
import type { FileSearchItem } from "@/types";

type SearchModalProps = {
  keyword: string;
  results: FileSearchItem[];
  loading: boolean;
  error: string | null;
  truncated: boolean;
  onLocate: (item: FileSearchItem) => void;
  onClose: () => void;
};

export function SearchModal({
  keyword,
  results,
  loading,
  error,
  truncated,
  onLocate,
  onClose,
}: SearchModalProps) {
  return (
    <ModalOverlay
      onClose={onClose}
      ariaLabel="搜索结果"
      contentClassName="search-modal"
    >
      <div className="search-modal-header">
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="search-modal-icon"
        >
          <circle cx="11" cy="11" r="8" />
          <path d="m21 21-4.35-4.35" />
        </svg>
        <span className="search-modal-input">{`搜索 “${keyword}”`}</span>
      </div>

      <div className="search-modal-results">
        {loading ? (
          <div className="search-modal-hint">
            <p className="muted">搜索中...</p>
          </div>
        ) : error ? (
          <div className="search-modal-hint">
            <p className="text-danger">{error}</p>
          </div>
        ) : results.length === 0 ? (
          <div className="search-modal-hint">
            <p className="muted">未找到匹配的文件</p>
          </div>
        ) : (
          <div className="search-results-list">
            {truncated && (
              <p className="search-truncated-hint">
                匹配结果过多，仅显示前 {results.length} 项，请缩小关键词
              </p>
            )}
            {results.map((item) => (
              <div
                key={`${item.user_file_id}-${item.entry_path ?? ""}-${item.name}`}
                className="search-result-item"
              >
                <span className="file-icon">
                  {item.is_directory ? "📁" : "📄"}
                </span>
                <div className="search-result-info">
                  <span className="search-result-name">{item.name}</span>
                  <span className="search-result-meta">{formatBytes(item.size)}</span>
                  <span className="search-result-path">{item.path}</span>
                </div>
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={() => onLocate(item)}
                >
                  定位
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="search-modal-footer">
        <span className="muted text-sm">
          {loading ? "搜索中..." : `找到 ${results.length} 个匹配`}
        </span>
        <span className="muted text-sm">按 ESC 关闭</span>
      </div>
    </ModalOverlay>
  );
}
