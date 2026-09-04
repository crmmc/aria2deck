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

function sf(name: string, isDir: boolean, size: number, path: string) {
  return { name, is_dir: isDir, is_directory: isDir, size, path, modified_at: 1_700_000_000_000 };
}

function shareResponse(items: ReturnType<typeof sf>[], total = items.length) {
  return { items, total, page: 1, page_size: 200 };
}

describe("SharePageClient", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Share Site" } as never);
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
    fireEvent.click(await screen.findByRole("button", { name: "下载文件" }));
    expect(mockApi.downloadShare).toHaveBeenCalledWith("abc123", "token-1");
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
    mockApi.browseShare.mockResolvedValue(
      shareResponse([sf("sub.txt", false, 12, "sub.txt"), sf("nested", true, 0, "nested")]) as never
    );

    render(<SharePageClient />);

    expect(await screen.findByText("sub.txt")).toBeInTheDocument();
    expect(screen.getByText("nested")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /nested/ }).tagName).toBe("BUTTON");
  });

  test("shows error when loading share info fails", async () => {
    window.history.pushState({}, "", "/s/notfound");
    mockApi.getShareInfo.mockRejectedValue(new Error("share not found") as never);

    render(<SharePageClient />);

    expect(await screen.findByText("访问出错")).toBeInTheDocument();
    expect(screen.getByText("share not found")).toBeInTheDocument();
  });

  test("shows directory error when browsing folder fails", async () => {
    window.history.pushState({}, "", "/s/folder2");
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "folder",
      file_size: 0,
      is_directory: true,
      has_password: false,
      is_expired: false,
      is_exhausted: false,
    } as never);
    mockApi.browseShare.mockRejectedValue(new Error("browse failed") as never);

    render(<SharePageClient />);

    expect(await screen.findByText("browse failed")).toBeInTheDocument();
  });

  test("shows password error when access share fails", async () => {
    window.history.pushState({}, "", "/s/protected");
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "demo.zip",
      file_size: 1024,
      is_directory: false,
      has_password: true,
      is_expired: false,
      is_exhausted: false,
    } as never);
    mockApi.accessShare.mockRejectedValue(new Error("wrong password") as never);

    render(<SharePageClient />);

    fireEvent.change(await screen.findByPlaceholderText("请输入提取码"), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));

    expect(await screen.findByText("wrong password")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("请输入提取码")).toBeInTheDocument();
  });

  test("renders expired and exhausted share states", async () => {
    window.history.pushState({}, "", "/s/expired1");
    mockApi.getShareInfo.mockResolvedValueOnce({
      file_name: "demo.zip",
      file_size: 1024,
      is_directory: false,
      has_password: false,
      is_expired: true,
      is_exhausted: false,
    } as never);

    const { unmount } = render(<SharePageClient />);
    expect(await screen.findByText("该分享已失效")).toBeInTheDocument();
    unmount();

    window.history.pushState({}, "", "/s/exhausted1");
    mockApi.getShareInfo.mockResolvedValueOnce({
      file_name: "demo.zip",
      file_size: 1024,
      is_directory: false,
      has_password: false,
      is_expired: false,
      is_exhausted: true,
    } as never);

    render(<SharePageClient />);
    expect(await screen.findByText("下载次数已用完")).toBeInTheDocument();
  });

  test("shows directory loading state, supports item download and go back", async () => {
    window.history.pushState({}, "", "/s/dirnav");
    const firstBrowseDeferred = createDeferred<ReturnType<typeof shareResponse>>();

    mockApi.getShareInfo.mockResolvedValue({
      file_name: "folder",
      file_size: 0,
      is_directory: true,
      has_password: false,
      is_expired: false,
      is_exhausted: false,
    } as never);
    mockApi.browseShare
      .mockReturnValueOnce(firstBrowseDeferred.promise as never)
      .mockResolvedValueOnce(
        shareResponse([sf("inside.txt", false, 22, "nested/inside.txt")]) as never
      )
      .mockResolvedValueOnce(
        shareResponse([sf("root.txt", false, 10, "root.txt"), sf("nested", true, 0, "nested")]) as never
      );

    render(<SharePageClient />);

    expect(await screen.findByText("加载目录中...")).toBeInTheDocument();

    firstBrowseDeferred.resolve(
      shareResponse([sf("root.txt", false, 10, "root.txt"), sf("nested", true, 0, "nested")])
    );

    expect(await screen.findByText("nested")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载" }));
    expect(mockApi.downloadShare).toHaveBeenCalledWith("dirnav", undefined, "root.txt");

    fireEvent.click(screen.getByText("nested"));
    expect(await screen.findByRole("button", { name: "↵ 返回上级" })).toBeInTheDocument();
    expect(mockApi.browseShare).toHaveBeenLastCalledWith("dirnav", undefined, "nested", 1, 200);
    fireEvent.click(screen.getByRole("button", { name: "↵ 返回上级" }));
    expect(await screen.findByText("root.txt")).toBeInTheDocument();
    expect(mockApi.browseShare).toHaveBeenLastCalledWith("dirnav", undefined, undefined, 1, 200);
  });

  test("uses access token when browsing and downloading an unlocked directory", async () => {
    window.history.pushState({}, "", "/s/secret-folder");
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "secret",
      file_size: 0,
      is_directory: true,
      has_password: true,
      is_expired: false,
      is_exhausted: false,
    } as never);
    mockApi.accessShare.mockResolvedValue({ access_token: "dir-token" } as never);
    mockApi.browseShare
      .mockResolvedValueOnce(
        shareResponse([sf("nested", true, 0, "nested"), sf("locked.txt", false, 42, "locked.txt")]) as never
      )
      .mockResolvedValueOnce(
        shareResponse([sf("inside.txt", false, 10, "nested/inside.txt")]) as never
      );

    render(<SharePageClient />);

    fireEvent.change(await screen.findByPlaceholderText("请输入提取码"), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));

    expect(await screen.findByText("locked.txt")).toBeInTheDocument();
    expect(mockApi.accessShare).toHaveBeenCalledWith("secret-folder", "secret");
    expect(mockApi.browseShare).toHaveBeenCalledWith("secret-folder", "dir-token", undefined, 1, 200);

    fireEvent.click(screen.getByRole("button", { name: "下载" }));
    expect(mockApi.downloadShare).toHaveBeenCalledWith(
      "secret-folder",
      "dir-token",
      "locked.txt"
    );

    fireEvent.click(screen.getByText("nested"));
    expect(await screen.findByText("inside.txt")).toBeInTheDocument();
    expect(mockApi.browseShare).toHaveBeenLastCalledWith(
      "secret-folder",
      "dir-token",
      "nested",
      1,
      200
    );
  });

  test("logs warning when site info loading fails", async () => {
    window.history.pushState({}, "", "/s/warn1");
    mockApi.getSiteInfo.mockRejectedValueOnce(new Error("site down") as never);
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "demo.zip",
      file_size: 1024,
      is_directory: false,
      has_password: false,
      is_expired: false,
      is_exhausted: false,
    } as never);

    render(<SharePageClient />);

    await waitFor(() => {
      expect(console.warn).toHaveBeenCalledWith("加载站点标题失败", expect.any(Error));
    });
  });

  test("updates document title when site info resolves after share info", async () => {
    window.history.pushState({}, "", "/s/title1");
    const siteInfoDeferred = createDeferred<{ site_title: string }>();
    mockApi.getSiteInfo.mockReturnValueOnce(siteInfoDeferred.promise as never);
    mockApi.getShareInfo.mockResolvedValue({
      file_name: "demo.zip",
      file_size: 1024,
      is_directory: false,
      has_password: false,
      is_expired: false,
      is_exhausted: false,
    } as never);

    render(<SharePageClient />);

    expect(await screen.findByRole("button", { name: "下载文件" })).toBeInTheDocument();
    expect(document.title).toBe("demo.zip - aria2 控制器");

    siteInfoDeferred.resolve({ site_title: "Late Site" });

    await waitFor(() => {
      expect(document.title).toBe("demo.zip - Late Site");
    });
  });
});

describe("SharePageClient directory pagination", () => {
  const dirInfo = {
    file_name: "folder",
    file_size: 0,
    is_directory: true,
    has_password: false,
    is_expired: false,
    is_exhausted: false,
  };

  function sharePage(items: ReturnType<typeof sf>[], total: number, page: number) {
    return { items, total, page, page_size: 200 };
  }

  test("large directory shows pagination and loads the next page", async () => {
    window.history.pushState({}, "", "/s/bigdir");
    mockApi.getShareInfo.mockResolvedValue(dirInfo as never);
    mockApi.browseShare
      .mockResolvedValueOnce(
        sharePage([sf("a.txt", false, 1, "a.txt"), sf("b.txt", false, 2, "b.txt")], 300, 1) as never
      )
      .mockResolvedValueOnce(sharePage([sf("c.txt", false, 3, "c.txt")], 300, 2) as never);

    render(<SharePageClient />);

    expect(await screen.findByText("a.txt")).toBeInTheDocument();
    expect(screen.getByText(/共 300 项/)).toBeInTheDocument();
    expect(screen.queryByText("c.txt")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    await waitFor(() => {
      expect(mockApi.browseShare).toHaveBeenLastCalledWith("bigdir", undefined, undefined, 2, 200);
    });
    expect(await screen.findByText("c.txt")).toBeInTheDocument();
    expect(screen.queryByText("a.txt")).not.toBeInTheDocument();
  });

  test("small directory hides pagination controls", async () => {
    window.history.pushState({}, "", "/s/smalldir");
    mockApi.getShareInfo.mockResolvedValue(dirInfo as never);
    mockApi.browseShare.mockResolvedValue(
      shareResponse([sf("only.txt", false, 5, "only.txt")]) as never
    );

    render(<SharePageClient />);

    expect(await screen.findByText("only.txt")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下一页" })).not.toBeInTheDocument();
  });
});
