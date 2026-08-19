import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import SharesPage from "@/app/(authenticated)/shares/page";
import { api } from "@/lib/api";

const showToastMock = jest.fn();
const showConfirmMock = jest.fn();

jest.mock("@/components/Toast", () => ({
  __esModule: true,
  useToast: () => ({
    showToast: showToastMock,
    showConfirm: showConfirmMock,
  }),
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listShares: jest.fn(),
    revokeShare: jest.fn(),
    deleteShares: jest.fn(),
    revokeAllShares: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const share = {
  id: 1,
  share_code: "abc123",
  file_name: "demo.zip",
  file_size: 1024,
  has_password: false,
  expires_at: null,
  max_downloads: null,
  download_count: 0,
  status: "active" as const,
  created_at: "2024-01-01T00:00:00Z",
  last_accessed_at: null,
};

describe("SharesPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    showConfirmMock.mockResolvedValue(true);
    mockApi.listShares.mockResolvedValue([share] as never);
    mockApi.revokeAllShares.mockResolvedValue({ ok: true, count: 1 } as never);
    mockApi.revokeShare.mockResolvedValue({ ok: true } as never);
    mockApi.deleteShares.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [],
    } as never);
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockResolvedValue(undefined),
      },
    });
  });

  test("renders share list and bulk revoke", async () => {
    render(<SharesPage />);

    expect(await screen.findByText("分享管理")).toBeInTheDocument();
    expect(screen.getByText("demo.zip")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));
    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
      expect(mockApi.revokeAllShares).toHaveBeenCalled();
    });
  });

  test("uses a native card copy button without nesting other controls", async () => {
    render(<SharesPage />);

    const copyCardButton = await screen.findByRole("button", {
      name: "打开分享 demo.zip",
    });

    expect(copyCardButton.tagName).toBe("BUTTON");
    expect(
      within(copyCardButton).queryByRole("checkbox", { name: "选择分享 demo.zip" })
    ).not.toBeInTheDocument();
    expect(
      within(copyCardButton).queryByRole("button", { name: "复制链接" })
    ).not.toBeInTheDocument();
    expect(
      within(copyCardButton).queryByRole("button", { name: "失效" })
    ).not.toBeInTheDocument();
    expect(
      within(copyCardButton).queryByRole("button", { name: "删除" })
    ).not.toBeInTheDocument();
  });

  test("deletes a share after confirmation via a single-element batch", async () => {
    render(<SharesPage />);

    expect(await screen.findByText("分享管理")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(mockApi.deleteShares).toHaveBeenCalledTimes(1);
      expect(mockApi.deleteShares).toHaveBeenCalledWith([1]);
    });
  });

  test("batch delete sends a single batch request and shows accepted count", async () => {
    mockApi.listShares.mockResolvedValue([
      share,
      { ...share, id: 2, share_code: "def456", file_name: "other.zip" },
    ] as never);
    mockApi.deleteShares.mockResolvedValue({
      accepted_count: 2,
      failed_count: 0,
      results: [],
    } as never);

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));

    await waitFor(() => {
      expect(mockApi.deleteShares).toHaveBeenCalledTimes(1);
      expect(mockApi.deleteShares).toHaveBeenCalledWith([1, 2]);
    });
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "已删除 2 条分享记录",
        "success"
      );
    });
  });

  test("batch delete shows warning toast on partial failure", async () => {
    mockApi.listShares.mockResolvedValue([
      share,
      { ...share, id: 2, share_code: "def456", file_name: "other.zip" },
    ] as never);
    mockApi.deleteShares.mockResolvedValue({
      accepted_count: 1,
      failed_count: 1,
      results: [],
    } as never);

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "已删除 1 条分享记录，1 条删除失败",
        "warning"
      );
    });
  });

  test("filters shares and select all only selects visible records", async () => {
    mockApi.listShares.mockResolvedValue([
      share,
      { ...share, id: 2, file_name: "movie.mkv", share_code: "movie1" },
    ] as never);

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "movie" } });
    fireEvent.click(screen.getByRole("button", { name: "全选" }));

    expect(screen.getByText("已选 1 项")).toBeInTheDocument();
  });

  test("renders expired and revoked statuses", async () => {
    mockApi.listShares.mockResolvedValue([
      {
        ...share,
        id: 2,
        share_code: "exp001",
        expires_at: "2000-01-01T00:00:00Z",
      },
      {
        ...share,
        id: 3,
        share_code: "rev001",
        status: "revoked",
      },
    ] as never);

    render(<SharesPage />);

    expect(await screen.findByText("分享管理")).toBeInTheDocument();
    expect(screen.getAllByText("已过期").length).toBeGreaterThan(0);
    expect(screen.getAllByText("已失效").length).toBeGreaterThan(0);
  });
});
