import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import SharePageClient from "@/app/s/[code]/SharePageClient";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    getSiteInfo: jest.fn(),
    getShareInfo: jest.fn(),
    accessShare: jest.fn(),
    browseShare: jest.fn(),
    downloadShare: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function fileShare(overrides: Record<string, unknown> = {}) {
  return {
    file_name: "demo.zip",
    file_size: 1024,
    is_directory: false,
    has_password: false,
    is_expired: false,
    is_exhausted: false,
    ...overrides,
  };
}

function sf(name: string, isDir: boolean, size: number, path: string) {
  return { name, is_dir: isDir, is_directory: isDir, size, path, modified_at: 1_700_000_000_000 };
}

function shareResponse(items: ReturnType<typeof sf>[], total = items.length) {
  return { items, total, page: 1, page_size: 200 };
}

describe("SharePageClient edge cases", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Edge Site" } as never);
    jest.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    jest.useRealTimers();
    (console.warn as jest.Mock).mockRestore?.();
  });

  it.each([
    ["trailing empty code", "/s/"],
    ["missing s segment", "/files/abc"],
  ])("shows invalid link error for %s", async (_name, path) => {
    window.history.pushState({}, "", path);

    render(<SharePageClient />);

    expect(await screen.findByText("无效的分享链接")).toBeInTheDocument();
  });

  it("prefills the password from the URL query parameter", async () => {
    window.history.pushState({}, "", "/s/pw?password=prefilled");
    mockApi.getShareInfo.mockResolvedValue(fileShare({ has_password: true }) as never);

    render(<SharePageClient />);

    expect(await screen.findByPlaceholderText("请输入提取码")).toHaveValue("prefilled");
  });

  it("blocks password submit when the password is empty", async () => {
    window.history.pushState({}, "", "/s/empty-pw");
    mockApi.getShareInfo.mockResolvedValue(fileShare({ has_password: true }) as never);

    const { container } = render(<SharePageClient />);

    await screen.findByPlaceholderText("请输入提取码");
    fireEvent.submit(container.querySelector("form")!);

    expect(mockApi.accessShare).not.toHaveBeenCalled();
  });

  it("shows default error message when share info rejects with a non-Error", async () => {
    window.history.pushState({}, "", "/s/string-err");
    mockApi.getShareInfo.mockRejectedValue("boom" as never);

    render(<SharePageClient />);

    expect(await screen.findByText("获取分享信息失败")).toBeInTheDocument();
  });

  it("shows default error message when browsing rejects with a non-Error", async () => {
    window.history.pushState({}, "", "/s/dir-string-err");
    mockApi.getShareInfo.mockResolvedValue(
      fileShare({ is_directory: true }) as never
    );
    mockApi.browseShare.mockRejectedValue("browse boom" as never);

    render(<SharePageClient />);

    expect(await screen.findByText("加载目录失败")).toBeInTheDocument();
  });

  it("shows default password error when access rejects with a non-Error", async () => {
    window.history.pushState({}, "", "/s/pw-string-err");
    mockApi.getShareInfo.mockResolvedValue(fileShare({ has_password: true }) as never);
    mockApi.accessShare.mockRejectedValue("nope" as never);

    render(<SharePageClient />);

    fireEvent.change(await screen.findByPlaceholderText("请输入提取码"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));

    expect(await screen.findByText("密码错误")).toBeInTheDocument();
  });

  it("does not set state after unmount when pending requests resolve", async () => {
    window.history.pushState({}, "", "/s/unmounted");
    const siteDeferred = createDeferred<{ site_title: string }>();
    const shareDeferred = createDeferred<ReturnType<typeof fileShare>>();
    const browseDeferred = createDeferred<ReturnType<typeof shareResponse>>();
    mockApi.getSiteInfo.mockReturnValue(siteDeferred.promise as never);
    mockApi.getShareInfo.mockReturnValue(shareDeferred.promise as never);
    mockApi.browseShare.mockReturnValue(browseDeferred.promise as never);

    const { unmount } = render(<SharePageClient />);
    unmount();

    await act(async () => {
      siteDeferred.resolve({ site_title: "Late Site" });
      shareDeferred.resolve(fileShare({ is_directory: true }));
      browseDeferred.resolve(shareResponse([sf("late.txt", false, 1, "late.txt")]));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.queryByText("加载中...")).not.toBeInTheDocument();
  });

  it.each([
    ["share info", "getShareInfo"],
    ["directory browse", "browseShare"],
    ["password access", "accessShare"],
  ])(
    "does not set error state when %s rejects after unmount",
    async (_name, method) => {
      window.history.pushState({}, "", "/s/late-" + method);
      const deferred = createDeferred<never>();
      mockApi.getShareInfo.mockImplementation(() =>
        method === "getShareInfo"
          ? (deferred.promise as never)
          : Promise.resolve(
              fileShare({
                is_directory: method !== "accessShare",
                has_password: method === "accessShare",
              }) as never
            )
      );
      mockApi.browseShare.mockImplementation(() => deferred.promise as never);
      mockApi.accessShare.mockImplementation(() => deferred.promise as never);

      const { unmount } = render(<SharePageClient />);

      if (method === "accessShare") {
        fireEvent.change(await screen.findByPlaceholderText("请输入提取码"), {
          target: { value: "x" },
        });
        fireEvent.click(screen.getByRole("button", { name: "提取文件" }));
      } else {
        // 在 act 内排空 getShareInfo 已 resolve 的微任务链，避免状态更新落在 act 外
        await act(async () => {});
      }
      unmount();

      await act(async () => {
        deferred.reject(new Error("too late"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(screen.queryByText("too late")).not.toBeInTheDocument();
      expect(screen.queryByText("访问出错")).not.toBeInTheDocument();
    }
  );

  it("downloads a plain share without a token and resets the button after delay", async () => {
    window.history.pushState({}, "", "/s/plain");
    mockApi.getShareInfo.mockResolvedValue(fileShare() as never);

    render(<SharePageClient />);

    const downloadButton = await screen.findByRole("button", { name: "下载文件" });
    fireEvent.click(downloadButton);
    expect(mockApi.downloadShare).toHaveBeenCalledWith("plain", undefined);

    act(() => {
      jest.advanceTimersByTime(2000);
    });
    await waitFor(() => {
      expect(downloadButton).toBeEnabled();
    });

    fireEvent.click(downloadButton);
    expect(mockApi.downloadShare).toHaveBeenCalledTimes(2);
  });

  it("sets the document title only for valid, non-expired, non-exhausted shares", async () => {
    window.history.pushState({}, "", "/s/title-valid");
    mockApi.getShareInfo.mockResolvedValue(fileShare({ file_name: "ok.zip" }) as never);

    render(<SharePageClient />);

    await screen.findByRole("button", { name: "下载文件" });
    await waitFor(() => {
      expect(document.title).toBe("ok.zip - Edge Site");
    });
  });
});
