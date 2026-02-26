import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import SettingsPage from "@/app/(authenticated)/settings/page";
import { api } from "@/lib/api";

const pushMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
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
};

describe("SettingsPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.me.mockResolvedValue(adminUser as never);
    mockApi.getConfig.mockResolvedValue(baseConfig as never);
    mockApi.getMachineStats.mockResolvedValue({
      disk_total: 100 * 1024 * 1024 * 1024,
      disk_used: 50 * 1024 * 1024 * 1024,
      disk_free: 50 * 1024 * 1024 * 1024,
      download_used: 20 * 1024 * 1024 * 1024,
      system_used: 30 * 1024 * 1024 * 1024,
    } as never);
    mockApi.getAria2Version.mockResolvedValue({
      connected: true,
      version: "1.36.0",
      enabled_features: ["BitTorrent"],
    } as never);
    mockApi.updateConfig.mockResolvedValue({} as never);
    mockApi.testAria2Connection.mockResolvedValue({
      connected: true,
      version: "1.36.0",
    } as never);
  });

  test("renders config form for admin", async () => {
    render(<SettingsPage />);

    expect(await screen.findByText("系统设置")).toBeInTheDocument();
    expect(screen.getByText("系统配置（仅管理员）")).toBeInTheDocument();
    expect(mockApi.getConfig).toHaveBeenCalled();
    expect(mockApi.getMachineStats).toHaveBeenCalled();
    expect(mockApi.getAria2Version).toHaveBeenCalled();
  });

  test("redirects non-admin and does not render admin content", async () => {
    mockApi.me.mockResolvedValue({ ...adminUser, is_admin: false } as never);

    render(<SettingsPage />);

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/tasks");
    });
    expect(screen.queryByText("系统设置")).not.toBeInTheDocument();
  });

  test("submits save and connection test", async () => {
    render(<SettingsPage />);

    expect(await screen.findByText("系统设置")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await waitFor(() => {
      expect(mockApi.testAria2Connection).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => {
      expect(mockApi.updateConfig).toHaveBeenCalled();
    });
  });

  test("shows validation message when testing empty rpc url", async () => {
    mockApi.getConfig.mockResolvedValue({
      ...baseConfig,
      aria2_rpc_url: "",
    } as never);
    render(<SettingsPage />);

    expect(await screen.findByText("系统设置")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => {
      expect(mockApi.testAria2Connection).not.toHaveBeenCalled();
    });
  });

  test("handles extension add/remove and pack format switch", async () => {
    render(<SettingsPage />);

    expect(await screen.findByText("系统设置")).toBeInTheDocument();

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
});
