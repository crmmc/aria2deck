import { Icon } from "@/shared/components/Icon";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { formatBytes } from "@/shared/lib/format";
import type { SearchHitView } from "../model";

type SearchModalProps = {
  keyword: string;
  results: SearchHitView[];
  loading: boolean;
  error: string | null;
  truncated: boolean;
  onLocate: (item: SearchHitView, trigger: Element) => void;
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
        <Icon name="search" className="search-modal-icon" />
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
                key={`${item.userFileId}-${item.entryPath ?? ""}-${item.name}`}
                className="search-result-item"
              >
                <span className="file-icon">
                  <Icon name={item.isDirectory ? "folder" : "file"} />
                </span>
                <div className="search-result-info">
                  <span className="search-result-name">{item.name}</span>
                  <span className="search-result-meta">{formatBytes(item.size)}</span>
                  <span className="search-result-path">{item.path}</span>
                </div>
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={(event) => onLocate(item, event.currentTarget)}
                >
                  定位
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </ModalOverlay>
  );
}
