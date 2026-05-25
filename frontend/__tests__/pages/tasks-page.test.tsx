import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import TasksPage from "@/app/(authenticated)/tasks/page";
import { api } from "@/lib/api";
import { useTaskWebSocket } from "@/hooks/useTaskWebSocket";
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
const mockUseTaskWebSocket = useTaskWebSocket as jest.MockedFunction<typeof useTaskWebSocket>;
let intervalCallbacks: Map<number, () => void>;
let intervalId = 0;

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
    mockUseTaskWebSocket.mockImplementation(() => undefined);
    showConfirmMock.mockResolvedValue(true);
    intervalCallbacks = new Map();
    intervalId = 0;
    jest.spyOn(global, "setInterval").mockImplementation(((callback: () => void) => {
      intervalId += 1;
      intervalCallbacks.set(intervalId, callback);
      return intervalId;
    }) as never);
    jest.spyOn(global, "clearInterval").mockImplementation(((id: number) => {
      intervalCallbacks.delete(id);
    }) as never);
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

  test("does not round an active partial task up to 100 percent", async () => {
    mockApi.listTasks.mockResolvedValue([
      {
        ...activeTask,
        total_length: 1000,
        completed_length: 998,
        download_speed: 0,
      },
    ]);

    render(<TasksPage />);

    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByText("99.8%")).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
  });

  test("uses a native button for the task copy target", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ubuntu\.iso/ }).tagName).toBe("BUTTON");
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

  test("keeps error task in list after websocket update", async () => {
    let wsCallbacks: {
      onTaskUpdate: (task: Task) => void;
    } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        status: "error",
        error: "network error",
      });
    });

    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  test("pauses fallback polling while websocket is connected and resumes after disconnect", async () => {
    let wsCallbacks: {
      onConnected?: () => void;
      onDisconnected?: () => void;
    } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onConnected?.();
    });

    await waitFor(() => {
      expect(mockApi.listTasks).toHaveBeenCalledWith("current");
    });
    mockApi.listTasks.mockClear();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });

    expect(mockApi.listTasks).not.toHaveBeenCalledWith("active");

    act(() => {
      wsCallbacks?.onDisconnected?.();
    });
    mockApi.listTasks.mockClear();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });

    await waitFor(() => {
      expect(mockApi.listTasks).toHaveBeenCalledWith("active");
    });
  });

  test("fallback polling refreshes current tasks when an active task disappears", async () => {
    const queuedTask: Task = {
      ...activeTask,
      id: 2,
      name: "queued.iso",
      status: "queued",
    };

    mockApi.listTasks
      .mockResolvedValueOnce([activeTask])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([queuedTask]);

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });

    await waitFor(() => {
      expect(mockApi.listTasks).toHaveBeenCalledWith("active");
      expect(mockApi.listTasks).toHaveBeenCalledWith("current");
      expect(screen.getByText("queued.iso")).toBeInTheDocument();
    });
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
  });
});
