import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppRouter } from "@/app/router";
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

function emptyStats(): unknown {
  return {
    counts: {
      queued: 0, active: 0, waiting: 0, paused: 0,
      completed: 0, failed: 0, cancelled: 0, expired: 0,
    },
    total: 0,
    download_speed: 0,
    upload_speed: 0,
    quota: { quota_bytes: 1, used_bytes: 0, reserved_bytes: 0, available_bytes: 1 },
  };
}

function settingsBody(): Record<string, unknown> {
  return {
    site_title: "Aria2Deck",
    max_task_size: 0,
    min_free_disk: 0,
    aria2_rpc_url: "http://127.0.0.1:6800/rpc",
    aria2_rpc_secret: null,
    aria2_bt_stop_timeout_seconds: 60,
    pack_format: "zip",
    pack_compression_level: 5,
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 3,
    ws_reconnect_factor: 2,
    history_retention_days: 90,
    tracker_fixed_list: [],
    tracker_remote_urls: [],
    tracker_refresh_interval_minutes: 0,
    download_connection_limits: {},
    account_security_rate_limit: 10,
    authenticated_api_rate_limit: 600,
    public_api_rate_limit: 300,
    share_access_rate_limit: 30,
    create_task_rate_limit: 60,
    create_torrent_rate_limit: 30,
    create_pack_rate_limit: 30,
    aria2_test_rate_limit: 10,
    rpc_rate_limit: 600,
    file_search_rate_limit: 120,
    create_share_rate_limit: 60,
    updated_at_ms: 1700000000000,
  };
}

function renderAt(path: string, me: UserResponse = ME) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v2/auth/me") return json(200, me);
    if (url === "/api/v2/public/site-info") {
      return json(200, {
        site_title: "Aria2Deck", ws_reconnect_max_delay_seconds: 30,
        ws_reconnect_jitter_seconds: 1, ws_reconnect_factor: 2,
      });
    }
    if (url === "/api/v2/system/status") {
      return json(200, { download_backend: { status: "ok", message: "" } });
    }
    if (url === "/api/v2/tasks/stats") return json(200, emptyStats());
    if (url.startsWith("/api/v2/tasks") || url.startsWith("/api/v2/history")) {
      return json(200, { items: [], total: 0, page: 1, page_size: 20 });
    }
    if (url === "/api/v2/credentials/rpc") {
      return json(200, { enabled: false, secret_prefix: null, secret: null });
    }
    if (url.startsWith("/api/v2/users")) return json(200, { items: [ME], total: 1, page: 1, page_size: 100 });
    if (url === "/api/v2/settings") return json(200, settingsBody());
    if (url === "/api/v2/stats/machine") {
      return json(200, {
        total_bytes: 1000, used_bytes: 500, free_bytes: 500,
        download_used_bytes: 200, system_used_bytes: 300,
      });
    }
    if (url === "/api/v2/settings/aria2/version") {
      return json(200, { connected: true, version: "1.37.0", features: [] });
    }
    if (url === "/api/v2/trackers") {
      return json(200, {
        items: [], entry_count: 0, remote_items: [], refresh_status: "success",
        failed_urls: [], updated_at_ms: null, last_refresh_at_ms: null,
      });
    }
    if (url.startsWith("/api/v2/storage/files")) {
      return json(200, { items: [], total: 0, page: 1, page_size: 20 });
    }
    if (url === "/api/v2/storage/disk-orphans/queue") {
      return json(200, {
        running: false, items: [], queued_count: 0, running_count: 0,
        done_count: 0, failed_count: 0, pending_bytes: 0,
      });
    }
    if (url.startsWith("/api/v2/storage/disk-orphans")) {
      return json(200, {
        items: [], total: 0, page: 1, page_size: 20, unrecognized_count: 0,
      });
    }
    return json(200, emptyStats());
  });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppProviders>
        <AppRouter />
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("router guards", () => {
  it("redirects unauthenticated users to /login", async () => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValueOnce(json(401, { error: { code: "unauthenticated", message: "请先登录" } }));
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <AppRouter />
        </AppProviders>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    });
  });

  it("renders tasks page for authenticated users", async () => {
    renderAt("/tasks");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "任务" })).toBeInTheDocument();
    });
  });

  it("shows 404 for unknown paths when authenticated", async () => {
    renderAt("/no-such-page");
    await waitFor(() => {
      expect(screen.getByText("页面不存在")).toBeInTheDocument();
    });
  });

  it("keeps public /s/:code accessible without login and renders share info", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (String(url).includes("/api/s/abc123/info")) {
        return json(200, {
          file_name: "公开文件.zip",
          file_size: 128,
          file_kind: "file",
          has_password: false,
          expires_at_ms: null,
          max_downloads: null,
          download_count: 0,
        });
      }
      return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    });
    render(
      <MemoryRouter initialEntries={["/s/abc123"]}>
        <AppProviders>
          <AppRouter />
        </AppProviders>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("公开文件.zip")).toBeInTheDocument();
    });
    expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument(); // 不跳登录。
  });
});

describe("batch-3 routes", () => {
  it("renders the profile page for authenticated users", async () => {
    renderAt("/profile");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "用户设置" })).toBeInTheDocument();
    });
  });

  it("renders admin pages for admins", async () => {
    renderAt("/users");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "用户管理" })).toBeInTheDocument();
    });
  });

  it("renders the settings page for admins", async () => {
    renderAt("/settings");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "系统设置" })).toBeInTheDocument();
    });
  });

  it.each([
    ["/settings/basic", "基础设置"],
    ["/settings/aria2", "aria2 后端"],
    ["/settings/pack", "打包设置"],
    ["/settings/realtime", "实时连接"],
    ["/settings/tracker", "Tracker 列表"],
    ["/settings/security", "安全与限流"],
  ])("renders settings deep link %s", async (path, heading) => {
    renderAt(path);
    expect(await screen.findByRole("heading", { name: heading, level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: heading })).toHaveAttribute("aria-current", "page");
  });

  it("renders the storage page for admins", async () => {
    renderAt("/storage");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "存储管理" })).toBeInTheDocument();
    });
  });

  it("renders the disk orphan page for admins", async () => {
    renderAt("/storage/orphans");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "未登记文件" })).toBeInTheDocument();
    });
  });

  it("redirects non-admin users from /storage/orphans to /tasks", async () => {
    renderAt("/storage/orphans", { ...ME, is_admin: false });
    expect(await screen.findByRole("heading", { name: "任务" })).toBeInTheDocument();
    const adminRequests = mockFetch.mock.calls.map(([url]) => String(url)).filter((url) =>
      url.startsWith("/api/v2/storage/disk-orphans"),
    );
    expect(adminRequests).toEqual([]);
  });

  it("redirects non-admin users from settings without administrator data requests", async () => {
    renderAt("/settings/security", { ...ME, is_admin: false });
    expect(await screen.findByRole("heading", { name: "任务" })).toBeInTheDocument();
    const adminRequests = mockFetch.mock.calls.map(([url]) => String(url)).filter((url) =>
      url.startsWith("/api/v2/settings")
      || url.startsWith("/api/v2/trackers")
      || url === "/api/v2/stats/machine"
      || url === "/api/v2/credentials/invalidate"
    );
    expect(adminRequests).toEqual([]);
  });

  it("redirects non-admin users from /users to /tasks", async () => {
    renderAt("/users", { ...ME, is_admin: false });
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "任务" })).toBeInTheDocument();
    });
    expect(mockFetch.mock.calls.find(([url]) => url.includes("/api/v2/users"))).toBeUndefined();
  });
});
