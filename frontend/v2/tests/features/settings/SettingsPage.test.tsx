import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { SettingsPage } from "@/features/settings/SettingsPage";
import type {
  MachineStatsResponse,
  SettingsResponse,
  TrackerStatusResponse,
  UserResponse,
} from "@/shared/api/types";

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
    tracker_fixed_list: ["udp://a/announce"],
    tracker_remote_urls: ["https://t.example/list.txt"],
    tracker_refresh_interval_minutes: 60,
    download_connection_limits: { total_connections: 100 },
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

function machine(overrides: Partial<MachineStatsResponse> = {}): MachineStatsResponse {
  return {
    total_bytes: 1000,
    used_bytes: 500,
    free_bytes: 500,
    download_used_bytes: 200,
    system_used_bytes: 300,
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

type Handler = (
  url: string,
  init?: { method?: string; body?: string },
) => unknown | Promise<unknown>;

function setup(handler: Handler = () => null, me: UserResponse = ME) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, me);
    if (url === "/api/v2/public/site-info") {
      return json(200, {
        site_title: "Aria2Deck",
        ws_reconnect_max_delay_seconds: 30,
        ws_reconnect_jitter_seconds: 1,
        ws_reconnect_factor: 2,
      });
    }
    const result = await handler(url, init);
    if (result !== null) return result;
    if (url === "/api/v2/settings") return json(200, settings());
    if (url === "/api/v2/trackers") return json(200, tracker());
    if (url === "/api/v2/stats/machine") return json(200, machine());
    if (url === "/api/v2/settings/aria2/version") {
      return json(200, { connected: true, version: "1.37.0", features: ["BitTorrent"] });
    }
    return json(200, {});
  });
  return render(
    <MemoryRouter initialEntries={["/settings"]}>
      <AppProviders>
        <SettingsPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function openAdvanced() {
  fireEvent.click(screen.getByRole("button", { name: /系统高级设置/ }));
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

beforeEach(() => {
  originalShowModal = HTMLDialogElement.prototype.showModal;
  originalClose = HTMLDialogElement.prototype.close;
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: jest.fn(function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    }),
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: jest.fn(function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    }),
  });
});

afterEach(() => {
  document.querySelectorAll("dialog").forEach((d) => d.removeAttribute("open"));
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: originalShowModal });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  }
  if (originalClose) {
    Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: originalClose });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  }
});

describe("SettingsPage（V1 系统设置迁移）", () => {
  it("renders the V1 page shell and every supported section", async () => {
    setup();
    expect(await screen.findByText("系统设置")).toBeInTheDocument();
    expect(screen.getByText("系统配置（仅管理员）")).toBeInTheDocument();
    expect(screen.getByText("系统配置")).toBeInTheDocument();
    expect(screen.getByText("aria2 后端配置")).toBeInTheDocument();
    expect(screen.getByText("打包设置")).toBeInTheDocument();
    expect(screen.getByText("WebSocket 重连设置")).toBeInTheDocument();
    expect(screen.getByText("Tracker 列表")).toBeInTheDocument();
    expect(screen.getByText("凭证安全")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeInTheDocument();
  });

  it("binds every field to the values from the server", async () => {
    setup();
    expect(await screen.findByLabelText("网站标题")).toHaveValue("Aria2Deck");
    expect(screen.getByLabelText("最大任务大小 (GB)")).toHaveValue(10);
    expect(screen.getByLabelText("最小剩余磁盘空间 (GB)")).toHaveValue(5);
    expect(screen.getByLabelText("历史保留天数")).toHaveValue(90);
    expect(screen.getByLabelText("aria2 RPC URL")).toHaveValue("http://127.0.0.1:6800/rpc");
    expect(screen.getByLabelText("aria2 RPC Secret")).toHaveValue("********");
    expect(screen.getByLabelText("BT 停止超时")).toHaveValue(60);
    expect(screen.getByLabelText("ZIP（Deflate64）")).toBeChecked();
    expect(screen.getByLabelText("压缩等级")).toHaveValue("5");
    expect(screen.getByLabelText("最大重连延迟")).toHaveValue("30");
    expect(screen.getByLabelText("固定 tracker 列表")).toHaveValue("udp://a/announce");
    expect(screen.getByLabelText("远程 tracker 列表 URL")).toHaveValue(
      "https://t.example/list.txt",
    );
    expect(screen.getByLabelText("tracker 刷新间隔（分钟）")).toHaveValue(60);
  });

  it("shows the aria2 connection banner and version", async () => {
    setup();
    expect(await screen.findByText("已连接")).toBeInTheDocument();
    expect(screen.getByText(/aria2 1\.37\.0/)).toBeInTheDocument();
  });

  it("shows the disconnected banner when the saved config cannot reach aria2", async () => {
    setup((url) =>
      url === "/api/v2/settings/aria2/version"
        ? json(200, { connected: false, version: null, features: [] })
        : null,
    );
    expect(await screen.findByText("未连接")).toBeInTheDocument();
    expect(
      screen.getByText("无法连接 aria2，请检查 RPC 地址与 Secret。"),
    ).toBeInTheDocument();
  });

  it("keeps the sections V2 cannot support out of the DOM entirely (C02-C06)", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    expect(screen.queryByText("文件管理配置")).not.toBeInTheDocument();
    expect(screen.queryByText("隐藏文件后缀名")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("输入后缀名，按回车添加")).not.toBeInTheDocument();
    expect(screen.queryByText("已登录保底连接数")).not.toBeInTheDocument();
    expect(screen.queryByText("匿名基础连接数")).not.toBeInTheDocument();
    expect(screen.queryByText("匿名可借用连接数")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("抖动系数")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("指数因子")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("清空 aria2 RPC Secret")).not.toBeInTheDocument();
  });

  it("renders the machine disk card from the download/system split", async () => {
    setup();
    expect(await screen.findByText("机器磁盘空间")).toBeInTheDocument();
    expect(screen.getByText(/下载占用：200\.0 B/)).toBeInTheDocument();
    expect(screen.getByText(/系统占用：300\.0 B/)).toBeInTheDocument();
    expect(screen.getByText(/总占用：500\.0 B/)).toBeInTheDocument();
  });

  it("falls back to the V1 未连接 banner when the version probe fails", async () => {
    setup((url) =>
      url === "/api/v2/settings/aria2/version"
        ? json(500, { error: { code: "internal", message: "探测失败" } })
        : null,
    );
    await screen.findByLabelText("网站标题");
    expect(screen.queryByText("已连接")).not.toBeInTheDocument();
    expect(screen.getByText("未连接")).toBeInTheDocument();
    // 探测未返回结果时不渲染原因段（V1 只在后端给出错误文本时显示）。
    expect(
      screen.queryByText("无法连接 aria2，请检查 RPC 地址与 Secret。"),
    ).not.toBeInTheDocument();
  });

  it("hides the machine card and keeps the form usable when stats fail", async () => {
    setup((url) =>
      url === "/api/v2/stats/machine"
        ? json(500, { error: { code: "internal", message: "服务器错误" } })
        : null,
    );
    await screen.findByLabelText("网站标题");
    expect(screen.queryByText("机器磁盘空间")).not.toBeInTheDocument();
  });

  it("shows the page error when the settings read fails", async () => {
    setup((url) =>
      url === "/api/v2/settings"
        ? json(500, { error: { code: "internal", message: "配置读取失败" } })
        : null,
    );
    expect(await screen.findByText("加载配置失败")).toBeInTheDocument();
    expect(screen.queryByText("系统配置（仅管理员）")).not.toBeInTheDocument();
  });

  it("redirects non admin users and sends no settings request", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, { ...ME, is_admin: false });
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
    expect(mockFetch.mock.calls.find(([url]) => url === "/api/v2/settings")).toBeUndefined();
  });
});

describe("SettingsPage 高级设置折叠区", () => {
  it("stays collapsed by default and reveals rate limits plus connection limits", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    const toggle = screen.getByRole("button", { name: /系统高级设置/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("接口频率限制")).not.toBeInTheDocument();

    openAdvanced();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("接口频率限制")).toBeInTheDocument();
    expect(screen.getByText("下载并发限制")).toBeInTheDocument();
    expect(screen.getByLabelText("账户安全限流")).toHaveValue(10);
    expect(screen.getByLabelText("系统总下载连接上限")).toHaveValue(100);
    expect(screen.getByLabelText("匿名总连接数")).toHaveValue(null);
  });

  it("clamps numeric inputs to their range like V1 did", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    openAdvanced();
    const createShare = screen.getByLabelText("创建分享限流");
    fireEvent.change(createShare, { target: { value: "1000001" } });
    expect(createShare).toHaveValue(1000000);
    const connections = screen.getByLabelText("系统总下载连接上限");
    fireEvent.change(connections, { target: { value: "1001" } });
    expect(connections).toHaveValue(1000);
  });
});

describe("SettingsPage 保存", () => {
  it("patches, re-reads and only then toasts success", async () => {
    const bodies: unknown[] = [];
    setup(async (url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") {
        bodies.push(JSON.parse(init.body ?? "{}"));
        return json(200, settings({ site_title: "新标题" }));
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "新标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByText("配置已保存");
    expect(bodies).toHaveLength(1);
    const body = bodies[0] as Record<string, unknown>;
    expect(body.site_title).toBe("新标题");
    expect(body.max_task_size).toBe(10 * GIB);
    expect(body.account_security_rate_limit).toBe(10);
    expect(body.aria2_rpc_secret).toBeUndefined();
    expect(body).not.toHaveProperty("hidden_file_extensions");
    const reads = mockFetch.mock.calls.filter(
      ([url, init]) => url === "/api/v2/settings" && (init?.method ?? "GET") === "GET",
    );
    expect(reads.length).toBeGreaterThanOrEqual(2);
  });

  it("submits a typed secret but never the mask", async () => {
    const bodies: unknown[] = [];
    setup((url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") {
        bodies.push(JSON.parse(init.body ?? "{}"));
        return json(200, settings());
      }
      return null;
    });
    await screen.findByLabelText("aria2 RPC Secret");
    fireEvent.change(screen.getByLabelText("aria2 RPC Secret"), {
      target: { value: " typed-secret " },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByText("配置已保存");
    expect((bodies[0] as Record<string, unknown>).aria2_rpc_secret).toBe("typed-secret");
  });

  it("shows a V1 style inline error and skips the PATCH on invalid input", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("最大任务大小 (GB)"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    const inline = await screen.findByText("最大任务大小必须是不小于 0 的数字");
    expect(inline).toHaveClass("save-error-inline");
    expect(
      mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH"),
    ).toBeUndefined();
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });

  it("keeps the backend message inline when the PATCH is rejected", async () => {
    setup((url, init) =>
      url === "/api/v2/settings" && init?.method === "PATCH"
        ? json(422, { error: { code: "invalid_input", message: "Tracker 地址无效" } })
        : null,
    );
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByText("Tracker 地址无效")).toBeInTheDocument();
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });

  it("falls back to the V1 page error when the re-read after saving fails", async () => {
    let reads = 0;
    setup((url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") return json(200, settings());
      if (url === "/api/v2/settings") {
        reads += 1;
        if (reads >= 2) return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByText("加载配置失败")).toBeInTheDocument();
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });
});

describe("SettingsPage aria2 连接测试", () => {
  it("tests the unsaved URL without posting a masked secret", async () => {
    const bodies: unknown[] = [];
    setup((url, init) => {
      if (url === "/api/v2/settings/aria2/test" && init?.method === "POST") {
        bodies.push(JSON.parse(init.body ?? "{}"));
        return json(200, { connected: true, version: "1.38.0", features: [] });
      }
      return null;
    });
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.change(screen.getByLabelText("aria2 RPC URL"), {
      target: { value: "http://127.0.0.1:6900/rpc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("测试结果：连接成功")).toBeInTheDocument();
    expect(screen.getByText(/aria2 版本: 1\.38\.0/)).toBeInTheDocument();
    expect(bodies[0]).toEqual({ aria2_rpc_url: "http://127.0.0.1:6900/rpc" });
  });

  it("sends a typed secret with the test request", async () => {
    const bodies: unknown[] = [];
    setup((url, init) => {
      if (url === "/api/v2/settings/aria2/test" && init?.method === "POST") {
        bodies.push(JSON.parse(init.body ?? "{}"));
        return json(200, { connected: true, version: "1.38.0", features: [] });
      }
      return null;
    });
    await screen.findByLabelText("aria2 RPC Secret");
    fireEvent.change(screen.getByLabelText("aria2 RPC Secret"), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("测试结果：连接成功");
    expect(bodies[0]).toEqual({ aria2_rpc_url: "http://127.0.0.1:6800/rpc", aria2_rpc_secret: "s3cret" });
  });

  it("requires an RPC URL before testing", async () => {
    setup((url) => (url === "/api/v2/settings" ? json(200, settings({ aria2_rpc_url: "" })) : null));
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("测试结果：连接失败")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.find(([url]) => url === "/api/v2/settings/aria2/test"),
    ).toBeUndefined();
  });

  it("surfaces a transport failure as a failed card plus toast", async () => {
    setup((url, init) =>
      url === "/api/v2/settings/aria2/test" && init?.method === "POST"
        ? json(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后重试" } })
        : null,
    );
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("测试结果：连接失败")).toBeInTheDocument();
    expect(screen.getByText("请求过于频繁，请稍后重试")).toBeInTheDocument();
  });
});

describe("SettingsPage Tracker 区块", () => {
  it("shows entry count, status and refresh time", async () => {
    setup();
    expect(await screen.findByText(/当前条目数：1/)).toBeInTheDocument();
    expect(screen.getByText(/上次刷新结果：成功/)).toBeInTheDocument();
  });

  it("maps every V2 refresh status to V1 wording", async () => {
    const cases: { status: string; expected: RegExp }[] = [
      { status: "never", expected: /上次刷新结果：从未刷新/ },
      { status: "success", expected: /上次刷新结果：成功/ },
      { status: "partial", expected: /部分失败（失败源 0 个）/ },
      { status: "failed", expected: /上次刷新结果：全部失败/ },
      { status: "refreshing", expected: /上次刷新结果：刷新中/ },
    ];
    for (const { status, expected } of cases) {
      const view = setup((url) =>
        url === "/api/v2/trackers" ? json(200, tracker({ refresh_status: status })) : null,
      );
      expect(await screen.findByText(expected)).toBeInTheDocument();
      view.unmount();
      mockFetch.mockReset();
    }
  });

  it("lists failed urls when the last refresh was partial", async () => {
    setup((url) =>
      url === "/api/v2/trackers"
        ? json(
            200,
            tracker({ refresh_status: "partial", failed_urls: ["https://bad.example/list"] }),
          )
        : null,
    );
    expect(await screen.findByText("失败源：https://bad.example/list")).toBeInTheDocument();
  });

  it("shows zero entries and never refreshed when the status cannot load", async () => {
    setup((url) =>
      url === "/api/v2/trackers"
        ? json(500, { error: { code: "internal", message: "Tracker 状态失败" } })
        : null,
    );
    expect(await screen.findByText("当前条目数：0")).toBeInTheDocument();
    expect(screen.getByText(/上次刷新结果：从未刷新/)).toBeInTheDocument();
  });

  it("refreshes the list and toasts success", async () => {
    setup((url, init) =>
      url === "/api/v2/trackers/refresh" && init?.method === "POST"
        ? json(200, tracker({ entry_count: 42, refresh_status: "success" }))
        : null,
    );
    await screen.findByText(/当前条目数：1/);
    const button = screen.getByRole("button", { name: "立即刷新" });
    fireEvent.click(button);
    expect(await screen.findByText("tracker 列表已刷新")).toBeInTheDocument();
    expect(screen.getByText("当前条目数：42")).toBeInTheDocument();
  });

  it("toasts an error when the refresh fails entirely", async () => {
    setup((url, init) =>
      url === "/api/v2/trackers/refresh" && init?.method === "POST"
        ? json(200, tracker({ refresh_status: "failed", failed_urls: ["https://bad.example"] }))
        : null,
    );
    await screen.findByText(/当前条目数：1/);
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    expect(
      await screen.findByText("刷新 tracker 列表失败：全部远程源不可用"),
    ).toBeInTheDocument();
  });

  it("toasts the backend message when the refresh request is rejected", async () => {
    setup((url, init) =>
      url === "/api/v2/trackers/refresh" && init?.method === "POST"
        ? json(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后重试" } })
        : null,
    );
    await screen.findByText(/当前条目数：1/);
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    expect(
      await screen.findByText("刷新 tracker 列表失败：请求过于频繁，请稍后重试"),
    ).toBeInTheDocument();
  });
});

describe("SettingsPage 凭证作废", () => {
  it("confirms first, then reports the invalidated counts", async () => {
    setup((url, init) =>
      url === "/api/v2/credentials/invalidate" && init?.method === "POST"
        ? json(200, { ok: true, api_token_count: 2, rpc_secret_count: 1 })
        : null,
    );
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    expect(within(dialog).getByText(/此操作不可恢复/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
    expect(
      await screen.findByText("已作废 2 个 API Token、1 个 RPC Secret"),
    ).toBeInTheDocument();
    const call = mockFetch.mock.calls.find(
      ([url, init]) => url === "/api/v2/credentials/invalidate" && init?.method === "POST",
    );
    expect(JSON.parse(call![1].body as string)).toEqual({
      confirmation: "INVALIDATE_ALL_CREDENTIALS",
    });
  });

  it("does nothing when the confirmation is cancelled", async () => {
    setup(() => null);
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(
      mockFetch.mock.calls.find(([url]) => url === "/api/v2/credentials/invalidate"),
    ).toBeUndefined();
  });

  it("toasts the backend message on failure", async () => {
    setup((url, init) =>
      url === "/api/v2/credentials/invalidate" && init?.method === "POST"
        ? json(422, { error: { code: "invalid_input", message: "凭证作废确认字符串不正确" } })
        : null,
    );
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
    expect(
      await screen.findByText("作废失败：凭证作废确认字符串不正确"),
    ).toBeInTheDocument();
  });
});

describe("SettingsPage 卸载保护（V1 语义：卸载后不再 setState / 弹 Toast）", () => {


  it("ignores successful write responses that land after unmount", async () => {
    const patch = deferred<unknown>();
    const test = deferred<unknown>();
    const invalidate = deferred<unknown>();
    const refresh = deferred<unknown>();
    const view = setup((url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") return patch.promise;
      if (url === "/api/v2/settings/aria2/test") return test.promise;
      if (url === "/api/v2/credentials/invalidate") return invalidate.promise;
      if (url === "/api/v2/trackers/refresh") return refresh.promise;
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
    await waitFor(() =>
      expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/credentials/invalidate")).toBe(true),
    );

    view.unmount();
    patch.resolve(json(200, settings()));
    test.resolve(json(200, { connected: true, version: "1.37.0", features: [] }));
    invalidate.resolve(json(200, { ok: true, api_token_count: 1, rpc_secret_count: 1 }));
    refresh.resolve(json(200, tracker()));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
    expect(screen.queryByText("tracker 列表已刷新")).not.toBeInTheDocument();
  });

  it("ignores write failures that land after unmount", async () => {
    const patch = deferred<unknown>();
    const test = deferred<unknown>();
    const invalidate = deferred<unknown>();
    const refresh = deferred<unknown>();
    const view = setup((url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") return patch.promise;
      if (url === "/api/v2/settings/aria2/test") return test.promise;
      if (url === "/api/v2/credentials/invalidate") return invalidate.promise;
      if (url === "/api/v2/trackers/refresh") return refresh.promise;
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
    await waitFor(() =>
      expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/credentials/invalidate")).toBe(true),
    );

    view.unmount();
    patch.reject(new Error("晚到的保存失败"));
    test.reject(new Error("晚到的测试失败"));
    invalidate.reject(new Error("晚到的作废失败"));
    refresh.reject(new Error("晚到的刷新失败"));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText(/晚到的/)).not.toBeInTheDocument();
  });

  it("ignores an initial settings read that resolves after unmount", async () => {
    const pending = deferred<unknown>();
    const view = setup((url) => (url === "/api/v2/settings" ? pending.promise : null));
    await waitFor(() =>
      expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/settings")).toBe(true),
    );
    view.unmount();
    pending.resolve(json(200, settings()));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByLabelText("网站标题")).not.toBeInTheDocument();
  });

  it("ignores an initial settings failure that lands after unmount", async () => {
    const pending = deferred<unknown>();
    const view = setup((url) => (url === "/api/v2/settings" ? pending.promise : null));
    await waitFor(() => expect(mockFetch).toHaveBeenCalledWith("/api/v2/settings", expect.anything()));
    view.unmount();
    pending.reject(new Error("晚到的读取失败"));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText("加载配置失败")).not.toBeInTheDocument();
  });
});

describe("SettingsPage 保存后的重读边界", () => {
  it("drops the success path when the post-save re-read lands after unmount", async () => {
    const reread = deferred<unknown>();
    let reads = 0;
    const view = setup((url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") return json(200, settings());
      if (url === "/api/v2/settings") {
        reads += 1;
        if (reads >= 2) return reread.promise;
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2));
    view.unmount();
    reread.resolve(json(200, settings()));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });
});
