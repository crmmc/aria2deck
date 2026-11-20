import { act, renderHook } from "@testing-library/react";

import { useSelection } from "@/shared/hooks/useSelection";

describe("useSelection", () => {
  it("toggles an id on and off", () => {
    const { result } = renderHook(() => useSelection<number>());

    act(() => result.current.toggle(7));
    expect(result.current.selected.has(7)).toBe(true);
    expect(result.current.selectedCount).toBe(1);

    act(() => result.current.toggle(7));
    expect(result.current.selected.has(7)).toBe(false);
    expect(result.current.selectedCount).toBe(0);
  });

  it("sets an item explicitly regardless of previous state", () => {
    const { result } = renderHook(() => useSelection<number>());

    act(() => result.current.setItemSelected(3, true));
    expect(Array.from(result.current.selected)).toEqual([3]);

    act(() => result.current.setItemSelected(3, false));
    expect(result.current.selected.size).toBe(0);
  });

  it("treats an empty id list as nothing selected", () => {
    const { result } = renderHook(() => useSelection<number>());
    expect(result.current.areAllSelected([])).toBe(false);
    expect(result.current.isAllSelected).toBe(false);
  });

  it("derives isAllSelected from the visible ids", () => {
    const { result } = renderHook(() => useSelection<number>([1, 2]));

    expect(result.current.isAllSelected).toBe(false);
    act(() => result.current.selectAll());
    expect(result.current.isAllSelected).toBe(true);
    expect(Array.from(result.current.selected).sort()).toEqual([1, 2]);

    act(() => result.current.setItemSelected(2, false));
    expect(result.current.isAllSelected).toBe(false);
    expect(result.current.areAllSelected([1, 2])).toBe(false);
    expect(result.current.areAllSelected([1])).toBe(true);
  });

  it("toggleAll selects every id then clears when all are already selected", () => {
    const { result } = renderHook(() => useSelection<number>([4, 5]));

    act(() => result.current.toggleAll());
    expect(Array.from(result.current.selected).sort()).toEqual([4, 5]);

    act(() => result.current.toggleAll());
    expect(result.current.selected.size).toBe(0);
  });

  it("clear empties the selection", () => {
    const { result } = renderHook(() => useSelection<number>([1]));

    act(() => result.current.selectAll());
    expect(result.current.selectedCount).toBe(1);

    act(() => result.current.clear());
    expect(result.current.selectedCount).toBe(0);
  });
});
