import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { LoginPage } from "@/features/auth/LoginPage";
import { DEFAULT_SITE_TITLE, setSiteTitle } from "@/shared/lib/siteTitle";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 1,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 1,
};

function siteInfo(): unknown {
  return {
    site_title: "Aria2Deck",
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 1,
    ws_reconnect_factor: 2,
  };
}

/** URL 路由 mock：站点信息固定成功，me/login 由用例覆盖。 */
function routeMocks(overrides: Record<string, unknown>): void {
  mockFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v2/public/site-info") return json(200, siteInfo());
    const override = overrides[url];
    if (override !== undefined) return override;
    return json(401, err());
  });
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <AppProviders>
        <LoginPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function passwordInput(): HTMLInputElement {
  return document.querySelector(".password-input-wrap .password-input") as HTMLInputElement;
}

function fillAndSubmit(username: string, password: string) {
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: username } });
  fireEvent.change(passwordInput(), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
}

describe("LoginPage", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setSiteTitle(DEFAULT_SITE_TITLE);
    document.title = "";
  });

  it("auto-focuses the username input", () => {
    mockFetch.mockResolvedValueOnce(json(401, err()));
    renderLogin();
    expect(screen.getByLabelText("用户名")).toHaveFocus();
  });

  it("toggles password visibility", () => {
    mockFetch.mockResolvedValueOnce(json(401, err()));
    renderLogin();
    const input = passwordInput();
    expect(input.type).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "显示密码" }));
    expect(input.type).toBe("text");
    expect(screen.getByRole("button", { name: "隐藏密码" })).toBeInTheDocument();
  });

  it("shows inline error on 401", async () => {
    routeMocks({
      "/api/v2/auth/login": json(401, err("用户名或密码错误")),
    });
    renderLogin();
    fillAndSubmit("admin", "wrong");
    await waitFor(() => {
      expect(document.querySelector(".alert-danger")).toHaveTextContent("用户名或密码无效");
    }, { timeout: 3000 });
  });

  it.each([
    ["account_frozen", "该用户已冻结，详情请联系管理员"],
    ["account_locked", "该用户已锁定，请联系管理员轮换新的密码"],
  ])("shows the dedicated message for %s instead of the generic 401 text", async (code, text) => {
    routeMocks({
      "/api/v2/auth/login": json(403, { error: { code, message: "后端原始文案" } }),
    });
    renderLogin();
    fillAndSubmit("admin", "whatever");
    await waitFor(() => {
      expect(document.querySelector(".alert-danger")).toHaveTextContent(text);
    }, { timeout: 3000 });
    expect(document.querySelector(".alert-danger")).not.toHaveTextContent("用户名或密码无效");
  });

  it("keeps the generic message for a plain 403 without a status code", async () => {
    routeMocks({
      "/api/v2/auth/login": json(403, { error: { code: "permission_denied", message: "没有权限" } }),
    });
    renderLogin();
    fillAndSubmit("admin", "whatever");
    await waitFor(() => {
      expect(document.querySelector(".alert-danger")).toHaveTextContent("没有权限");
    }, { timeout: 3000 });
  });

  it("shows network error inline", async () => {
    routeMocks({
      "/api/v2/auth/login": Promise.reject(new TypeError("fetch failed")),
    });
    renderLogin();
    fillAndSubmit("admin", "x");
    await waitFor(() => {
      expect(document.querySelector(".alert-danger")).toHaveTextContent(
        "网络连接失败，请检查地址或网络",
      );
    }, { timeout: 3000 });
  });

  it("shows the backend message for other server errors", async () => {
    routeMocks({
      "/api/v2/auth/login": json(500, { error: { code: "internal", message: "数据库不可用" } }),
    });
    renderLogin();
    fillAndSubmit("admin", "x");
    await waitFor(() => {
      expect(document.querySelector(".alert-danger")).toHaveTextContent("数据库不可用");
    }, { timeout: 3000 });
  });

  it("redirects when already logged in", async () => {
    routeMocks({
      "/api/v2/auth/me": json(200, ME),
    });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/tasks" element={<div>任务页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("任务页占位", undefined, { timeout: 3000 })).toBeInTheDocument();
  });

  it("successful login navigates to tasks", async () => {
    routeMocks({
      "/api/v2/auth/login": json(200, ME),
    });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/tasks" element={<div>登录后任务页</div>} />
            <Route path="/profile" element={<div>个人资料页</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    fillAndSubmit("admin", "right-password");
    expect(await screen.findByText("登录后任务页", undefined, { timeout: 3000 })).toBeInTheDocument();
  });

  it("redirects back to the origin page after login", async () => {
    routeMocks({
      "/api/v2/auth/login": json(200, ME),
    });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: "/files" } }]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/files" element={<div>回到文件页</div>} />
            <Route path="/tasks" element={<div>任务页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    fillAndSubmit("admin", "pw");
    expect(await screen.findByText("回到文件页")).toBeInTheDocument();
  });

  it("already-authenticated redirect honors state.from too", async () => {
    routeMocks({
      "/api/v2/auth/me": json(200, ME),
    });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: "/files" } }]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/files" element={<div>免登回文件页</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("免登回文件页")).toBeInTheDocument();
  });

  // ============ G6 初始密码登录引导 ============

  it("login with an initial password redirects to /profile", async () => {
    const initialUser = { ...ME, is_initial_password: true };
    routeMocks({
      "/api/v2/auth/login": json(200, initialUser),
    });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/profile" element={<div>改密引导页</div>} />
            <Route path="/tasks" element={<div>任务页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    fillAndSubmit("admin", "initial-password");
    expect(await screen.findByText("改密引导页", undefined, { timeout: 3000 })).toBeInTheDocument();
  });

  it("already-authenticated initial-password user is also guided to /profile", async () => {
    const initialUser = { ...ME, is_initial_password: true };
    routeMocks({
      "/api/v2/auth/me": json(200, initialUser),
    });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/profile" element={<div>免登改密页</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("免登改密页")).toBeInTheDocument();
  });

  // ============ G1 站点标题 ============

  it("renders the dynamic site title as the heading and document title", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(200, {
          site_title: "公司下载站",
          ws_reconnect_max_delay_seconds: 30,
          ws_reconnect_jitter_seconds: 1,
          ws_reconnect_factor: 2,
        });
      }
      return json(401, err());
    });
    renderLogin();
    expect(await screen.findByRole("heading", { name: "公司下载站" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("公司下载站"));
  });

  it("falls back to the default title when site info fails", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(401, err());
    });
    renderLogin();
    expect(await screen.findByRole("heading", { name: "Aria2Deck" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Aria2Deck"));
  });

  it("sends the PBKDF2 client hash on the wire (WebCrypto path)", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    const { webcrypto } = await import("node:crypto");
    Object.defineProperty(globalThis, "crypto", {
      value: webcrypto,
      configurable: true,
      writable: true,
    });
    try {
      mockFetch.mockResolvedValueOnce(json(401, err()));
      mockFetch.mockResolvedValueOnce(json(200, ME));
      renderLogin();
      fillAndSubmit("admin", "right-password");
      await waitFor(() => {
        const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/login");
        expect(call).toBeDefined();
        // 独立基准：pbkdf2_hmac('sha256', b'right-password', sha256(b'admin').hexdigest().encode(), 10000)
        expect(JSON.parse(call![1].body as string)).toEqual({
          username: "admin",
          password: "da5b623564816f0bbdf7a25143f9a91d7d302c88dd976e3d33c2f64b9a0c7a4c",
        });
      });
    } finally {
      if (descriptor !== undefined) {
        Object.defineProperty(globalThis, "crypto", descriptor);
      } else {
        delete (globalThis as { crypto?: unknown }).crypto;
      }
    }
  });

  it("sends the same wire hash via the pure JS fallback (no subtle)", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", {
      value: {}, // 非安全上下文：无 subtle，走纯 JS PBKDF2。
      configurable: true,
      writable: true,
    });
    try {
      mockFetch.mockResolvedValueOnce(json(401, err()));
      mockFetch.mockResolvedValueOnce(json(200, ME));
      renderLogin();
      fillAndSubmit("admin", "right-password");
      await waitFor(() => {
        const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/login");
        expect(call).toBeDefined();
        expect(JSON.parse(call![1].body as string).password).toBe(
          "da5b623564816f0bbdf7a25143f9a91d7d302c88dd976e3d33c2f64b9a0c7a4c",
        );
      });
    } finally {
      if (descriptor !== undefined) {
        Object.defineProperty(globalThis, "crypto", descriptor);
      } else {
        delete (globalThis as { crypto?: unknown }).crypto;
      }
    }
  });

  it("shows generic error when hashing itself fails", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", {
      value: { subtle: { digest: async () => Promise.reject(new Error("boom")) } },
      configurable: true,
      writable: true,
    });
    try {
      mockFetch.mockResolvedValueOnce(json(401, err()));
      renderLogin();
      fillAndSubmit("admin", "x");
      await waitFor(() => {
        expect(document.querySelector(".alert-danger")).toHaveTextContent("boom");
      });
    } finally {
      if (descriptor !== undefined) {
        Object.defineProperty(globalThis, "crypto", descriptor);
      } else {
        delete (globalThis as { crypto?: unknown }).crypto;
      }
    }
  });
});

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}
function err(message = "请先登录"): unknown {
  return { error: { code: "unauthenticated", message } };
}
