import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import SharePageClient from "@/app/s/[code]/SharePageClient";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    getSiteInfo: jest.fn(),
    getShareInfo: jest.fn(),
    accessShare: jest.fn(),
    browseShare: jest.fn(),
    shareDownloadUrl: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

describe("SharePageClient", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Share Site" } as never);
    mockApi.shareDownloadUrl.mockReturnValue("http://localhost/download");
    jest.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    (console.warn as jest.Mock).mockRestore?.();
  });

  test("shows invalid link error for placeholder code", async () => {
    window.history.pushState({}, "", "/s/_");

    render(<SharePageClient />);

    expect(await screen.findByText("访问出错")).toBeInTheDocument();
    expect(screen.getByText("无效的分享链接")).toBeInTheDocument();
  });

  test("unlocks password-protected share", async () => {
    window.history.pushState({}, "", "/s/abc123");
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "demo.zip",
      file_size: 1024,
      is_directory: false,
      has_password: true,
      is_expired: false,
      is_exhausted: false,
    } as never);
    mockApi.accessShare.mockResolvedValue({ access_token: "token-1" } as never);

    render(<SharePageClient />);

    expect(await screen.findByPlaceholderText("请输入提取码")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("请输入提取码"), {
      target: { value: "1234" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));

    await waitFor(() => {
      expect(mockApi.accessShare).toHaveBeenCalledWith("abc123", "1234");
    });
    expect(await screen.findByRole("button", { name: "下载文件" })).toBeInTheDocument();
  });

  test("loads directory items for non-password share", async () => {
    window.history.pushState({}, "", "/s/folder1");
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "folder",
      file_size: 0,
      is_directory: true,
      has_password: false,
      is_expired: false,
      is_exhausted: false,
    } as never);
    mockApi.browseShare.mockResolvedValue([
      { name: "sub.txt", is_dir: false, size: 12, path: "sub.txt" },
      { name: "nested", is_dir: true, size: 0, path: "nested" },
    ] as never);

    render(<SharePageClient />);

    expect(await screen.findByText("sub.txt")).toBeInTheDocument();
    expect(screen.getByText("nested")).toBeInTheDocument();
  });
});
