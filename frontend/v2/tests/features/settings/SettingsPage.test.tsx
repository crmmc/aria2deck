import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";

import { AppProviders } from "@/app/providers";
import * as toastContext from "@/shared/components/toast-context";
import { settingsCommands, type SettingsCommand } from "@/shared/lib/commands";
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

type SetupOptions = {
  me?: UserResponse;
  entries?: string[];
  initialIndex?: number;
};

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function HistoryControls() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate(-1)}>测试后退</button>
      <button type="button" onClick={() => navigate(1)}>测试前进</button>
    </>
  );
}

function setup(handler: Handler = () => null, options: SetupOptions = {}) {
  const entries = options.entries ?? ["/settings"];
  const me = options.me ?? ME;
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
    <MemoryRouter initialEntries={entries} initialIndex={options.initialIndex ?? entries.length - 1}>
      <AppProviders>
        <Routes>
          <Route
            path="/settings/:section?"
            element={<><SettingsPage /><LocationProbe /><HistoryControls /></>}
          />
          <Route path="/tasks" element={<div>任务页占位</div>} />
        </Routes>
      </AppProviders>
    </MemoryRouter>,
  );
}

function setupAt(path: string, handler: Handler = () => null) {
  return setup(handler, { entries: [path] });
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

function openSection(label: string) {
  fireEvent.click(screen.getByRole("button", { name: label }));
}

function openAdvanced() {
  fireEvent.click(screen.getByRole("button", { name: /系统高级设置/ }));
}

function saveBar(): HTMLElement {
  const bar = document.getElementById("save-bar");
  expect(bar).not.toBeNull();
  return bar!;
}

function patchCalls() {
  return mockFetch.mock.calls.filter(([, init]) => init?.method === "PATCH");
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
  document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  }
  if (originalClose) {
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  }
});

describe("SettingsPage 六分区与权限", () => {
  it("uses the plain glass frame, defaults to basic and mounts a clean save bar", async () => {
    const view = setup();
    expect(await screen.findByLabelText("网站标题")).toHaveValue("Aria2Deck");
    const frame = view.container.querySelector(".glass-frame");
    expect(frame).toBeInTheDocument();
    expect(frame).not.toHaveClass("full-height", "animate-in");
    expect(within(screen.getByRole("navigation", { name: "设置分区" })).getAllByRole("button")).toHaveLength(6);
    expect(screen.getByRole("button", { name: "基础设置" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "基础设置", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "aria2 后端", level: 2 })).not.toBeInTheDocument();
    expect(screen.getByText(/系统占用 300 B/)).toBeInTheDocument();
    expect(screen.getByText(/下载占用 200 B/)).toBeInTheDocument();
    expect(saveBar()).toHaveClass("clean");
    expect(screen.getByRole("button", { name: "保存配置" })).toBeDisabled();
  });

  it.each([
    ["/settings/basic", "basic", "基础设置"],
    ["/settings/aria2", "aria2", "aria2 后端"],
    ["/settings/pack", "pack", "打包设置"],
    ["/settings/realtime", "realtime", "实时连接"],
    ["/settings/tracker", "tracker", "Tracker 列表"],
    ["/settings/security", "security", "安全与限流"],
  ])("opens deep link %s as one active panel", async (path, section, heading) => {
    setupAt(path);
    expect(await screen.findByRole("heading", { name: heading, level: 2 })).toBeInTheDocument();
    expect(document.getElementById(`settings-${section}`)).toBeInTheDocument();
    expect(document.querySelectorAll("#settings-panel > fieldset > section")).toHaveLength(1);
    expect(screen.getByRole("button", { name: heading })).toHaveAttribute("aria-current", "page");
  });

  it("normalizes an invalid section to basic with a replace navigation", async () => {
    setupAt("/settings/not-a-section");
    expect(await screen.findByLabelText("网站标题")).toBeInTheDocument();
    expect(screen.getByTestId("current-path")).toHaveTextContent("/settings/basic");
    expect(screen.getByRole("button", { name: "基础设置" })).toHaveAttribute("aria-current", "page");
  });

  it("keeps one form instance and its draft across navigation, back and forward", async () => {
    setup();
    const title = await screen.findByLabelText("网站标题");
    fireEvent.change(title, { target: { value: "跨分区草稿" } });
    expect(saveBar()).not.toHaveClass("clean");

    openSection("aria2 后端");
    expect(await screen.findByLabelText("aria2 RPC URL")).toBeInTheDocument();
    openSection("打包设置");
    expect(await screen.findByRole("radiogroup", { name: "打包格式" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "测试后退" }));
    expect(await screen.findByLabelText("aria2 RPC URL")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "测试前进" }));
    expect(await screen.findByRole("radiogroup", { name: "打包格式" })).toBeInTheDocument();
    openSection("基础设置");
    expect(await screen.findByLabelText("网站标题")).toHaveValue("跨分区草稿");
    expect(saveBar()).not.toHaveClass("clean");
    expect(mockFetch.mock.calls.filter(([url, init]) =>
      url === "/api/v2/settings" && (init?.method ?? "GET") === "GET"
    )).toHaveLength(1);
  });

  it("keeps all eleven rate limits, six connection limits and credentials in security", async () => {
    setupAt("/settings/security");
    expect(await screen.findByLabelText("账户安全限流")).toHaveValue(10);
    expect(screen.getByLabelText("普通已登录 API 限流")).toHaveValue(600);
    expect(screen.queryByLabelText("创建分享限流")).not.toBeInTheDocument();
    openAdvanced();
    expect(screen.getByLabelText("创建分享限流")).toHaveValue(60);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(17);
    expect(screen.getByLabelText("系统总下载连接上限")).toHaveValue(100);
    expect(screen.getByLabelText("匿名总连接数")).toHaveValue(null);
    expect(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" })).toBeInTheDocument();
    expect(document.querySelectorAll("#settings-security")).toHaveLength(1);
  });

  it("shows the page error only when the settings read fails", async () => {
    const view = setup((url) =>
      url === "/api/v2/settings"
        ? json(500, { error: { code: "internal", message: "配置读取失败" } })
        : null,
    );
    expect(await screen.findByText("加载配置失败")).toBeInTheDocument();
    expect(view.container.querySelector(".glass-frame")).not.toHaveClass("full-height", "animate-in");
    expect(screen.queryByText("系统配置（仅管理员）")).not.toBeInTheDocument();
    expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/trackers")).toBe(false);
  });

  it("keeps the form usable and reports real unavailable states when independent reads fail", async () => {
    setup((url) => {
      if (url === "/api/v2/trackers") {
        return json(500, { error: { code: "internal", message: "Tracker 状态失败" } });
      }
      if (url === "/api/v2/stats/machine") {
        return json(500, { error: { code: "internal", message: "机器统计失败" } });
      }
      if (url === "/api/v2/settings/aria2/version") {
        return json(500, { error: { code: "internal", message: "aria2 状态失败" } });
      }
      return null;
    });
    expect(await screen.findByLabelText("网站标题")).toBeInTheDocument();
    expect(screen.queryByText("机器磁盘空间")).not.toBeInTheDocument();
    openSection("aria2 后端");
    expect(await screen.findByText("aria2 状态未知")).toBeInTheDocument();
    openSection("Tracker 列表");
    expect(await screen.findByText("Tracker 状态不可用")).toBeInTheDocument();
    expect(saveBar()).toHaveClass("clean");
  });

  it.each([
    { connected: true, heading: "aria2 已连接", detail: "版本 1.37.0" },
    { connected: false, heading: "aria2 未连接", detail: "无法连接 aria2，请检查 RPC 地址与 Secret。" },
  ])("distinguishes the HTTP 200 aria2 state $heading", async ({ connected, heading, detail }) => {
    setupAt("/settings/aria2", (url) =>
      url === "/api/v2/settings/aria2/version"
        ? json(200, { connected, version: connected ? "1.37.0" : null, features: [] })
        : null,
    );
    expect(await screen.findByText(heading)).toBeInTheDocument();
    expect(screen.getByText(detail)).toBeInTheDocument();
  });

  it("redirects regular users before every administrator request", async () => {
    setup(() => null, { me: { ...ME, is_admin: false }, entries: ["/settings/security"] });
    expect(await screen.findByText("任务页占位")).toBeInTheDocument();
    const adminRequests = mockFetch.mock.calls.map(([url]) => String(url)).filter((url) =>
      url.startsWith("/api/v2/settings")
      || url.startsWith("/api/v2/trackers")
      || url === "/api/v2/stats/machine"
      || url === "/api/v2/credentials/invalidate"
    );
    expect(adminRequests).toEqual([]);
  });

  it("keeps the save bar opaque and hides it through the clean class", () => {
    const css = readFileSync(resolve(__dirname, "../../../src/styles/legacy/06-components.css"), "utf8");
    const saveRule = css.match(/(?:^|\n)\.save-bar\s*\{([^}]*)\}/)?.[1];
    const cleanRule = css.match(/\.save-bar\.clean\s*\{([^}]*)\}/)?.[1];
    expect(saveRule).toMatch(/background:\s*#fff;/);
    expect(saveRule).toMatch(/backdrop-filter:\s*none;/);
    expect(cleanRule).toMatch(/display:\s*none;/);
  });
});

describe("SettingsPage dirty、放弃与变化项保存", () => {
  it("treats normalized numbers, tracker blank lines and a cleared mask as unchanged", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("最大任务大小"), { target: { value: "10.0" } });
    expect(saveBar()).toHaveClass("clean");

    openSection("aria2 后端");
    const secret = await screen.findByLabelText("aria2 RPC Secret");
    fireEvent.change(secret, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "显示密钥" }));
    expect(secret).toHaveAttribute("type", "text");
    expect(saveBar()).toHaveClass("clean");

    openSection("Tracker 列表");
    fireEvent.change(await screen.findByLabelText("固定 tracker 列表"), {
      target: { value: "udp://a/announce\n\n" },
    });
    expect(saveBar()).toHaveClass("clean");
    expect(screen.getByRole("button", { name: "保存配置" })).toBeDisabled();
  });

  it("discards the complete draft, restores the last snapshot and clears errors", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "放弃我" } });
    fireEvent.change(screen.getByLabelText("最大任务大小"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("最大任务大小必须是不小于 0 的数字");
    fireEvent.click(screen.getByRole("button", { name: "放弃更改" }));
    expect(screen.getByLabelText("网站标题")).toHaveValue("Aria2Deck");
    expect(screen.getByLabelText("最大任务大小")).toHaveValue(10);
    expect(screen.queryByText("最大任务大小必须是不小于 0 的数字")).not.toBeInTheDocument();
    expect(saveBar()).toHaveClass("clean");
  });

  it("validates hidden sections before saving and skips the PATCH", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("最大任务大小"), { target: { value: "" } });
    openSection("aria2 后端");
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("最大任务大小必须是不小于 0 的数字");
    expect(patchCalls()).toHaveLength(0);
    expect(saveBar()).not.toHaveClass("clean");
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });

  it.each([
    {
      section: "basic",
      heading: "基础设置",
      change: () => fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "新标题" } }),
      expected: { site_title: "新标题" },
    },
    {
      section: "aria2",
      heading: "aria2 后端",
      change: () => fireEvent.change(screen.getByLabelText("aria2 RPC Secret"), { target: { value: " typed-secret " } }),
      expected: { aria2_rpc_secret: "typed-secret" },
    },
    {
      section: "pack",
      heading: "打包设置",
      change: () => fireEvent.click(screen.getByRole("radio", { name: "TAR · Zstandard" })),
      expected: { pack_format: "tar.zst" },
    },
    {
      section: "realtime",
      heading: "实时连接",
      change: () => fireEvent.change(screen.getByLabelText("最大重连延迟数值"), { target: { value: "45" } }),
      expected: { ws_reconnect_max_delay_seconds: 45 },
    },
    {
      section: "tracker",
      heading: "Tracker 列表",
      change: () => fireEvent.change(screen.getByLabelText("固定 tracker 列表"), {
        target: { value: "udp://a/announce\nhttps://b.example/announce\n" },
      }),
      expected: { tracker_fixed_list: ["udp://a/announce", "https://b.example/announce"] },
    },
    {
      section: "security",
      heading: "安全与限流",
      change: () => fireEvent.change(screen.getByLabelText("账户安全限流"), { target: { value: "11" } }),
      expected: { account_security_rate_limit: 11 },
    },
  ])("patches only the changed top-level field in $section", async ({ section, heading, change, expected }) => {
    const bodies: unknown[] = [];
    setupAt(`/settings/${section}`, (url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") {
        bodies.push(JSON.parse(init.body ?? "{}"));
        return json(200, settings());
      }
      return null;
    });
    await screen.findByRole("heading", { name: heading, level: 2 });
    change();
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByText("配置已保存")).toBeInTheDocument();
    expect(bodies).toEqual([expected]);
    expect(saveBar()).toHaveClass("clean");
    const reads = mockFetch.mock.calls.filter(([url, init]) =>
      url === "/api/v2/settings" && (init?.method ?? "GET") === "GET"
    );
    expect(reads).toHaveLength(2);
  });

  it("submits the complete current connection map when one child changes", async () => {
    const bodies: Record<string, unknown>[] = [];
    const saved = settings({
      download_connection_limits: {
        total_connections: 100,
        authenticated_per_user_connections: 8,
        anonymous_per_ip_connections: 3,
      },
    });
    setupAt("/settings/security", (url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") {
        bodies.push(JSON.parse(init.body ?? "{}") as Record<string, unknown>);
        return json(200, saved);
      }
      if (url === "/api/v2/settings") return json(200, saved);
      return null;
    });
    await screen.findByLabelText("账户安全限流");
    openAdvanced();
    fireEvent.change(screen.getByLabelText("系统总下载连接上限"), { target: { value: "101" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByText("配置已保存");
    expect(bodies).toEqual([{
      download_connection_limits: {
        total_connections: 101,
        authenticated_per_user_connections: 8,
        anonymous_per_ip_connections: 3,
      },
    }]);
  });

  it("does not send an empty PATCH or announce success", async () => {
    setup();
    await screen.findByLabelText("网站标题");
    fireEvent.submit(screen.getByRole("form", { name: "系统设置表单" }));
    await waitFor(() => expect(screen.getByRole("form", { name: "系统设置表单" })).toHaveAttribute("aria-busy", "false"));
    expect(patchCalls()).toHaveLength(0);
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });

  it("keeps edits and dirty state when the PATCH is rejected", async () => {
    setup((url, init) =>
      url === "/api/v2/settings" && init?.method === "PATCH"
        ? json(422, { error: { code: "invalid_input", message: "保存被拒绝" } })
        : null,
    );
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "保留草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存被拒绝");
    expect(screen.getByLabelText("网站标题")).toHaveValue("保留草稿");
    expect(saveBar()).not.toHaveClass("clean");
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });

  it("uses the page error and never announces success when the post-save read fails", async () => {
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
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "新标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    expect(await screen.findByText("加载配置失败")).toBeInTheDocument();
    expect(screen.queryByText("配置已保存")).not.toBeInTheDocument();
  });

  it("adopts the successfully re-read server snapshot instead of the PATCH response", async () => {
    let reads = 0;
    setup((url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") {
        return json(200, settings({ site_title: "PATCH 响应不应直接采用" }));
      }
      if (url === "/api/v2/settings") {
        reads += 1;
        return json(200, settings({ site_title: reads === 1 ? "Aria2Deck" : "重读后的标题" }));
      }
      return null;
    });
    await screen.findByLabelText("网站标题");
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "本地标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByText("配置已保存");
    expect(screen.getByLabelText("网站标题")).toHaveValue("重读后的标题");
    expect(saveBar()).toHaveClass("clean");
  });

  it("disables editing and duplicate saves until PATCH and re-read finish", async () => {
    const patch = deferred<unknown>();
    setup((url, init) =>
      url === "/api/v2/settings" && init?.method === "PATCH" ? patch.promise : null,
    );
    const title = await screen.findByLabelText("网站标题");
    fireEvent.change(title, { target: { value: "保存中的标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    const savingButton = await screen.findByRole("button", { name: "保存中..." });
    expect(savingButton).toBeDisabled();
    expect(title).toBeDisabled();
    expect(screen.getByRole("button", { name: "放弃更改" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("form", { name: "系统设置表单" }));
    expect(patchCalls()).toHaveLength(1);

    await act(async () => patch.resolve(json(200, settings())));
    expect(await screen.findByText("配置已保存")).toBeInTheDocument();
    await waitFor(() => expect(title).not.toBeDisabled());
    expect(patchCalls()).toHaveLength(1);
  });
});

describe("SettingsPage aria2 连接测试", () => {
  it("tests the unsaved URL without a masked secret and preserves dirty state", async () => {
    const bodies: unknown[] = [];
    setupAt("/settings/aria2", (url, init) => {
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
    expect(screen.getByText("aria2 版本: 1.38.0")).toBeInTheDocument();
    expect(bodies[0]).toEqual({ aria2_rpc_url: "http://127.0.0.1:6900/rpc" });
    expect(saveBar()).not.toHaveClass("clean");
  });

  it("sends a typed secret with the test request", async () => {
    const bodies: unknown[] = [];
    setupAt("/settings/aria2", (url, init) => {
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
    expect(bodies[0]).toEqual({
      aria2_rpc_url: "http://127.0.0.1:6800/rpc",
      aria2_rpc_secret: "s3cret",
    });
  });

  it("requires an RPC URL before testing", async () => {
    setupAt("/settings/aria2", (url) =>
      url === "/api/v2/settings" ? json(200, settings({ aria2_rpc_url: "" })) : null,
    );
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("测试结果：连接失败")).toBeInTheDocument();
    expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/settings/aria2/test")).toBe(false);
    expect(saveBar()).toHaveClass("clean");
  });

  it("separates transport failure from an HTTP 200 disconnected result", async () => {
    setupAt("/settings/aria2", (url, init) =>
      url === "/api/v2/settings/aria2/test" && init?.method === "POST"
        ? json(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后重试" } })
        : null,
    );
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接测试请求失败")).toBeInTheDocument();
    expect(screen.getAllByText("请求过于频繁，请稍后重试")).toHaveLength(2);
    expect(screen.queryByText("测试结果：连接失败")).not.toBeInTheDocument();
  });
});

describe("SettingsPage Tracker 与凭证独立操作", () => {
  it("shows real partial status and failed sources", async () => {
    setupAt("/settings/tracker", (url) =>
      url === "/api/v2/trackers"
        ? json(200, tracker({ refresh_status: "partial", failed_urls: ["https://bad.example/list"] }))
        : null,
    );
    expect(await screen.findByText("当前共 1 条 Tracker")).toBeInTheDocument();
    expect(screen.getByText(/部分失败（失败源 1 个）/)).toBeInTheDocument();
    expect(screen.getByText("失败源：https://bad.example/list")).toBeInTheDocument();
  });

  it("refreshes tracker status without changing dirty", async () => {
    setupAt("/settings/tracker", (url, init) =>
      url === "/api/v2/trackers/refresh" && init?.method === "POST"
        ? json(200, tracker({ entry_count: 42, refresh_status: "success" }))
        : null,
    );
    await screen.findByText("当前共 1 条 Tracker");
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    expect(await screen.findByText("tracker 列表已刷新")).toBeInTheDocument();
    expect(screen.getByText("当前共 42 条 Tracker")).toBeInTheDocument();
    expect(saveBar()).toHaveClass("clean");
  });

  it("treats a fully failed refresh as an error", async () => {
    setupAt("/settings/tracker", (url, init) =>
      url === "/api/v2/trackers/refresh" && init?.method === "POST"
        ? json(200, tracker({ refresh_status: "failed", failed_urls: ["https://bad.example"] }))
        : null,
    );
    await screen.findByText("当前共 1 条 Tracker");
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    expect(await screen.findByText("刷新 tracker 列表失败：全部远程源不可用")).toBeInTheDocument();
  });

  it("surfaces a rejected refresh request", async () => {
    setupAt("/settings/tracker", (url, init) =>
      url === "/api/v2/trackers/refresh" && init?.method === "POST"
        ? json(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后重试" } })
        : null,
    );
    await screen.findByText("当前共 1 条 Tracker");
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    expect(await screen.findByText("刷新 tracker 列表失败：请求过于频繁，请稍后重试")).toBeInTheDocument();
  });

  it("confirms credentials first, sends the fixed string and reports real counts", async () => {
    setupAt("/settings/security", (url, init) =>
      url === "/api/v2/credentials/invalidate" && init?.method === "POST"
        ? json(200, { ok: true, api_token_count: 2, rpc_secret_count: 1 })
        : null,
    );
    await screen.findByLabelText("账户安全限流");
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    expect(within(dialog).getByText(/此操作不可恢复/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
    expect(await screen.findByText("已作废 2 个 API Token、1 个 RPC Secret")).toBeInTheDocument();
    const call = mockFetch.mock.calls.find(([url, init]) =>
      url === "/api/v2/credentials/invalidate" && init?.method === "POST"
    );
    expect(JSON.parse(call![1].body as string)).toEqual({
      confirmation: "INVALIDATE_ALL_CREDENTIALS",
    });
    expect(saveBar()).toHaveClass("clean");
  });

  it("does nothing when credential confirmation is cancelled", async () => {
    setupAt("/settings/security");
    await screen.findByLabelText("账户安全限流");
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/credentials/invalidate")).toBe(false);
  });

  it("toasts the real credential failure", async () => {
    setupAt("/settings/security", (url, init) =>
      url === "/api/v2/credentials/invalidate" && init?.method === "POST"
        ? json(422, { error: { code: "invalid_input", message: "凭证作废确认字符串不正确" } })
        : null,
    );
    await screen.findByLabelText("账户安全限流");
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "作废全部凭证" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认作废" }));
    expect(await screen.findByText("作废失败：凭证作废确认字符串不正确")).toBeInTheDocument();
  });
});

describe("SettingsPage 卸载保护", () => {
  async function startDelayedWrites() {
    await screen.findByLabelText("aria2 RPC URL");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    openSection("Tracker 列表");
    await screen.findByRole("button", { name: "立即刷新" });
    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    openSection("安全与限流");
    await screen.findByRole("button", { name: "作废全部 API Token 与 RPC Secret" });
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) =>
      url === "/api/v2/credentials/invalidate"
    )).toBe(true));
    openSection("基础设置");
    const title = await screen.findByLabelText("网站标题");
    fireEvent.change(title, { target: { value: "卸载前草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(patchCalls()).toHaveLength(1));
  }

  it("ignores successful write responses that land after unmount", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn().mockResolvedValue(true),
    });
    const patch = deferred<unknown>();
    const test = deferred<unknown>();
    const invalidate = deferred<unknown>();
    const refresh = deferred<unknown>();
    const view = setupAt("/settings/aria2", (url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") return patch.promise;
      if (url === "/api/v2/settings/aria2/test") return test.promise;
      if (url === "/api/v2/credentials/invalidate") return invalidate.promise;
      if (url === "/api/v2/trackers/refresh") return refresh.promise;
      return null;
    });
    await startDelayedWrites();
    view.unmount();
    patch.resolve(json(200, settings()));
    test.resolve(json(200, { connected: true, version: "1.37.0", features: [] }));
    invalidate.resolve(json(200, { ok: true, api_token_count: 1, rpc_secret_count: 1 }));
    refresh.resolve(json(200, tracker()));
    await act(async () => new Promise((done) => setTimeout(done, 0)));
    expect(showToast).not.toHaveBeenCalled();
    useToastSpy.mockRestore();
  });

  it("ignores write failures that land after unmount", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn().mockResolvedValue(true),
    });
    const patch = deferred<unknown>();
    const test = deferred<unknown>();
    const invalidate = deferred<unknown>();
    const refresh = deferred<unknown>();
    const view = setupAt("/settings/aria2", (url, init) => {
      if (url === "/api/v2/settings" && init?.method === "PATCH") return patch.promise;
      if (url === "/api/v2/settings/aria2/test") return test.promise;
      if (url === "/api/v2/credentials/invalidate") return invalidate.promise;
      if (url === "/api/v2/trackers/refresh") return refresh.promise;
      return null;
    });
    await startDelayedWrites();
    view.unmount();
    patch.reject(new Error("晚到的保存失败"));
    test.reject(new Error("晚到的测试失败"));
    invalidate.reject(new Error("晚到的作废失败"));
    refresh.reject(new Error("晚到的刷新失败"));
    await act(async () => new Promise((done) => setTimeout(done, 0)));
    expect(showToast).not.toHaveBeenCalled();
    useToastSpy.mockRestore();
  });

  it("ignores an initial settings read that resolves after unmount", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn().mockResolvedValue(true),
    });
    const pending = deferred<unknown>();
    const view = setup((url) => url === "/api/v2/settings" ? pending.promise : null);
    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/settings")).toBe(true));
    const callsBeforeResolve = mockFetch.mock.calls.length;
    view.unmount();
    pending.resolve(json(200, settings()));
    await act(async () => new Promise((done) => setTimeout(done, 0)));
    expect(showToast).not.toHaveBeenCalled();
    expect(mockFetch.mock.calls).toHaveLength(callsBeforeResolve);
    useToastSpy.mockRestore();
  });

  it("ignores an initial settings failure that lands after unmount", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn().mockResolvedValue(true),
    });
    const pending = deferred<unknown>();
    const view = setup((url) => url === "/api/v2/settings" ? pending.promise : null);
    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/settings")).toBe(true));
    view.unmount();
    pending.reject(new Error("晚到的读取失败"));
    await act(async () => new Promise((done) => setTimeout(done, 0)));
    expect(showToast).not.toHaveBeenCalled();
    useToastSpy.mockRestore();
  });

  it("drops the success path when the post-save re-read lands after unmount", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn().mockResolvedValue(true),
    });
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
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "新标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2));
    const callsBeforeResolve = mockFetch.mock.calls.length;
    view.unmount();
    reread.resolve(json(200, settings()));
    await act(async () => new Promise((done) => setTimeout(done, 0)));
    expect(showToast).not.toHaveBeenCalled();
    expect(mockFetch.mock.calls).toHaveLength(callsBeforeResolve);
    useToastSpy.mockRestore();
  });
});

describe("SettingsPage 快捷操作", () => {
  it.each([
    ["basic", "settings-basic"],
    ["realtime", "settings-realtime"],
    ["security", "settings-security"],
  ] as [SettingsCommand, string][])("%s navigates and focuses without scrolling", async (section, targetId) => {
    setupAt("/settings/aria2");
    await screen.findByLabelText("aria2 RPC URL");
    const scrollMock = jest.fn();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollMock,
    });
    try {
      act(() => settingsCommands.request(section));
      await waitFor(() => expect(document.getElementById(targetId)).toBeInTheDocument());
      expect(screen.getByTestId("current-path")).toHaveTextContent(`/settings/${section}`);
      expect(document.getElementById(targetId)).toHaveFocus();
      expect(scrollMock).not.toHaveBeenCalled();
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
          configurable: true,
          value: originalScrollIntoView,
        });
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
      }
    }
  });
});
