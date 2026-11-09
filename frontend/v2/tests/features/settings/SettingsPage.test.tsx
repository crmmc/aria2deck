import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { SettingsPage } from "@/features/settings/SettingsPage";
import type { SettingsResponse, TrackerStatusResponse, UserResponse } from "@/shared/api/types";

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

const GIB = 1024 ** 3;

function settings(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    site_title: "Aria2Deck",
    max_task_size: 10 * GIB,
    min_free_disk: 5 * GIB,
    aria2_rpc_url: "http://127.0.0.1:6800/rpc",
    aria2_rpc_secret: "********",
    aria2_bt_stop_timeout_seconds: 60,
    pack_format: "zip",
    pack_compression_level: 5,
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 3,
    ws_reconnect_factor: 2,
    history_retention_days: 90,
    tracker_fixed_list: [],
    tracker_remote_urls: ["https://t.example/list.txt"],
    tracker_refresh_interval_minutes: 60,
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
    ...overrides,
  };
}

function tracker(overrides: Partial<TrackerStatusResponse> = {}): TrackerStatusResponse {
  return {
    items: ["udp://a/announce"],
    entry_count: 1,
    remote_items: ["udp://a/announce"],
    refresh_status: "success",
    failed_urls: [],
    updated_at_ms: 1700000000000,
    last_refresh_at_ms: 1700000000000,
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(
  handler: (url: string, init?: { method?: string; body?: string }) => unknown,
  me: UserResponse = ME,
) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, me);
    const result = handler(url, init);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { json?: unknown }).json === "function"
    ) {
      return result;
    }
    // 处理器未命中（null）时的默认读取响应。
    if (result === null) {
      if (url === "/api/v2/settings" && init?.method === "GET") return json(200, settings());
      if (url === "/api/v2/trackers" && init?.method === "GET") return json(200, tracker());
    }
    return json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/settings"]}>
      <AppProviders>
        <SettingsPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function openAdvanced() {
  fireEvent.click(screen.getByText("高级（打包 / 历史保留 / 凭证作废）"));
}

describe("SettingsPage load and layout", () => {
  it("renders all sections with field values from the server", async () => {
    setup(() => null);
    expect(await screen.findByLabelText("网站标题")).toHaveValue("Aria2Deck");
    expect(screen.getByLabelText("RPC 地址")).toHaveValue("http://127.0.0.1:6800/rpc");
    expect(screen.queryByLabelText(/隐藏文件后缀/)).not.toBeInTheDocument();
    const connectionLimits = screen.getByLabelText(
      "下载连接数限制（每行一条：配置键=连接数，0-1000）",
    );
    expect(connectionLimits).toBeInTheDocument();
    expect(connectionLimits).toHaveAttribute(
      "placeholder",
      [
        "total_connections=100",
        "authenticated_per_user_connections=16",
        "authenticated_per_file_connections=8",
        "anonymous_total_connections=40",
        "anonymous_per_ip_connections=4",
        "anonymous_per_file_connections=2",
      ].join("\n"),
    );
    expect(screen.getByText("RPC 连接参数；Secret 掩码显示，留空保持不变。")).toBeInTheDocument();
    expect(screen.getByLabelText("账户安全限流")).toHaveValue("10");
    expect(screen.getByLabelText("打包格式")).toHaveValue("zip");
    expect(screen.getByText(/共 1 条/)).toBeInTheDocument();
    expect(screen.getByText("刷新成功")).toBeInTheDocument();
  });

  it("keeps the advanced section collapsed by default and expands on click", async () => {
    const { container } = setup(() => null);
    await screen.findByLabelText("网站标题");
    const details = container.querySelector("details.advanced-section") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    openAdvanced();
    expect(details.open).toBe(true);
    expect(screen.getByText("作废全部凭证…")).toBeInTheDocument();
  });

  it("exercises every field control once without breaking the form", async () => {
    const { container } = setup(() => null);
    await screen.findByLabelText("网站标题");
    openAdvanced();
    // 遍历全部 input/textarea/select：先改为临时值再改回，触发每个内联处理器。
    const controls = Array.from(
      container.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
        "input:not([type='checkbox']), textarea, select",
      ),
    );
    expect(controls.length).toBeGreaterThan(20);
    for (const control of controls) {
      const original = control.value;
      const temp =
        control.tagName === "SELECT" ? (original === "zip" ? "tar.zst" : "zip") : `${original}0`;
      fireEvent.change(control, { target: { value: temp } });
      fireEvent.change(control, { target: { value: original } });
    }
    // 保存仍应成功：值已还原，payload 与服务器一致。
    fireEvent.click(screen.getByRole("button", { name: "保存全部" }));
    await waitFor(() => {
      expect(mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH")).toBeDefined();
    });
    expect(await screen.findByText("设置已保存")).toBeInTheDocument();
  });

  it("shows an unloaded hint when tracker status fails but settings load", async () => {
    setup((url) => {
      if (url === "/api/v2/trackers") {
        return json(500, { error: { code: "internal", message: "Tracker 状态失败" } });
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    expect(screen.getByText("Tracker 状态未加载。")).toBeInTheDocument();
  });

  it("shows the error state with working retry when the initial load fails", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(500, { error: { code: "internal", message: "后端错误" } });
    });
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <AppProviders>
          <SettingsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/trackers") return json(200, tracker());
      return json(200, settings());
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByLabelText("网站标题")).toBeInTheDocument();
  });

  it("redirects non-admin users away from settings", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") {
        return json(200, { ...ME, is_admin: false });
      }
      return json(200, settings());
    });
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <AppProviders>
          <Routes>
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/tasks" element={<div>任务页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("任务页占位")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([url]) => url.includes("/api/v2/settings"))).toBeUndefined();
  });
});

describe("SettingsPage save", () => {
  it("patches then re-reads and only then toasts success", async () => {
    let patched = 0;
    setup((_url, init) => {
      if (init?.method === "PATCH") {
        patched += 1;
        return settings({ site_title: "新标题" });
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "新标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存全部" }));
    await waitFor(() => expect(patched).toBe(1));
    const patchCall = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")!;
    const body = JSON.parse(patchCall[1].body as string);
    expect(body.site_title).toBe("新标题");
    expect(body.aria2_rpc_secret).toBeUndefined(); // 掩码不覆盖真值。
    expect(body.max_task_size).toBe(10 * GIB);
    expect(body.account_security_rate_limit).toBe(10);
    expect(body.hidden_file_extensions).toBeUndefined();
    // 保存成功后重读一次。
    await waitFor(() => {
      const reads = mockFetch.mock.calls.filter(
        ([url, init]) => url === "/api/v2/settings" && init?.method === "GET",
      );
      expect(reads.length).toBeGreaterThanOrEqual(2);
    });
    expect(await screen.findByText("设置已保存")).toBeInTheDocument();
  });

  it("includes the typed secret but never the mask", async () => {
    setup((_url, init) => {
      if (init?.method === "PATCH") return settings();
      return null;
    });
    await screen.findByLabelText("RPC Secret");
    fireEvent.change(screen.getByLabelText("RPC Secret"), { target: { value: " typed-secret " } });
    fireEvent.click(screen.getByRole("button", { name: "保存全部" }));
    await waitFor(() => {
      const patchCall = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(patchCall).toBeDefined();
    });
    const body = JSON.parse(
      mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")![1].body as string,
    );
    expect(body.aria2_rpc_secret).toBe("typed-secret");
  });

  it("shows an inline validation error and skips the PATCH", async () => {
    setup(() => null);
    await screen.findByLabelText("账户安全限流");
    fireEvent.change(screen.getByLabelText("账户安全限流"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "保存全部" }));
    expect(await screen.findByText(/账户安全限流必须在 1 到 1000000 之间/)).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
    expect(screen.queryByText("设置已保存")).not.toBeInTheDocument();
  });

  it("surfaces PATCH failure inline without success toast", async () => {
    setup((_url, init) => {
      if (init?.method === "PATCH") {
        return json(422, { error: { code: "invalid_input", message: "Tracker 地址无效" } });
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存全部" }));
    expect(await screen.findByText("Tracker 地址无效")).toBeInTheDocument();
    expect(screen.queryByText("设置已保存")).not.toBeInTheDocument();
  });

  it("reports saved-but-reread-failed without success toast", async () => {
    let reads = 0;
    setup((url, init) => {
      if (init?.method === "PATCH") return settings();
      if (url === "/api/v2/settings") {
        reads += 1;
        if (reads >= 2) return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存全部" }));
    expect(await screen.findByText("已保存，但重新读取失败，请刷新页面查看最新配置")).toBeInTheDocument();
    expect(screen.queryByText("设置已保存")).not.toBeInTheDocument();
  });
});

describe("SettingsPage aria2 connection test", () => {
  it("shows version and feature tags on success", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/settings/aria2/test") {
        return { connected: true, version: "1.37.0", features: ["GZip", "BitTorrent"] };
      }
      return null;
    });
    await screen.findByLabelText("RPC 地址");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接成功")).toBeInTheDocument();
    expect(screen.getByText(/1\.37\.0/)).toBeInTheDocument();
    expect(screen.getByText("GZip")).toBeInTheDocument();
    expect(screen.getByText("BitTorrent")).toBeInTheDocument();
  });

  it("shows failure without leaking URL or version details", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/settings/aria2/test") {
        return { connected: false, version: null, features: [] };
      }
      return null;
    });
    await screen.findByLabelText("RPC 地址");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    const alert = await screen.findByText("连接失败");
    expect(alert).toBeInTheDocument();
    expect(screen.queryByText(/127\.0\.0\.1/)).not.toBeInTheDocument();
  });

  it("surfaces 429 with Chinese message and keeps the button usable", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/settings/aria2/test") {
        return json(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后重试" } });
      }
      return null;
    });
    await screen.findByLabelText("RPC 地址");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("请求过于频繁，请稍后重试")).toBeInTheDocument();
    expect(screen.queryByText("连接成功")).not.toBeInTheDocument();
    expect(screen.queryByText("连接失败")).not.toBeInTheDocument(); // 未得到测试结果。
    expect(
      (screen.getByRole("button", { name: "测试连接" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});

describe("SettingsPage tracker refresh", () => {
  it("lists failed URLs after a partial refresh", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/trackers/refresh") {
        return tracker({
          refresh_status: "partial",
          failed_urls: ["https://bad.example/list", "https://worse.example/list"],
        });
      }
      return null;
    });
    await screen.findByText("刷新成功");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Tracker" }));
    expect(await screen.findByText(/部分源刷新失败（2 个）/)).toBeInTheDocument();
    expect(screen.getByText("https://bad.example/list")).toBeInTheDocument();
    expect(screen.getByText("https://worse.example/list")).toBeInTheDocument();
    expect(screen.getByText("部分源失败")).toBeInTheDocument();
  });

  it("toasts kept-old-list on full failure", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/trackers/refresh") {
        return tracker({ refresh_status: "failed", failed_urls: ["https://bad.example"] });
      }
      return null;
    });
    await screen.findByText("刷新成功");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Tracker" }));
    expect(await screen.findByText("Tracker 刷新失败，已保留旧列表")).toBeInTheDocument();
  });

  it("toasts entry count on success", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/trackers/refresh") {
        return tracker({ entry_count: 42 });
      }
      return null;
    });
    await screen.findByText("刷新成功");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Tracker" }));
    expect(await screen.findByText("Tracker 已刷新，共 42 条")).toBeInTheDocument();
  });

  it("toasts network failure without changing the status card", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/trackers/refresh") {
        return json(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后重试" } });
      }
      return null;
    });
    await screen.findByText("刷新成功");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Tracker" }));
    expect(await screen.findByText("请求过于频繁，请稍后重试")).toBeInTheDocument();
  });
});

describe("SettingsPage credential invalidation", () => {
  it("requires the exact confirmation string and reports counts", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/invalidate") {
        return { ok: true, api_token_count: 2, rpc_secret_count: 3 };
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    openAdvanced();
    fireEvent.click(screen.getByRole("button", { name: "作废全部凭证…" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    expect(dialog.textContent).toContain("INVALIDATE_ALL_CREDENTIALS");
    // 先取消：对话框关闭且不提交。
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "作废全部凭证…" }));
    await screen.findByRole("dialog", { name: "作废全部凭证" });
    // 输入不匹配时确认不可用。
    fireEvent.change(screen.getByLabelText("凭证作废确认串"), {
      target: { value: "wrong-string" },
    });
    expect(
      (screen.getByRole("button", { name: "确认作废" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("凭证作废确认串"), {
      target: { value: "INVALIDATE_ALL_CREDENTIALS" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/credentials/invalidate",
      );
      expect(JSON.parse(call![1].body as string)).toEqual({
        confirmation: "INVALIDATE_ALL_CREDENTIALS",
      });
    });
    expect(await screen.findByText("已作废 2 个 API Token、3 个 RPC Secret")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("keeps the dialog open with an inline error on 422", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/invalidate") {
        return json(422, { error: { code: "invalid_input", message: "凭证作废确认字符串不正确" } });
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    openAdvanced();
    fireEvent.click(screen.getByRole("button", { name: "作废全部凭证…" }));
    await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.change(screen.getByLabelText("凭证作废确认串"), {
      target: { value: "INVALIDATE_ALL_CREDENTIALS" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认作废" }));
    expect(await screen.findByText("凭证作废确认字符串不正确")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "作废全部凭证" })).toBeInTheDocument();
    // Escape 也能关闭对话框。
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });
});

describe("SettingsPage 机器磁盘卡（E1）", () => {
  it("renders the machine disk card with download/system split", async () => {
    setup((url) => {
      if (url === "/api/v2/stats/machine") {
        return {
          total_bytes: 1000,
          used_bytes: 500,
          free_bytes: 500,
          download_used_bytes: 200,
          system_used_bytes: 300,
        };
      }
      return null;
    });
    const card = await screen.findByRole("region", { name: undefined });
    void card;
    expect(await screen.findByText("机器磁盘空间")).toBeInTheDocument();
    expect(screen.getByLabelText("机器磁盘占用")).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByText(/下载占用：200 B/)).toBeInTheDocument();
    expect(screen.getByText(/系统占用：300 B/)).toBeInTheDocument();
    expect(screen.getByText(/总占用：500 B/)).toBeInTheDocument();
  });

  it("hides the card when machine stats fail to load", async () => {
    setup((url) => {
      if (url === "/api/v2/stats/machine") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return null;
    });
    await screen.findByText("基本");
    expect(screen.queryByText("机器磁盘空间")).not.toBeInTheDocument();
  });
});
