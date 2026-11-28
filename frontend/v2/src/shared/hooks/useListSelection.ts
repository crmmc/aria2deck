import { useCallback, useState, type KeyboardEvent } from "react";

import { isEditableTarget, rangeBetween } from "@/shared/lib/selection";
import { useSelection } from "./useSelection";

const SELECT_INDEX_ATTR = "data-select-index";
const ROW_CHECKBOX_SELECTOR = `input[type="checkbox"][${SELECT_INDEX_ATTR}]`;

/**
 * 在 useSelection 之上补列表级交互：锚点范围选择、Shift+方向键扩缩、Ctrl/Cmd+A、Escape。
 * 键盘处理挂在列表容器上，行复选框需带 data-select-index（= 该行在 visibleIds 中的下标）。
 */
export function useListSelection<TId>(visibleIds: readonly TId[]) {
  const {
    selected,
    setSelected,
    toggle: toggleItem,
    selectAll,
    toggleAll: toggleAllVisible,
    clear: clearSelected,
  } = useSelection<TId>(visibleIds);
  const [anchor, setAnchor] = useState<TId | null>(null);

  const selectRange = useCallback(
    (target: TId) => {
      const nextAnchor = anchor !== null && visibleIds.includes(anchor) ? anchor : target;
      setSelected(new Set(rangeBetween(visibleIds, nextAnchor, target)));
      setAnchor(nextAnchor);
    },
    [anchor, setSelected, visibleIds],
  );

  const toggle = useCallback(
    (id: TId, shiftKey = false) => {
      if (shiftKey) {
        selectRange(id);
        return;
      }
      toggleItem(id);
      setAnchor(id);
    },
    [selectRange, toggleItem],
  );

  const toggleAll = useCallback(() => {
    toggleAllVisible();
    setAnchor(null);
  }, [toggleAllVisible]);

  const clear = useCallback(() => {
    clearSelected();
    setAnchor(null);
  }, [clearSelected]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (isEditableTarget(event.target)) return;

      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "a") {
        event.preventDefault();
        selectAll(visibleIds);
        return;
      }
      if (event.key === "Escape") {
        clear();
        return;
      }
      if (!event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;

      const checkbox = (event.target as Element | null)?.closest<HTMLInputElement>(ROW_CHECKBOX_SELECTOR);
      if (!checkbox) return;
      // 先拦默认行为再判边界：列表首尾按 Shift+方向键也不滚动页面。
      event.preventDefault();
      const index = Number(checkbox.dataset.selectIndex);
      const nextIndex = index + (event.key === "ArrowDown" ? 1 : -1);
      if (nextIndex < 0 || nextIndex >= visibleIds.length) return;

      const nextAnchor = anchor !== null && visibleIds.includes(anchor) ? anchor : visibleIds[index];
      setSelected(new Set(rangeBetween(visibleIds, nextAnchor, visibleIds[nextIndex])));
      setAnchor(nextAnchor);
      event.currentTarget
        .querySelector<HTMLElement>(`input[type="checkbox"][${SELECT_INDEX_ATTR}="${nextIndex}"]`)
        ?.focus();
    },
    [anchor, clear, selectAll, setSelected, visibleIds],
  );

  return { selected, setSelected, toggle, toggleAll, clear, handleKeyDown };
}
