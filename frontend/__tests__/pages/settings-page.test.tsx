import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import SettingsPage from "@/app/(authenticated)/settings/page";
import { api } from "@/lib/api";

const pushMock = jest.fn();
const routerMock = { push: pushMock };

jest.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    me: jest.fn(),
    getConfig: jest.fn(),
    getMachineStats: jest.fn(),
    getAria2Version: jest.fn(),
    updateConfig: jest.fn(),
    testAria2Connection: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const adminUser = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota: 1024 * 1024 * 1024,
  is_initial_password: false,
};

const baseConfig = {
  max_task_size: 1024 * 1024 * 1024,
  min_free_disk: 1024 * 1024 * 1024,
  aria2_rpc_url: "http://localhost:6800/jsonrpc",
  aria2_rpc_secret: "secret",
  hidden_file_extensions: [".aria2"],
  pack_format: "zip",
  pack_compression_level: 5,
  ws_reconnect_max_delay: 30,
  ws_reconnect_jitter: 0.2,
  ws_reconnect_factor: 2,
  site_title: "Aria2 控制器",
  rate_limit_account_security: 5,
  rate_limit_authenticated_api: 60,
  rate_limit_public_api: 60,
  rate_limit_share_access: 5,
  rate_limit_authenticated_download: 300,
  rate_limit_anonymous_download: 60,
  rate_limit_create_task: 30,
  rate_limit_create_torrent: 20,
  rate_limit_create_pack: 5,
  rate_limit_aria2_test: 10,
  rate_limit_rpc: 300,
  download_total_connections: 100,
  download_authenticated_reserved_connections: 60,
  download_authenticated_per_user_connections: 16,
  download_authenticated_per_file_connections: 8,
  download_anonymous_base_connections: 20,
  download_anonymous_borrow_connections: 20,
  download_anonymous_per_ip_connections: 4,
  download_anonymous_per_file_connections: 2,
};

const baseMachineStats = {
  disk_total: 100 * 1024 * 1024 * 1024,
  disk_used: 50 * 1024 * 1024 * 1024,
  disk_free: 50 * 1024 * 1024 * 1024,
  download_used: 20 * 1024 * 1024 * 1024,
  system_used: 30 * 1024 * 1024 * 1024,
};

const baseAria2Version = {
  connected: true,
  version: "1.36.0",
  enabled_features: ["BitTorrent"],
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface InitialLoadOptions {
  config?: typeof baseConfig;
  configError?: Error;
  meError?: Error;
  user?: typeof adminUser;
}

async function renderWithInitialLoad(options: InitialLoadOptions = {}) {
  const meDeferred = createDeferred<typeof adminUser>();
  const configDeferred = createDeferred<typeof baseConfig>();
  const statsDeferred = createDeferred<typeof baseMachineStats>();
  const versionDeferred = createDeferred<typeof baseAria2Version>();
  const user = options.user ?? adminUser;

  mockApi.me.mockReturnValue(meDeferred.promise as never);
  mockApi.getConfig.mockReturnValue(configDeferred.promise as never);
  mockApi.getMachineStats.mockReturnValue(statsDeferred.promise as never);
  mockApi.getAria2Version.mockReturnValue(versionDeferred.promise as never);

  await act(async () => {
    render(<SettingsPage />);
  });

  await act(async () => {
    if (options.meError) {
      meDeferred.reject(options.meError);
    } else {
      meDeferred.resolve(user);
    }
    await meDeferred.promise.catch(() => undefined);
    await Promise.resolve();
  });

  if (options.meError || !user.is_admin) {
    await act(async () => {
      await Promise.resolve();
    });
    return;
  }

  await act(async () => {
    if (options.configError) {
      configDeferred.reject(options.configError);
    } else {
      configDeferred.resolve(options.config ?? baseConfig);
    }
    statsDeferred.resolve(baseMachineStats);
    versionDeferred.resolve(baseAria2Version);
    await Promise.allSettled([
      configDeferred.promise,
      statsDeferred.promise,
      versionDeferred.promise,
    ]);
    await Promise.resolve();
  });

  await act(async () => {
    await Promise.resolve();
  });
}

describe("SettingsPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.updateConfig.mockResolvedValue({} as never);
    mockApi.testAria2Connection.mockResolvedValue({
      connected: true,
      version: "1.36.0",
    } as never);
  });

  afterEach(() => {
    jest.clearAllTimers();
  });

  test("renders config form for admin", async () => {
    await renderWithInitialLoad();

    expect(screen.getByText("系统设置")).toBeInTheDocument();
    expect(screen.getByText("系统配置（仅管理员）")).toBeInTheDocument();
    expect(mockApi.getConfig).toHaveBeenCalled();
    expect(mockApi.getMachineStats).toHaveBeenCalled();
    expect(mockApi.getAria2Version).toHaveBeenCalled();
  });

  test("redirects non-admin and does not render admin content", async () => {
    await renderWithInitialLoad({ user: { ...adminUser, is_admin: false } });

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/tasks");
    });
    expect(screen.queryByText("系统设置")).not.toBeInTheDocument();
  });

  test("submits save and connection test", async () => {
    await renderWithInitialLoad();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    });
    await waitFor(() => {
      expect(mockApi.testAria2Connection).toHaveBeenCalled();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    });
    await waitFor(() => {
      expect(mockApi.updateConfig).toHaveBeenCalled();
    });
  });

  test("shows validation message when testing empty rpc url", async () => {
    await renderWithInitialLoad({ config: { ...baseConfig, aria2_rpc_url: "" } });

    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => {
      expect(mockApi.testAria2Connection).not.toHaveBeenCalled();
    });
  });

  test("shows connection error message when test api rejects", async () => {
    await renderWithInitialLoad();
    mockApi.testAria2Connection.mockRejectedValueOnce(new Error("rpc down") as never);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    });

    expect(await screen.findByText("测试结果：连接失败")).toBeInTheDocument();
    expect(screen.getByText("rpc down")).toBeInTheDocument();
  });

  test("submits undefined secret when masked secret value is kept", async () => {
    await renderWithInitialLoad();

    fireEvent.change(screen.getByPlaceholderText("留空表示无密钥"), {
      target: { value: "***" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => {
      expect(mockApi.updateConfig).toHaveBeenCalled();
    });

    const lastCall = mockApi.updateConfig.mock.calls.at(-1)?.[0];
    expect(lastCall?.aria2_rpc_secret).toBeUndefined();
  });

  test.each([
    {
      index: 0,
      message: "最大任务大小必须为正数",
    },
    {
      index: 1,
      message: "最小剩余磁盘空间必须为正数",
    },
  ])("shows save error and blocks submit when numeric field $index is invalid", async ({ index, message }) => {
    await renderWithInitialLoad();
    const numberInputs = screen.getAllByRole("spinbutton");
    fireEvent.change(numberInputs[index], { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(mockApi.updateConfig).not.toHaveBeenCalled();
  });

  test("handles extension add/remove and pack format switch", async () => {
    await renderWithInitialLoad();

    const extensionInput = screen.getByPlaceholderText("输入后缀名，按回车添加");
    const addButton = screen.getByRole("button", { name: "添加" });

    fireEvent.change(extensionInput, { target: { value: "tmp" } });
    fireEvent.click(addButton);
    expect(screen.getAllByText(".tmp", { selector: ".chip span" }).length).toBe(1);

    fireEvent.change(extensionInput, { target: { value: "tmp" } });
    fireEvent.keyDown(extensionInput, { key: "Enter", code: "Enter" });
    expect(screen.getAllByText(".tmp", { selector: ".chip span" }).length).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: ".part" }));
    fireEvent.click(screen.getByRole("button", { name: ".part" }));
    expect(screen.getAllByText(".part", { selector: ".chip span" }).length).toBe(1);

    const tmpChip = screen.getByText(".tmp", { selector: ".chip span" }).closest(".chip");
    expect(tmpChip).not.toBeNull();
    fireEvent.click(within(tmpChip as HTMLElement).getByRole("button"));
    expect(screen.queryByText(".tmp", { selector: ".chip span" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "TAR + Zstandard" }));
    expect(
      screen.getByText("TAR+Zstandard: 0-9 会映射到 zstd 速度/压缩率档位"),
    ).toBeInTheDocument();
  });

  test("shows page error when initial config loading fails", async () => {
    await renderWithInitialLoad({ configError: new Error("load failed") });

    expect(await screen.findByText("加载配置失败")).toBeInTheDocument();
    expect(screen.queryByText("系统设置")).not.toBeInTheDocument();
  });

  test("does not show save success when reload fails after save", async () => {
    await renderWithInitialLoad();
    mockApi.getConfig.mockRejectedValueOnce(new Error("reload failed") as never);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    });

    expect(screen.queryByText("✓ 配置已保存")).not.toBeInTheDocument();
    expect(await screen.findByText("加载配置失败")).toBeInTheDocument();
  });
});
