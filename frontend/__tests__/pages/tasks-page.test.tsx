import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import TasksPage from "@/app/(authenticated)/tasks/page";
import { api } from "@/lib/api";
import type { Task } from "@/types";

const showToastMock = jest.fn();
const showConfirmMock = jest.fn();

jest.mock("@/components/Toast", () => ({
  __esModule: true,
  useToast: () => ({
    showToast: showToastMock,
    showConfirm: showConfirmMock,
  }),
}));

jest.mock("@/components/StatsWidget", () => ({
  __esModule: true,
  default: () => <div data-testid="stats-widget">stats</div>,
}));

jest.mock("@/hooks/useTaskWebSocket", () => ({
  __esModule: true,
  useTaskWebSocket: jest.fn(),
}));

jest.mock("@/lib/notification", () => ({
  __esModule: true,
  sendTaskCompleteNotification: jest.fn(),
  sendTaskErrorNotification: jest.fn(),
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listTasks: jest.fn<Promise<Task[]>, [string?]>(),
    createTask: jest.fn<Promise<Task>, [string]>(),
    uploadTorrent: jest.fn<Promise<Task>, [string, Record<string, unknown>?]>(),
    cancelTask: jest.fn<Promise<{ ok: boolean }>, [number]>(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const activeTask = {
  id: 1,
  name: "ubuntu.iso",
  uri: "https://example.com/ubuntu.iso",
  status: "active",
  total_length: 1024 * 1024 * 1024,
  completed_length: 512 * 1024 * 1024,
  download_speed: 2 * 1024 * 1024,
  upload_speed: 0,
  frozen_space: 0,
  error: null,
  created_at: "2024-01-01T00:00:00Z",
};

describe("TasksPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    showConfirmMock.mockResolvedValue(true);
    jest.spyOn(global, "setInterval").mockImplementation((() => 1) as never);
    jest.spyOn(global, "clearInterval").mockImplementation((() => undefined) as never);
    mockApi.listTasks.mockImplementation(async (statusFilter?: string) => {
      if (statusFilter === "active") {
        return [activeTask];
      }
      return [activeTask];
    });
    mockApi.createTask.mockResolvedValue({
      ...activeTask,
      id: 2,
      name: "new-task.zip",
      uri: "https://example.com/new-task.zip",
    });
    mockApi.cancelTask.mockResolvedValue({ ok: true });
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockResolvedValue(undefined),
      },
    });
  });

  afterEach(() => {
    (global.setInterval as jest.Mock).mockRestore?.();
    (global.clearInterval as jest.Mock).mockRestore?.();
  });

  test("renders current tasks and creates a new task", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("任务")).toBeInTheDocument();
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByTestId("stats-widget")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL..."), {
      target: { value: "https://example.com/new-task.zip" },
    });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    await waitFor(() => {
      expect(mockApi.createTask).toHaveBeenCalledWith("https://example.com/new-task.zip");
    });
  });

  test("batch cancels selected active tasks", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
      expect(mockApi.cancelTask).toHaveBeenCalledWith(1);
    });
  });

  test("handles batch add empty and success paths", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));

    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("请输入至少一个链接", "warning");
    });

    fireEvent.change(screen.getByPlaceholderText(/magnet:\?xt=urn:btih/), {
      target: { value: "https://example.com/a\nhttps://example.com/b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(mockApi.createTask).toHaveBeenCalledWith("https://example.com/a");
      expect(mockApi.createTask).toHaveBeenCalledWith("https://example.com/b");
    });
  });
});
