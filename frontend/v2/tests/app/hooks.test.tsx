import { renderHook } from "@testing-library/react";

import { useAuth, useToast } from "@/app/hooks";

describe("context hooks guard", () => {
  it("useAuth throws outside provider", () => {
    expect(() => renderHook(() => useAuth())).toThrow("useAuth 必须在 AuthProvider 内使用");
  });

  it("useToast throws outside provider", () => {
    expect(() => renderHook(() => useToast())).toThrow("useToast 必须在 ToastProvider 内使用");
  });
});
