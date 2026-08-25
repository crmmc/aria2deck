import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import TasksPage from "@/app/(authenticated)/tasks/page";
import { api } from "@/lib/api";
import { useTaskWebSocket } from "@/hooks/useTaskWebSocket";
import type {
  BatchCancelTasksResponse,
  BatchCreateTasksResponse,
  CreateTaskItem,
  Task,
  TorrentPreview,
  UploadTorrentRequest,
} from "@/types";
import { ApiError } from "@/lib/api";

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

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    __esModule: true,
    ApiError: actual.ApiError,
    api: {
      listTasks: jest.fn<Promise<Task[]>, [string?]>(),
      createTasks: jest.fn<Promise<BatchCreateTasksResponse>, CreateTaskItem[]>(),
      retryTask: jest.fn<Promise<Task>, [number]>(),
      previewTorrent: jest.fn<Promise<TorrentPreview>, [string]>(),
      uploadTorrent: jest.fn<Promise<Task>, [string, UploadTorrentRequest?]>(),
      cancelTasks: jest.fn<Promise<BatchCancelTasksResponse>, [number[]]>(),
    },
  };
});

const mockApi = api as jest.Mocked<typeof api>;
const mockUseTaskWebSocket = useTaskWebSocket as jest.MockedFunction<typeof useTaskWebSocket>;
let intervalCallbacks: Map<number, () => void>;
let intervalId = 0;

const activeTask: Task = {
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

const torrentPreview = {
  info_hash: "abc",
  name: "Fedora Workstation",
  file_count: 3,
  total_size: 4234,
  files: [
    { index: 1, path: ["Fedora Workstation", "iso.bin"], size: 4096 },
    { index: 2, path: ["Fedora Workstation", "docs", "release.pdf"], size: 48 },
    { index: 3, path: ["Fedora Workstation", "docs", "install.pdf"], size: 90 },
  ],
  tree: [
    {
      type: "directory" as const,
      name: "Fedora Workstation",
      path: ["Fedora Workstation"],
      size: 4234,
      children: [
        {
          type: "file" as const,
          name: "iso.bin",
          path: ["Fedora Workstation", "iso.bin"],
          index: 1,
          size: 4096,
        },
        {
          type: "directory" as const,
          name: "docs",
          path: ["Fedora Workstation", "docs"],
          size: 138,
          children: [
            {
              type: "file" as const,
              name: "release.pdf",
              path: ["Fedora Workstation", "docs", "release.pdf"],
              index: 2,
              size: 48,
            },
            {
              type: "file" as const,
              name: "install.pdf",
              path: ["Fedora Workstation", "docs", "install.pdf"],
              index: 3,
              size: 90,
            },
          ],
        },
      ],
    },
  ],
  limits: { max_files: 5000 },
  default_selection: "all" as const,
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
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [
        {
          input_index: 0,
          accepted: true,
          task_id: 2,
          status: "queued",
          error: null,
        },
      ],
    });
    mockApi.retryTask.mockResolvedValue({
      ...activeTask,
      id: 99,
      name: "retried.zip",
      uri: "https://example.com/ubuntu.iso",
    });
    mockApi.previewTorrent.mockResolvedValue(torrentPreview);
    mockApi.uploadTorrent.mockResolvedValue({
      ...activeTask,
      id: 3,
      name: "Fedora Workstation",
      uri: "magnet:?xt=urn:btih:abc",
    });
    mockApi.cancelTasks.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [
        { task_id: 1, ok: true, state: "cancelled", accepted: true, error: null },
      ],
    });
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockResolvedValue(undefined),
      },
    });
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  const flushAsync = () =>
    act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

  test("renders current tasks and creates a new task in one request", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("任务")).toBeInTheDocument();
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByTestId("stats-widget")).toBeInTheDocument();

    mockApi.listTasks.mockClear();
    const input = screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL...");
    fireEvent.change(input, { target: { value: "https://example.com/new-task.zip" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    await waitFor(() => {
      expect(mockApi.createTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.createTasks).toHaveBeenCalledWith([
        { uri: "https://example.com/new-task.zip" },
      ]);
    });
    await waitFor(() => {
      expect(input).toHaveValue("");
      expect(showToastMock).toHaveBeenCalledWith("任务已提交", "success");
    });
    await waitFor(() => {
      expect(mockApi.listTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.listTasks).toHaveBeenCalledWith("current");
    });
  });

  test("single accepted + refresh reject keeps success semantics and warns", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    mockApi.listTasks.mockRejectedValue(new Error("network down"));
    const input = screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL...");
    fireEvent.change(input, { target: { value: "https://example.com/new-task.zip" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("任务已提交", "success");
      expect(showToastMock).toHaveBeenCalledWith(
        "任务已提交，但列表刷新失败，请手动刷新",
        "warning"
      );
    });
    expect(input).toHaveValue("");
    expect(screen.queryByText(/提交失败/)).not.toBeInTheDocument();
    expect(mockApi.listTasks).toHaveBeenCalledTimes(1);
  });

  test("keeps input and shows item error when single create fails", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [
        {
          input_index: 0,
          accepted: false,
          task_id: null,
          status: null,
          error: "链接无效",
        },
      ],
    });
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();

    const input = screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL...");
    fireEvent.change(input, { target: { value: "ftp://bad" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    await waitFor(() => {
      expect(screen.getByText("链接无效")).toBeInTheDocument();
    });
    expect(input).toHaveValue("ftp://bad");
    expect(mockApi.listTasks).not.toHaveBeenCalled();
  });

  test("shows uncertainty message on 502 for single create", async () => {
    mockApi.createTasks.mockRejectedValue(
      new ApiError("bad gateway", 502)
    );
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();

    const input = screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL...");
    fireEvent.change(input, { target: { value: "https://example.com/x" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    await waitFor(() => {
      expect(
        screen.getByText("提交结果暂无法确认，请刷新任务列表")
      ).toBeInTheDocument();
    });
    expect(input).toHaveValue("https://example.com/x");
    expect(mockApi.listTasks).not.toHaveBeenCalled();
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

  test("batch cancels selected active tasks in one request", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
      expect(mockApi.cancelTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.cancelTasks).toHaveBeenCalledWith([1]);
    });
  });

  test("cancels a single task inline via one-element array", async () => {
    render(<TasksPage />);

    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("取消任务"));

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalled();
      expect(mockApi.cancelTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.cancelTasks).toHaveBeenCalledWith([1]);
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
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 2,
      failed_count: 0,
      results: [],
    });

    mockApi.listTasks.mockClear();
    fireEvent.change(screen.getByPlaceholderText(/magnet:\?xt=urn:btih/), {
      target: {
        value:
          "https://example.com/a\n\nhttps://example.com/a\nhttps://example.com/b",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(mockApi.createTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.createTasks).toHaveBeenCalledWith([
        { uri: "https://example.com/a" },
        { uri: "https://example.com/b" },
      ]);
    });
    await waitFor(() => {
      expect(mockApi.listTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.listTasks).toHaveBeenCalledWith("current");
      expect(showToastMock).toHaveBeenCalledWith("已提交 2 个任务", "success");
    });
    expect(screen.queryByLabelText("批量下载链接")).not.toBeInTheDocument();
  });

  test("batch partial failure closes dialog with summary toast", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 5,
      failed_count: 2,
      results: [],
    });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: {
        value: Array.from({ length: 7 }, (_, i) => `https://example.com/${i}`).join("\n"),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(mockApi.createTasks).toHaveBeenCalledTimes(1);
      expect(showToastMock).toHaveBeenCalledWith(
        "提交完成：成功5个，失败2个",
        "warning"
      );
    });
    expect(screen.queryByLabelText("批量下载链接")).not.toBeInTheDocument();
    expect(mockApi.listTasks).toHaveBeenCalledTimes(1);
  });

  test("batch partial accepted + refresh reject closes modal and warns", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 5,
      failed_count: 2,
      results: [],
    });
    mockApi.listTasks.mockRejectedValue(new Error("network down"));
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/a\nhttps://example.com/b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "提交完成：成功5个，失败2个",
        "warning"
      );
      expect(showToastMock).toHaveBeenCalledWith(
        "任务已提交，但列表刷新失败，请手动刷新",
        "warning"
      );
    });
    expect(screen.queryByLabelText("批量下载链接")).not.toBeInTheDocument();
    expect(mockApi.listTasks).toHaveBeenCalledTimes(1);
  });

  test("batch all failed keeps textarea and shows single item error", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [
        {
          input_index: 0,
          accepted: false,
          task_id: null,
          status: null,
          error: "配额不足",
        },
      ],
    });
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/only" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(screen.getByText("配额不足")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("批量下载链接")).toHaveValue(
      "https://example.com/only"
    );
    expect(mockApi.listTasks).not.toHaveBeenCalled();
  });

  test("batch 502 keeps input without refresh", async () => {
    mockApi.createTasks.mockRejectedValue(new ApiError("bad gateway", 502));
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(
        screen.getByText("提交结果暂无法确认，请刷新任务列表")
      ).toBeInTheDocument();
    });
    expect(screen.getByLabelText("批量下载链接")).toHaveValue(
      "https://example.com/x"
    );
    expect(mockApi.listTasks).not.toHaveBeenCalled();
  });

  test("rejects batch input over 30 unique links without requests", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));

    const uris = Array.from(
      { length: 31 },
      (_, index) => `https://example.com/${index}`
    ).join("\n");
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: uris },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "一次最多添加 30 个任务",
        "warning"
      );
    });
    expect(mockApi.createTasks).not.toHaveBeenCalled();
  });

  test("does not refresh list while batch request is in flight", async () => {
    let releaseGate = () => {};
    const gate = new Promise<void>((resolve) => {
      releaseGate = resolve;
    });
    mockApi.createTasks.mockImplementation(async () => {
      await gate;
      return { accepted_count: 1, failed_count: 0, results: [] };
    });
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: {
        value: Array.from({ length: 8 }, (_, i) => `https://example.com/${i}`).join("\n"),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => expect(mockApi.createTasks).toHaveBeenCalledTimes(1));
    expect(mockApi.listTasks).not.toHaveBeenCalled();

    // 提交中禁止重复提交
    fireEvent.click(screen.getByRole("button", { name: "添加中..." }));
    expect(mockApi.createTasks).toHaveBeenCalledTimes(1);

    releaseGate();
    await waitFor(() => {
      expect(mockApi.listTasks).toHaveBeenCalledTimes(1);
      expect(mockApi.listTasks).toHaveBeenCalledWith("current");
    });
    expect(screen.queryByLabelText("批量下载链接")).not.toBeInTheDocument();
  });

  test("rejects torrent over 10 MiB before reading it", async () => {
    const readAsDataURL = jest.spyOn(FileReader.prototype, "readAsDataURL");
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    const file = new File(["torrent"], "large.torrent", {
      type: "application/x-bittorrent",
    });
    Object.defineProperty(file, "size", { value: 10 * 1024 * 1024 + 1 });
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [file] },
    });

    expect(
      await screen.findByText("种子文件过大，最大支持 10 MB")
    ).toBeInTheDocument();
    expect(readAsDataURL).not.toHaveBeenCalled();
    expect(mockApi.previewTorrent).not.toHaveBeenCalled();
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

  test("retries error task via retryTask(id) not createTask(uri)", async () => {
    mockApi.listTasks.mockImplementation(async () => [
      {
        ...activeTask,
        status: "error",
        error: "network error",
      },
    ]);

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(mockApi.retryTask).toHaveBeenCalledWith(1);
    });
    expect(mockApi.createTasks).not.toHaveBeenCalled();
  });

  test("keeps paused and waiting tasks in list after websocket update", async () => {
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
        status: "paused",
        error: "任务已被外部暂停，请联系管理员处理",
      });
    });

    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByText("已暂停")).toBeInTheDocument();
    expect(
      screen.getByText("任务已被外部暂停，请联系管理员处理")
    ).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        status: "waiting",
        error: null,
      });
    });

    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.getByText("等待中")).toBeInTheDocument();
  });

  test("renders backend status_label for queued and paused tasks", async () => {
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
        status: "queued",
        status_label: "排队中(配额)",
      });
    });

    expect(screen.getByText("排队中(配额)")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        status: "paused",
        status_label: "已暂停",
      });
    });

    expect(screen.getByText("已暂停")).toBeInTheDocument();
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

  test("previews torrent and creates selected torrent task", async () => {
    const readAsDataURL = jest
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        Object.defineProperty(this, "result", {
          configurable: true,
          value: "data:application/x-bittorrent;base64,dG9ycmVudA==",
        });
        this.onload?.({} as ProgressEvent<FileReader>);
      });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    const input = screen.getByLabelText("上传种子文件");
    const file = new File(["torrent"], "fedora.torrent", {
      type: "application/x-bittorrent",
    });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(mockApi.previewTorrent).toHaveBeenCalledWith("dG9ycmVudA==");
    });

    expect(await screen.findByText("添加 BT 下载任务")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("搜索文件")).toBeInTheDocument();
    expect(screen.getByText("Fedora Workstation")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: /release\.pdf/ }));
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));

    expect(await screen.findByText("确认下载内容")).toBeInTheDocument();
    expect(screen.getByText("iso.bin")).toBeInTheDocument();
    expect(screen.queryByText("release.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("install.pdf")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认" }));

    await waitFor(() => {
      expect(mockApi.uploadTorrent).toHaveBeenCalledWith("dG9ycmVudA==", {
        selected_file_indexes: [1, 3],
      });
    });

    readAsDataURL.mockRestore();
  });

  test("torrent search filters rows without changing selection", async () => {
    const readAsDataURL = jest
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        Object.defineProperty(this, "result", {
          configurable: true,
          value: "data:application/x-bittorrent;base64,dG9ycmVudA==",
        });
        this.onload?.({} as ProgressEvent<FileReader>);
      });

    render(<TasksPage />);
    const input = screen.getByLabelText("上传种子文件");
    fireEvent.change(input, {
      target: {
        files: [
          new File(["torrent"], "fedora.torrent", {
            type: "application/x-bittorrent",
          }),
        ],
      },
    });

    expect(await screen.findByText("添加 BT 下载任务")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("搜索文件"), {
      target: { value: "install" },
    });

    expect(screen.getByText("install.pdf")).toBeInTheDocument();
    expect(screen.queryByText("release.pdf")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    expect(await screen.findByText("release.pdf")).toBeInTheDocument();

    readAsDataURL.mockRestore();
  });

  test("torrent cancel asks for confirmation", async () => {
    const readAsDataURL = jest
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        Object.defineProperty(this, "result", {
          configurable: true,
          value: "data:application/x-bittorrent;base64,dG9ycmVudA==",
        });
        this.onload?.({} as ProgressEvent<FileReader>);
      });

    render(<TasksPage />);
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: {
        files: [
          new File(["torrent"], "fedora.torrent", {
            type: "application/x-bittorrent",
          }),
        ],
      },
    });

    expect(await screen.findByText("添加 BT 下载任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(screen.getByRole("dialog", { name: "取消添加任务" })).toBeInTheDocument();
    expect(screen.getByText("取消添加任务？")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认取消" }));
    expect(
      screen.queryByText("添加 BT 下载任务")
    ).not.toBeInTheDocument();

    readAsDataURL.mockRestore();
  });

  test("torrent preview failure shows error text", async () => {
    mockApi.previewTorrent.mockRejectedValue(new Error("预览失败"));
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["torrent"], "fedora.torrent")] },
    });

    expect(await screen.findByText("预览失败")).toBeInTheDocument();
  });

  test("torrent upload rejects non-torrent files", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] },
    });

    expect(await screen.findByText("请选择 .torrent 文件")).toBeInTheDocument();
    expect(mockApi.previewTorrent).not.toHaveBeenCalled();
  });

  test.each([
    ["文件读取结果无效", (reader: FileReader) => {
      Object.defineProperty(reader, "result", { configurable: true, value: null });
      reader.onload?.({} as ProgressEvent<FileReader>);
    }],
    ["文件编码格式无效", (reader: FileReader) => {
      Object.defineProperty(reader, "result", { configurable: true, value: "data:application/x-bittorrent;base64," });
      reader.onload?.({} as ProgressEvent<FileReader>);
    }],
    ["文件读取失败", (reader: FileReader) => {
      reader.onerror?.({} as ProgressEvent<FileReader>);
    }],
  ])("torrent upload shows error '%s'", async (message, trigger) => {
    const readAsDataURL = jest
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        trigger(this);
      });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["torrent"], "fedora.torrent")] },
    });

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(mockApi.previewTorrent).not.toHaveBeenCalled();

    readAsDataURL.mockRestore();
  });

  test("torrent created with existing id replaces the task in place", async () => {
    mockApi.uploadTorrent.mockResolvedValue({
      ...activeTask,
      name: "Fedora Workstation (updated)",
    });
    const readAsDataURL = jest
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        Object.defineProperty(this, "result", {
          configurable: true,
          value: "data:application/x-bittorrent;base64,dG9ycmVudA==",
        });
        this.onload?.({} as ProgressEvent<FileReader>);
      });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["torrent"], "fedora.torrent")] },
    });

    expect(await screen.findByText("添加 BT 下载任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    expect(await screen.findByText("确认下载内容")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认" }));

    await waitFor(() => {
      expect(
        screen.getByText("Fedora Workstation (updated)")
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();

    readAsDataURL.mockRestore();
  });

  test("torrent created with non-visible status closes wizard without adding task", async () => {
    mockApi.uploadTorrent.mockResolvedValue({
      ...activeTask,
      status: "complete",
      completed_length: 1024 * 1024 * 1024,
    });
    const readAsDataURL = jest
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        Object.defineProperty(this, "result", {
          configurable: true,
          value: "data:application/x-bittorrent;base64,dG9ycmVudA==",
        });
        this.onload?.({} as ProgressEvent<FileReader>);
      });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["torrent"], "fedora.torrent")] },
    });

    expect(await screen.findByText("添加 BT 下载任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    expect(await screen.findByText("确认下载内容")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认" }));

    await waitFor(() => {
      expect(mockApi.uploadTorrent).toHaveBeenCalled();
    });
    expect(
      screen.queryByText("添加 BT 下载任务")
    ).not.toBeInTheDocument();
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();

    readAsDataURL.mockRestore();
  });

  test("shows raw error message when single create fails with non-502 error", async () => {
    mockApi.createTasks.mockRejectedValue(new ApiError("服务器繁忙", 500));
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();

    const input = screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL...");
    fireEvent.change(input, { target: { value: "https://example.com/y" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    expect(await screen.findByText("服务器繁忙")).toBeInTheDocument();
    expect(input).toHaveValue("https://example.com/y");
    expect(mockApi.listTasks).not.toHaveBeenCalled();
  });

  test("batch all failed with multiple results keeps dialog and shows count error", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 0,
      failed_count: 2,
      results: [
        { input_index: 0, accepted: false, task_id: null, status: null, error: "配额不足" },
        { input_index: 1, accepted: false, task_id: null, status: null, error: "链接无效" },
      ],
    });
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/a\nhttps://example.com/b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    expect(
      await screen.findByText("提交失败：2 个任务均未成功")
    ).toBeInTheDocument();
    expect(screen.getByLabelText("批量下载链接")).toHaveValue(
      "https://example.com/a\nhttps://example.com/b"
    );
    expect(mockApi.listTasks).not.toHaveBeenCalled();
  });

  test("shows raw error message when batch create fails with non-502 error", async () => {
    mockApi.createTasks.mockRejectedValue(new ApiError("服务器繁忙", 500));
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    mockApi.listTasks.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/y" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    expect(await screen.findByText("服务器繁忙")).toBeInTheDocument();
    expect(screen.getByLabelText("批量下载链接")).toHaveValue(
      "https://example.com/y"
    );
    expect(mockApi.listTasks).not.toHaveBeenCalled();
  });

  test("falls back to default filter when localStorage read fails", async () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    jest
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("quota exceeded");
      });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    expect(screen.getByLabelText("筛选任务")).toHaveValue("all");
    expect(warnSpy).toHaveBeenCalledWith(
      "读取任务筛选条件失败",
      expect.any(Error)
    );
    warnSpy.mockRestore();
  });

  test("warns when persisting filter status fails", async () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    jest
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("quota exceeded");
      });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    await waitFor(() => {
      expect(warnSpy).toHaveBeenCalledWith(
        "保存任务筛选条件失败",
        expect.any(Error)
      );
    });
    warnSpy.mockRestore();
  });

  test("websocket notification is surfaced as toast", async () => {
    let wsCallbacks: {
      onNotification?: (message: string, level: "info" | "warning" | "error") => void;
    } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onNotification?.("存储空间不足", "warning");
    });

    expect(showToastMock).toHaveBeenCalledWith("存储空间不足", "warning");
  });

  test("task completing via websocket notifies and removes it from the list", async () => {
    let wsCallbacks: { onTaskUpdate: (task: Task) => void } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        status: "complete",
        completed_length: 1024 * 1024 * 1024,
      });
    });

    expect(showToastMock).toHaveBeenCalledWith("ubuntu.iso 下载完成", "success");
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
  });

  test("ignores websocket updates for already deleted tasks", async () => {
    let wsCallbacks: {
      onTaskUpdate: (task: Task) => void;
    } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("取消任务"));
    await waitFor(() => {
      expect(mockApi.cancelTasks).toHaveBeenCalledWith([1]);
    });
    await waitFor(() => {
      expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
    });

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        status: "complete",
        completed_length: 1024 * 1024 * 1024,
      });
    });

    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
    expect(showToastMock).not.toHaveBeenCalledWith(
      "ubuntu.iso 下载完成",
      "success"
    );
  });

  test.each([
    [
      "item failure",
      {
        accepted_count: 0,
        failed_count: 1,
        results: [{ task_id: 1, ok: false, state: "paused", accepted: false, error: "已被外部暂停" }],
      },
      "取消失败：已被外部暂停",
    ],
    [
      "missing result item",
      { accepted_count: 0, failed_count: 1, results: [] },
      "取消失败：未知错误",
    ],
  ])(
    "inline cancel failure '%s' shows error toast",
    async (_name, response, expectedToast) => {
      mockApi.cancelTasks.mockResolvedValue(response);
      render(<TasksPage />);
      expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

      fireEvent.click(screen.getByTitle("取消任务"));
      await waitFor(() => expect(showConfirmMock).toHaveBeenCalled());
      await waitFor(() => {
        expect(mockApi.cancelTasks).toHaveBeenCalledWith([1]);
      });
      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith(expectedToast, "error");
      });
      expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    }
  );

  test("deleting a failed task uses delete wording", async () => {
    mockApi.listTasks.mockImplementation(async () => [
      { ...activeTask, status: "error", error: "network error" },
    ]);
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("删除失败任务"));
    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalledWith(
        expect.objectContaining({ title: "删除任务", confirmText: "删除" })
      );
    });
  });

  test.each([
    [{ retryable: false, retry_blocked_reason: "任务已被外部暂停" }, "任务已被外部暂停"],
    [{ retryable: false }, "不可重试"],
  ])(
    "non-retryable task shows blocked reason",
    async (overrides, expectedToast) => {
      mockApi.listTasks.mockImplementation(async () => [
        { ...activeTask, status: "error", error: "network error", ...overrides },
      ]);
      render(<TasksPage />);
      expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith(expectedToast, "warning");
      });
      expect(mockApi.retryTask).not.toHaveBeenCalled();
    }
  );

  test("retry replaces existing task via upsert", async () => {
    mockApi.listTasks.mockImplementation(async () => [
      { ...activeTask, status: "error", error: "network error" },
    ]);
    mockApi.retryTask.mockResolvedValue({
      ...activeTask,
      name: "ubuntu-retried.iso",
      status: "queued",
    });
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(screen.getByText("ubuntu-retried.iso")).toBeInTheDocument();
    });
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
    expect(showToastMock).toHaveBeenCalledWith("已重新添加下载任务", "success");
  });

  test("batch cancel reports partial failures", async () => {
    mockApi.listTasks.mockImplementation(async () => [
      activeTask,
      { ...activeTask, id: 2, name: "centos.iso", uri: "https://example.com/centos.iso" },
    ]);
    mockApi.cancelTasks.mockResolvedValue({
      accepted_count: 1,
      failed_count: 1,
      results: [
        { task_id: 1, ok: true, state: "cancelled", accepted: true, error: null },
        { task_id: 2, ok: false, state: "error", accepted: false, error: "取消失败" },
      ],
    });
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));

    await waitFor(() => {
      expect(mockApi.cancelTasks).toHaveBeenCalledWith([1, 2]);
    });
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "已取消 1 个任务，1 个取消失败",
        "warning"
      );
    });
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
    expect(screen.getByText("centos.iso")).toBeInTheDocument();
  });


  test("torrent preview owned-file failure shows warning toast only", async () => {
    mockApi.previewTorrent.mockRejectedValue(new Error("您已拥有此文件"));
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["torrent"], "fedora.torrent")] },
    });

    await flushAsync();
    await waitFor(() =>
      expect(mockApi.previewTorrent).toHaveBeenCalledTimes(1)
    );
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("您已拥有此文件", "warning");
    });
    expect(screen.queryByText("您已拥有此文件")).not.toBeInTheDocument();
  });

  test("polling warns when fetching active tasks fails", async () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    mockApi.listTasks.mockImplementation(async (statusFilter?: string) => {
      if (statusFilter === "active") {
        throw new Error("network down");
      }
      return [activeTask];
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });
    await flushAsync();

    await waitFor(() => {
      expect(warnSpy).toHaveBeenCalledWith(
        "轮询活动任务失败",
        expect.any(Error)
      );
    });
    warnSpy.mockRestore();
  });

  test("polling warns when follow-up current refresh fails", async () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    let currentCalls = 0;
    mockApi.listTasks.mockImplementation(async (statusFilter?: string) => {
      if (statusFilter === "active") {
        return [];
      }
      currentCalls += 1;
      if (currentCalls === 1) {
        return [activeTask];
      }
      throw new Error("network down");
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });
    await flushAsync();

    expect(mockApi.listTasks).toHaveBeenCalledWith("active");
    await waitFor(() => {
      expect(warnSpy).toHaveBeenCalledWith(
        "刷新当前任务列表失败",
        expect.any(Error)
      );
    });
    warnSpy.mockRestore();
  });

  test.each([
    [new Error("network down"), "network down"],
    ["plain failure", "未知错误"],
  ])(
    "websocket sync failure surfaces warning toast",
    async (rejection, expectedMessage) => {
      let wsCallbacks: { onConnected?: () => void } | null = null;
      mockUseTaskWebSocket.mockImplementation((callbacks) => {
        wsCallbacks = callbacks;
      });
      mockApi.listTasks.mockRejectedValue(rejection);

      render(<TasksPage />);
      expect(await screen.findByText("任务")).toBeInTheDocument();

      act(() => {
        wsCallbacks?.onConnected?.();
      });
      await flushAsync();

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith(
          `同步任务状态失败: ${expectedMessage}`,
          "warning"
        );
      });
    }
  );

  test("inline cancel request rejection shows error toast", async () => {
    mockApi.cancelTasks.mockRejectedValue(new Error("network down"));
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("取消任务"));
    await waitFor(() => expect(showConfirmMock).toHaveBeenCalled());
    await waitFor(() => {
      expect(mockApi.cancelTasks).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "取消失败：network down",
        "error"
      );
    });
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
  });

  test("retry failure shows error toast", async () => {
    mockApi.listTasks.mockImplementation(async () => [
      { ...activeTask, status: "error", error: "network error" },
    ]);
    mockApi.retryTask.mockRejectedValue(new Error("network down"));
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(mockApi.retryTask).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "重试失败：network down",
        "error"
      );
    });
  });

  test("batch cancel request rejection shows error toast", async () => {
    mockApi.cancelTasks.mockRejectedValue(new Error("network down"));
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    await waitFor(() => expect(showConfirmMock).toHaveBeenCalled());
    await waitFor(() => {
      expect(mockApi.cancelTasks).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith(
        "批量取消失败：network down",
        "error"
      );
    });
  });

  test("escape while batch adding keeps the dialog open", async () => {
    let releaseGate = () => {};
    const gate = new Promise<void>((resolve) => {
      releaseGate = resolve;
    });
    mockApi.createTasks.mockImplementation(async () => {
      await gate;
      return { accepted_count: 1, failed_count: 0, results: [] };
    });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/a" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    await waitFor(() => expect(mockApi.createTasks).toHaveBeenCalledTimes(1));

    fireEvent(
      screen.getByRole("dialog", { name: "批量添加任务" }),
      new Event("cancel")
    );
    expect(screen.getByLabelText("批量下载链接")).toBeInTheDocument();

    releaseGate();
    await waitFor(() =>
      expect(screen.queryByLabelText("批量下载链接")).not.toBeInTheDocument()
    );
  });

  test("escape closes the batch dialog and clears input", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/a" },
    });

    fireEvent(
      screen.getByRole("dialog", { name: "批量添加任务" }),
      new Event("cancel")
    );

    expect(screen.queryByLabelText("批量下载链接")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    expect(screen.getByLabelText("批量下载链接")).toHaveValue("");
  });

  test("filters, searches and sorts tasks through the toolbar", async () => {
    const tasks: Task[] = [
      activeTask,
      {
        ...activeTask,
        id: 2,
        name: "debian.iso",
        uri: "https://example.com/debian.iso",
        status: "queued",
        download_speed: 0,
        total_length: 0,
        completed_length: 0,
      },
      {
        ...activeTask,
        id: 3,
        name: "arch.iso",
        uri: "https://example.com/arch.iso",
        status: "waiting",
        download_speed: 1024,
        total_length: 2048,
        completed_length: 2048,
      },
      {
        ...activeTask,
        id: 4,
        name: "alpine.iso",
        uri: "https://example.com/alpine.iso",
        status: "complete",
        completed_length: 1024 * 1024 * 1024,
        download_speed: 0,
      },
      { ...activeTask, id: 5, name: null, uri: "https://example.com/x" },
    ];
    mockApi.listTasks.mockImplementation(async () => tasks);
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    const bodyOrder = (name: string) =>
      document.body.textContent?.indexOf(name) ?? -1;

    fireEvent.change(screen.getByLabelText("搜索任务"), {
      target: { value: "arch" },
    });
    expect(screen.getByText("arch.iso")).toBeInTheDocument();
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "" } });

    fireEvent.change(screen.getByLabelText("筛选任务"), {
      target: { value: "active" },
    });
    expect(screen.getByText("debian.iso")).toBeInTheDocument();
    expect(screen.getByText("arch.iso")).toBeInTheDocument();
    expect(screen.queryByText("alpine.iso")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("筛选任务"), {
      target: { value: "all" },
    });

    fireEvent.change(screen.getByLabelText("排序方式"), {
      target: { value: "speed" },
    });
    await waitFor(() => {
      expect(bodyOrder("ubuntu.iso")).toBeLessThan(bodyOrder("arch.iso"));
    });

    fireEvent.change(screen.getByLabelText("排序方式"), {
      target: { value: "progress" },
    });
    await waitFor(() => {
      expect(bodyOrder("arch.iso")).toBeLessThan(bodyOrder("ubuntu.iso"));
      expect(bodyOrder("ubuntu.iso")).toBeLessThan(bodyOrder("debian.iso"));
    });
  });

  test("toggle select all switches between selecting and clearing", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(screen.getByRole("button", { name: "取消全选" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
  });

  test("skips fallback polling while document is hidden", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();
    mockApi.listTasks.mockClear();

    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => true,
    });
    try {
      act(() => {
        intervalCallbacks.forEach((callback) => callback());
      });
      await flushAsync();
    } finally {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        get: () => false,
      });
    }

    expect(mockApi.listTasks).not.toHaveBeenCalledWith("active");
  });

  test("skips overlapping fallback poll while previous one is in flight", async () => {
    let releaseGate = () => {};
    const gate = new Promise<void>((resolve) => {
      releaseGate = resolve;
    });
    mockApi.listTasks.mockImplementation(async (statusFilter?: string) => {
      if (statusFilter === "active") {
        await gate;
        return [activeTask];
      }
      return [activeTask];
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();
    mockApi.listTasks.mockClear();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });
    await flushAsync();
    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });
    await flushAsync();

    expect(mockApi.listTasks).toHaveBeenCalledTimes(1);

    releaseGate();
    await flushAsync();
  });

  test("single create with null item error falls back to generic message", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [
        { input_index: 0, accepted: false, task_id: null, status: null, error: null },
      ],
    });
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    const input = screen.getByPlaceholderText("粘贴磁力链接、HTTP 或 FTP URL...");
    fireEvent.change(input, { target: { value: "ftp://bad" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));

    expect(await screen.findByText("提交失败")).toBeInTheDocument();
  });

  test("batch all failed with null item error falls back to generic message", async () => {
    mockApi.createTasks.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [
        { input_index: 0, accepted: false, task_id: null, status: null, error: null },
      ],
    });
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: { value: "https://example.com/only" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    expect(await screen.findByText("提交失败")).toBeInTheDocument();
    expect(screen.getByLabelText("批量下载链接")).toBeInTheDocument();
  });

  test("torrent upload change without file is a no-op", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [] },
    });
    await flushAsync();

    expect(mockApi.previewTorrent).not.toHaveBeenCalled();
  });

  test.each([
    ["single cancel", "取消下载", () => screen.getByTitle("取消任务")],
    ["batch cancel", "批量取消", () => screen.getByRole("button", { name: "取消下载" })],
  ])("aborts %s when confirmation is dismissed", async (_name, _title, getTrigger) => {
    showConfirmMock.mockResolvedValueOnce(false);
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    if (_title === "批量取消") {
      fireEvent.click(screen.getAllByRole("checkbox")[0]);
    }
    fireEvent.click(getTrigger());
    await waitFor(() => expect(showConfirmMock).toHaveBeenCalled());
    await flushAsync();

    expect(mockApi.cancelTasks).not.toHaveBeenCalled();
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
  });

  test.each([
    [
      "result item failure",
      {
        accepted_count: 0,
        failed_count: 1,
        results: [
          { task_id: 1, ok: false, state: "error", accepted: false, error: "任务已失败" },
        ],
      },
      "删除失败：任务已失败",
    ],
    [
      "request rejection",
      null,
      "删除失败：network down",
    ],
  ])(
    "deleting a failed task reports '%s'",
    async (_name, response, expectedToast) => {
      if (response === null) {
        mockApi.cancelTasks.mockRejectedValue(new Error("network down"));
      } else {
        mockApi.cancelTasks.mockResolvedValue(response);
      }
      mockApi.listTasks.mockImplementation(async () => [
        { ...activeTask, status: "error", error: "network error" },
      ]);
      render(<TasksPage />);
      expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

      fireEvent.click(screen.getByTitle("删除失败任务"));
      await waitFor(() => expect(showConfirmMock).toHaveBeenCalled());
      await flushAsync();

      await waitFor(() => {
        expect(showToastMock).toHaveBeenCalledWith(expectedToast, "error");
      });
      expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    }
  );

  test("deleting a failed task without name shows fallback name", async () => {
    mockApi.listTasks.mockImplementation(async () => [
      { ...activeTask, name: null, status: "error", error: "network error" },
    ]);
    render(<TasksPage />);
    expect(await screen.findByText("未知文件")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("删除失败任务"));
    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '确定要删除失败任务 "未知文件" 吗？',
        })
      );
    });
  });

  test("websocket update for unknown completed task is a no-op", async () => {
    let wsCallbacks: { onTaskUpdate: (task: Task) => void } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        id: 999,
        status: "complete",
        completed_length: 1024 * 1024 * 1024,
      });
    });

    expect(showToastMock).not.toHaveBeenCalled();
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
    expect(screen.queryByText("下载任务")).not.toBeInTheDocument();
  });

  test("websocket error for existing unnamed task uses fallback name", async () => {
    let wsCallbacks: { onTaskUpdate: (task: Task) => void } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });
    mockApi.listTasks.mockImplementation(async () => [
      { ...activeTask, name: null },
    ]);

    render(<TasksPage />);
    expect(await screen.findByText("未知文件")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({ ...activeTask, name: null, status: "error", error: "x" });
    });

    expect(showToastMock).toHaveBeenCalledWith("下载任务 下载失败", "error");
  });

  test("polling keeps deleted tasks out of the refreshed list", async () => {
    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("取消任务"));
    await waitFor(() => {
      expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
    });
    await flushAsync();

    act(() => {
      intervalCallbacks.forEach((callback) => callback());
    });
    await flushAsync();

    expect(mockApi.listTasks).toHaveBeenCalledWith("active");
    expect(screen.queryByText("ubuntu.iso")).not.toBeInTheDocument();
  });

  test("websocket update for new visible task prepends it to the list", async () => {
    let wsCallbacks: { onTaskUpdate: (task: Task) => void } | null = null;
    mockUseTaskWebSocket.mockImplementation((callbacks) => {
      wsCallbacks = callbacks;
    });

    render(<TasksPage />);
    expect(await screen.findByText("ubuntu.iso")).toBeInTheDocument();

    act(() => {
      wsCallbacks?.onTaskUpdate({
        ...activeTask,
        id: 42,
        name: "new-task.zip",
      });
    });

    expect(screen.getByText("new-task.zip")).toBeInTheDocument();
    expect(screen.getByText("ubuntu.iso")).toBeInTheDocument();
  });
});
