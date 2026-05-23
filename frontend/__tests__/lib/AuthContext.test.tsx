import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import { AuthProvider, useAuth } from "@/lib/AuthContext";
import { api, authEvents, ApiError } from "@/lib/api";
import type { User } from "@/types";

jest.mock("@/lib/api", () => ({
  api: {
    me: jest.fn<Promise<User>, []>(),
    logout: jest.fn<Promise<{ ok: boolean }>, []>(),
    getSiteInfo: jest.fn<Promise<{ site_title: string }>, []>().mockResolvedValue({ site_title: 'Test Site' }),
  },
  authEvents: {
    listeners: new Set<() => void>(),
    onUnauthorized: jest.fn((callback: () => void) => {
      const listeners = (jest.requireMock("@/lib/api").authEvents as typeof authEvents).listeners;
      listeners.add(callback);
      return () => listeners.delete(callback);
    }),
    emit: jest.fn(() => {
      const listeners = (jest.requireMock("@/lib/api").authEvents as typeof authEvents).listeners;
      listeners.forEach((cb: () => void) => cb());
    }),
  },
  ApiError: class ApiError extends Error {
    constructor(
      message: string,
      public status: number,
      public isUnauthorized: boolean = false,
      public isNetworkError: boolean = false
    ) {
      super(message);
      this.name = "ApiError";
    }
  },
}));

const mockRouter = {
  push: jest.fn(),
  replace: jest.fn(),
  prefetch: jest.fn(),
  back: jest.fn(),
};

let mockPathname = "/tasks";

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  usePathname: () => mockPathname,
}));

function TestComponent() {
  const { user, loading, error, logout, refreshUser, retryAuth, siteTitle } = useAuth();
  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="user">{user ? user.username : "null"}</div>
      <div data-testid="error">{error || "null"}</div>
      <div data-testid="site-title">{siteTitle}</div>
      <button onClick={logout}>Logout</button>
      <button onClick={refreshUser}>Refresh</button>
      <button onClick={retryAuth}>Retry</button>
    </div>
  );
}

describe("useAuth", () => {
  it("throws error when used outside AuthProvider", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    
    function BadComponent() {
      useAuth();
      return null;
    }
    
    expect(() => render(<BadComponent />)).toThrow(
      "useAuth 必须在 AuthProvider 内使用"
    );
    
    consoleError.mockRestore();
  });
});

describe("AuthProvider", () => {
  const mockApi = api as jest.Mocked<typeof api>;
  const mockAuthEvents = authEvents as jest.Mocked<typeof authEvents>;

  beforeEach(() => {
    jest.clearAllMocks();
    mockRouter.push.mockClear();
    mockAuthEvents.listeners.clear();
    mockPathname = "/tasks";
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Test Site" });
  });

  it("renders children", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false });

    render(
      <AuthProvider>
        <div data-testid="child">Child</div>
        <TestComponent />
      </AuthProvider>
    );

    expect(screen.getByTestId("child")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });
  });

  it("shows loading state initially", async () => {
    let resolveMe: (value: User) => void;
    mockApi.me.mockImplementation(() => new Promise((resolve) => {
      resolveMe = resolve;
    }));
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    expect(screen.getByTestId("loading").textContent).toBe("true");
    
    await act(async () => {
      resolveMe!({ id: 1, username: "test", is_admin: false });
    });
  });

  it("sets user on successful api.me()", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "testuser", is_admin: false });
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
      expect(screen.getByTestId("user").textContent).toBe("testuser");
    });
  });

  it("redirects to login on 401 error", async () => {
    const { ApiError: MockApiError } = jest.requireMock("@/lib/api");
    mockApi.me.mockRejectedValue(new MockApiError("Unauthorized", 401, true, false));
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
      expect(screen.getByTestId("user").textContent).toBe("null");
    });
    
    await waitFor(() => {
      expect(mockRouter.push).toHaveBeenCalledWith("/login");
    });
  });

  it("does not redirect public share pages on initial 401", async () => {
    const { ApiError: MockApiError } = jest.requireMock("@/lib/api");
    mockPathname = "/s/public-code";
    mockApi.me.mockRejectedValue(new MockApiError("Unauthorized", 401, true, false));

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
      expect(screen.getByTestId("user").textContent).toBe("null");
    });

    expect(mockRouter.push).not.toHaveBeenCalledWith("/login");
  });

  it("starts loading site info while auth check is still pending", async () => {
    let resolveMe: (value: User) => void;
    mockApi.me.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMe = resolve;
        })
    );
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Fast Site" });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(mockApi.getSiteInfo).toHaveBeenCalled();
    });
    expect(screen.getByTestId("loading").textContent).toBe("true");

    await act(async () => {
      resolveMe!({ id: 1, username: "test", is_admin: false });
    });

    await waitFor(() => {
      expect(screen.getByTestId("site-title").textContent).toBe("Fast Site");
      expect(document.title).toBe("Fast Site");
    });
  });

  it("shows error on network error", async () => {
    const { ApiError: MockApiError } = jest.requireMock("@/lib/api");
    mockApi.me.mockRejectedValue(new MockApiError("Network error", 0, false, true));
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
      expect(screen.getByTestId("error").textContent).toBe("无法连接服务器，请检查网络连接");
    });
  });

  it("shows error on server error", async () => {
    const { ApiError: MockApiError } = jest.requireMock("@/lib/api");
    mockApi.me.mockRejectedValue(new MockApiError("Internal Server Error", 500, false, false));
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
      expect(screen.getByTestId("error").textContent).toContain("服务器错误");
    });
  });

  it("logout calls api.logout and redirects", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false });
    mockApi.logout.mockResolvedValue({ ok: true });
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("test");
    });
    
    act(() => {
      screen.getByText("Logout").click();
    });

    await waitFor(() => {
      expect(mockApi.logout).toHaveBeenCalled();
      expect(mockRouter.push).toHaveBeenCalledWith("/login");
    });
  });

  it("logout logs error and still redirects when api.logout fails", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false });
    mockApi.logout.mockRejectedValue(new Error("logout failed"));
    const consoleSpy = jest.spyOn(console, "error").mockImplementation(() => {});

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("test");
    });

    act(() => {
      screen.getByText("Logout").click();
    });

    await waitFor(() => {
      expect(mockApi.logout).toHaveBeenCalled();
      expect(consoleSpy).toHaveBeenCalledWith(
        "退出登录请求失败，已执行本地登出",
        expect.any(Error),
      );
      expect(mockRouter.push).toHaveBeenCalledWith("/login");
    });

    consoleSpy.mockRestore();
  });

  it("refreshUser updates user state", async () => {
    mockApi.me
      .mockResolvedValueOnce({ id: 1, username: "user1", is_admin: false })
      .mockResolvedValueOnce({ id: 1, username: "user2", is_admin: true });
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("user1");
    });
    
    act(() => {
      screen.getByText("Refresh").click();
    });
    
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("user2");
    });
  });

  it("retryAuth resets error and triggers re-fetch", async () => {
    const { ApiError: MockApiError } = jest.requireMock("@/lib/api");
    mockApi.me
      .mockRejectedValueOnce(new MockApiError("Network error", 0, false, true))
      .mockResolvedValueOnce({ id: 1, username: "recovered", is_admin: false });
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );
    
    await waitFor(() => {
      expect(screen.getByTestId("error").textContent).toBe("无法连接服务器，请检查网络连接");
    });
    
    act(() => {
      screen.getByText("Retry").click();
    });
    
    await waitFor(() => {
      expect(screen.getByTestId("error").textContent).toBe("null");
      expect(screen.getByTestId("user").textContent).toBe("recovered");
    });
  });

  it("authEvents.onUnauthorized triggers redirect", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("test");
    });

    act(() => {
      mockAuthEvents.emit();
    });
    
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("null");
      expect(mockRouter.push).toHaveBeenCalledWith("/login");
    });
  });

  it("keeps custom site title after route change", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false });
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "My Custom Title" });

    const { rerender } = render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(document.title).toBe("My Custom Title");
    });

    act(() => {
      document.title = "aria2 控制器";
      mockPathname = "/history";
    });

    rerender(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(document.title).toBe("My Custom Title");
    });
  });
});
