import { useCallback, useMemo, useState } from "react";

export function useSelection<TId>(visibleIds: readonly TId[] = []) {
  const [selected, setSelected] = useState<Set<TId>>(() => new Set());

  const areAllSelected = useCallback(
    (ids: readonly TId[] = visibleIds) => ids.length > 0 && ids.every((id) => selected.has(id)),
    [selected, visibleIds]
  );

  const isAllSelected = useMemo(
    () => areAllSelected(visibleIds),
    [areAllSelected, visibleIds]
  );

  const toggle = useCallback((id: TId) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const setItemSelected = useCallback((id: TId, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  }, []);

  const selectAll = useCallback((ids: readonly TId[] = visibleIds) => {
    setSelected(new Set(ids));
  }, [visibleIds]);

  const clear = useCallback(() => {
    setSelected(new Set());
  }, []);

  const toggleAll = useCallback((ids: readonly TId[] = visibleIds) => {
    setSelected(areAllSelected(ids) ? new Set() : new Set(ids));
  }, [areAllSelected, visibleIds]);

  return {
    selected,
    selectedCount: selected.size,
    isAllSelected,
    areAllSelected,
    setSelected,
    toggle,
    setItemSelected,
    selectAll,
    clear,
    toggleAll,
  };
}
