import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import HistoryPage from "@/app/(authenticated)/history/page";
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
    listHistory: jest.fn(),
    createTasks: jest.fn(),
    retryTask: jest.fn(),
    deleteHistoryRecords: jest.fn(),
    clearHistory: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const baseRecord = {
  id: 1,
  task_name: "failed-file.zip",
  uri: "https://example.com/failed-file.zip",
  total_length: 1024,
  result: "failed" as const,
  reason: "network error",
  created_at: "2024-01-01T00:00:00Z",
  finished_at: "2024-01-01T00:30:00Z",
  retryable: true as boolean | undefined,
  retry_blocked_reason: null as string | null | undefined,
};

describe("HistoryPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    showConfirmMock.mockResolvedValue(true);
    mockApi.listHistory.mockResolvedValue([
      baseRecord,
      { ...baseRecord, id: 2, result: "completed", task_name: "ok-file.zip", retryable: false, retry_blocked_reason: "已完成不可重试" },
    ] as never);
    mockApi.retryTask.mockResolvedValue({ id: 100 } as never);
    mockApi.deleteHistoryRecords = jest
      .fn()
      .mockResolvedValue({ accepted_count: 1, failed_count: 0, results: [] } as never);
    mockApi.clearHistory.mockResolvedValue({ ok: true, count: 2 } as never);
  });

  test("per-record delete button removes the record via single-element batch", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    expect(screen.getByText("ok-file.zip")).toBeInTheDocument();

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

    fireEvent.click(screen.getAllByTitle("删除这条历史记录")[0]);

    await waitFor(() => {
      expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument();
      expect(screen.queryByText("已选 1 项")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
    expect(mockApi.listHistory).toHaveBeenCalledTimes(1);
  });

  test("batch delete sends a single request for all selected records", async () => {
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

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    await waitFor(() =>
      expect(mockApi.deleteHistoryRecords).toHaveBeenCalledTimes(1)
    );
    expect(mockApi.deleteHistoryRecords).toHaveBeenCalledWith([1, 2]);
    await waitFor(() =>
      expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument()
    );
    expect(screen.queryByText("ok-file.zip")).not.toBeInTheDocument();
  });

  test("completed record shows no retry-blocked warning", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("ok-file.zip")).toBeInTheDocument();
    expect(screen.queryByText("已完成不可重试")).not.toBeInTheDocument();
    // cancelled 类阻塞原因（如已过期）用非红展示，不套 text-danger
  });

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

  test("disables retry and shows reason when history is expired", async () => {
    mockApi.listHistory.mockResolvedValue([
      {
        ...baseRecord,
        retryable: false,
        retry_blocked_reason: "已过期",
      },
    ] as never);

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
    mockApi.listHistory.mockResolvedValue([
      {
        ...baseRecord,
        retryable: false,
        retry_blocked_reason: "任务创建数据不完整，无法重试，请重新添加",
      },
    ] as never);

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
    mockApi.listHistory.mockResolvedValue([
      {
        ...baseRecord,
        result: "cancelled",
        reason: null,
        retryable: true,
        retry_blocked_reason: null,
      },
    ] as never);

    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(mockApi.retryTask).toHaveBeenCalledWith(1);
    });
  });

  test("shows retry for failed without retryable field (legacy payload)", async () => {
    mockApi.listHistory.mockResolvedValue([
      {
        id: 1,
        task_name: "legacy-failed.zip",
        uri: "https://example.com/legacy-failed.zip",
        total_length: 1024,
        result: "failed" as const,
        reason: "network error",
        created_at: "2024-01-01T00:00:00Z",
        finished_at: "2024-01-01T00:30:00Z",
      },
    ] as never);

    render(<HistoryPage />);

    expect(await screen.findByText("legacy-failed.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(mockApi.retryTask).toHaveBeenCalledWith(1);
    });
  });

  test("clears all history after confirmation", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("任务历史")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空历史" }));

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
      expect(mockApi.clearHistory).toHaveBeenCalled();
    });
  });

  test("load failure shows error toast", async () => {
    mockApi.listHistory.mockRejectedValue(new Error("boom"));

    render(<HistoryPage />);

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("加载历史失败", "error");
    });
  });

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
      expect(mockApi.listHistory).toHaveBeenCalledTimes(2);
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
      expect(mockApi.listHistory).toHaveBeenCalledTimes(2);
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
    // 仅成功删除的记录被移除
    await waitFor(() => {
      expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument();
    });
    expect(screen.getByText("ok-file.zip")).toBeInTheDocument();
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
      expect(mockApi.listHistory).toHaveBeenCalledTimes(2);
    });
  });

  test("clear all failure shows error toast", async () => {
    mockApi.clearHistory.mockRejectedValue(new Error("权限不足"));

    render(<HistoryPage />);

    expect(await screen.findByText("任务历史")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空历史" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("清空失败：权限不足", "error");
    });
  });

  test("clear all success empties the list", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空历史" }));

    await waitFor(() => {
      expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument();
    });
    expect(await screen.findByText("暂无历史记录")).toBeInTheDocument();
  });

  test.each([
    ["failed-file.zip", "file", true],
    ["nothing-matches", "nomatch", false],
    ["failed-file.zip", "   ", true],
  ] as const)("%j search keyword filters records", async (name, keyword, visible) => {
    mockApi.listHistory.mockResolvedValue([
      { ...baseRecord, task_name: name },
    ] as never);

    render(<HistoryPage />);

    expect(await screen.findByText(name)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索历史" }), {
      target: { value: keyword },
    });

    if (visible) {
      expect(screen.getByText(name)).toBeInTheDocument();
    } else {
      expect(screen.queryByText(name)).not.toBeInTheDocument();
      expect(screen.getByText("暂无历史记录")).toBeInTheDocument();
    }
  });

  test.each([
    ["completed", "ok-file.zip", "failed-file.zip"],
    ["failed", "failed-file.zip", "ok-file.zip"],
    ["cancelled", "cancelled-file.zip", "failed-file.zip"],
  ] as const)("%s filter shows only matching records", async (status, kept, hidden) => {
    mockApi.listHistory.mockResolvedValue([
      baseRecord,
      { ...baseRecord, id: 2, result: "completed", task_name: "ok-file.zip", retryable: false, retry_blocked_reason: "已完成不可重试" },
      { ...baseRecord, id: 3, result: "cancelled", task_name: "cancelled-file.zip" },
    ] as never);

    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("筛选历史"), {
      target: { value: status },
    });

    expect(screen.getByText(kept)).toBeInTheDocument();
    expect(screen.queryByText(hidden)).not.toBeInTheDocument();
  });

  test("loading state renders placeholder", async () => {
    let resolveList: (value: unknown) => void = () => {};
    mockApi.listHistory.mockImplementation(
      () => new Promise((resolve) => { resolveList = resolve; }) as never
    );

    const { unmount } = render(<HistoryPage />);
    expect(screen.getByText("加载中...")).toBeInTheDocument();
    unmount();
    resolveList([]);
  });

  test("unmounting during load skips the toast", async () => {
    let rejectList: (reason: unknown) => void = () => {};
    mockApi.listHistory.mockImplementation(
      () => new Promise((_resolve, reject) => { rejectList = reject; }) as never
    );

    const { unmount } = render(<HistoryPage />);
    unmount();
    rejectList(new Error("late"));

    await Promise.resolve();
    expect(showToastMock).not.toHaveBeenCalledWith("加载历史失败", "error");
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

  test.each([
    ["batch delete", "删除", "deleteHistoryRecords"],
    ["clear all", "清空历史", "clearHistory"],
  ] as const)("%s cancelled confirmation performs no request", async (_name, buttonLabel, apiName) => {
    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    if (buttonLabel === "删除") {
      fireEvent.click(screen.getByRole("button", { name: "全选" }));
    }
    showConfirmMock.mockResolvedValueOnce(false);
    fireEvent.click(screen.getAllByRole("button", { name: buttonLabel })[0]);

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
    });
    expect(mockApi[apiName]).not.toHaveBeenCalled();
  });

  test("select-all button toggles to 取消全选 and back", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));

    expect(screen.getByRole("button", { name: "取消全选" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
  });

  test("empty history renders empty state without clear button", async () => {
    mockApi.listHistory.mockResolvedValue([] as never);

    render(<HistoryPage />);

    expect(await screen.findByText("暂无历史记录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "清空历史" })).not.toBeInTheDocument();
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

  test("batch operating state disables the delete buttons", async () => {
    let resolveBatch: (value: unknown) => void = () => {};
    mockApi.deleteHistoryRecords.mockImplementation(
      () => new Promise((resolve) => { resolveBatch = resolve; }) as never
    );

    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    const clearButton = await screen.findByRole("button", { name: "清空历史" });
    expect(clearButton).toBeDisabled();
    expect(clearButton.className).toContain("opacity-60");

    resolveBatch({ accepted_count: 2, failed_count: 0, results: [] });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "清空历史" })).toBeEnabled();
    });
  });

  test("clear-all operating state disables the batch delete button", async () => {
    let resolveClear: (value: unknown) => void = () => {};
    mockApi.clearHistory.mockImplementation(
      () => new Promise((resolve) => { resolveClear = resolve; }) as never
    );

    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "清空历史" }));

    const batchDelete = (await screen.findAllByRole("button", { name: "删除" }))[0];
    expect(batchDelete).toBeDisabled();
    expect(batchDelete.className).toContain("opacity-60");

    resolveClear({ ok: true, count: 2 });
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
      expect(screen.getByText("暂无历史记录")).toBeInTheDocument();
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
