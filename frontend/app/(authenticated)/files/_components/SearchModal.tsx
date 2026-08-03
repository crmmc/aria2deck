import type { RefObject } from "react";
import { ModalOverlay } from "@/components/ModalOverlay";
import { formatBytes } from "@/lib/utils";
import type { FileInfo } from "@/types";

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  const y = date.getFullYear();
  const m = date.getMonth() + 1;
  const d = date.getDate();
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${y}/${m}/${d} ${hh}:${mm}`;
}

type SearchModalProps = {
  searchKeyword: string;
  searchResults: FileInfo[];
  inputRef: RefObject<HTMLInputElement>;
  onInputChange: (value: string) => void;
  onResultClick: (file: FileInfo) => void;
  onClose: () => void;
};

export function SearchModal({
  searchKeyword,
  searchResults,
  inputRef,
  onInputChange,
  onResultClick,
  onClose,
}: SearchModalProps) {
  return (
    <ModalOverlay
      onClose={onClose}
      ariaLabel="搜索文件"
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
          <input
            ref={inputRef}
            type="text"
            className="search-modal-input"
            placeholder="搜索文件名..."
            value={searchKeyword}
            onChange={(e) => onInputChange(e.target.value)}
            aria-label="搜索文件名"
          />
          {searchKeyword && (
            <button
              type="button"
              className="search-modal-clear"
              onClick={() => onInputChange("")}
              aria-label="清除搜索"
            >
              ✕
            </button>
          )}
        </div>

        <div className="search-modal-results">
          {searchKeyword.trim() === "" ? (
            <div className="search-modal-hint">
              <p className="muted">输入关键词搜索文件</p>
              <p className="muted text-sm">按 ESC 关闭</p>
            </div>
          ) : searchResults.length === 0 ? (
            <div className="search-modal-hint">
              <p className="muted">未找到匹配的文件</p>
            </div>
          ) : (
            <div className="search-results-list">
              {searchResults.map((file) => (
                <button
                  type="button"
                  key={file.id}
                  className="search-result-item"
                  onClick={() => onResultClick(file)}
                >
                  <span className="file-icon">
                    {file.is_directory ? "📁" : "📄"}
                  </span>
                  <div className="search-result-info">
                    <span className="search-result-name">{file.name}</span>
                    <span className="search-result-meta">
                      {formatBytes(file.size)} · {formatDate(file.created_at)}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="search-modal-footer">
          <span className="muted text-sm">
            {searchResults.length > 0
              ? `找到 ${searchResults.length} 个文件`
              : "⌘F 打开搜索"}
          </span>
        </div>
    </ModalOverlay>
  );
}
