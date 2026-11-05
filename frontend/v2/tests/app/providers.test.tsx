import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { useAuth } from "@/app/hooks";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 100,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 100,
};

function Probe() {
  const { user, loading } = useAuth();
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="user">{user?.username ?? "none"}</span>
    </div>
  );
}

describe("AppProviders auth", () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it("loads me on mount and exposes user", async () => {
    mockFetch.mockResolvedValueOnce(json(200, ME));
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("admin");
    });
    expect(screen.getByTestId("loading").textContent).toBe("false");
  });

  it("clears user when me fails", async () => {
    mockFetch.mockResolvedValueOnce(json(401, { error: { code: "unauthenticated", message: "请先登录" } }));
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("none");
    });
  });

  it("login sets user and logout clears", async () => {
    mockFetch.mockResolvedValueOnce(json(200, ME));
    let auth: ReturnType<typeof useAuth> | null = null;
    function LoginProbe() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <LoginProbe />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());
    mockFetch.mockResolvedValueOnce(json(200, { ok: true }));
    await act(async () => {
      await auth!.logout();
    });
    expect(auth!.user).toBeNull();
    expect(mockFetch).toHaveBeenLastCalledWith(
      "/api/v2/auth/logout",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("401 from any client call clears the user (unified handler)", async () => {
    mockFetch.mockResolvedValueOnce(json(200, ME));
    let auth: ReturnType<typeof useAuth> | null = null;
    function Both() {
      auth = useAuth();
      return null;
    }
    render(
      <MemoryRouter>
        <AppProviders>
          <Both />
        </AppProviders>
      </MemoryRouter>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());
    mockFetch.mockResolvedValueOnce(
      json(401, { error: { code: "unauthenticated", message: "请先登录" } }),
    );
    const { api } = await import("@/shared/api/client");
    await expect(api.get("/api/v2/tasks")).rejects.toMatchObject({ status: 401 });
    await waitFor(() => expect(auth!.user).toBeNull());
  });
});

function json(status: number, body: unknown): unknown {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
  };
}
