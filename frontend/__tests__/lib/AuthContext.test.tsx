import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import { AuthProvider, useAuth } from "@/lib/AuthContext";
import { api, authEvents, ApiError } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: {
    me: jest.fn(),
    logout: jest.fn(),
    getSiteInfo: jest.fn().mockResolvedValue({ site_title: 'Test Site' }),
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

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  usePathname: () => "/tasks",
}));

function TestComponent() {
  const { user, loading, error, logout, refreshUser, retryAuth } = useAuth();
  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="user">{user ? user.username : "null"}</div>
      <div data-testid="error">{error || "null"}</div>
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
  });

  it("renders children", async () => {
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false } as never);
    
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
    let resolveMe: (value: unknown) => void;
    mockApi.me.mockImplementation(() => new Promise((resolve) => {
      resolveMe = resolve;
    }) as never);
    
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
    mockApi.me.mockResolvedValue({ id: 1, username: "testuser", is_admin: false } as never);
    
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
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false } as never);
    mockApi.logout.mockResolvedValue({ ok: true } as never);
    
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

  it("refreshUser updates user state", async () => {
    mockApi.me
      .mockResolvedValueOnce({ id: 1, username: "user1", is_admin: false } as never)
      .mockResolvedValueOnce({ id: 1, username: "user2", is_admin: true } as never);
    
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
      .mockResolvedValueOnce({ id: 1, username: "recovered", is_admin: false } as never);
    
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
    mockApi.me.mockResolvedValue({ id: 1, username: "test", is_admin: false } as never);
    
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
});
