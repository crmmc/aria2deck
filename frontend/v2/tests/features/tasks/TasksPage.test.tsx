import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { TasksPage } from "@/features/tasks/TasksPage";
import type { TaskResponse, TaskStatsResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

type PageSocket = {
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  readyState: number;
  send: jest.Mock;
  close: jest.Mock;
};

let pageSockets: PageSocket[] = [];

class ControlledWebSocket {
  static OPEN = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  readyState = 0;
  send = jest.fn();
  close = jest.fn();

  constructor() {
    pageSockets.push(this);
  }
}

const ME: UserResponse = {
  status: "active",
  id: 1, username: "admin", is_admin: true, quota_bytes: 1,
  is_initial_password: false, pending_delete: false,
  used_bytes: 0, reserved_bytes: 0, available_bytes: 1,
};

function task(id: number, status: TaskResponse["status"] = "active", overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id, status, status_label: "下载中", name: `task-${id}.bin`, uri: `https://example.com/${id}.bin`,
    total_bytes: 100, completed_bytes: 40, download_speed: 10, upload_speed: 0,
    peer_count: 0, connection_count: 1, files: [], observation_fresh: true,
    reserved_bytes: 0, error_code: null, error_message: "", retryable: true,
    created_at_ms: id, updated_at_ms: id, finished_at_ms: null,
    ...overrides,
  };
}

const STATS: TaskStatsResponse = {
  counts: { queued: 0, active: 1, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 },
  total: 1, download_speed: 100, upload_speed: 0,
  quota: { quota_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
  disk: { total_bytes: 1000, used_bytes: 100, reserved_bytes: 50, available_bytes: 850, limited: false },
};

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function tasksEnvelope(tasks: TaskResponse[]): unknown {
  return { items: tasks, total: tasks.length, page: 1, page_size: 100 };
}

function setup(tasks: TaskResponse[] = [task(1), task(2, "failed")]): void {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") {
      return json(200, { site_title: "Aria2Deck", ws_reconnect_max_delay_seconds: 30, ws_reconnect_jitter_seconds: 1, ws_reconnect_factor: 2 });
    }
    if (url === "/api/v2/tasks/stats") return json(200, STATS);
    if (url.startsWith("/api/v2/tasks") && (!init?.method || init.method === "GET")) {
      // 真实后端按 status 参数过滤：mock 也必须遵守，否则测不出「初始只取管线任务」。
      const requested = new URLSearchParams(String(url).split("?")[1] ?? "").getAll("status");
      const selected = requested.length ? tasks.filter((item) => requested.includes(item.status)) : tasks;
      return json(200, tasksEnvelope(selected));
    }
    return json(404, { error: { code: "not_found", message: "未找到" } });
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/tasks"]}>
      <AppProviders>
        <TasksPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

async function renderWithTasks(tasks?: TaskResponse[]) {
  setup(tasks);
  const view = renderPage();
  await screen.findByText("暂无活动任务", undefined, { timeout: 3000 }).catch(() => undefined);
  await waitFor(() => expect(screen.getByText("管理您的下载")).toBeInTheDocument());
  await waitFor(() => expect(document.querySelectorAll(".task-card-inner").length).toBeGreaterThan(0));
  return view;
}

describe("TasksPage（V1 任务页）", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  let originalClose: HTMLDialogElement["close"] | undefined;

  beforeEach(() => {
    pageSockets = [];
    (global as unknown as { WebSocket: unknown }).WebSocket = ControlledWebSocket;
    originalShowModal = HTMLDialogElement.prototype.showModal;
    originalClose = HTMLDialogElement.prototype.close;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: jest.fn(function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      }),
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: jest.fn(function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      }),
    });
  });

  afterEach(() => {
    delete (global as unknown as { WebSocket?: unknown }).WebSocket;
    document.querySelectorAll("dialog").forEach((d) => d.removeAttribute("open"));
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: originalShowModal });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    }
    if (originalClose) {
      Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: originalClose });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
    }
  });

  it("加载当前视图任务并渲染卡片（初始加载只取管线任务）", async () => {
    await renderWithTasks();
    expect(screen.getByText("task-1.bin")).toBeInTheDocument();
    // 初始加载不取终态；失败任务只会在会话内经 WS 保留。
    expect(screen.queryByText("task-2.bin")).not.toBeInTheDocument();
    // 统计：disk 口径（C26）。
    expect(screen.getByText("存储空间").parentElement?.textContent).toContain("850.0 B");
  });

  
  
  it("搜索过滤任务", async () => {
    await renderWithTasks();
    fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "task-1" } });
    await waitFor(() => {
      expect(screen.getByText("task-1.bin")).toBeInTheDocument();
      expect(screen.queryByText("task-2.bin")).not.toBeInTheDocument();
    });
  });

  it("筛选「进行中」隐藏失败任务", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("tab", { name: /^进行中/ }));
    await waitFor(() => {
      expect(screen.getByText("task-1.bin")).toBeInTheDocument();
      expect(screen.queryByText("task-2.bin")).not.toBeInTheDocument();
    });
  });

  it("按下载进度排序", async () => {
    await renderWithTasks([
      task(1, "active", { completed_bytes: 10 }),
      task(2, "active", { completed_bytes: 90, name: "bbb.bin" }),
    ]);
    fireEvent.change(screen.getByLabelText("任务排序"), { target: { value: "progress" } });
    await waitFor(() => {
      const cards = document.querySelectorAll(".task-card-inner .task-name");
      expect(cards[0].textContent).toContain("bbb.bin");
    });
  });

  it("全选后批量取消（确认框→成功提示）", async () => {
    await renderWithTasks([task(1), task(2)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
      if (url === "/api/v2/tasks/cancel") {
        const body = JSON.parse(init?.body ?? "{}");
        return json(200, {
          items: body.task_ids.map((id: number) => ({ accepted: true, task: task(id), outcome: null, error: null, stop_confirmed: true })),
          accepted_count: body.task_ids.length,
          rejected_count: 0,
        });
      }
      return json(200, tasksEnvelope([task(1), task(2)]));
    });
    fireEvent.click(screen.getByLabelText("选择全部可见任务"));
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    const dialog = await screen.findByRole("dialog", undefined, { timeout: 3000 });
    expect(dialog).toHaveTextContent("确定要取消选中的 2 个任务吗？");
    // V1 原貌：批量取消确认框的确认按钮文案也是「取消」（cancelText 缺省 + confirmText="取消"）。
    const buttons = within(dialog).getAllByRole("button", { name: "取消" });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => {
      expect(screen.getByText("已取消 2 个任务")).toBeInTheDocument();
    });
  });

  it("单条创建成功清空输入并提示", async () => {
    await renderWithTasks();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(200, { items: [{ accepted: true, task: task(9), outcome: null, error: null, stop_confirmed: null }], accepted_count: 1, rejected_count: 0 });
      }
      return json(200, tasksEnvelope([]));
    });
    const input = screen.getByLabelText("下载链接");
    fireEvent.change(input, { target: { value: "https://example.com/x.bin" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    await screen.findByText("任务已提交");
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(""));
  });

  it("创建遇 502 显示『提交结果暂无法确认』", async () => {
    await renderWithTasks();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(502, { error: { code: "upstream", message: "上游失败" } });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.change(screen.getByLabelText("下载链接"), { target: { value: "https://a/b" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    await screen.findByText("提交结果暂无法确认，请刷新任务列表");
  });

  
  
  it("WS task_update 帧更新列表并提示完成", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({ type: "task_update", task: task(1, "completed", { status_label: "已完成" }) }),
      });
    });
    await screen.findByText("task-1.bin 下载完成");
    expect(screen.queryByText("task-1.bin")).not.toBeInTheDocument();
  });

  it("批量添加部分失败列出失败项并可回填重试", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    fireEvent.change(textarea, { target: { value: "https://a/1\nhttps://a/2" } });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(200, {
          items: [
            { accepted: true, task: task(11), outcome: null, error: null, stop_confirmed: null },
            { accepted: false, task: null, outcome: null, error: { code: "bad_uri", message: "链接无效" }, stop_confirmed: null },
          ],
          accepted_count: 1,
          rejected_count: 1,
        });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    await screen.findByText("已提交 1 个，1 个失败");
    fireEvent.click(screen.getByRole("button", { name: "重试失败项" }));
    await waitFor(() => {
      expect((screen.getByLabelText("批量下载链接") as HTMLTextAreaElement).value).toBe("https://a/2");
    });
  });

  it("上传非种子文件给出错误提示", async () => {
    await renderWithTasks();
    const fileInput = screen.getByLabelText("上传种子文件");
    fireEvent.change(fileInput, { target: { files: [new File(["x"], "x.txt")] } });
    await screen.findByText("请选择 .torrent 文件");
  });

  it("分段数量来自完整会话数据，筛选搜索排序不重建无关节点", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({ type: "task_update", task: task(2, "failed") }),
      });
    });

    expect(await screen.findByRole("tab", { name: "当前任务 2" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "进行中 1" })).toBeInTheDocument();
    const header = document.querySelector(".page-header");
    const stats = document.querySelector(".transmission-card");
    const section = document.querySelector("section[aria-label='任务列表']");

    fireEvent.change(screen.getByLabelText("任务排序"), { target: { value: "progress" } });
    fireEvent.click(screen.getByRole("tab", { name: "进行中 1" }));
    expect(screen.getByText("task-1.bin")).toBeInTheDocument();
    expect(screen.queryByText("task-2.bin")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "无命中" } });
    await screen.findByText("暂无活动任务");

    expect(screen.getByRole("tab", { name: "当前任务 2" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "进行中 1" })).toBeInTheDocument();
    expect(document.querySelector(".page-header")).toBe(header);
    expect(document.querySelector(".transmission-card")).toBe(stats);
    expect(document.querySelector("section[aria-label='任务列表']")).toBe(section);
  });

  it("清除任务搜索保留焦点且不重新请求列表", async () => {
    await renderWithTasks([task(1), task(2)]);
    const listCalls = () => mockFetch.mock.calls.filter(([url, init]) =>
      String(url).startsWith("/api/v2/tasks?") && (!init?.method || init.method === "GET"),
    ).length;
    const before = listCalls();
    const input = screen.getByLabelText("搜索任务");

    fireEvent.change(input, { target: { value: "task-1" } });
    expect(screen.queryByText("task-2.bin")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空任务搜索" }));

    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
    expect(screen.getByText("task-2.bin")).toBeInTheDocument();
    expect(listCalls()).toBe(before);
  });

});
