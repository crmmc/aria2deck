import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppLayout } from "@/app/layouts/AppLayout";
import { emitToast } from "@/shared/components/toast-context";
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

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function statusEnvelope(status: "ok" | "degraded", message = ""): unknown {
  return { download_backend: { status, message } };
}

function siteInfo(title = "Aria2Deck"): unknown {
  return {
    site_title: title,
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 1,
    ws_reconnect_factor: 2,
  };
}

const defaultHandler = (url: string): unknown => {
  if (url === "/api/v2/auth/me") return ME;
  if (url === "/api/v2/public/site-info") return siteInfo();
  return statusEnvelope("ok");
};

function setup(handler: (url: string) => unknown = defaultHandler) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string) => {
    const result = handler(url);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { then?: unknown }).then === "function"
    ) {
      return result;
    }
    return json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/tasks"]}>
      <AppProviders>
        <Routes>
          <Route path="/tasks" element={<AppLayout />} />
          <Route path="/profile" element={<div>个人资料页占位</div>} />
          <Route path="/settings" element={<div>系统设置占位</div>} />
        </Routes>
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("AppLayout（V1 应用壳）", () => {
  beforeEach(() => {
    setSiteTitle(DEFAULT_SITE_TITLE);
  });

  it("renders V1 sidebar nav entries with admin items", async () => {
    setup();
    await waitFor(() => expect(document.querySelector(".sidebar-nav")).toBeTruthy());
    // V1 桌面侧栏：展开后显示文字（折叠态仅图标）。
    fireEvent.mouseEnter(document.querySelector(".sidebar")!);
    const sidebarNav = document.querySelector(".sidebar-nav");
    expect(sidebarNav?.textContent).toContain("任务");
    expect(sidebarNav?.textContent).toContain("任务历史");
    expect(sidebarNav?.textContent).toContain("文件");
    expect(sidebarNav?.textContent).toContain("分享");
    expect(sidebarNav?.textContent).toContain("设置");
    expect(sidebarNav?.textContent).toContain("多用户管理");
    expect(sidebarNav?.textContent).toContain("存储管理");
    expect(sidebarNav?.textContent).toContain("系统设置");
    // 退出登录按钮
    expect(document.querySelector(".logout-btn")).toBeTruthy();
  });

  it("shows site title in sidebar header only when expanded", async () => {
    setup((url) => (url === "/api/v2/public/site-info" ? siteInfo("团队下载站") : defaultHandler(url)));
    await waitFor(() => expect(document.querySelector(".sidebar")).toBeTruthy());
    // 默认折叠：不渲染标题文本。
    expect(document.querySelector(".sidebar-title")).toBeNull();
    fireEvent.mouseEnter(document.querySelector(".sidebar")!);
    await waitFor(() =>
      expect(document.querySelector(".sidebar-title")?.textContent).toBe("团队下载站"),
    );
    fireEvent.mouseLeave(document.querySelector(".sidebar")!);
    await waitFor(() => expect(document.querySelector(".sidebar-title")).toBeNull());
  });

  it("omits admin entries for regular users", async () => {
    setup((url) =>
      url === "/api/v2/auth/me" ? { ...ME, is_admin: false } : defaultHandler(url),
    );
    await waitFor(() => expect(document.querySelector(".sidebar-nav")).toBeTruthy());
    fireEvent.mouseEnter(document.querySelector(".sidebar")!);
    const nav = document.querySelector(".sidebar-nav");
    expect(nav?.textContent).not.toContain("多用户管理");
    expect(nav?.textContent).not.toContain("系统设置");
  });

  it("renders mobile bottom nav with more entry", async () => {
    setup();
    await waitFor(() => expect(document.querySelector(".bottom-nav")).toBeTruthy());
    const bottom = document.querySelector(".bottom-nav");
    expect(bottom?.textContent).toContain("任务");
    expect(bottom?.textContent).toContain("更多");
  });

  it("shows loading skeleton while initializing", () => {
    mockFetch.mockReset();
    let release: (value: unknown) => void = () => undefined;
    mockFetch.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <AppLayout />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(document.querySelector(".skeleton-header")).toBeTruthy();
    release(json(200, ME));
  });

  it("shows connection-failure screen with retry on network error", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      throw new TypeError("fetch failed");
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <AppLayout />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("连接失败")).toBeInTheDocument();
    expect(screen.getByText("无法连接服务器，请检查网络连接")).toBeInTheDocument();

    // 重试成功后进入应用壳。
    mockFetch.mockImplementation(async (url: string) => json(200, defaultHandler(url)));
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(document.querySelector(".sidebar-nav")).toBeTruthy());
  });

  it("shows server error message on 5xx", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(500, { error: { code: "internal", message: "数据库不可用" } });
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <AppLayout />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("服务器错误: 数据库不可用")).toBeInTheDocument();
  });

  it("shows the initial-password warning banner and navigates to profile", async () => {
    setup((url) =>
      url === "/api/v2/auth/me" ? { ...ME, is_initial_password: true } : defaultHandler(url),
    );
    expect(await screen.findByText(/正在使用默认密码/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "前往修改" }));
    expect(await screen.findByText("个人资料页占位")).toBeInTheDocument();
  });

  it("安全提醒常驻排在临时提示之上", async () => {
    setup((url) =>
      url === "/api/v2/auth/me" ? { ...ME, is_initial_password: true } : defaultHandler(url),
    );
    expect(await screen.findByText(/正在使用默认密码/)).toBeInTheDocument();

    act(() => {
      emitToast("批量操作失败", "error");
    });

    const items = Array.from(document.querySelectorAll<HTMLElement>(".notification-stack-item"));
    const bannerItem = items.find((item) => item.querySelector(".password-warning-banner"));
    const toastItem = items.find((item) => item.querySelector(".toast-item"));
    expect(bannerItem).toBeDefined();
    expect(toastItem).toBeDefined();
    expect(Number(bannerItem?.style.order)).toBe(0);
    expect(Number(toastItem?.style.order)).toBeGreaterThan(0);
  });

  it("dismisses the initial-password banner via close button", async () => {
    setup((url) =>
      url === "/api/v2/auth/me" ? { ...ME, is_initial_password: true } : defaultHandler(url),
    );
    expect(await screen.findByText(/正在使用默认密码/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭安全提醒" }));
    await waitFor(() =>
      expect(screen.queryByText(/正在使用默认密码/)).not.toBeInTheDocument(),
    );
  });

  it("shows the degraded backend banner with message and dismiss control", async () => {
    setup((url) => {
      if (url === "/api/v2/system/status") return statusEnvelope("degraded", "下载后端连接异常");
      return defaultHandler(url);
    });
    expect(await screen.findByText("下载后端连接异常")).toBeInTheDocument();
    expect(screen.getByText("下载后端异常")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭服务状态提醒" }));
    await waitFor(() =>
      expect(screen.queryByText("下载后端连接异常")).not.toBeInTheDocument(),
    );
  });

  it("admin degraded banner navigates to settings", async () => {
    setup((url) => {
      if (url === "/api/v2/system/status") return statusEnvelope("degraded", "下载后端连接异常");
      return defaultHandler(url);
    });
    expect(await screen.findByText("下载后端连接异常")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看系统设置" }));
    expect(await screen.findByText("系统设置占位")).toBeInTheDocument();
  });

  it("logout button in sidebar footer logs out", async () => {
    setup();
    await waitFor(() => expect(document.querySelector(".logout-btn")).toBeTruthy());
    fireEvent.click(document.querySelector(".logout-btn")!);
    await waitFor(() => {
      const calls = mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/logout");
      expect(calls).toHaveLength(1);
    });
  });
});
