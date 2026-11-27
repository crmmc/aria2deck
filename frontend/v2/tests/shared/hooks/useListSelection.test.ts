import { act, renderHook } from "@testing-library/react";

import { useListSelection } from "@/shared/hooks/useListSelection";

const IDS = [1, 2, 3, 4];

describe("useListSelection", () => {
  it("普通切换单项，并把该项记为锚点", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggle(2));
    expect(Array.from(result.current.selected)).toEqual([2]);

    act(() => result.current.toggle(4, true));
    expect(Array.from(result.current.selected).sort()).toEqual([2, 3, 4]);
  });

  it("Shift 范围替换此前选择，范围外项目不保留", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggle(1));
    act(() => result.current.toggle(4));
    expect(result.current.selected.size).toBe(2);

    act(() => result.current.toggle(2, true));
    expect(Array.from(result.current.selected).sort()).toEqual([2, 3, 4]);
  });

  it("没有锚点时 Shift 只选中目标项并建立锚点", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggle(3, true));
    expect(Array.from(result.current.selected)).toEqual([3]);

    act(() => result.current.toggle(1, true));
    expect(Array.from(result.current.selected).sort()).toEqual([1, 2, 3]);
  });

  it("锚点在列表中失效后 Shift 退化为单选", () => {
    const { result, rerender } = renderHook(
      ({ ids }: { ids: number[] }) => useListSelection(ids),
      { initialProps: { ids: IDS } },
    );

    act(() => result.current.toggle(2));
    rerender({ ids: [1, 3, 4] });

    act(() => result.current.toggle(4, true));
    expect(Array.from(result.current.selected)).toEqual([4]);
  });

  it("排序变化后按新的可见顺序计算范围", () => {
    const { result, rerender } = renderHook(
      ({ ids }: { ids: number[] }) => useListSelection(ids),
      { initialProps: { ids: IDS } },
    );

    act(() => result.current.toggle(1));
    rerender({ ids: [4, 3, 2, 1] });

    act(() => result.current.toggle(3, true));
    expect(Array.from(result.current.selected).sort()).toEqual([1, 2, 3]);
  });

  it("clear 同时清空选择与锚点", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggle(2));
    act(() => result.current.clear());
    expect(result.current.selected.size).toBe(0);

    act(() => result.current.toggle(4, true));
    expect(Array.from(result.current.selected)).toEqual([4]);
  });

  it("toggleAll 沿用全选/取消全选语义", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggleAll());
    expect(Array.from(result.current.selected).sort()).toEqual(IDS);

    act(() => result.current.toggleAll());
    expect(result.current.selected.size).toBe(0);
  });

  it("toggleAll 同时重置锚点，取消全选后 Shift 只选目标", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggle(2));
    act(() => result.current.toggleAll());
    expect(result.current.selected.size).toBe(IDS.length);

    act(() => result.current.toggleAll());
    expect(result.current.selected.size).toBe(0);

    act(() => result.current.toggle(4, true));
    expect(Array.from(result.current.selected)).toEqual([4]);
  });

  it("目标不在可见列表内时 Shift 不产生不可见选择", () => {
    const { result } = renderHook(() => useListSelection(IDS));

    act(() => result.current.toggle(99, true));
    expect(result.current.selected.size).toBe(0);
  });
});
