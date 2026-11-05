import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { LoginPage } from "@/features/auth/LoginPage";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
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
  return document.querySelector(".password-row input") as HTMLInputElement;
}

function fillAndSubmit(username: string, password: string) {
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: username } });
  fireEvent.change(passwordInput(), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
}

describe("LoginPage", () => {
  beforeEach(() => {
    mockFetch.mockReset();
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
    mockFetch.mockResolvedValueOnce(json(401, err()));
    mockFetch.mockResolvedValueOnce(json(401, err("用户名或密码错误")));
    renderLogin();
    fillAndSubmit("admin", "wrong");
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("用户名或密码错误");
    });
  });

  it("shows network error inline", async () => {
    mockFetch.mockResolvedValueOnce(json(401, err())); // me 探测失败
    mockFetch.mockRejectedValueOnce(new TypeError("fetch failed")); // 登录网络失败
    renderLogin();
    fillAndSubmit("admin", "x");
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("网络异常");
    });
  });

  it("redirects when already logged in", async () => {
    mockFetch.mockResolvedValueOnce(json(200, ME));
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
    expect(await screen.findByText("任务页占位")).toBeInTheDocument();
  });

  it("successful login navigates to tasks", async () => {
    mockFetch.mockResolvedValueOnce(json(401, err())); // me 未登录
    mockFetch.mockResolvedValueOnce(json(200, ME)); // 登录成功
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AppProviders>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/tasks" element={<div>登录后任务页</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    fillAndSubmit("admin", "right-password");
    expect(await screen.findByText("登录后任务页")).toBeInTheDocument();
  });

  it("redirects back to the origin page after login", async () => {
    mockFetch.mockResolvedValueOnce(json(401, err()));
    mockFetch.mockResolvedValueOnce(json(200, ME));
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
    mockFetch.mockResolvedValueOnce(json(200, ME));
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

  it("uses WebCrypto digest when subtle is available", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    const digest = jest.fn(async () => new Uint8Array([0xab, 0xcd]).buffer);
    Object.defineProperty(globalThis, "crypto", {
      value: { subtle: { digest } },
      configurable: true,
      writable: true,
    });
    // jsdom 不带 TextEncoder，用 Node 的补齐（仅本用例）。
    const encoderDescriptor = Object.getOwnPropertyDescriptor(globalThis, "TextEncoder");
    const { TextEncoder: NodeTextEncoder } = await import("node:util");
    Object.defineProperty(globalThis, "TextEncoder", {
      value: NodeTextEncoder,
      configurable: true,
      writable: true,
    });
    try {
      mockFetch.mockResolvedValueOnce(json(401, err()));
      mockFetch.mockResolvedValueOnce(json(200, ME));
      render(
        <MemoryRouter initialEntries={["/login"]}>
          <AppProviders>
            <LoginPage />
          </AppProviders>
        </MemoryRouter>,
      );
      fillAndSubmit("admin", "whatever");
      await waitFor(() => {
        const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/login");
        expect(call).toBeDefined();
        expect(JSON.parse(call![1].body as string).password).toBe("abcd");
      });
      expect(digest).toHaveBeenCalledTimes(1); // 参数为跨 realm 的 Uint8Array，仅验证调用。
    } finally {
      if (descriptor !== undefined) {
        Object.defineProperty(globalThis, "crypto", descriptor);
      } else {
        delete (globalThis as { crypto?: unknown }).crypto;
      }
      if (encoderDescriptor !== undefined) {
        Object.defineProperty(globalThis, "TextEncoder", encoderDescriptor);
      } else {
        delete (globalThis as { TextEncoder?: unknown }).TextEncoder;
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
      expect(await screen.findByRole("alert")).toHaveTextContent("登录失败，请重试");
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
