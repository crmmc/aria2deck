import { act, renderHook } from "@testing-library/react";

import { useIsMobile } from "@/shared/hooks/useIsMobile";

function setViewportWidth(width: number) {
  Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
}

describe("useIsMobile", () => {
  const originalWidth = window.innerWidth;

  afterEach(() => {
    setViewportWidth(originalWidth);
  });

  it("uses the 768px default breakpoint", () => {
    setViewportWidth(767);
    const narrow = renderHook(() => useIsMobile());
    expect(narrow.result.current).toBe(true);
    narrow.unmount();

    setViewportWidth(768);
    const wide = renderHook(() => useIsMobile());
    expect(wide.result.current).toBe(false);
  });

  it("respects a custom breakpoint", () => {
    setViewportWidth(900);
    const { result } = renderHook(() => useIsMobile(1024));
    expect(result.current).toBe(true);
  });

  it("re-evaluates on window resize", () => {
    setViewportWidth(1200);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);

    act(() => {
      setViewportWidth(600);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current).toBe(true);

    act(() => {
      setViewportWidth(1000);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current).toBe(false);
  });

  it("stops listening after unmount", () => {
    setViewportWidth(1200);
    const removeEventListener = jest.spyOn(window, "removeEventListener");
    const { unmount } = renderHook(() => useIsMobile());
    unmount();
    expect(removeEventListener).toHaveBeenCalledWith("resize", expect.any(Function));
    removeEventListener.mockRestore();
  });
});
