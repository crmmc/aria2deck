import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import HistoryPage from "@/app/(authenticated)/history/page";
import { api } from "@/lib/api";
import type { HistoryPageResponse, TaskHistory } from "@/types";

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
    listHistoryPage: jest.fn(),
    createTasks: jest.fn(),
    retryTask: jest.fn(),
    deleteHistoryRecords: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const baseRecord: TaskHistory = {
  id: 1,
  task_name: "failed-file.zip",
  uri: "https://example.com/failed-file.zip",
  total_length: 1024,
  result: "failed",
  reason: "network error",
  created_at: "2024-01-01T00:00:00Z",
  finished_at: "2024-01-01T00:30:00Z",
  retryable: true,
  retry_blocked_reason: null,
};

const twoRecords: TaskHistory[] = [
  baseRecord,
  {
    ...baseRecord,
    id: 2,
    result: "completed",
    task_name: "ok-file.zip",
    retryable: false,
    retry_blocked_reason: "已完成不可重试",
  },
];

function pageOf(items: TaskHistory[], overrides: Partial<HistoryPageResponse> = {}): HistoryPageResponse {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 20,
    ...overrides,
  };
}

function mockPage(items: TaskHistory[], overrides: Partial<HistoryPageResponse> = {}) {
  mockApi.listHistoryPage.mockResolvedValue(pageOf(items, overrides));
}

describe("HistoryPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    showConfirmMock.mockResolvedValue(true);
    mockPage(twoRecords);
    mockApi.retryTask.mockResolvedValue({ id: 100 } as never);
    mockApi.deleteHistoryRecords = jest
      .fn()
      .mockResolvedValue({ accepted_count: 1, failed_count: 0, results: [] } as never);
  });

  describe("paged loading", () => {
    test("loads first page with default params on mount", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      expect(mockApi.listHistoryPage).toHaveBeenCalledWith({
        page: 1,
        pageSize: 20,
        status: "all",
        q: "",
      });
    });

    test("renders pagination controls with total and changes page", async () => {
      mockPage(twoRecords, { total: 60 });
      render(<HistoryPage />);

      expect(await screen.findByText("共 60 项")).toBeInTheDocument();

      mockPage([{ ...baseRecord, id: 3, task_name: "page2-file.zip" }], { total: 60, page: 2 });
      fireEvent.click(screen.getByRole("button", { name: "2" }));

      expect(await screen.findByText("page2-file.zip")).toBeInTheDocument();
      expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
        page: 2,
        pageSize: 20,
        status: "all",
        q: "",
      });
    });

    test("changing page size reloads from page 1", async () => {
      mockPage(twoRecords, { total: 60 });
      render(<HistoryPage />);

      expect(await screen.findByText("共 60 项")).toBeInTheDocument();
      fireEvent.change(screen.getByRole("combobox", { name: "每页条数" }), {
        target: { value: "50" },
      });

      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
          page: 1,
          pageSize: 50,
          status: "all",
          q: "",
        });
      });
    });

    test("no pagination controls when total is zero", async () => {
      mockPage([], { total: 0 });
      render(<HistoryPage />);

      expect(await screen.findByText("暂无历史记录")).toBeInTheDocument();
      expect(screen.queryByText(/共 .* 项/)).not.toBeInTheDocument();
    });

    test("load failure shows error toast", async () => {
      mockApi.listHistoryPage.mockRejectedValue(new Error("boom"));

      render(<HistoryPage />);

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith("加载历史失败", "error");
      });
    });

    test("loading state renders placeholder", async () => {
      let resolveList: (value: HistoryPageResponse) => void = () => {};
      mockApi.listHistoryPage.mockImplementation(
        () => new Promise<HistoryPageResponse>((resolve) => { resolveList = resolve; })
      );

      const { unmount } = render(<HistoryPage />);
      expect(screen.getByText("加载中...")).toBeInTheDocument();
      unmount();
      resolveList(pageOf([]));
    });

    test("unmounting during load skips the toast", async () => {
      let rejectList: (reason: unknown) => void = () => {};
      mockApi.listHistoryPage.mockImplementation(
        () => new Promise<HistoryPageResponse>((_resolve, reject) => { rejectList = reject; })
      );

      const { unmount } = render(<HistoryPage />);
      unmount();
      rejectList(new Error("late"));

      await Promise.resolve();
      expect(showToastMock).not.toHaveBeenCalledWith("加载历史失败", "error");
    });
  });

  describe("filter and search", () => {
    test("status filter reloads with the selected status and resets to page 1", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      mockPage([twoRecords[1]], { total: 1 });
      fireEvent.change(screen.getByLabelText("筛选历史"), {
        target: { value: "completed" },
      });

      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
          page: 1,
          pageSize: 20,
          status: "completed",
          q: "",
        });
      });
    });

    test("status filter change only triggers one request with page=1", async () => {
      mockPage(twoRecords, { total: 60 });
      render(<HistoryPage />);
      expect(await screen.findByText("共 60 项")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "2" }));
      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith(
          expect.objectContaining({ page: 2 })
        );
      });
      const callsBefore = mockApi.listHistoryPage.mock.calls.length;

      fireEvent.change(screen.getByLabelText("筛选历史"), {
        target: { value: "failed" },
      });

      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
          page: 1,
          pageSize: 20,
          status: "failed",
          q: "",
        });
      });
      // 条件变化只应发出一笔"第 1 页 + 新条件"的请求，不允许先按旧页码发一笔再重置
      expect(mockApi.listHistoryPage.mock.calls.length).toBe(callsBefore + 1);
      expect(
        mockApi.listHistoryPage.mock.calls.some(
          ([params]) => params.status === "failed" && params.page !== 1
        )
      ).toBe(false);
    });

    test("changing page clears stale selection from the previous page", async () => {
      mockPage(twoRecords, { total: 60, page: 1 });
      render(<HistoryPage />);
      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("checkbox", { name: "选择 failed-file.zip" }));
      expect(await screen.findByText("已选 1 项")).toBeInTheDocument();

      // 翻页后返回另一批记录，残留选中必须被清空，否则工具栏计数与批量删除都指向旧页
      mockPage(
        [
          {
            ...baseRecord,
            id: 3,
            task_name: "page2-file.zip",
            result: "completed",
            retryable: false,
            retry_blocked_reason: null,
          },
        ],
        { total: 60, page: 2 }
      );
      fireEvent.click(screen.getByRole("button", { name: "2" }));

      expect(await screen.findByText("page2-file.zip")).toBeInTheDocument();
      expect(screen.queryByText("已选 1 项")).not.toBeInTheDocument();
    });

    test("stale paged response cannot overwrite newer filter results", async () => {
      mockPage(twoRecords, { total: 60 });
      render(<HistoryPage />);
      expect(await screen.findByText("共 60 项")).toBeInTheDocument();

      let resolveOld: ((value: HistoryPageResponse) => void) | undefined;
      fireEvent.click(screen.getByRole("button", { name: "2" }));
      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith(
          expect.objectContaining({ page: 2 })
        );
      });
      mockApi.listHistoryPage.mockImplementationOnce(
        () =>
          new Promise<HistoryPageResponse>((resolve) => {
            resolveOld = resolve;
          })
      );
      fireEvent.click(screen.getByRole("button", { name: "3" }));
      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith(
          expect.objectContaining({ page: 3 })
        );
      });

      mockPage([twoRecords[1]], { total: 1 });
      fireEvent.change(screen.getByLabelText("筛选历史"), {
        target: { value: "completed" },
      });
      expect(await screen.findByText("ok-file.zip")).toBeInTheDocument();

      // 旧页请求最后才返回，其响应必须被丢弃，不能覆盖新筛选结果
      await act(async () => {
        resolveOld?.(pageOf([twoRecords[1]], { total: 60, page: 3 }));
        await Promise.resolve();
      });
      expect(screen.getByText("ok-file.zip")).toBeInTheDocument();
      expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument();
    });

    test("search keyword is debounced before querying", async () => {
      jest.useFakeTimers();
      try {
        render(<HistoryPage />);
        await act(async () => {
          await Promise.resolve();
        });
        expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
        const callsBefore = mockApi.listHistoryPage.mock.calls.length;

        fireEvent.change(screen.getByRole("textbox", { name: "搜索历史" }), {
          target: { value: "failed" },
        });
        // 防抖窗口内不立即发请求
        expect(mockApi.listHistoryPage.mock.calls.length).toBe(callsBefore);

        await act(async () => {
          jest.advanceTimersByTime(300);
          await Promise.resolve();
        });

        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
          page: 1,
          pageSize: 20,
          status: "all",
          q: "failed",
        });
      } finally {
        jest.useRealTimers();
      }
    });

    test("search resets to page 1", async () => {
      jest.useFakeTimers();
      try {
        mockPage(twoRecords, { total: 60 });
        render(<HistoryPage />);
        await act(async () => {
          await Promise.resolve();
        });
        expect(await screen.findByText("共 60 项")).toBeInTheDocument();

        fireEvent.click(screen.getByRole("button", { name: "2" }));
        await act(async () => {
          await Promise.resolve();
        });
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith(
          expect.objectContaining({ page: 2 })
        );

        fireEvent.change(screen.getByRole("textbox", { name: "搜索历史" }), {
          target: { value: "ok" },
        });
        await act(async () => {
          jest.advanceTimersByTime(300);
          await Promise.resolve();
        });

        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
          page: 1,
          pageSize: 20,
          status: "all",
          q: "ok",
        });
      } finally {
        jest.useRealTimers();
      }
    });
  });

  describe("per-record actions", () => {
    test("per-record delete button removes the record via single-element batch", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();

      mockPage([twoRecords[1]], { total: 1 });
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
      await waitFor(() =>
        expect(mockApi.deleteHistoryRecords).toHaveBeenCalledWith([1])
      );

      await waitFor(() =>
        expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument()
      );
    });

    test("single delete clears the deleted record from selection", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("checkbox", { name: "选择 failed-file.zip" }));
      expect(screen.getByText("已选 1 项")).toBeInTheDocument();

      mockPage([twoRecords[1]], { total: 1 });
      fireEvent.click(screen.getAllByTitle("删除这条历史记录")[0]);

      await waitFor(() => {
        expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument();
        expect(screen.queryByText("已选 1 项")).not.toBeInTheDocument();
      });
      expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
    });

    test("single delete with failed item shows error and reloads", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      mockApi.deleteHistoryRecords.mockResolvedValue({
        accepted_count: 0,
        failed_count: 1,
        results: [{ history_id: 1, ok: false, state: "failed", accepted: false, error: "记录不存在" }],
      } as never);

      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith("删除失败：记录不存在", "error");
      });
      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenCalledTimes(2);
      });
      expect(screen.getByText("failed-file.zip")).toBeInTheDocument();
    });

    test("single delete rejection shows error toast and reloads", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      mockApi.deleteHistoryRecords.mockRejectedValue(new Error("服务不可用"));

      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith("删除失败：服务不可用", "error");
      });
      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenCalledTimes(2);
      });
    });

    test("single delete with empty results falls back to 未知错误", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      mockApi.deleteHistoryRecords.mockResolvedValue({
        accepted_count: 0,
        failed_count: 1,
        results: [],
      } as never);

      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith("删除失败：未知错误", "error");
      });
    });

    test("unmounting during single delete skips state updates", async () => {
      let resolveDelete: (value: unknown) => void = () => {};
      mockApi.deleteHistoryRecords.mockImplementation(
        () => new Promise((resolve) => { resolveDelete = resolve; }) as never
      );

      const { unmount } = render(<HistoryPage />);
      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
      unmount();

      resolveDelete({ accepted_count: 1, failed_count: 0, results: [] });
      await Promise.resolve();
    });
  });

  describe("retry", () => {
    test("renders records and retries via retryTask(id)", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("任务历史")).toBeInTheDocument();
      expect(screen.getByText("failed-file.zip")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      await waitFor(() => {
        expect(mockApi.retryTask).toHaveBeenCalledWith(1);
      });
      expect(mockApi.createTasks).not.toHaveBeenCalled();
    });

    test("completed record shows no retry-blocked warning", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("ok-file.zip")).toBeInTheDocument();
      expect(screen.queryByText("已完成不可重试")).not.toBeInTheDocument();
    });

    test("disables retry and shows reason when history is expired", async () => {
      mockPage([
        {
          ...baseRecord,
          retryable: false,
          retry_blocked_reason: "已过期",
        },
      ]);

      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      const retryButton = screen.getByRole("button", { name: "重试" });
      expect(retryButton).toBeDisabled();
      expect(screen.getByText("已过期")).toBeInTheDocument();

      fireEvent.click(retryButton);
      expect(mockApi.retryTask).not.toHaveBeenCalled();
      expect(mockApi.createTasks).not.toHaveBeenCalled();
    });

    test("disables retry when retryable is false", async () => {
      mockPage([
        {
          ...baseRecord,
          retryable: false,
          retry_blocked_reason: "任务创建数据不完整，无法重试，请重新添加",
        },
      ]);

      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      const retryButton = screen.getByRole("button", { name: "重试" });
      expect(retryButton).toBeDisabled();
      expect(
        screen.getByText("任务创建数据不完整，无法重试，请重新添加")
      ).toBeInTheDocument();

      fireEvent.click(retryButton);
      expect(mockApi.retryTask).not.toHaveBeenCalled();
    });

    test("shows retry for cancelled when retryable is true", async () => {
      mockPage([
        {
          ...baseRecord,
          result: "cancelled",
          reason: null,
          retryable: true,
          retry_blocked_reason: null,
        },
      ]);

      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      await waitFor(() => {
        expect(mockApi.retryTask).toHaveBeenCalledWith(1);
      });
    });

    test("shows retry for failed without retryable field (legacy payload)", async () => {
      mockPage([
        {
          id: 1,
          task_name: "legacy-failed.zip",
          uri: "https://example.com/legacy-failed.zip",
          total_length: 1024,
          result: "failed",
          reason: "network error",
          created_at: "2024-01-01T00:00:00Z",
          finished_at: "2024-01-01T00:30:00Z",
        },
      ]);

      render(<HistoryPage />);

      expect(await screen.findByText("legacy-failed.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      await waitFor(() => {
        expect(mockApi.retryTask).toHaveBeenCalledWith(1);
      });
    });

    test("retry failure shows error toast", async () => {
      mockApi.retryTask.mockRejectedValue(new Error("配额不足"));

      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "重试" }));

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith("重试失败：配额不足", "error");
      });
    });
  });

  describe("copy", () => {
    test("copy button copies the record uri", async () => {
      const writeText = jest.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });

      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getAllByRole("button", { name: "复制" })[0]);

      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith("https://example.com/failed-file.zip");
      });
    });
  });

  describe("batch delete", () => {
    test("select-all only selects current page records and batch delete sends one request", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "全选" }));

      mockApi.deleteHistoryRecords.mockResolvedValue({
        accepted_count: 2,
        failed_count: 0,
        results: [
          { history_id: 1, ok: true, state: "deleted", accepted: true, error: null },
          { history_id: 2, ok: true, state: "deleted", accepted: true, error: null },
        ],
      } as never);

      // 删除成功后页面重新拉取当前页
      mockPage([], { total: 0 });
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      await waitFor(() =>
        expect(mockApi.deleteHistoryRecords).toHaveBeenCalledTimes(1)
      );
      expect(mockApi.deleteHistoryRecords).toHaveBeenCalledWith([1, 2]);
      await waitFor(() =>
        expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument()
      );
    });

    test("batch delete with partial failure shows warning toast", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "全选" }));

      mockApi.deleteHistoryRecords.mockResolvedValue({
        accepted_count: 1,
        failed_count: 1,
        results: [
          { history_id: 1, ok: true, state: "deleted", accepted: true, error: null },
          { history_id: 2, ok: false, state: "failed", accepted: false, error: "被引用" },
        ],
      } as never);

      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith(
          "已删除 1 条，1 条删除失败",
          "warning"
        );
      });
    });

    test("batch delete rejection shows error toast and reloads", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "全选" }));
      mockApi.deleteHistoryRecords.mockRejectedValue(new Error("服务器错误"));

      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith("删除失败：服务器错误", "error");
      });
      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenCalledTimes(2);
      });
    });

    test("select-all button toggles to 取消全选 and back", async () => {
      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "全选" }));

      expect(screen.getByRole("button", { name: "取消全选" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
      expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
    });

    test("batch operating state disables the delete buttons", async () => {
      let resolveBatch: (value: unknown) => void = () => {};
      mockApi.deleteHistoryRecords.mockImplementation(
        () => new Promise((resolve) => { resolveBatch = resolve; }) as never
      );

      render(<HistoryPage />);

      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "全选" }));
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      const batchDelete = await screen.findAllByRole("button", { name: "删除" });
      expect(batchDelete[0]).toBeDisabled();
      expect(batchDelete[0].className).toContain("opacity-60");

      resolveBatch({ accepted_count: 2, failed_count: 0, results: [] });
      await waitFor(() => {
        expect(screen.getAllByRole("button", { name: "删除" })[0]).toBeEnabled();
      });
    });

    test("unmounting after batch delete confirm skips state updates", async () => {
      let resolveBatch: (value: unknown) => void = () => {};
      mockApi.deleteHistoryRecords.mockImplementation(
        () => new Promise((resolve) => { resolveBatch = resolve; }) as never
      );

      const { unmount } = render(<HistoryPage />);
      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "全选" }));
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
      unmount();

      resolveBatch({ accepted_count: 2, failed_count: 0, results: [] });
      await Promise.resolve();
    });
  });

  describe("page fallback after deletion", () => {
    test("deleting the last record of a non-first page goes back one page", async () => {
      const onlyRecord = { ...baseRecord, id: 21, task_name: "last-on-page2.zip" };
      mockApi.listHistoryPage.mockImplementation(async (params) => {
        if (params.page === 2) {
          return pageOf([onlyRecord], { total: 21, page: 2 });
        }
        return pageOf(twoRecords, { total: 21 });
      });

      render(<HistoryPage />);

      expect(await screen.findByText("共 21 项")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "2" }));
      expect(await screen.findByText("last-on-page2.zip")).toBeInTheDocument();

      // 删除后第 2 页为空 → 回退到第 1 页并重新拉取
      mockApi.listHistoryPage.mockImplementation(async () =>
        pageOf(twoRecords, { total: 20 })
      );
      fireEvent.click(screen.getAllByTitle("删除这条历史记录")[0]);

      await waitFor(() => {
        expect(mockApi.listHistoryPage).toHaveBeenLastCalledWith({
          page: 1,
          pageSize: 20,
          status: "all",
          q: "",
        });
      });
      expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    });
  });

  test("batch delete cancelled confirmation performs no request", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    showConfirmMock.mockResolvedValueOnce(false);
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
    });
    expect(mockApi.deleteHistoryRecords).not.toHaveBeenCalled();
  });
});
