import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { useAuth } from "@/shared/auth/hooks";
import { DEFAULT_SITE_TITLE, getSiteTitle, setSiteTitle } from "@/shared/lib/siteTitle";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
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
    setSiteTitle(DEFAULT_SITE_TITLE);
  });

  it("loads me on mount and exposes user", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(200, {
          site_title: "Aria2Deck",
          ws_reconnect_max_delay_seconds: 30,
          ws_reconnect_jitter_seconds: 1,
          ws_reconnect_factor: 2,
        });
      }
      return json(200, ME);
    });
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
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(200, {
          site_title: "Aria2Deck",
          ws_reconnect_max_delay_seconds: 30,
          ws_reconnect_jitter_seconds: 1,
          ws_reconnect_factor: 2,
        });
      }
      return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    });
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("user").textContent).toBe("none");
    });
  });

  it("keeps the user when refresh fails with a network error (G9)", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    });
    let auth: ReturnType<typeof useAuth> | null = null;
    function Refresher() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <Refresher />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());
    // 网络瞬断：refresh 失败但保留用户。
    mockFetch.mockImplementation(async () => {
      throw new TypeError("fetch failed");
    });
    await act(async () => {
      await auth!.refresh();
    });
    expect(auth!.user).not.toBeNull();
  });

  it("fetches site info once and falls back on failure (G1)", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(200, {
          site_title: "自定义站名",
          ws_reconnect_max_delay_seconds: 30,
          ws_reconnect_jitter_seconds: 1,
          ws_reconnect_factor: 2,
        });
      }
      return json(200, ME);
    });
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => expect(getSiteTitle()).toBe("自定义站名"));
    // 站点信息只拉取一次（后续请求不重复）。
    const calls = () =>
      mockFetch.mock.calls.filter(([url]) => url === "/api/v2/public/site-info").length;
    expect(calls()).toBe(1);
    await act(async () => {});
    expect(calls()).toBe(1);
  });

  it("keeps the default title on failure or blank site titles (G1)", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(200, ME);
    });
    const { unmount } = render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await act(async () => {});
    expect(getSiteTitle()).toBe(DEFAULT_SITE_TITLE); // 失败回退默认。

    // 卸载后迟到的响应不再写入（cancelled 守卫）。
    const holder: { resolve?: (value: unknown) => void } = {};
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return new Promise((resolve) => {
          holder.resolve = resolve;
        });
      }
      return json(200, ME);
    });
    const pending = render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    pending.unmount();
    holder.resolve?.(json(200, { site_title: "迟到标题" }));
    await act(async () => {});
    expect(getSiteTitle()).toBe(DEFAULT_SITE_TITLE);
    unmount();

    // 空白标题回退默认。
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(200, {
          site_title: "  ",
          ws_reconnect_max_delay_seconds: 30,
          ws_reconnect_jitter_seconds: 1,
          ws_reconnect_factor: 2,
        });
      }
      return json(200, ME);
    });
    document.title = "";
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await act(async () => {});
    expect(getSiteTitle()).toBe(DEFAULT_SITE_TITLE);
    expect(document.title).toBe("Aria2Deck");
  });

  it("login sets user and logout clears", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return json(200, ME);
    });
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
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return json(200, ME);
    });
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
