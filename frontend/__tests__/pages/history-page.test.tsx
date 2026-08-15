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
    createTask: jest.fn(),
    retryTask: jest.fn(),
    deleteHistory: jest.fn(),
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
    mockApi.deleteHistory = jest.fn().mockResolvedValue({ ok: true } as never);
    mockApi.clearHistory.mockResolvedValue({ ok: true, count: 2 } as never);
  });

  test("per-record delete button removes the record", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("failed-file.zip")).toBeInTheDocument();
    expect(screen.getByText("ok-file.zip")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await waitFor(() => expect(mockApi.deleteHistory).toHaveBeenCalled());

    await waitFor(() =>
      expect(screen.queryByText("failed-file.zip")).not.toBeInTheDocument()
    );
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
    expect(mockApi.createTask).not.toHaveBeenCalled();
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
    expect(mockApi.createTask).not.toHaveBeenCalled();
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
});
