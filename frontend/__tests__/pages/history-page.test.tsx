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
};

describe("HistoryPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    showConfirmMock.mockResolvedValue(true);
    mockApi.listHistory.mockResolvedValue([
      baseRecord,
      { ...baseRecord, id: 2, result: "completed", task_name: "ok-file.zip" },
    ] as never);
    mockApi.createTask.mockResolvedValue({ id: 100 } as never);
    mockApi.clearHistory.mockResolvedValue({ ok: true, count: 2 } as never);
  });

  test("renders records and allows retry", async () => {
    render(<HistoryPage />);

    expect(await screen.findByText("任务历史")).toBeInTheDocument();
    expect(screen.getByText("failed-file.zip")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(mockApi.createTask).toHaveBeenCalledWith("https://example.com/failed-file.zip");
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
