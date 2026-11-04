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

  test("listShares failure shows error toast", async () => {
    mockApi.listShares.mockRejectedValue(new Error("boom"));

    render(<SharesPage />);

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("加载分享记录失败", "error");
    });
  });

  test("revoke share succeeds, toasts and reloads", async () => {
    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "失效" }));

    await waitFor(() => {
      expect(mockApi.revokeShare).toHaveBeenCalledWith(1);
      expect(showToastMock).toHaveBeenCalledWith("分享已失效", "success");
    });
    await waitFor(() => {
      expect(mockApi.listShares).toHaveBeenCalledTimes(2);
    });
  });

  test("revoke share failure shows error toast", async () => {
    mockApi.revokeShare.mockRejectedValue(new Error("网络错误"));

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "失效" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("操作失败：网络错误", "error");
    });
  });

  test("single delete with failed item shows error from results", async () => {
    mockApi.deleteShares.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [{ id: 1, ok: false, error: "文件不存在" }],
    } as never);

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("删除失败：文件不存在", "error");
    });
  });

  test("single delete rejection shows error toast", async () => {
    mockApi.deleteShares.mockRejectedValue(new Error("服务不可用"));

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("删除失败：服务不可用", "error");
    });
  });

  test("revoke all failure shows error toast", async () => {
    mockApi.revokeAllShares.mockRejectedValue(new Error("超时"));

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("操作失败：超时", "error");
    });
  });

  test("toggling a record checkbox selects it", async () => {
    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "选择分享 demo.zip" }));

    expect(screen.getByText("已选 1 项")).toBeInTheDocument();
  });

  test("select all twice clears the selection", async () => {
    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(screen.getByText("已选 1 项")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.queryByText(/已选 1 项/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
  });

  test("batch delete rejection shows error toast", async () => {
    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    mockApi.deleteShares.mockRejectedValue(new Error("服务器错误"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("部分删除失败：服务器错误", "error");
    });
  });

  test.each([
    "active",
    "revoked",
  ] as const)(
    "filter status %s via the select filters records",
    async (status: string) => {
      mockApi.listShares.mockResolvedValue([
        share,
        { ...share, id: 2, share_code: "rev002", file_name: "gone.zip", status: "revoked" },
      ] as never);

      render(<SharesPage />);

      expect(await screen.findByText("demo.zip")).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("分享状态筛选"), {
        target: { value: status },
      });

      if (status === "active") {
        expect(screen.getByText("demo.zip")).toBeInTheDocument();
        expect(screen.queryByText("gone.zip")).not.toBeInTheDocument();
      } else {
        expect(screen.queryByText("demo.zip")).not.toBeInTheDocument();
        expect(screen.getByText("gone.zip")).toBeInTheDocument();
      }
    }
  );

  test("copy link with password appends encoded password", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    mockApi.listShares.mockResolvedValue([
      { ...share, password: "p@ss 字" },
    ] as never);

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        `${window.location.origin}/s/abc123?password=${encodeURIComponent("p@ss 字")}`
      );
    });
  });

  test("copy link without password copies the bare link", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/abc123`);
    });
  });

  test.each([
    ["single delete", "删除"],
    ["revoke all", "一键失效全部"],
    ["batch delete", "删除选中"],
  ] as const)("%s cancelled confirmation performs no request", async (_name, buttonName) => {
    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    if (buttonName === "删除选中") {
      fireEvent.click(screen.getByRole("button", { name: "全选" }));
    }

    showConfirmMock.mockResolvedValueOnce(false);
    fireEvent.click(screen.getByRole("button", { name: buttonName }));

    // 确认被取消时不发起任何请求
    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
    });
    expect(mockApi.deleteShares).not.toHaveBeenCalled();
    expect(mockApi.revokeAllShares).not.toHaveBeenCalled();
  });

  test("single delete with empty results falls back to 未知错误", async () => {
    mockApi.deleteShares.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [],
    } as never);

    render(<SharesPage />);

    expect(await screen.findByText("demo.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("删除失败：未知错误", "error");
    });
  });

  test("unmounting during load skips the toast", async () => {
    let resolveList: (value: unknown) => void = () => {};
    mockApi.listShares.mockImplementation(
      () => new Promise((resolve) => { resolveList = resolve; }) as never
    );

    const { unmount } = render(<SharesPage />);
    unmount();
    resolveList(Promise.reject(new Error("late")));

    await Promise.resolve();
    expect(showToastMock).not.toHaveBeenCalledWith("加载分享记录失败", "error");
  });
});
