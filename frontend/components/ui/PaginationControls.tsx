"use client";

import { useLayoutEffect, useRef, useState } from "react";

type PaginationControlsProps = {
  currentPage: number;
  pageSize: number;
  totalFiles: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
};

type PageItem = number | "ellipsis";

export const MIN_PAGE_CAPACITY = 7;
const PAGE_SLOT_PX = 36;

function normalizeCapacity(capacity: number): number {
  if (!Number.isFinite(capacity)) return MIN_PAGE_CAPACITY;
  return Math.max(MIN_PAGE_CAPACITY, Math.floor(capacity));
}

function centerWindow(current: number, size: number, min: number, max: number): [number, number] {
  let start = current - Math.floor((size - 1) / 2);
  let end = start + size - 1;
  if (start < min) {
    end += min - start;
    start = min;
  }
  if (end > max) {
    start -= end - max;
    end = max;
  }
  return [Math.max(min, start), Math.min(max, end)];
}

export function buildPageItems(currentPage: number, totalPages: number, capacity: number): PageItem[] {
  const cap = normalizeCapacity(capacity);
  if (totalPages <= cap) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }

  const firstInner = 2;
  const lastInner = totalPages - 1;
  const innerSlots = cap - 2;
  const windowSize = Math.max(1, innerSlots - 2);
  let [start, end] = centerWindow(currentPage, windowSize, firstInner, lastInner);

  while (true) {
    const leftEllipsis = start > firstInner ? 1 : 0;
    const rightEllipsis = end < lastInner ? 1 : 0;
    const used = end - start + 1 + leftEllipsis + rightEllipsis;
    if (used >= innerSlots) break;

    const canLeft = start > firstInner;
    const canRight = end < lastInner;
    if (!canLeft && !canRight) break;

    if (canLeft && canRight) {
      const leftDist = currentPage - start;
      const rightDist = end - currentPage;
      if (leftDist <= rightDist) start -= 1;
      else end += 1;
    } else if (canLeft) {
      start -= 1;
    } else {
      end += 1;
    }
  }

  if (start === firstInner + 1) start = firstInner;
  if (end === lastInner - 1) end = lastInner;

  const items: PageItem[] = [1];
  if (start > firstInner) items.push("ellipsis");
  for (let page = start; page <= end; page += 1) items.push(page);
  if (end < lastInner) items.push("ellipsis");
  items.push(totalPages);
  return items;
}

function parsePx(value: string): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function outerWidth(el: HTMLElement): number {
  const cs = getComputedStyle(el);
  return el.offsetWidth + parsePx(cs.marginLeft) + parsePx(cs.marginRight);
}

export function measurePageCapacity(container: HTMLElement): number {
  const cs = getComputedStyle(container);
  const padX = parsePx(cs.paddingLeft) + parsePx(cs.paddingRight);
  const gap = parsePx(cs.columnGap || cs.gap);
  const children = Array.from(container.children) as HTMLElement[];
  const gapTotal = children.length > 1 ? gap * (children.length - 1) : 0;

  let fixed = padX + gapTotal;
  for (const child of children) {
    if (child.dataset.paginationPages === "true") {
      child.querySelectorAll<HTMLElement>("[data-pagination-arrow='true']").forEach((arrow) => {
        fixed += arrow.offsetWidth;
      });
      const childCs = getComputedStyle(child);
      fixed += parsePx(childCs.marginLeft) + parsePx(childCs.marginRight);
    } else {
      fixed += outerWidth(child);
    }
  }

  const available = container.clientWidth - fixed;
  return Math.max(MIN_PAGE_CAPACITY, Math.floor(available / PAGE_SLOT_PX));
}

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
