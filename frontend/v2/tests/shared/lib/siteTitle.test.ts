import { act, renderHook } from "@testing-library/react";

import {
  DEFAULT_SITE_TITLE,
  getSiteTitle,
  setSiteTitle,
  subscribeSiteTitle,
  useSiteTitle,
} from "@/shared/lib/siteTitle";

describe("siteTitle store（G1）", () => {
  afterEach(() => {
    setSiteTitle(DEFAULT_SITE_TITLE);
  });

  it("defaults to Aria2Deck", () => {
    expect(getSiteTitle()).toBe(DEFAULT_SITE_TITLE);
  });

  it("updates the value and notifies subscribers", () => {
    const listener = jest.fn();
    const unsubscribe = subscribeSiteTitle(listener);
    setSiteTitle("公司下载站");
    expect(getSiteTitle()).toBe("公司下载站");
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    setSiteTitle(DEFAULT_SITE_TITLE);
    expect(listener).toHaveBeenCalledTimes(1); // 退订后不再收到通知。
  });

  it("re-renders the hook on updates", () => {
    const { result } = renderHook(() => useSiteTitle());
    expect(result.current).toBe(DEFAULT_SITE_TITLE);
    act(() => setSiteTitle("团队站"));
    expect(result.current).toBe("团队站");
  });

  it("blank titles fall back to the default", () => {
    setSiteTitle("   ");
    expect(getSiteTitle()).toBe(DEFAULT_SITE_TITLE);
  });
});
