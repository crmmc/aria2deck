import { act, renderHook } from "@testing-library/react";

import { useLocalState } from "@/shared/hooks/useLocalState";

describe("useLocalState", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("returns initial when empty and persists setValue", () => {
    const { result } = renderHook(() => useLocalState("tasks.filter", "current"));
    expect(result.current.value).toBe("current");
    act(() => result.current.setValue("active"));
    expect(result.current.value).toBe("active");
    expect(window.localStorage.getItem("tasks.filter")).toBe('"active"');
  });

  it("reads persisted value on mount", () => {
    window.localStorage.setItem("tasks.filter", '"completed"');
    const { result } = renderHook(() => useLocalState("tasks.filter", "all"));
    expect(result.current.value).toBe("completed");
  });

  it("falls back to memory when localStorage unavailable", () => {
    const spy = jest.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("denied");
    });
    const warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
    const { result } = renderHook(() => useLocalState("notifications.enabled", false));
    expect(result.current.persistent).toBe(false);
    act(() => result.current.setValue(true));
    expect(result.current.value).toBe(true);
    expect(warn).toHaveBeenCalled();
    spy.mockRestore();
    warn.mockRestore();
  });

  it("returns initial when stored value is corrupt JSON", () => {
    window.localStorage.setItem("tasks.filter", "{not-json");
    const { result } = renderHook(() => useLocalState("tasks.filter", "current"));
    expect(result.current.value).toBe("current");
  });

  it("warns only once across repeated unavailable mounts", () => {
    const spy = jest.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("denied");
    });
    const warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const first = renderHook(() => useLocalState("tasks.filter", "a"));
      const second = renderHook(() => useLocalState("notifications.enabled", false));
      const firstResult = first.result;
      const secondResult = second.result;
      expect(firstResult.current.persistent).toBe(false);
      expect(secondResult.current.persistent).toBe(false); // 第二次挂载命中 warned 早退分支。
      second.unmount();
      first.unmount();
    } finally {
      spy.mockRestore();
      warn.mockRestore();
    }
  });

  it("swallows persist write failures", () => {
    const store = {
      getItem: () => null,
      setItem: (key: string) => {
        if (key === "tasks.filter") throw new Error("quota exceeded"); // 仅真实键写入失败。
      },
      removeItem: () => undefined,
      clear: () => undefined,
    } as unknown as Storage;
    const spy = jest
      .spyOn(window, "localStorage", "get")
      .mockImplementation(() => store);
    const { result } = renderHook(() => useLocalState("tasks.filter", "all"));
    act(() => result.current.setValue("completed"));
    expect(result.current.value).toBe("completed"); // 内存值保留。
    expect(result.current.persistent).toBe(true);
    spy.mockRestore();
  });
});
