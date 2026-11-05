import { act, renderHook } from "@testing-library/react";

import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";

describe("useDebouncedValue", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it("returns the initial value immediately and updates after delay", () => {
    const { result, rerender } = renderHook(
      ({ value }) => useDebouncedValue(value, 300),
      { initialProps: { value: "a" } },
    );
    expect(result.current).toBe("a");
    rerender({ value: "b" });
    expect(result.current).toBe("a"); // 延迟窗口内保持旧值。
    act(() => jest.advanceTimersByTime(300));
    expect(result.current).toBe("b");
  });

  it("cancels pending updates on rapid changes (only last wins)", () => {
    const { result, rerender } = renderHook(
      ({ value }) => useDebouncedValue(value, 100),
      { initialProps: { value: 1 } },
    );
    rerender({ value: 2 });
    act(() => jest.advanceTimersByTime(60));
    rerender({ value: 3 });
    act(() => jest.advanceTimersByTime(60)); // 距离上次变更仅 60ms，未到期。
    expect(result.current).toBe(1);
    act(() => jest.advanceTimersByTime(40));
    expect(result.current).toBe(3);
  });

  it("unmount clears the pending timer without state updates", () => {
    const { result, rerender, unmount } = renderHook(
      ({ value }) => useDebouncedValue(value, 100),
      { initialProps: { value: "x" } },
    );
    rerender({ value: "y" });
    unmount();
    act(() => jest.advanceTimersByTime(200));
    expect(result.current).toBe("x");
  });
});
