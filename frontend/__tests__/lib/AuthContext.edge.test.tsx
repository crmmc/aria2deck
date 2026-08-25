import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import { AuthProvider, useAuth } from "@/lib/AuthContext";
import { api } from "@/lib/api";
import type { User } from "@/types";

jest.mock("@/lib/api", () => ({
  api: {
    me: jest.fn<Promise<User>, []>(),
    logout: jest.fn<Promise<{ ok: boolean }>, []>(),
    getSiteInfo: jest.fn<Promise<{ site_title: string }>, []>(),
  },
  authEvents: {
    onUnauthorized: jest.fn(() => () => {}),
    emit: jest.fn(),
  },
  ApiError: class MockApiError extends Error {
    constructor(
      message: string,
      public status: number,
      public isUnauthorized: boolean = false,
      public isNetworkError: boolean = false
    ) {
      super(message);
      this.name = "MockApiError";
    }
  },
}));

const { ApiError: MockApiError } = jest.requireMock("@/lib/api") as {
  ApiError: new (
    message: string,
    status: number,
    isUnauthorized?: boolean,
    isNetworkError?: boolean
  ) => Error & { isUnauthorized: boolean; isNetworkError: boolean };
};

const pushA = jest.fn();
const pushB = jest.fn();
let mockPush: ReturnType<typeof jest.fn> = pushA;
let mockPathname = "/tasks";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
  usePathname: () => mockPathname,
}));

function TestComponent() {
  const { user, loading, error, refreshUser } = useAuth();
  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="user">{user ? user.username : "null"}</div>
      <div data-testid="error">{error || "null"}</div>
      <button onClick={refreshUser}>Refresh</button>
    </div>
  );
}

describe("AuthProvider edge cases", () => {
  const mockApi = api as jest.Mocked<typeof api>;

  beforeEach(() => {
    jest.clearAllMocks();
    mockPush = pushA;
    mockPathname = "/tasks";
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "aria2 控制器" });
  });

  it("refreshUser clears user on unauthorized error", async () => {
    mockApi.me
      .mockResolvedValueOnce({ id: 1, username: "alice", is_admin: false, quota: 1024 })
      .mockRejectedValueOnce(new MockApiError("expired", 401, true));

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("alice");
    });

    act(() => {
      screen.getByText("Refresh").click();
    });

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("null");
    });
  });

  it("refreshUser keeps user when a non-unauthorized error occurs", async () => {
    mockApi.me
      .mockResolvedValueOnce({ id: 1, username: "alice", is_admin: false, quota: 1024 })
      .mockRejectedValueOnce(new MockApiError("boom", 500));

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("alice");
    });

    act(() => {
      screen.getByText("Refresh").click();
    });

    await waitFor(() => {
      expect(mockApi.me).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByTestId("user").textContent).toBe("alice");
  });

  it("does not re-initialize auth when the effect re-runs after initialization", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "alice", is_admin: false, quota: 1024 });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("alice");
    });

    // 换一个新的 push 引用使 initializeAuth effect 重新执行
    mockPush = pushB;
    act(() => {
      screen.getByText("Refresh").click();
    });

    await waitFor(() => {
      expect(mockApi.me).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByTestId("user").textContent).toBe("alice");
  });

  it("falls back to default site title and warns when getSiteInfo fails", async () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    mockApi.getSiteInfo.mockRejectedValue(new Error("site down"));
    mockApi.me.mockResolvedValue({ id: 1, username: "alice", is_admin: false, quota: 1024 });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("alice");
    });
    expect(warnSpy).toHaveBeenCalledWith("加载站点标题失败", expect.any(Error));

    warnSpy.mockRestore();
  });

  it("shows 未知错误 when api.me rejects with a non-ApiError", async () => {
    mockApi.me.mockRejectedValue(new Error("plain failure"));

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("error").textContent).toBe("未知错误");
      expect(screen.getByTestId("loading").textContent).toBe("false");
      expect(screen.getByTestId("user").textContent).toBe("null");
    });
    expect(pushA).not.toHaveBeenCalled();
  });
});
