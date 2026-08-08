import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import TasksPage from "@/app/(authenticated)/tasks/page";
import { api } from "@/lib/api";
import { useTaskWebSocket } from "@/hooks/useTaskWebSocket";
import type { Task, TorrentPreview, UploadTorrentRequest } from "@/types";

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
    createTask: jest.fn<Promise<Task>, [string, AbortSignal?]>(),
    previewTorrent: jest.fn<Promise<TorrentPreview>, [string]>(),
    uploadTorrent: jest.fn<Promise<Task>, [string, UploadTorrentRequest?]>(),
    cancelTask: jest.fn<Promise<{ ok: boolean }>, [number]>(),
  },
}));

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
    mockApi.createTask.mockResolvedValue({
      ...activeTask,
      id: 2,
      name: "new-task.zip",
      uri: "https://example.com/new-task.zip",
    });
    mockApi.previewTorrent.mockResolvedValue(torrentPreview);
    mockApi.uploadTorrent.mockResolvedValue({
      ...activeTask,
      id: 3,
      name: "Fedora Workstation",
      uri: "magnet:?xt=urn:btih:abc",
    });
    mockApi.cancelTask.mockResolvedValue({ ok: true });
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockResolvedValue(undefined),
      },
    });
  });

  afterEach(() => {
    jest.restoreAllMocks();
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
      target: {
        value:
          "https://example.com/a\n\nhttps://example.com/a\nhttps://example.com/b",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => {
      expect(mockApi.createTask).toHaveBeenCalledWith(
        "https://example.com/a",
        expect.anything()
      );
      expect(mockApi.createTask).toHaveBeenCalledWith(
        "https://example.com/b",
        expect.anything()
      );
      expect(mockApi.createTask).toHaveBeenCalledTimes(2);
    });
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
    expect(mockApi.createTask).not.toHaveBeenCalled();
  });

  test("limits batch concurrency and reports partial failures", async () => {
    let activeRequests = 0;
    let maxActiveRequests = 0;
    let releaseGate = () => {};
    const gate = new Promise<void>((resolve) => {
      releaseGate = resolve;
    });
    mockApi.createTask.mockImplementation(async (uri) => {
      activeRequests++;
      maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
      try {
        await gate;
        if (uri.includes("fail")) throw new Error("failed");
        return {
          ...activeTask,
          id: mockApi.createTask.mock.calls.length + 10,
          name: uri,
          uri,
        };
      } finally {
        activeRequests--;
      }
    });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: {
        value: [
          "https://example.com/1",
          "https://example.com/fail-2",
          "https://example.com/3",
          "https://example.com/4",
          "https://example.com/5",
          "https://example.com/fail-6",
          "https://example.com/7",
        ].join("\n"),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => expect(mockApi.createTask).toHaveBeenCalledTimes(3));
    expect(maxActiveRequests).toBe(3);
    releaseGate();

    await waitFor(() => {
      expect(mockApi.createTask).toHaveBeenCalledTimes(7);
      expect(showToastMock).toHaveBeenCalledWith(
        "添加完成：成功 5 个，失败 2 个",
        "warning"
      );
    });
    expect(maxActiveRequests).toBeLessThanOrEqual(3);
  });

  test("stops scheduling batch tasks when cancelled", async () => {
    const signals: AbortSignal[] = [];
    mockApi.createTask.mockImplementation((_uri, signal) => {
      if (!signal) throw new Error("missing abort signal");
      signals.push(signal);
      return new Promise<Task>((_resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true }
        );
      });
    });

    render(<TasksPage />);
    expect(await screen.findByText("任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    fireEvent.change(screen.getByLabelText("批量下载链接"), {
      target: {
        value: Array.from(
          { length: 8 },
          (_, index) => `https://example.com/${index}`
        ).join("\n"),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));

    await waitFor(() => expect(mockApi.createTask).toHaveBeenCalledTimes(3));
    const cancelButtons = screen.getAllByRole("button", { name: "取消" });
    fireEvent.click(cancelButtons[cancelButtons.length - 1]);

    await waitFor(() => expect(signals.every((signal) => signal.aborted)).toBe(true));
    expect(mockApi.createTask).toHaveBeenCalledTimes(3);
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

    readAsDataURL.mockRestore();
  });
});
