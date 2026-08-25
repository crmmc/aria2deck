import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import StoragePage from "@/app/(authenticated)/storage/page";
import { api } from "@/lib/api";

const replaceMock = jest.fn();
const showToastMock = jest.fn();
const authUserState: {
  user: {
    id: number;
    username: string;
    is_admin: boolean;
    quota: number;
    is_initial_password: boolean;
  } | null;
} = {
  user: {
    id: 1,
    username: "admin",
    is_admin: true,
    quota: 1024 * 1024 * 1024,
    is_initial_password: false,
  },
};

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

jest.mock("@/lib/AuthContext", () => ({
  __esModule: true,
  useAuth: () => ({ user: authUserState.user }),
}));

jest.mock("@/components/Toast", () => ({
  __esModule: true,
  useToast: () => ({
    showToast: showToastMock,
  }),
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listStoredFiles: jest.fn(),
    bulkDeleteStoredFiles: jest.fn(),
    getFileUsers: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const file = {
  id: 1,
  content_hash: "hash_abc",
  original_name: "movie.mkv",
  size: 1024 * 1024,
  is_directory: false,
  ref_count: 1,
  created_at: "2024-01-01T00:00:00Z",
  real_path: "/tmp/movie.mkv",
  exists_on_disk: true,
};

describe("StoragePage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    authUserState.user = {
      id: 1,
      username: "admin",
      is_admin: true,
      quota: 1024 * 1024 * 1024,
      is_initial_password: false,
    };
    mockApi.listStoredFiles.mockResolvedValue({
      files: [file],
      total: 1,
      page: 1,
      page_size: 20,
    });
    mockApi.getFileUsers.mockResolvedValue({
      file_id: 1,
      users: [{ user_id: 1, username: "admin", display_name: "movie.mkv" }],
    } as never);
    mockApi.bulkDeleteStoredFiles.mockResolvedValue({
      deleted_count: 1,
      failed_ids: [],
      errors: [],
    } as never);
  });

  test("renders storage files and opens user list modal", async () => {
    render(<StoragePage />);

    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    expect(screen.getByText("movie.mkv")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "1" })[0]);
    await waitFor(() => {
      expect(mockApi.getFileUsers).toHaveBeenCalledWith(1);
    });
    expect(await screen.findByText("引用用户")).toBeInTheDocument();
  });

  test("deletes selected files", async () => {
    render(<StoragePage />);

    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));

    await waitFor(() => {
      expect(mockApi.bulkDeleteStoredFiles).toHaveBeenCalledWith([1]);
    });
  });

  test("shows error toast when deleting selected files fails", async () => {
    mockApi.bulkDeleteStoredFiles.mockRejectedValue(new Error("delete failed") as never);
    render(<StoragePage />);

    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("删除失败", "error");
    });
  });

  test("renders exactly one storage table after loading files", async () => {
    const { container } = render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    expect(container.querySelectorAll("table.table")).toHaveLength(1);
  });

  test("paginates and resets to page one when filters change", async () => {
    mockApi.listStoredFiles.mockImplementation(
      async (page = 1, pageSize = 20, search, _orphanOnly) => ({
        files: [{ ...file, id: page, original_name: search || `file-${page}` }],
        total: 41,
        page,
        page_size: pageSize,
      }),
    );
    render(<StoragePage />);

    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenCalledWith(1, 20, undefined, false);
    });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenCalledWith(2, 20, undefined, false);
    });
    expect(await screen.findByText("共 41 项")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenLastCalledWith(1, 20, undefined, false);
    });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenLastCalledWith(2, 20, undefined, false);
    });

    fireEvent.change(screen.getByLabelText("搜索文件名"), {
      target: { value: "movie" },
    });
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenCalledWith(1, 20, "movie", false);
    });

    fireEvent.click(screen.getByLabelText("仅显示孤立文件"));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenCalledWith(1, 20, "movie", true);
    });
  });

  test("invalidates the old page request and selection before loading a new page", async () => {
    mockApi.listStoredFiles.mockResolvedValueOnce({
      files: [{ ...file, original_name: "page-one-file" }],
      total: 21,
      page: 1,
      page_size: 20,
    });
    render(<StoragePage />);
    expect(await screen.findByText("page-one-file")).toBeInTheDocument();

    const oldPageRequest = deferred<Awaited<ReturnType<typeof api.listStoredFiles>>>();
    const newPageRequest = deferred<Awaited<ReturnType<typeof api.listStoredFiles>>>();
    mockApi.listStoredFiles.mockImplementation((page) => {
      if (page === 1) return oldPageRequest.promise;
      if (page === 2) return newPageRequest.promise;
      throw new Error(`unexpected page: ${page}`);
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "选择 page-one-file" }));
    expect(screen.getByRole("button", { name: "删除选中 (1)" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenLastCalledWith(1, 20, undefined, false);
    });

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenLastCalledWith(2, 20, undefined, false);
    });
    expect(screen.getByRole("button", { name: "删除选中 (0)" })).toBeDisabled();

    await act(async () => {
      oldPageRequest.resolve({
        files: [{ ...file, id: 11, original_name: "stale-page-one" }],
        total: 21,
        page: 1,
        page_size: 20,
      });
      await oldPageRequest.promise;
    });
    expect(screen.queryByText("stale-page-one")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除选中 (0)" })).toBeDisabled();

    await act(async () => {
      newPageRequest.resolve({
        files: [{ ...file, id: 21, original_name: "page-two-file" }],
        total: 21,
        page: 2,
        page_size: 20,
      });
      await newPageRequest.promise;
    });
    expect(await screen.findByText("page-two-file")).toBeInTheDocument();
    expect(screen.getByText("共 21 项")).toBeInTheDocument();
  });

  test("ignores stale responses from older filters", async () => {
    render(<StoragePage />);
    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();

    const oldRequest = deferred<Awaited<ReturnType<typeof api.listStoredFiles>>>();
    const latestRequest = deferred<Awaited<ReturnType<typeof api.listStoredFiles>>>();
    mockApi.listStoredFiles.mockImplementation((_page, _pageSize, search) => {
      if (search === "old") return oldRequest.promise;
      if (search === "latest") return latestRequest.promise;
      throw new Error(`unexpected search: ${search}`);
    });

    fireEvent.change(screen.getByLabelText("搜索文件名"), {
      target: { value: "old" },
    });
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenCalledWith(1, 20, "old", false);
    });
    fireEvent.change(screen.getByLabelText("搜索文件名"), {
      target: { value: "latest" },
    });
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenCalledWith(1, 20, "latest", false);
    });

    await act(async () => {
      latestRequest.resolve({
        files: [{ ...file, id: 3, original_name: "latest-file" }],
        total: 1,
        page: 1,
        page_size: 20,
      });
      await latestRequest.promise;
    });
    expect(await screen.findByText("latest-file")).toBeInTheDocument();

    await act(async () => {
      oldRequest.resolve({
        files: [{ ...file, id: 2, original_name: "old-file" }],
        total: 1,
        page: 1,
        page_size: 20,
      });
      await oldRequest.promise;
    });
    expect(screen.getByText("latest-file")).toBeInTheDocument();
    expect(screen.queryByText("old-file")).not.toBeInTheDocument();
  });

  test("returns from an emptied last page after deletion", async () => {
    let deleted = false;
    mockApi.listStoredFiles.mockImplementation(async (page = 1, pageSize = 20) => {
      if (page === 2 && !deleted) {
        return {
          files: [{ ...file, id: 21, original_name: "last-file" }],
          total: 21,
          page,
          page_size: pageSize,
        };
      }
      return {
        files: [file],
        total: deleted ? 20 : 21,
        page,
        page_size: pageSize,
      };
    });
    mockApi.bulkDeleteStoredFiles.mockImplementation(async () => {
      deleted = true;
      return { deleted_count: 1, failed_ids: [], errors: [] };
    });
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("last-file")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 last-file" }));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));

    await waitFor(() => {
      const pageOneCalls = mockApi.listStoredFiles.mock.calls.filter(
        ([requestedPage]) => requestedPage === 1,
      );
      expect(pageOneCalls).toHaveLength(2);
    });
    expect(await screen.findByText("共 20 项")).toBeInTheDocument();
  });

  test("returns from an emptied last page after refresh", async () => {
    let pageTwoLoads = 0;
    mockApi.listStoredFiles.mockImplementation(async (page = 1, pageSize = 20) => {
      if (page === 2) {
        pageTwoLoads += 1;
        return pageTwoLoads === 1
          ? {
              files: [{ ...file, id: 21, original_name: "last-file" }],
              total: 21,
              page,
              page_size: pageSize,
            }
          : { files: [], total: 20, page, page_size: pageSize };
      }
      return {
        files: [file],
        total: pageTwoLoads > 1 ? 20 : 21,
        page,
        page_size: pageSize,
      };
    });
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("last-file")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => {
      const pageOneCalls = mockApi.listStoredFiles.mock.calls.filter(
        ([requestedPage]) => requestedPage === 1,
      );
      expect(pageOneCalls).toHaveLength(2);
    });
    expect(await screen.findByText("共 20 项")).toBeInTheDocument();
  });

  test("shows error toast when loading stored files fails", async () => {
    mockApi.listStoredFiles.mockRejectedValue(new Error("load failed") as never);
    render(<StoragePage />);

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("加载存储文件失败", "error");
    });
    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    expect(await screen.findByText("暂无存储文件")).toBeInTheDocument();
  });

  test("selects and clears the whole page via the select-all checkbox", async () => {
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));
    expect(screen.getByRole("button", { name: "删除选中 (1)" })).toBeEnabled();

    fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));
    expect(screen.getByRole("button", { name: "删除选中 (0)" })).toBeDisabled();
  });

  test("surfaces partial deletion errors alongside the success toast", async () => {
    mockApi.bulkDeleteStoredFiles.mockResolvedValue({
      deleted_count: 1,
      failed_ids: [],
      errors: ["hash_abc: 文件被占用"],
    } as never);
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 movie.mkv" }));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("已删除 1 个文件", "success");
    });
    expect(showToastMock).toHaveBeenCalledWith("hash_abc: 文件被占用", "error");
  });

  test("shows error toast when loading file users fails", async () => {
    mockApi.getFileUsers.mockRejectedValue(new Error("users failed") as never);
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "1" })[0]);

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("加载用户列表失败", "error");
    });
  });

  test("shows empty state inside the user modal and closes it", async () => {
    mockApi.getFileUsers.mockResolvedValue({ file_id: 1, users: [] } as never);
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "1" })[0]);

    expect(await screen.findByText("无引用用户")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => {
      expect(screen.queryByLabelText("文件引用用户")).not.toBeInTheDocument();
    });
  });

  test.each([
    {
      name: "directory entry",
      file: { ...file, is_directory: true },
      expected: /📁/,
    },
    {
      name: "missing on disk entry",
      file: { ...file, exists_on_disk: false },
      expected: "缺失",
    },
  ])("renders table decorations for a $name", async ({ file: variant, expected }) => {
    mockApi.listStoredFiles.mockResolvedValue({
      files: [variant],
      total: 1,
      page: 1,
      page_size: 20,
    } as never);
    render(<StoragePage />);

    expect(await screen.findByText(/movie\.mkv/)).toBeInTheDocument();
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  test("closes the user modal via the backdrop button", async () => {
    render(<StoragePage />);

    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "1" })[0]);
    expect(await screen.findByText("引用用户")).toBeInTheDocument();

    const dialog = screen.getByLabelText("文件引用用户");
    fireEvent.click(dialog.querySelector(".modal-backdrop-button") as HTMLElement);
    await waitFor(() => {
      expect(screen.queryByLabelText("文件引用用户")).not.toBeInTheDocument();
    });
  });

  test("ignores stale load failures superseded by a newer request", async () => {
    mockApi.listStoredFiles.mockResolvedValueOnce({
      files: [file],
      total: 21,
      page: 1,
      page_size: 20,
    } as never);
    render(<StoragePage />);
    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();

    let rejectStale: (reason?: unknown) => void = () => {};
    mockApi.listStoredFiles.mockImplementation((page) =>
      page === 2
        ? Promise.resolve({ files: [file], total: 21, page, page_size: 20 })
        : new Promise((_res, rej) => {
            rejectStale = rej;
          }) as never,
    );

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(mockApi.listStoredFiles).toHaveBeenLastCalledWith(2, 20, undefined, false);
    });

    await act(async () => {
      rejectStale(new Error("stale failure"));
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(showToastMock).not.toHaveBeenCalledWith("加载存储文件失败", "error");
  });

  test("ignores deletion failure after unmount", async () => {
    render(<StoragePage />);
    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();

    let rejectDelete: (reason?: unknown) => void = () => {};
    mockApi.bulkDeleteStoredFiles.mockReturnValue(
      new Promise((_res, rej) => {
        rejectDelete = rej;
      }) as never,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 movie.mkv" }));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));
    await waitFor(() => {
      expect(mockApi.bulkDeleteStoredFiles).toHaveBeenCalled();
    });
    cleanup();

    await act(async () => {
      rejectDelete(new Error("late failure"));
      await Promise.resolve();
    });
  });

  test("ignores file users failure after unmount", async () => {
    render(<StoragePage />);
    expect(await screen.findByText("movie.mkv")).toBeInTheDocument();

    let rejectUsers: (reason?: unknown) => void = () => {};
    mockApi.getFileUsers.mockReturnValue(
      new Promise((_res, rej) => {
        rejectUsers = rej;
      }) as never,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "1" })[0]);
    await waitFor(() => {
      expect(mockApi.getFileUsers).toHaveBeenCalled();
    });
    cleanup();

    await act(async () => {
      rejectUsers(new Error("late failure"));
      await Promise.resolve();
    });
  });

  test("redirects non-admin users to the tasks page", async () => {
    authUserState.user = {
      id: 2,
      username: "user",
      is_admin: false,
      quota: 1024 * 1024 * 1024,
      is_initial_password: false,
    };
    render(<StoragePage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/tasks");
    });
    expect(mockApi.listStoredFiles).not.toHaveBeenCalled();
  });
});
