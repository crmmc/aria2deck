import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppLayout } from "@/app/layouts/AppLayout";
import { emitTaskActivity } from "@/shared/lib/taskEvents";
import { DEFAULT_SITE_TITLE, setSiteTitle } from "@/shared/lib/siteTitle";
import type { TaskStatsResponse, UserResponse } from "@/shared/api/types";

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

function stats(overrides: Partial<TaskStatsResponse> = {}): TaskStatsResponse {
  return {
    counts: {
      queued: 0, active: 1, waiting: 0, paused: 0,
      completed: 0, failed: 0, cancelled: 0, expired: 0,
    },
    total: 1,
    download_speed: 4096,
    upload_speed: 1024,
    quota: { quota_bytes: 200, used_bytes: 80, reserved_bytes: 20, available_bytes: 100, limited: false },
    ...overrides,
  };
}

function setup(handler: (url: string) => unknown) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string) => json(200, handler(url)));
  return render(
    <MemoryRouter initialEntries={["/tasks"]}>
      <AppProviders>
        <AppLayout />
      </AppProviders>
    </MemoryRouter>,
  );
}

const defaultHandler = (url: string): unknown => {
  if (url === "/api/v2/auth/me") return ME;
  if (url === "/api/v2/tasks/stats") return stats();
  if (url === "/api/v2/public/site-info") return siteInfo();
  return statusEnvelope("ok");
};

describe("AppLayout", () => {
  // client 的网络健康信号与站点标题是模块级状态：每个用例前复位，避免跨用例泄漏。
  beforeEach(async () => {
    setSiteTitle(DEFAULT_SITE_TITLE);
    mockFetch.mockReset();
    mockFetch.mockResolvedValue(json(200, {}) as never);
    const { api } = await import("@/shared/api/client");
    await api.get("/api/v2/__health_probe__");
  });
  it("renders nav entries, username, site title and stats summary", async () => {
    setup((url) => {
      if (url === "/api/v2/public/site-info") return siteInfo("团队下载站");
      return defaultHandler(url);
    });
    const nav = await screen.findByLabelText("主导航");
    expect(nav.textContent).toContain("任务");
    expect(nav.textContent).toContain("历史");
    expect(nav.textContent).toContain("文件");
    expect(nav.textContent).toContain("分享");
    await screen.findByText("admin");
    // G1 侧栏品牌使用站点标题。
    expect(document.querySelector(".app-brand-title")?.textContent).toBe("团队下载站");
    // G11 统计：速度/活跃/可用空间/冻结。
    const statsBox = await screen.findByLabelText("任务统计");
    expect(statsBox.textContent).toContain("↓ 4.00 KiB/s");
    expect(statsBox.textContent).toContain("↑ 1.00 KiB/s");
    expect(statsBox.textContent).toContain("活跃任务：1");
    expect(statsBox.textContent).toContain("可用空间：100 B / 200 B");
    expect(statsBox.textContent).toContain("已冻结 20 B（下载中）");
    expect(screen.getByLabelText("空间用量")).toHaveAttribute("aria-valuenow", "40");
  });

  it("falls back to the default brand when site info fails", async () => {
    setup((url) => {
      if (url === "/api/v2/public/site-info") {
        return { status: 500, ok: false, json: () => Promise.resolve({}) };
      }
      return defaultHandler(url);
    });
    await screen.findByLabelText("主导航");
    await waitFor(() =>
      expect(document.querySelector(".app-brand-title")?.textContent).toBe("Aria2Deck"),
    );
  });

  it("shows the degraded banner when backend reports degraded", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats();
      if (url === "/api/v2/public/site-info") return siteInfo();
      return statusEnvelope("degraded", "下载后端连接异常");
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("下载后端连接异常");
  });

  it("renders zero quota without divide-by-zero progress", async () => {
    setup((url) => {
      if (url === "/api/v2/tasks/stats") {
        return stats({
          quota: { quota_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
        });
      }
      return defaultHandler(url);
    });
    await screen.findByLabelText("任务统计");
    expect(screen.getByLabelText("空间用量")).toHaveAttribute("aria-valuenow", "0");
  });

  // ============ G4 初始密码横幅 ============

  it("shows the initial-password warning banner and navigates to profile", async () => {
    const initialUser = { ...ME, is_initial_password: true };
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, initialUser);
      return json(200, defaultHandler(url));
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <Routes>
            <Route path="/tasks" element={<AppLayout />} />
            <Route path="/profile" element={<div>个人资料页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("正在使用初始密码");
    fireEvent.click(screen.getByRole("button", { name: "前往修改" }));
    expect(await screen.findByText("个人资料页占位")).toBeInTheDocument();
  });

  it("hides the initial-password banner after a successful password change refresh", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return { ...ME, is_initial_password: true };
      return defaultHandler(url);
    });
    expect(await screen.findByText(/正在使用初始密码/)).toBeInTheDocument();
    // 网络瞬断触发连接横幅，其重试按钮驱动 refresh；恢复后 me 已改密 → 两条横幅都消失。
    const { api } = await import("@/shared/api/client");
    mockFetch.mockImplementation(async () => {
      throw new TypeError("fetch failed");
    });
    await expect(api.get("/api/v2/__probe__")).rejects.toMatchObject({ status: 0 });
    expect(await screen.findByText(/连接失败/)).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") {
        return json(200, { ...ME, is_initial_password: false });
      }
      return json(200, defaultHandler(url));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.queryByText(/正在使用初始密码/)).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/连接失败/)).not.toBeInTheDocument();
  });

  // ============ G9 连接失败横幅 ============

  it("shows the connection banner on network errors and recovers via retry", async () => {
    setup((url) => defaultHandler(url));
    await screen.findByText("admin");
    expect(screen.queryByText(/连接失败/)).not.toBeInTheDocument();

    // 任意请求网络异常（经 client 真实路径）→ 连接失败横幅，且不触发登出。
    const { api } = await import("@/shared/api/client");
    mockFetch.mockImplementation(async () => {
      throw new TypeError("fetch failed");
    });
    await expect(api.get("/api/v2/tasks/stats")).rejects.toMatchObject({ status: 0 });
    expect(await screen.findByText(/连接失败，请检查网络后重试/)).toBeInTheDocument();
    expect(screen.getByText("admin")).toBeInTheDocument(); // 未被登出。

    // 网络恢复：重试触发 refresh 成功 → 横幅消失。
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(200, defaultHandler(url));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.queryByText(/连接失败/)).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("admin")).toBeInTheDocument();
  });

  // ============ G11 统计轮询与活动触发 ============

  it("polls stats every 5 seconds while visible", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => defaultHandler(url));
      // 等待初始挂载（含 auth/site/stats 首轮）。
      await screen.findByLabelText("任务统计", {}, {});
      const callsAfterMount = () =>
        mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/stats").length;
      const initial = callsAfterMount();
      act(() => jest.advanceTimersByTime(5000));
      await waitFor(() => expect(callsAfterMount()).toBe(initial + 1));
      act(() => jest.advanceTimersByTime(5000));
      await waitFor(() => expect(callsAfterMount()).toBe(initial + 2));
    } finally {
      jest.useRealTimers();
    }
  });

  it("refreshes stats promptly on task activity events (debounced)", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => defaultHandler(url));
      await screen.findByLabelText("任务统计");
      const callsAfterMount = () =>
        mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/stats").length;
      const initial = callsAfterMount();
      act(() => {
        emitTaskActivity();
        emitTaskActivity();
      });
      // 防抖窗口内只发一笔。
      act(() => jest.advanceTimersByTime(1000));
      await waitFor(() => expect(callsAfterMount()).toBe(initial + 1));
    } finally {
      jest.useRealTimers();
    }
  });

  it("keeps last status when polling fails", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats();
      if (url === "/api/v2/public/site-info") return siteInfo();
      return statusEnvelope("degraded", "下载后端连接异常");
    });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/system/status") throw new TypeError("fetch failed");
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(200, defaultHandler(url));
    });
    fireEvent.click(screen.getAllByRole("button", { name: "登出" })[0]);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(screen.getByRole("alert")).toHaveTextContent("下载后端连接异常");
  });

  it("logs out from the sidebar and clears the session user", async () => {
    const { container } = setup((url) => defaultHandler(url));
    await screen.findByText("admin");
    fireEvent.click(container.querySelector(".app-logout")!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/logout");
      expect(call).toBeDefined();
    });
    await waitFor(() => expect(screen.queryByText("admin")).not.toBeInTheDocument());
  });

  it("keeps layout functional when status and stats both fail", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      throw new TypeError("fetch failed");
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <AppLayout />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("admin");
    // 轮询失败无降级横幅（网络失败保持上次读数，不误报 degraded）。
    expect(
      screen.queryByText(/下载后端连接异常/),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("任务统计")).not.toBeInTheDocument(); // 统计失败不显示。
  });

  it("logout from bottom nav toasts trigger then logs out", async () => {
    setup((url) => defaultHandler(url));
    await screen.findByLabelText("底部导航");
    fireEvent.click(screen.getByLabelText("底部导航").querySelector("button")!);
    expect(await screen.findByText("已触发登出")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭提示" }));
    await waitFor(() => expect(screen.queryByText("已触发登出")).not.toBeInTheDocument());
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/logout");
      expect(call).toBeDefined();
    });
  });
});
