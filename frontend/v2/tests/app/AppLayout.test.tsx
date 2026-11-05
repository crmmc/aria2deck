import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppLayout } from "@/app/layouts/AppLayout";
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

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function statusEnvelope(status: "ok" | "degraded", message = ""): unknown {
  return { download_backend: { status, message } };
}

function stats(speed = 4096): unknown {
  return {
    counts: {
      queued: 0, active: 1, waiting: 0, paused: 0,
      completed: 0, failed: 0, cancelled: 0, expired: 0,
    },
    total: 1,
    download_speed: speed,
    upload_speed: 0,
    quota: { quota_bytes: 1, used_bytes: 0, reserved_bytes: 0, available_bytes: 1 },
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

describe("AppLayout", () => {
  it("renders nav entries, username and stats summary", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats();
      return statusEnvelope("ok");
    });
    const nav = await screen.findByLabelText("主导航");
    expect(nav.textContent).toContain("任务");
    expect(nav.textContent).toContain("历史");
    expect(nav.textContent).toContain("文件");
    expect(nav.textContent).toContain("分享");
    await screen.findByText("admin");
    const statsBox = await screen.findByLabelText("任务统计");
    expect(statsBox.textContent).toContain("4.00 KiB/s");
    expect(statsBox.textContent).toContain("任务总数：1");
  });

  it("shows the degraded banner when backend reports degraded", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats(0);
      return statusEnvelope("degraded", "下载后端连接异常");
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("下载后端连接异常");
  });

  it("keeps last status when polling fails", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats(0);
      return statusEnvelope("degraded", "下载后端连接异常");
    });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/system/status") throw new TypeError("fetch failed");
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(200, statusEnvelope("ok"));
    });
    fireEvent.click(screen.getAllByRole("button", { name: "登出" })[0]);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(screen.getByRole("alert")).toHaveTextContent("下载后端连接异常");
  });

  it("logs out from the sidebar and clears the session user", async () => {
    const { container } = setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats(0);
      return statusEnvelope("ok");
    });
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
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // 轮询失败无横幅。
    expect(screen.queryByLabelText("任务统计")).not.toBeInTheDocument(); // 统计失败不显示。
  });

  it("logout from bottom nav toasts trigger then logs out", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/me") return ME;
      if (url === "/api/v2/tasks/stats") return stats(0);
      return statusEnvelope("ok");
    });
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
