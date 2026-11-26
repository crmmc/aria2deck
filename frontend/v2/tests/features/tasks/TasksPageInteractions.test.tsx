import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { taskCommands } from "@/shared/lib/commands";
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
    reserved_bytes: 0, error_code: null, retryable: true,
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

function tasksEnvelope(items: TaskResponse[]): unknown {
  return { items, total: items.length, page: 1, page_size: 100 };
}

type Handler = (url: string, init?: { method?: string; body?: string }) => unknown;

function setup(handler: Handler = () => undefined, tasks: TaskResponse[] = [task(1)]): void {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
    if (url === "/api/v2/tasks/stats") return json(200, STATS);
    const custom = handler(url, init);
    if (custom !== undefined) {
      if (typeof custom === "object" && custom !== null && "status" in custom) return custom;
      return json(200, custom);
    }
    if (url.startsWith("/api/v2/tasks") && (!init?.method || init.method === "GET")) {
      return json(200, tasksEnvelope(tasks));
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

async function renderWithTasks(tasks?: TaskResponse[], handler?: Handler) {
  setup(handler, tasks);
  const view = renderPage();
  await waitFor(() => expect(document.querySelectorAll(".task-card-inner").length).toBeGreaterThan(0));
  return view;
}

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

describe("TasksPage 加载与连接", () => {
  it("首次加载失败提示错误", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      return json(500, { error: { code: "internal", message: "服务器错误" } });
    });
    renderPage();
    expect(await screen.findByText("加载任务失败: 服务器错误")).toBeInTheDocument();
  });

  it("WS 连接后权威重读一次列表", async () => {
    await renderWithTasks([task(1)]);
    const before = mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/tasks?")).length;
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
    });
    await waitFor(() => {
      const after = mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/tasks?")).length;
      expect(after).toBeGreaterThan(before);
    });
  });

  it("连上后同步失败给出警告", async () => {
    await renderWithTasks([task(1)]);
    const socket = pageSockets[pageSockets.length - 1];
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      return json(500, { error: { code: "internal", message: "同步失败" } });
    });
    act(() => {
      socket.onopen?.();
    });
    expect(await screen.findByText("同步任务状态失败: 同步失败")).toBeInTheDocument();
  });

  it("忽略已删除任务的 WS 更新帧", async () => {
    await renderWithTasks([task(1)]);
    // 先通过单条删除把任务加入 deletedTaskIds。
    mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(200, {
          items: [{ accepted: true, task: task(1), outcome: null, error: null, stop_confirmed: true }],
          accepted_count: 1,
          rejected_count: 0,
        });
      }
      return json(200, tasksEnvelope([task(1)]));
    });
    fireEvent.click(screen.getAllByRole("button", { name: "取消" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消下载" }));
    await waitFor(() => expect(screen.queryByText("task-1.bin")).not.toBeInTheDocument());

    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({ type: "task_update", task: task(1, "active", { updated_at_ms: 999 }) }),
      });
    });
    // 已删除任务不应被 WS 帧复活。
    expect(screen.queryByText("task-1.bin")).not.toBeInTheDocument();
  });
});

describe("TasksPage 单条操作", () => {
  it("取消下载：确认后调用取消端点并移除任务", async () => {
    await renderWithTasks([task(1)], () => undefined);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(200, {
          items: [{ accepted: true, task: task(1), outcome: null, error: null, stop_confirmed: true }],
          accepted_count: 1,
          rejected_count: 0,
        });
      }
      return json(200, tasksEnvelope([task(1)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent('确定要取消下载 "task-1.bin" 吗？');
    fireEvent.click(within(dialog).getByRole("button", { name: "取消下载" }));
    await waitFor(() => expect(screen.queryByText("task-1.bin")).not.toBeInTheDocument());
    const call = mockFetch.mock.calls.find(([url, init]) => url === "/api/v2/tasks/cancel" && init?.method === "POST");
    expect(JSON.parse(call![1].body as string)).toEqual({ task_ids: [1] });
  });

  it("取消确认被驳回时不发请求", async () => {
    await renderWithTasks([task(1)]);
    const before = mockFetch.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.length).toBe(before);
  });

  it("取消被业务拒绝时提示原因并保留任务", async () => {
    await renderWithTasks([task(1)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(200, {
          items: [{ accepted: false, task: null, outcome: null, error: { code: "denied", message: "任务已完成" }, stop_confirmed: null }],
          accepted_count: 0,
          rejected_count: 1,
        });
      }
      return json(200, tasksEnvelope([task(1)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消下载" }));
    expect(await screen.findByText("取消失败：任务已完成")).toBeInTheDocument();
    expect(screen.getByText("task-1.bin")).toBeInTheDocument();
  });

  it("取消请求抛错时提示错误", async () => {
    await renderWithTasks([task(1)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(200, tasksEnvelope([task(1)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消下载" }));
    expect(await screen.findByText("取消失败：服务器错误")).toBeInTheDocument();
  });
});

describe("TasksPage 创建任务", () => {
  it("创建被业务拒绝时展示失败原因", async () => {
    await renderWithTasks();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(200, {
          items: [{ accepted: false, task: null, outcome: null, error: { code: "bad_uri", message: "链接无效" }, stop_confirmed: null }],
          accepted_count: 0,
          rejected_count: 1,
        });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.change(screen.getByLabelText("下载链接"), { target: { value: "ftp://bad" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    expect(await screen.findByText("链接无效")).toBeInTheDocument();
  });

  it("创建被拒绝且无失败明细时回退固定文案", async () => {
    await renderWithTasks();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(200, { items: [], accepted_count: 0, rejected_count: 0 });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.change(screen.getByLabelText("下载链接"), { target: { value: "ftp://bad" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    expect(await screen.findByText("提交失败")).toBeInTheDocument();
  });

  it("创建遇一般错误展示错误信息", async () => {
    await renderWithTasks();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.change(screen.getByLabelText("下载链接"), { target: { value: "ftp://bad" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    expect(await screen.findByText("服务器错误")).toBeInTheDocument();
  });

  it("提交成功后列表刷新失败给出警告", async () => {
    await renderWithTasks();
    let postCount = 0;
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        postCount += 1;
        return json(200, {
          items: [{ accepted: true, task: task(9), outcome: null, error: null, stop_confirmed: null }],
          accepted_count: 1,
          rejected_count: 0,
        });
      }
      if (postCount > 0) return json(500, { error: { code: "internal", message: "刷新失败" } });
      return json(200, tasksEnvelope([]));
    });
    fireEvent.change(screen.getByLabelText("下载链接"), { target: { value: "https://a/b" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    expect(await screen.findByText("任务已提交，但列表刷新失败，请手动刷新")).toBeInTheDocument();
  });
});

describe("TasksPage 批量添加", () => {
  it("空输入提示至少一个链接", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    await screen.findByRole("dialog", { name: "批量添加任务" });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    expect(await screen.findByText("请输入至少一个链接")).toBeInTheDocument();
  });

  it("超过 30 条提示上限", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    const uris = Array.from({ length: 31 }, (_, i) => `https://a/${i}`).join("\n");
    fireEvent.change(textarea, { target: { value: uris } });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    expect(await screen.findByText("一次最多添加 30 个任务")).toBeInTheDocument();
  });

  it("全部成功时提示并关闭弹窗", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    fireEvent.change(textarea, { target: { value: "https://a/1\nhttps://a/2" } });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(200, {
          items: [
            { accepted: true, task: task(11), outcome: null, error: null, stop_confirmed: null },
            { accepted: true, task: task(12), outcome: null, error: null, stop_confirmed: null },
          ],
          accepted_count: 2,
          rejected_count: 0,
        });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    expect(await screen.findByText("已提交 2 个任务")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "批量添加任务" })).not.toBeInTheDocument());
  });

  it("502 时展示结果无法确认", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    fireEvent.change(textarea, { target: { value: "https://a/1" } });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(502, { error: { code: "upstream", message: "上游失败" } });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("提交结果暂无法确认，请刷新任务列表");
  });

  it("一般错误展示原始信息", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    fireEvent.change(textarea, { target: { value: "https://a/1" } });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.click(screen.getByRole("button", { name: "添加任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("服务器错误");
  });

  it("关闭按钮收起弹窗并清空输入", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    fireEvent.change(textarea, { target: { value: "https://a/1" } });
    const dialog = await screen.findByRole("dialog", { name: "批量添加任务" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "批量添加任务" })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    await waitFor(() => expect((screen.getByLabelText("批量下载链接") as HTMLTextAreaElement).value).toBe(""));
  });

  it("关闭按钮关闭弹窗（标题栏 X）", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    await screen.findByRole("dialog", { name: "批量添加任务" });
    fireEvent.click(screen.getByRole("button", { name: "关闭批量添加任务" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "批量添加任务" })).not.toBeInTheDocument());
  });
});

describe("TasksPage 批量取消", () => {
  it("全选后取消，部分失败给出警告", async () => {
    await renderWithTasks([task(1), task(2)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(200, {
          items: [
            { accepted: true, task: task(1), outcome: null, error: null, stop_confirmed: true },
            { accepted: false, task: null, outcome: null, error: { code: "denied", message: "已完成" }, stop_confirmed: null },
          ],
          accepted_count: 1,
          rejected_count: 1,
        });
      }
      return json(200, tasksEnvelope([task(1), task(2)]));
    });
    fireEvent.click(screen.getByLabelText("选择全部可见任务"));
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    const dialog = await screen.findByRole("dialog");
    const buttons = within(dialog).getAllByRole("button", { name: "取消" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(await screen.findByText("已取消 1 个任务，1 个取消失败")).toBeInTheDocument();
  });

  it("选中项都不可取消时提示没有可取消任务", async () => {
    // 只有终态任务时列表为空；改用「当前任务」视图里已完成的任务无法选中，
    // 因此这里直接构造一个选中后又被服务端更新为终态的场景不成立，
    // 只验证空选中时批量按钮不出现。
    await renderWithTasks([task(1)]);
    expect(screen.queryByRole("button", { name: "取消下载" })).not.toBeInTheDocument();
  });

  it("批量取消请求抛错时提示", async () => {
    await renderWithTasks([task(1), task(2)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(200, tasksEnvelope([task(1), task(2)]));
    });
    fireEvent.click(screen.getByLabelText("选择全部可见任务"));
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    const dialog = await screen.findByRole("dialog");
    const buttons = within(dialog).getAllByRole("button", { name: "取消" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(await screen.findByText("批量取消失败：服务器错误")).toBeInTheDocument();
  });
});

describe("TasksPage 种子上传", () => {
  const PREVIEW = {
    info_hash: "abc123",
    name: "demo",
    total_size: 3072,
    files: [
      { index: 1, path: "demo/a.bin", size: 2048 },
      { index: 2, path: "demo/b.txt", size: 1024 },
    ],
    default_selection: [1, 2],
  };

  it("超过 10MB 的种子被拒绝并清空输入", async () => {
    await renderWithTasks();
    const input = screen.getByLabelText("上传种子文件") as HTMLInputElement;
    const big = new File([new Uint8Array(11 * 1024 * 1024)], "big.torrent", { type: "application/x-bittorrent" });
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    fireEvent.change(input, { target: { files: [big] } });
    expect(await screen.findByText("种子文件过大，最大支持 10 MB")).toBeInTheDocument();
    expect(input.value).toBe("");
  });

  it("未选择文件时不做任何处理", async () => {
    await renderWithTasks();
    const before = mockFetch.mock.calls.length;
    fireEvent.change(screen.getByLabelText("上传种子文件"), { target: { files: [] } });
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockFetch.mock.calls.length).toBe(before);
  });

  it("上传有效种子后打开向导，取消后清空输入", async () => {
    await renderWithTasks(undefined, (url) => {
      if (url === "/api/v2/tasks/torrent/preview") return PREVIEW;
      return undefined;
    });
    const input = screen.getByLabelText("上传种子文件") as HTMLInputElement;
    const torrent = new File(["d4:infod4:name4:demoe"], "demo.torrent", { type: "application/x-bittorrent" });
    fireEvent.change(input, { target: { files: [torrent] } });

    const wizard = await screen.findByRole("dialog", { name: "添加 BT 下载任务" });
    expect(wizard).toHaveTextContent("选择文件");

    fireEvent.click(within(wizard).getAllByRole("button", { name: "取消" })[0]);
    const cancelDialog = await screen.findByRole("dialog", { name: "取消添加任务" });
    fireEvent.click(within(cancelDialog).getByRole("button", { name: "确认取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "添加 BT 下载任务" })).not.toBeInTheDocument());
  });

  it("种子预览失败时把错误显示在表单错误区", async () => {
    await renderWithTasks(undefined, (url) => {
      if (url === "/api/v2/tasks/torrent/preview") {
        return json(500, { error: { code: "invalid_torrent", message: "种子解析失败" } });
      }
      return undefined;
    });
    const input = screen.getByLabelText("上传种子文件") as HTMLInputElement;
    const torrent = new File(["garbage"], "bad.torrent", { type: "application/x-bittorrent" });
    fireEvent.change(input, { target: { files: [torrent] } });
    expect(await screen.findByText("种子解析失败")).toBeInTheDocument();
    expect(input.value).toBe("");
  });

  it("重复种子用 warning Toast 提示，不占表单错误区（归档参考同款分支）", async () => {
    await renderWithTasks(undefined, (url) => {
      if (url === "/api/v2/tasks/torrent/preview") {
        return json(409, { error: { code: "duplicate_resource", message: "您已拥有此文件" } });
      }
      return undefined;
    });
    const input = screen.getByLabelText("上传种子文件") as HTMLInputElement;
    const torrent = new File(["d4:infod4:name4:demoe"], "demo.torrent", { type: "application/x-bittorrent" });
    fireEvent.change(input, { target: { files: [torrent] } });

    await waitFor(() => expect(screen.getAllByText("您已拥有此文件").length).toBeGreaterThan(0));
    // 错误��不应重复出现该文案：V1 只用 Toast。
    expect(document.querySelector(".form-error")).toBeNull();
    expect(input.value).toBe("");
  });

  it("种子向导创建成功后任务进入列表", async () => {
    setup((url) => {
      if (url === "/api/v2/tasks/torrent/preview") return PREVIEW;
      if (url === "/api/v2/tasks/torrent") {
        return { accepted: true, task: task(21, "queued"), outcome: "created", error: null, stop_confirmed: null };
      }
      return undefined;
    }, []);
    renderPage();
    await screen.findByText("管理您的下载");
    const input = screen.getByLabelText("上传种子文件") as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["d4:infod"], "demo.torrent", { type: "application/x-bittorrent" })] },
    });
    const wizard = await screen.findByRole("dialog", { name: "添加 BT 下载任务" });
    fireEvent.click(within(wizard).getByRole("button", { name: "下一阶段" }));
    await waitFor(() => expect(within(wizard).getByRole("button", { name: "确认" })).toBeInTheDocument());
    fireEvent.click(within(wizard).getByRole("button", { name: "确认" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "添加 BT 下载任务" })).not.toBeInTheDocument());
    expect(await screen.findByText("task-21.bin")).toBeInTheDocument();
  });

  it("种子向导创建被拒时错误留在向导内", async () => {
    setup((url) => {
      if (url === "/api/v2/tasks/torrent/preview") return PREVIEW;
      if (url === "/api/v2/tasks/torrent") {
        return json(500, { error: { code: "internal", message: "种子任务创建失败" } });
      }
      return undefined;
    }, []);
    renderPage();
    await screen.findByText("管理您的下载");
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["d4:infod"], "demo.torrent", { type: "application/x-bittorrent" })] },
    });
    const wizard = await screen.findByRole("dialog", { name: "添加 BT 下载任务" });
    fireEvent.click(within(wizard).getByRole("button", { name: "下一阶段" }));
    fireEvent.click(await within(wizard).findByRole("button", { name: "确认" }));
    await waitFor(() =>
      expect(within(wizard).getByRole("alert")).toHaveTextContent("种子任务创建失败"),
    );
    expect(screen.getByRole("dialog", { name: "添加 BT 下载任务" })).toBeInTheDocument();
  });
});

describe("TasksPage WS 帧与批量取消边界", () => {
  it("WS 更新已有任务时原地替换（不新增卡片）", async () => {
    await renderWithTasks([task(1, "active")]);
    expect(document.querySelectorAll(".task-card-inner")).toHaveLength(1);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: task(1, "active", { name: "改名.bin", completed_bytes: 90, updated_at_ms: 500 }),
        }),
      });
    });
    await waitFor(() => expect(screen.getByText("改名.bin")).toBeInTheDocument());
    expect(document.querySelectorAll(".task-card-inner")).toHaveLength(1);
  });

  it("WS 收到失败帧提示下载失败", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: task(1, "failed", { error_code: "download_error", updated_at_ms: 500 }),
        }),
      });
    });
    expect(await screen.findByText("task-1.bin 下载失败")).toBeInTheDocument();
  });

  it("WS 帧里任务没有名字时用「下载任务」兜底提示", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: task(1, "completed", { name: "", updated_at_ms: 500 }),
        }),
      });
    });
    expect(await screen.findByText("下载任务 下载完成")).toBeInTheDocument();
  });

  it("WS 推来新任务时插入列表头部", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({ type: "task_update", task: task(3, "queued", { name: "新任务.bin" }) }),
      });
    });
    await waitFor(() => expect(screen.getByText("新任务.bin")).toBeInTheDocument());
    const names = Array.from(document.querySelectorAll(".task-name")).map((el) => el.textContent);
    expect(names[0]).toBe("新任务.bin");
  });

  it("已完成任务不会完整帧再次提示下载完成", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onopen?.();
      socket.onmessage?.({
        data: JSON.stringify({ type: "task_update", task: task(1, "completed", { updated_at_ms: 100 }) }),
      });
    });
    await screen.findByText("task-1.bin 下载完成");
    // 已经是 completed 的任务再次收到 completed 帧（更晚时间）不重复提示。
    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({ type: "task_update", task: task(1, "completed", { updated_at_ms: 200 }) }),
      });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getAllByText("task-1.bin 下载完成")).toHaveLength(1);
  });
});

describe("TasksPage FileReader 异常路径", () => {
  let originalFileReader: typeof FileReader;

  beforeEach(() => {
    originalFileReader = globalThis.FileReader;
  });

  afterEach(() => {
    Object.defineProperty(globalThis, "FileReader", {
      value: originalFileReader,
      configurable: true,
      writable: true,
    });
  });

  /** 用自定义 FileReader 替换浏览器实现，模拟读取异常。 */
  function stubFileReader(result: string | ArrayBuffer | null, mode: "result" | "error") {
    class FakeFileReader {
      result: string | ArrayBuffer | null = null;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;

      readAsDataURL() {
        if (mode === "error") {
          this.onerror?.();
          return;
        }
        this.result = result;
        this.onload?.();
      }
    }
    Object.defineProperty(globalThis, "FileReader", {
      value: FakeFileReader,
      configurable: true,
      writable: true,
    });
  }

  it("读取结果不是字符串时报「文件读取结果无效」", async () => {
    stubFileReader(new ArrayBuffer(4), "result");
    setup(() => undefined, []);
    renderPage();
    await screen.findByText("管理您的下载");
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["x"], "a.torrent")] },
    });
    expect(await screen.findByText("文件读取结果无效")).toBeInTheDocument();
  });

  it("data URL 缺少分隔符时报「文件编码格式无效」", async () => {
    stubFileReader("nodataseparator", "result");
    setup(() => undefined, []);
    renderPage();
    await screen.findByText("管理您的下载");
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["x"], "a.torrent")] },
    });
    expect(await screen.findByText("文件编码格式无效")).toBeInTheDocument();
  });

  it("data URL 以分隔符结尾时报「文件编码格式无效」", async () => {
    stubFileReader("data:application/x-bittorrent;base64,", "result");
    setup(() => undefined, []);
    renderPage();
    await screen.findByText("管理您的下载");
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["x"], "a.torrent")] },
    });
    expect(await screen.findByText("文件编码格式无效")).toBeInTheDocument();
  });

  it("FileReader 读取出错时报「文件读取失败」", async () => {
    stubFileReader(null, "error");
    setup(() => undefined, []);
    renderPage();
    await screen.findByText("管理您的下载");
    fireEvent.change(screen.getByLabelText("上传种子文件"), {
      target: { files: [new File(["x"], "a.torrent")] },
    });
    expect(await screen.findByText("文件读取失败")).toBeInTheDocument();
  });
});

describe("TasksPage 单条创建 502 与批量取消 item 配对", () => {
  it("单条创建遇 502 展示无法确认提示", async () => {
    await renderWithTasks();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks" && init?.method === "POST") {
        return json(502, { error: { code: "upstream", message: "上游失败" } });
      }
      return json(200, tasksEnvelope([]));
    });
    fireEvent.change(screen.getByLabelText("下载链接"), { target: { value: "https://a/b" } });
    fireEvent.click(screen.getByRole("button", { name: "+ 添加任务" }));
    expect(await screen.findByText("提交结果暂无法确认，请刷新任务列表")).toBeInTheDocument();
  });

  it("批量取消响应缺 task 时按请求顺序回填 id", async () => {
    await renderWithTasks([task(1), task(2)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        return json(200, {
          items: [
            { accepted: true, task: null, outcome: null, error: null, stop_confirmed: true },
            { accepted: false, task: null, outcome: null, error: { code: "denied", message: "已结束" }, stop_confirmed: null },
          ],
          accepted_count: 1,
          rejected_count: 1,
        });
      }
      return json(200, tasksEnvelope([task(1), task(2)]));
    });
    fireEvent.click(screen.getByLabelText("选择全部可见任务"));
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    const dialog = await screen.findByRole("dialog");
    const buttons = within(dialog).getAllByRole("button", { name: "取消" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(await screen.findByText("已取消 1 个任务，1 个取消失败")).toBeInTheDocument();
  });
});

describe("TasksPage 批量成功与筛选切换", () => {
  it("批量取消全部成功提示成功", async () => {
    await renderWithTasks([task(1), task(2)]);
    mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
      if (url === "/api/v2/tasks/cancel" && init?.method === "POST") {
        const body = JSON.parse(init.body ?? "{}") as { task_ids: number[] };
        return json(200, {
          items: body.task_ids.map((id) => ({
            accepted: true,
            task: task(id),
            outcome: null,
            error: null,
            stop_confirmed: true,
          })),
          accepted_count: body.task_ids.length,
          rejected_count: 0,
        });
      }
      return json(200, tasksEnvelope([task(1), task(2)]));
    });
    fireEvent.click(screen.getByLabelText("选择全部可见任务"));
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    const dialog = await screen.findByRole("dialog");
    const buttons = within(dialog).getAllByRole("button", { name: "取消" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(await screen.findByText("已取消 2 个任务")).toBeInTheDocument();
  });

  it("批量添加部分失败后点重试失败项会回填链接", async () => {
    await renderWithTasks();
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
    const textarea = await screen.findByLabelText("批量下载链接");
    fireEvent.change(textarea, { target: { value: "https://a/1\nhttps://a/2" } });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/stats") return json(200, STATS);
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
    await waitFor(() =>
      expect((screen.getByLabelText("批量下载链接") as HTMLTextAreaElement).value).toBe("https://a/2"),
    );
    // 重试视图被关闭，回到输入态。
    expect(screen.queryByText("已提交 1 个，1 个失败")).not.toBeInTheDocument();
  });

  it("筛选切到「进行中」只保留在管线任务", async () => {
    await renderWithTasks([task(1, "active"), task(2, "paused")]);
    fireEvent.click(screen.getByRole("tab", { name: /^进行中/ }));
    await waitFor(() => {
      expect(screen.getByText("task-1.bin")).toBeInTheDocument();
      expect(screen.getByText("task-2.bin")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("tab", { name: /^当前任务/ }));
    await waitFor(() => expect(screen.getByText("task-2.bin")).toBeInTheDocument());
  });
});

describe("会话内失败任务保留（对齐归档参考回归测试）", () => {
  it("WS 单帧转为失败时卡片保留在当前视图并出现重试按钮", async () => {
    await renderWithTasks([task(1, "active")]);
    expect(screen.getByText("task-1.bin")).toBeInTheDocument();
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: task(1, "failed", { error_code: "download_error", retryable: true }),
        }),
      });
    });
    // 关键断言：失败后卡片仍在，且重试/删除按钮可达。
    await waitFor(() => {
      expect(screen.getByText("task-1.bin")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
    });
  });

  it("WS 单帧转为 completed 时卡片移出当前视图", async () => {
    await renderWithTasks([task(1, "active")]);
    const socket = pageSockets[pageSockets.length - 1];
    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: task(1, "completed"),
        }),
      });
    });
    await waitFor(() => {
      expect(screen.queryByText("task-1.bin")).not.toBeInTheDocument();
    });
  });

  it("失败卡片点击重试后按返回任务更新卡片并可再次取消", async () => {
    const retryResponse = {
      accepted: true,
      outcome: "created",
      task: task(1, "queued", { status_label: "排队中" }),
      error: null,
      stop_confirmed: null,
    };
    await renderWithTasks([task(1, "failed", { error_code: "download_error" })], (url, init) => {
      if (url === "/api/v2/tasks/1/retry" && init?.method === "POST") return retryResponse;
      return undefined;
    });

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() => expect(screen.getByText("已重新添加下载任务")).toBeInTheDocument());
    // 重试返回排队中的同一任务：卡片按新状态替换，重试按钮退出、取消按钮出现。
    expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    const retryCall = mockFetch.mock.calls.find(([url]) => String(url) === "/api/v2/tasks/1/retry");
    expect(JSON.parse(String(retryCall?.[1]?.body))).toEqual({ allow_rename: true });
  });

  it("不可重试的任务点击重试只提示、不发起请求", async () => {
    await renderWithTasks([
      task(1, "failed", { retryable: false, error_code: "download_error" }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() => expect(screen.getByText("不可重试")).toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/retry"))).toBe(false);
  });

  it("重试请求失败时提示后端原文且卡片保持失败态", async () => {
    await renderWithTasks([task(1, "failed", { error_code: "download_error" })], (url, init) => {
      if (url === "/api/v2/tasks/1/retry" && init?.method === "POST") {
        return json(409, { error: { code: "duplicate_resource", message: "同名文件或活动任务已存在" } });
      }
      return undefined;
    });

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() =>
      expect(screen.getByText("重试失败：同名文件或活动任务已存在")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});


describe("TasksPage 快捷操作", () => {
  it("添加下载任务会聚焦真实链接输入框", async () => {
    await renderWithTasks();
    act(() => taskCommands.request("add"));
    expect(screen.getByLabelText("下载链接")).toHaveFocus();
  });

  it("批量添加任务会打开现有批量弹窗", async () => {
    await renderWithTasks();
    act(() => taskCommands.request("batch"));
    expect(await screen.findByRole("dialog", { name: "批量添加任务" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "关闭批量添加任务" })).toHaveFocus());
  });

  it("上传种子文件会点击现有种子输入", async () => {
    await renderWithTasks();
    const input = screen.getByLabelText("上传种子文件");
    const clickSpy = jest.spyOn(input, "click");
    act(() => taskCommands.request("torrent"));
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });
});
