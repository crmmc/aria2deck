import { render, screen, waitFor } from "@testing-library/react";

import { App } from "@/app/App";
import type { UserResponse, TaskStatsResponse } from "@/shared/api/types";

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

const STATS: TaskStatsResponse = {
  counts: { queued: 0, active: 0, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 },
  total: 0,
  download_speed: 0,
  upload_speed: 0,
  quota: { quota_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
  disk: { total_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
};

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

describe("App", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    window.localStorage.clear();
    window.history.replaceState({}, "", "/tasks");
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url.startsWith("/api/v2/tasks")) {
        return json(200, { items: [], total: 0, page: 1, page_size: 100 });
      }
      return json(404, { error: { code: "not_found", message: "未找到" } });
    });
  });

  it("mounts the router inside providers and renders the tasks page for the current route", async () => {
    render(<App />);

    // 应用壳（侧栏）与页面标题都要出现，证明 Router + Providers + 页面三层都挂上了。
    await waitFor(() => expect(screen.getByText("管理您的下载")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "任务" })).toBeInTheDocument();
  });
});
