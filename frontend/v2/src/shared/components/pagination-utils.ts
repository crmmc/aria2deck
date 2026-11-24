/** 分页控件的纯算法与容器测量（从 PaginationControls 拆分以满足 react-refresh 单导出约束）。 */

export const MIN_PAGE_CAPACITY = 7;
const PAGE_SLOT_PX = 36;

export type PageItem = number | "ellipsis";

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

  // centerWindow 已保证窗口落在 [firstInner, lastInner] 内且规模足够，
  // 因此这里只需把窗口贴到仍空缺的一侧，不存在“双侧同时可扩展”的情形。
  while (true) {
    const leftEllipsis = start > firstInner ? 1 : 0;
    const rightEllipsis = end < lastInner ? 1 : 0;
    const used = end - start + 1 + leftEllipsis + rightEllipsis;
    if (used >= innerSlots) break;

    const canLeft = start > firstInner;
    const canRight = end < lastInner;
    if (!canLeft && !canRight) break;
    if (canLeft) start -= 1;
    else end += 1;
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
