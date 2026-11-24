"use client";

import { useLayoutEffect, useRef, useState } from "react";
import {
  buildPageItems,
  measurePageCapacity,
  MIN_PAGE_CAPACITY,
} from "@/shared/components/pagination-utils";

type PaginationControlsProps = {
  currentPage: number;
  pageSize: number;
  totalFiles: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
};

export function PaginationControls({
  currentPage,
  pageSize,
  totalFiles,
  onPageChange,
  onPageSizeChange,
}: PaginationControlsProps) {
  const totalPages = Math.max(1, Math.ceil(totalFiles / pageSize));
  const containerRef = useRef<HTMLDivElement>(null);
  const [capacity, setCapacity] = useState(MIN_PAGE_CAPACITY);
  const pageItems = buildPageItems(currentPage, totalPages, capacity);
  const [jumpValue, setJumpValue] = useState("");

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    if (typeof ResizeObserver !== "function") {
      setCapacity(MIN_PAGE_CAPACITY);
      return;
    }

    const update = () => {
      setCapacity(measurePageCapacity(container));
    };

    const observer = new ResizeObserver(update);
    observer.observe(container);
    update();
    return () => observer.disconnect();
  }, [onPageSizeChange, totalFiles]);

  const submitJump = (raw: string) => {
    const page = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(page)) return;
    const clamped = Math.min(Math.max(Math.trunc(page), 1), totalPages);
    if (clamped !== currentPage) onPageChange(clamped);
    setJumpValue("");
  };

  return (
    <div ref={containerRef} className="pagination-controls flex items-center justify-end gap-2">
      {onPageSizeChange && (
        <select
          className="select-sm"
          value={pageSize}
          aria-label="每页条数"
          onChange={(event) => {
            onPageSizeChange(Number(event.target.value));
            onPageChange(1);
          }}
        >
          {[10, 20, 30, 50, 100].map((size) => (
            <option key={size} value={size}>{size} 条/页</option>
          ))}
        </select>
      )}
      <span className="text-sm muted" style={{ marginLeft: 4 }}>
        共 {totalFiles} 项
      </span>
      <div className="flex items-center gap-0" data-pagination-pages="true" style={{ marginLeft: 8 }}>
        <button
          type="button"
          className="button secondary btn-sm"
          data-pagination-arrow="true"
          style={{ borderRadius: "4px 0 0 4px" }}
          disabled={currentPage <= 1}
          aria-label="首页"
          onClick={() => onPageChange(1)}
        >
          ‹‹
        </button>
        <button
          type="button"
          className="button secondary btn-sm"
          data-pagination-arrow="true"
          style={{ borderRadius: 0 }}
          disabled={currentPage <= 1}
          aria-label="上一页"
          onClick={() => onPageChange(currentPage - 1)}
        >
          ‹
        </button>
        {pageItems.map((item, index) =>
          item === "ellipsis" ? (
            <span
              key={`ellipsis-${index}`}
              aria-hidden="true"
              className="muted"
              style={{
                background: "none",
                border: "none",
                minWidth: 24,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                pointerEvents: "none",
                userSelect: "none",
              }}
            >
              …
            </span>
          ) : (
            <button
              type="button"
              key={item}
              className={`button btn-sm ${item === currentPage ? "primary" : "secondary"}`}
              style={{ borderRadius: 0, minWidth: 32 }}
              onClick={() => {
                if (item !== currentPage) onPageChange(item);
              }}
            >
              {item}
            </button>
          ),
        )}
        <button
          type="button"
          className="button secondary btn-sm"
          data-pagination-arrow="true"
          style={{ borderRadius: 0 }}
          disabled={currentPage >= totalPages}
          aria-label="下一页"
          onClick={() => onPageChange(currentPage + 1)}
        >
          ›
        </button>
        <button
          type="button"
          className="button secondary btn-sm"
          data-pagination-arrow="true"
          style={{ borderRadius: "0 4px 4px 0" }}
          disabled={currentPage >= totalPages}
          aria-label="末页"
          onClick={() => onPageChange(totalPages)}
        >
          ››
        </button>
      </div>
      <input
        type="number"
        className="jump-page-input"
        aria-label="跳至第几页"
        value={jumpValue}
        onChange={(event) => setJumpValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") submitJump(event.currentTarget.value);
        }}
        onBlur={(event) => submitJump(event.target.value)}
      />
    </div>
  );
}
