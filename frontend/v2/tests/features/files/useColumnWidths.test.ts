import { act, renderHook } from "@testing-library/react";

import { sanitizeWidths, useColumnWidths } from "@/features/files/components/useColumnWidths";

describe("sanitizeWidths", () => {
  it("非对象输入回退默认", () => {
    expect(sanitizeWidths(null)).toEqual({ type: 84, size: 92, date: 146, ops: 122 });
    expect(sanitizeWidths("bad")).toEqual({ type: 84, size: 92, date: 146, ops: 122 });
  });

  it("非数字与非法值逐项回退", () => {
    expect(sanitizeWidths({ type: "wide", size: Number.NaN, date: 120, ops: -10 })).toEqual({
      type: 84,
      size: 92,
      date: 120,
      ops: 100,
    });
  });
});

describe("useColumnWidths 守卫", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("未开始拖拽时 updateDrag 不生效", () => {
    const { result } = renderHook(() => useColumnWidths());
    act(() => result.current.updateDrag("type", 300));
    expect(result.current.widths.type).toBe(84);
  });

  it("未开始拖拽时 endDrag 不写存储", () => {
    const { result } = renderHook(() => useColumnWidths());
    act(() => result.current.endDrag());
    expect(window.localStorage.getItem("files.column_widths")).toBeNull();
  });

  it("拖拽结束后持久化并清除瞬态", () => {
    const { result } = renderHook(() => useColumnWidths());
    act(() => result.current.beginDrag());
    act(() => result.current.updateDrag("type", 120));
    expect(result.current.widths.type).toBe(120);
    act(() => result.current.endDrag());
    expect(result.current.widths.type).toBe(120);
    expect(JSON.parse(window.localStorage.getItem("files.column_widths")!).type).toBe(120);
  });

  it("resetColumn 只复位目标列", () => {
    const { result } = renderHook(() => useColumnWidths());
    act(() => result.current.beginDrag());
    act(() => result.current.updateDrag("type", 120));
    act(() => result.current.endDrag());
    act(() => result.current.resetColumn("type"));
    expect(result.current.widths.type).toBe(84);
  });
});
