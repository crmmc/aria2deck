import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import StoragePage from "@/app/(authenticated)/storage/page";
import { api } from "@/lib/api";

const replaceMock = jest.fn();
const showToastMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

jest.mock("@/lib/AuthContext", () => {
  const user = {
    id: 1,
    username: "admin",
    is_admin: true,
    quota: 1024 * 1024 * 1024,
    is_initial_password: false,
  };
  return {
    __esModule: true,
    useAuth: () => ({ user }),
  };
});

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

    fireEvent.click(screen.getByRole("button", { name: "1" }));
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
      async (page, pageSize, search, orphanOnly) => ({
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
    expect(await screen.findByText("第 2 / 3 页，共 41 项")).toBeInTheDocument();

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
    expect(screen.getByText("第 2 / 2 页，共 21 项")).toBeInTheDocument();
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
    mockApi.listStoredFiles.mockImplementation(async (page, pageSize) => {
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
    expect(await screen.findByText("第 1 / 1 页，共 20 项")).toBeInTheDocument();
  });

  test("returns from an emptied last page after refresh", async () => {
    let pageTwoLoads = 0;
    mockApi.listStoredFiles.mockImplementation(async (page, pageSize) => {
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
    expect(await screen.findByText("第 1 / 1 页，共 20 项")).toBeInTheDocument();
  });
});
