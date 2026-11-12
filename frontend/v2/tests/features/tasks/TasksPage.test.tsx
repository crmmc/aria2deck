import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { TasksPage } from "@/features/tasks/TasksPage";
import { resetNotificationDegradeFlag } from "@/shared/notifications";
import type { TaskResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

type PageSocket = {
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
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
  onerror: (() => void) | null = null;
  readyState = 0;
  send = jest.fn();
  close = jest.fn();

  constructor() {
    pageSockets.push(this);
  }
}

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 1,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 1,
};

function task(
  id: number,
  status: TaskResponse["status"] = "active",
  overrides: Partial<TaskResponse> = {},
): TaskResponse {
  return {
    id, status, status_label: "下载中", name: `task-${id}.bin`, uri: null,
    total_bytes: 100, completed_bytes: 40, download_speed: 10, upload_speed: 0,
    peer_count: 0, connection_count: 1, files: [], observation_fresh: true,
    reserved_bytes: 0, error_code: null, retryable: false,
    created_at_ms: 1, updated_at_ms: 2, finished_at_ms: null,
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function list(items: TaskResponse[]): unknown {
  return { items, total: items.length, page: 1, page_size: 20 };
}

function stats(): unknown {
  return {
    counts: { queued: 0, active: 0, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 },
    total: 0, download_speed: 0, upload_speed: 0,
    quota: { quota_bytes: 1, used_bytes: 0, reserved_bytes: 0, available_bytes: 1 },
  };
}

function siteInfo(): unknown {
  return {
    site_title: "Aria2Deck",
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 1,
    ws_reconnect_factor: 2,
  };
}

function setup(tasks: TaskResponse[]) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/tasks/stats") return json(200, stats());
    if (url === "/api/v2/public/site-info") return json(200, siteInfo());
    return json(200, list(tasks));
  });
  return render(
    <MemoryRouter initialEntries={["/tasks"]}>
      <AppProviders>
        <TasksPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

async function openCreateDialog() {
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  await screen.findByRole("dialog", { name: "新建下载任务" });
}

function pickTorrentFile(overrides: { name?: string; size?: number } = {}) {
  const file = {
    name: "sample.torrent",
    size: 128,
    arrayBuffer: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer),
    ...overrides,
  } as unknown as File;
  const clickSpy = jest
    .spyOn(HTMLInputElement.prototype, "click")
    .mockImplementation(function (this: HTMLInputElement) {
      Object.defineProperty(this, "files", { value: [file], configurable: true });
      this.onchange?.(new Event("change"));
    });
  return clickSpy;
}

function stubClipboard(writeText: jest.Mock) {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  return () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
  };
}

describe("TasksPage", () => {
  beforeEach(() => {
    resetNotificationDegradeFlag();
    window.localStorage.clear();
    pageSockets = [];
    (global as Record<string, unknown>).WebSocket = ControlledWebSocket;
  });

  afterEach(() => {
    delete (global as Record<string, unknown>).WebSocket;
  });

  it("shows empty state when no tasks", async () => {
    setup([]);
    expect(await screen.findByText("暂无任务")).toBeInTheDocument();
  });

  it("renders tasks with status badges", async () => {
    setup([task(1), task(2, "waiting")]);
    expect(await screen.findByTestId("task-row-1")).toBeInTheDocument();
    expect(screen.getByTestId("task-row-2")).toBeInTheDocument();
  });

  it("creates tasks from multi-line textarea", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    await openCreateDialog();
    const textarea = screen.getByLabelText("下载链接列表");
    fireEvent.change(textarea, {
      target: { value: "https://example.com/a.iso\nhttps://example.com/b.iso" },
    });
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [], accepted_count: 2, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks");
      expect(call).toBeDefined();
      const body = JSON.parse(call![1].body as string);
      expect(body.tasks).toHaveLength(2);
    });
  });

  it("opens failures dialog on partial failure and retries only the failed items", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    await openCreateDialog();
    fireEvent.change(screen.getByLabelText("下载链接列表"), {
      target: { value: "https://a\nhttps://b" },
    });
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [
          { accepted: true },
          { accepted: false, error: { code: "invalid_input", message: "不支持" } },
        ],
        accepted_count: 1,
        rejected_count: 1,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));
    expect(await screen.findByRole("dialog", { name: /1 条任务失败/ })).toBeInTheDocument();
    expect(screen.getByText("不支持")).toBeInTheDocument();

    const posts = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks");
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0][1].body as string)).toEqual({
      tasks: [
        { uri: "https://a", options: { allow_rename: true } },
        { uri: "https://b", options: { allow_rename: true } },
      ],
    });

    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    // 第二次载荷恰为失败 URI 集合，不含首次已受理项。
    expect(JSON.parse(posts()[1][1].body as string)).toEqual({
      tasks: [{ uri: "https://b", options: { allow_rename: true } }],
    });
    expect(await screen.findByText("已添加 1 个任务")).toBeInTheDocument();
  });

  it("re-failed retry keeps the failures dialog scoped to the current round", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    await openCreateDialog();
    fireEvent.change(screen.getByLabelText("下载链接列表"), {
      target: { value: "https://a\nhttps://b\nhttps://c" },
    });
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [
          { accepted: true },
          { accepted: false, error: { code: "invalid_input", message: "不支持" } },
          { accepted: false, error: { code: "invalid_input", message: "不支持" } },
        ],
        accepted_count: 1,
        rejected_count: 2,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));
    expect(await screen.findByRole("dialog", { name: /2 条任务失败/ })).toBeInTheDocument();

    // 重试 b、c：b 成功、c 仍失败 → 新弹窗只含本轮失败项 c，不累积旧失败。
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [
          { accepted: true },
          { accepted: false, error: { code: "quota_exceeded", message: "配额不足" } },
        ],
        accepted_count: 1,
        rejected_count: 1,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
    expect(await screen.findByRole("dialog", { name: /1 条任务失败/ })).toBeInTheDocument();
    expect(screen.getByText("配额不足")).toBeInTheDocument();
    expect(screen.queryByText("不支持")).not.toBeInTheDocument();

    const posts = mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks");
    expect(JSON.parse(posts[posts.length - 1][1].body as string)).toEqual({
      tasks: [
        { uri: "https://b", options: { allow_rename: true } },
        { uri: "https://c", options: { allow_rename: true } },
      ],
    });

    // 页脚「关闭」收起失败弹窗（与右上角 × 同一 onClose）。
    const failureDialog = screen.getByRole("dialog", { name: /1 条任务失败/ });
    fireEvent.click(within(failureDialog).getByText("关闭"));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /1 条任务失败/ })).not.toBeInTheDocument(),
    );
  });

  it("shows 502 as result-uncertain message", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    await openCreateDialog();
    fireEvent.change(screen.getByLabelText("下载链接列表"), {
      target: { value: "https://x" },
    });
    mockFetch.mockResolvedValueOnce(
      json(502, { error: { code: "submission_uncertain", message: "结果不可确认" } }),
    );
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));
    expect(await screen.findByText(/结果暂无法确认/)).toBeInTheDocument();
  });

  it.each([
    [502, "下载结果暂无法确认，请稍后在任务列表核对"],
    [500, "服务器错误"],
  ] as const)(
    "retry-failed surfaces 502 as uncertain and other errors as API message (status %i)",
    async (status, expected) => {
      setup([]);
      await screen.findByText("暂无任务");
      await openCreateDialog();
      fireEvent.change(screen.getByLabelText("下载链接列表"), {
        target: { value: "https://a" },
      });
      mockFetch.mockResolvedValueOnce(
        json(200, {
          items: [
            { accepted: false, error: { code: "invalid_input", message: "不支持" } },
          ],
          accepted_count: 0,
          rejected_count: 1,
        }),
      );
      fireEvent.click(screen.getByRole("button", { name: /添加/ }));
      expect(await screen.findByRole("dialog", { name: /1 条任务失败/ })).toBeInTheDocument();
      mockFetch.mockResolvedValueOnce(
        json(status, { error: { code: "upstream", message: expected === "服务器错误" ? "服务器错误" : "结果不可确认" } }),
      );
      fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
    },
  );

  it("filters trigger reload with status params", async () => {
    setup([task(1)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getByRole("tab", { name: "已完成" }));
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("status=completed");
    });
  });

  it("sort headers cycle asc→desc→default, reset page and send sort params", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(200, { items: [task(1)], total: 45, page: 1, page_size: 20 });
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <TasksPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByTestId("task-row-1");
    const lastListUrl = () => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url.startsWith("/api/v2/tasks?")).toBe(true);
      return url;
    };
    // 每次请求后表格会经历 loading 重建，用 findBy 等待表格重新挂载再继续。
    const rowBack = () => screen.findByTestId("task-row-1");

    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(lastListUrl()).toContain("page=2"));
    await rowBack();

    // 第一次点击：升序，且排序变化回第 1 页。
    fireEvent.click(await screen.findByRole("button", { name: "名称排序" }));
    await waitFor(() => expect(lastListUrl()).toContain("sort=name&order=asc"));
    expect(lastListUrl()).toContain("page=1");

    // 第二次点击：降序。
    fireEvent.click(await screen.findByRole("button", { name: "名称排序（升序）" }));
    await waitFor(() => expect(lastListUrl()).toContain("sort=name&order=desc"));

    // 第三次点击：还原默认（无排序参数）。
    fireEvent.click(await screen.findByRole("button", { name: "名称排序（降序）" }));
    await waitFor(() => expect(lastListUrl()).not.toContain("sort="));
    await rowBack();
    expect(screen.getByRole("button", { name: "名称排序" })).toBeInTheDocument();

    // 切换排序列：直接进入新列升序。
    fireEvent.click(await screen.findByRole("button", { name: "大小排序" }));
    await waitFor(() => expect(lastListUrl()).toContain("sort=size&order=asc"));
    await rowBack();
    expect(screen.getByRole("button", { name: "大小排序（升序）" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "名称排序" })).toBeInTheDocument();
  });

  // ============ T1 搜索（防抖 + q 参数 + 页码重置） ============

  it("search debounces input and sends the q parameter", async () => {
    jest.useFakeTimers();
    try {
      setup([task(1)]);
      await screen.findByTestId("task-row-1");
      const callsBefore = mockFetch.mock.calls.filter(([url]) =>
        String(url).startsWith("/api/v2/tasks?"),
      ).length;
      fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "ubuntu" } });
      fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "ubuntu.iso" } });
      // 防抖窗口内不发请求。
      expect(
        mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/tasks?")),
      ).toHaveLength(callsBefore);
      act(() => jest.advanceTimersByTime(300));
      await waitFor(() => {
        const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
        expect(url).toContain("q=ubuntu.iso");
      });
    } finally {
      jest.useRealTimers();
    }
  });

  it("search change resets to page 1", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(200, { items: [task(1)], total: 45, page: 1, page_size: 20 });
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <TasksPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByTestId("task-row-1");
    const listUrls = () =>
      mockFetch.mock.calls
        .map(([url]) => String(url))
        .filter((url) => url.startsWith("/api/v2/tasks?"));
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(listUrls().at(-1)).toContain("page=2"));
    fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "x" } });
    // 防抖 300ms 后发请求：回第 1 页并带 q 参数。
    await waitFor(
      () => {
        expect(listUrls().at(-1)).toContain("page=1");
        expect(listUrls().at(-1)).toContain("q=x");
      },
      { timeout: 2000 },
    );
  });

  // ============ T10/T11 取消确认 ============

  it("batch cancel asks for confirmation, cancels only in-flight tasks and clears selection", async () => {
    setup([task(1), task(2), task(3, "failed")]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 task-2.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 task-3.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量取消/ }));
    // 确认弹窗：仅 2 个进行中任务计入文案。
    const batchDialog = await screen.findByRole("dialog", { name: "批量取消" });
    expect(batchDialog).toHaveTextContent("确定要取消选中的 2 个任务吗？");
    // 页脚取消先收起弹窗（不改选择）。
    fireEvent.click(within(batchDialog).getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "批量取消" })).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /批量取消/ }));
    await screen.findByRole("dialog", { name: "批量取消" });
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }, { accepted: true }], accepted_count: 2, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认取消" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/cancel");
      // 失败任务不在取消载荷中。
      expect(JSON.parse(call![1].body)).toEqual({ task_ids: [1, 2] });
    });
    expect(await screen.findByText("已取消 2 个任务")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/已选/)).not.toBeInTheDocument();
    });
  });

  it("batch cancel with no in-flight selection only toasts", async () => {
    setup([task(1, "failed")]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量取消/ }));
    expect(await screen.findByText("没有可取消的任务")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/cancel"),
    ).toHaveLength(0);
  });

  it("single cancel requires a danger confirmation before the request", async () => {
    setup([task(1)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "取消" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "取消下载" });
    expect(dialog).toHaveTextContent("task-1.bin");
    // 先放弃（页脚取消与右上角关闭都收起弹窗）：不发请求。
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "取消下载" })).not.toBeInTheDocument(),
    );
    expect(
      mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/cancel"),
    ).toHaveLength(0);

    fireEvent.click(screen.getAllByRole("button", { name: "取消" })[0]);
    const reopened = await screen.findByRole("dialog", { name: "取消下载" });
    fireEvent.click(within(reopened).getByRole("button", { name: "关闭" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "取消下载" })).not.toBeInTheDocument(),
    );

    fireEvent.click(screen.getAllByRole("button", { name: "取消" })[0]);
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "取消下载" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/cancel");
      expect(JSON.parse(call![1].body)).toEqual({ task_ids: [1] });
    });
    expect(await screen.findByText("已取消 1 个任务")).toBeInTheDocument();
  });

  it("reports partial cancel failure and cancel API error", async () => {
    setup([task(1), task(2)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量取消/ }));
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      if (url === "/api/v2/tasks/cancel") {
        return json(200, {
          items: [{ accepted: false, error: { code: "invalid_state", message: "已终态" } }],
          accepted_count: 0,
          rejected_count: 1,
        });
      }
      return json(200, list([task(1), task(2)]));
    });
    fireEvent.click(await screen.findByRole("button", { name: "确认取消" }));
    expect(await screen.findByText("已取消 0 个任务，1 个取消失败")).toBeInTheDocument();

    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      if (url === "/api/v2/tasks/cancel") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return json(200, list([task(1)]));
    });
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "取消" })[0]);
    fireEvent.click(await screen.findByRole("button", { name: "取消下载" }));
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
  });

  // ============ T13 失败任务删除 ============

  it("failed task exposes delete with confirmation", async () => {
    setup([task(1, "failed")]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "删除任务" });
    expect(dialog).toHaveTextContent("task-1.bin");
    // 页脚取消先收起弹窗。
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "删除任务" })).not.toBeInTheDocument(),
    );

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(
      within(await screen.findByRole("dialog", { name: "删除任务" })).getByRole("button", {
        name: "删除",
      }),
    );
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/cancel");
      expect(JSON.parse(call![1].body)).toEqual({ task_ids: [1] });
    });
    expect(await screen.findByText("已删除 1 个任务")).toBeInTheDocument();
  });

  // ============ T12 复制链接 ============

  it("copies the display uri from the row button with clipboard", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    const restore = stubClipboard(writeText);
    try {
      setup([task(1, "active", { uri: "magnet:?xt=urn:btih:abc" })]);
      await screen.findByTestId("task-row-1");
      fireEvent.click(screen.getAllByRole("button", { name: "复制" })[0]);
      expect(writeText).toHaveBeenCalledWith("magnet:?xt=urn:btih:abc");
      expect(await screen.findByText("链接已复制")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("copy failure falls back to an error toast", async () => {
    const writeText = jest.fn().mockRejectedValue(new Error("denied"));
    const restore = stubClipboard(writeText);
    try {
      setup([task(1, "active", { uri: "https://example.com/f.bin" })]);
      await screen.findByTestId("task-row-1");
      fireEvent.click(screen.getAllByRole("button", { name: "复制" })[0]);
      expect(await screen.findByText("复制失败，请手动复制")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("mobile card copy target copies the uri on click", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    const restore = stubClipboard(writeText);
    try {
      setup([task(1, "active", { uri: "https://example.com/m.bin" })]);
      const card = await screen.findByTestId("task-card-1");
      fireEvent.click(card.querySelector(".task-card-copy-target")!);
      expect(writeText).toHaveBeenCalledWith("https://example.com/m.bin");
      expect(await screen.findByText("链接已复制")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  // ============ T14 重试禁用原因 ============

  it("shows a disabled retry button with reason for completed tasks", async () => {
    setup([task(1, "completed")]);
    await screen.findByTestId("task-row-1");
    const retry = screen.getAllByRole("button", { name: "重试" })[0];
    expect(retry).toBeDisabled();
    expect(retry).toHaveAttribute("title", "已完成的任务不可重试");
  });

  it("shows a disabled retry button with reason for expired tasks", async () => {
    setup([task(1, "expired")]);
    await screen.findByTestId("task-row-1");
    const retry = screen.getAllByRole("button", { name: "重试" })[0];
    expect(retry).toBeDisabled();
    expect(retry).toHaveAttribute("title", "已过期的任务不可重试");
  });

  it("retries a retryable task and reports failure toast", async () => {
    const failed = task(1, "failed", { retryable: true });
    setup([failed]);
    await screen.findByTestId("task-row-1");
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: true, task: task(1, "queued") }),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "重试" })[0]);
    // 重试确认弹窗默认勾选自动加后缀；确认后才发出请求。
    const retryDialog = await screen.findByRole("dialog", { name: "重试任务" });
    expect(within(retryDialog).getByRole("checkbox")).toBeChecked();
    fireEvent.click(within(retryDialog).getByRole("button", { name: "确认重试" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/1/retry");
      expect(call).toBeDefined();
    });
    expect(JSON.parse(
      mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/1/retry")![1].body as string,
    )).toEqual({ allow_rename: true });
    expect(await screen.findByText("重试已创建")).toBeInTheDocument();

    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      if (url === "/api/v2/tasks/1/retry") {
        return json(404, { error: { code: "not_found", message: "任务不存在" } });
      }
      return json(200, list([failed]));
    });
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "重试" })[0]);
    const secondRetryDialog = await screen.findByRole("dialog", { name: "重试任务" });
    fireEvent.click(within(secondRetryDialog).getByRole("button", { name: "确认重试" }));
    expect(await screen.findByText("任务不存在")).toBeInTheDocument();
  });

  // ============ T16 进度条状态 ============

  it("progress bar tone follows task status", async () => {
    setup([
      task(1, "active"),
      task(2, "failed"),
      task(3, "completed", { completed_bytes: 100 }),
      task(4, "waiting", { total_bytes: null }),
    ]);
    await screen.findByTestId("task-row-1");
    const rowFill = (id: number) =>
      document.querySelector(
        `[data-testid="task-row-${id}"] .progress-fill`,
      ) as HTMLElement;
    expect(rowFill(1).className).toContain("progress-active");
    expect(rowFill(2).className).toContain("progress-error");
    expect(rowFill(3).style.width).toBe("100%");
    // 未知大小时进度为 0%，大小列与卡片显示占位符。
    expect(rowFill(4).style.width).toBe("0%");
    expect(
      document.querySelector('[data-testid="task-row-4"] .progress-detail')?.textContent,
    ).toContain("—");
    expect(screen.getByTestId("task-card-4").textContent).toContain("—");
    expect(
      document.querySelector('[aria-label="task-1.bin 下载进度"]')?.getAttribute("role"),
    ).toBe("progressbar");
  });

  // ============ T19 移动端卡片 ============

  it("mobile card variant renders alongside the desktop table", async () => {
    setup([task(1), task(2, "failed")]);
    expect(await screen.findByTestId("task-card-1")).toBeInTheDocument();
    expect(screen.getByTestId("task-card-2")).toBeInTheDocument();
    expect(document.querySelector("table.task-table.desktop-only")).not.toBeNull();
    expect(document.querySelector("ul.task-card-list.mobile-only")).not.toBeNull();
    // 卡片含名称/状态/进度/速度与操作。
    const card = screen.getByTestId("task-card-1");
    expect(card.textContent).toContain("task-1.bin");
    expect(card.textContent).toContain("下载中");
    expect(card.querySelector(".progress-fill")).not.toBeNull();
    expect(card.textContent).toContain("10 B/s");
  });

  it("row and header checkbox toggles plus clear-selection", async () => {
    setup([task(1), task(2)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getByLabelText("全选任务"));
    expect(screen.getAllByLabelText("选择 task-1.bin")[0]).toBeChecked();
    fireEvent.click(screen.getByLabelText("全选任务")); // 头部反选
    expect(screen.getAllByLabelText("选择 task-1.bin")[0]).not.toBeChecked();
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[0]); // 行内反选
    expect(screen.getAllByLabelText("选择 task-1.bin")[0]).not.toBeChecked();
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: "清除选择" }));
    expect(screen.getAllByLabelText("选择 task-1.bin")[0]).not.toBeChecked();
    // 移动卡片的勾选复用同一处理器。
    fireEvent.click(screen.getAllByLabelText("选择 task-1.bin")[1]);
    expect(screen.getAllByLabelText("选择 task-1.bin")[0]).toBeChecked();
  });

  it("shows error state when loading fails and retries", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(502, { error: { code: "upstream", message: "上游不可用" } });
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <TasksPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("上游不可用")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(200, list([task(1)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByTestId("task-row-1")).toBeInTheDocument();
  });

  // ============ T7 种子上传前端校验 ============

  it("torrent upload rejects non-.torrent files without a request", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const clickSpy = pickTorrentFile({ name: "movie.mp4" });
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    expect(await screen.findByText("请选择 .torrent 文件")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.filter(([url]) => String(url).includes("torrent")),
    ).toHaveLength(0);
  });

  it("torrent upload rejects files over 10 MB", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const clickSpy = pickTorrentFile({ name: "big.torrent", size: 10 * 1024 * 1024 + 1 });
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    expect(await screen.findByText("种子文件过大，最大支持 10 MB")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.filter(([url]) => String(url).includes("torrent")),
    ).toHaveLength(0);
  });

  it("torrent flow: wizard selection then confirm create", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const preview = {
      info_hash: "abc",
      name: "ubuntu.iso",
      files: [
        { index: 0, path: "ubuntu.iso", size: 1024 },
        { index: 1, path: "skip.txt", size: 8 },
      ],
      total_size: 1032,
      default_selection: [0, 1],
    };
    const clickSpy = pickTorrentFile();
    mockFetch.mockResolvedValueOnce(json(200, preview));
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    const dialog = await screen.findByRole("dialog", { name: /ubuntu\.iso/ });
    expect(dialog).toBeInTheDocument();
    expect(await screen.findByText(/已选 2 项 · 1\.01 KiB/)).toBeInTheDocument();

    // 取消第二项后进入确认阶段并创建。
    fireEvent.click(screen.getByLabelText("选择文件 skip.txt"));
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    expect(screen.getByText(/确认下载内容：1 个文件 · 1\.00 KiB/)).toBeInTheDocument();
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: true, task: task(7, "queued", { name: "ubuntu.iso" }) }),
    );
    fireEvent.click(screen.getByRole("button", { name: /确认创建（1 项）/ }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/torrent");
      const body = JSON.parse(call![1].body as string);
      expect(body.torrent.length).toBeGreaterThan(0);
      expect(body.selected_file_indexes).toEqual([0]);
      expect(body.allow_rename).toBe(true);
    });
    expect(await screen.findByText("种子任务已创建")).toBeInTheDocument();
  });

  it("torrent preview failure shows toast", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const clickSpy = pickTorrentFile();
    mockFetch.mockResolvedValueOnce(
      json(422, { error: { code: "invalid_input", message: "种子文件无效" } }),
    );
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    expect(await screen.findByText("种子文件无效")).toBeInTheDocument();
  });

  it("torrent picker cancel (no file) makes no request", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const clickSpy = jest
      .spyOn(HTMLInputElement.prototype, "click")
      .mockImplementation(function (this: HTMLInputElement) {
        Object.defineProperty(this, "files", { value: [], configurable: true });
        this.onchange?.(new Event("change"));
      });
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    expect(
      mockFetch.mock.calls.filter(([url]) => String(url).includes("torrent")),
    ).toHaveLength(0);
  });

  it("create dialog cancel closes without a request", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    await openCreateDialog();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("create failure other than 502 rethrows to the dialog inline error", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    await openCreateDialog();
    fireEvent.change(screen.getByLabelText("下载链接列表"), {
      target: { value: "https://x" },
    });
    mockFetch.mockResolvedValueOnce(
      json(500, { error: { code: "internal", message: "服务器错误" } }),
    );
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));
    expect(await screen.findByText("服务器错误")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "新建下载任务" })).toBeInTheDocument();
  });

  it("torrent wizard cancel closes after confirmation", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const preview = {
      info_hash: "h",
      name: "t.iso",
      files: [{ index: 0, path: "t.iso", size: 10 }],
      total_size: 10,
      default_selection: [0],
    };
    const clickSpy = pickTorrentFile();
    mockFetch.mockResolvedValueOnce(json(200, preview));
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    await screen.findByRole("dialog", { name: /t\.iso/ });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    // 已有选择 → 二次确认。
    expect(await screen.findByText(/取消添加任务？/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("torrent create business rejection stays in the wizard with the backend message", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const preview = {
      info_hash: "h",
      name: "p.iso",
      files: [{ index: 0, path: "p.iso", size: 10 }],
      total_size: 10,
      default_selection: [0],
    };
    const clickSpy = pickTorrentFile();
    mockFetch.mockResolvedValueOnce(json(200, preview));
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    await screen.findByRole("dialog", { name: /p\.iso/ });
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    // 业务拒绝（含严格同名冲突）单项响应：错误留在原向导，不进 URI 失败重提链。
    mockFetch.mockResolvedValueOnce(
      json(200, {
        accepted: false,
        task: null,
        outcome: null,
        error: { code: "duplicate_resource", message: "同名文件或活动任务已存在" },
        stop_confirmed: null,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /确认创建（1 项）/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("同名文件或活动任务已存在");
    // 业务拒绝不换弹窗：仍在原向导，可保留选择与策略重试。
    expect(screen.getByRole("dialog", { name: /p\.iso/ })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /条任务失败/ })).not.toBeInTheDocument();
  });

  it("discards stale load responses after filter changes", async () => {
    // 简化：直接用受控 promise 拦截第一笔列表请求。
    let releaseFirst: (value: unknown) => void = () => undefined;
    const firstCall = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    let calls = 0;
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      calls += 1;
      if (calls === 1) {
        return firstCall as never;
      }
      return json(200, list([task(1, "active")]));
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <TasksPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("tab", { name: "已完成" }); // 等初始渲染完成。
    fireEvent.click(screen.getByRole("tab", { name: "已完成" })); // 发出更新的请求。
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2));
    // 旧响应迟到：返回 completed 数据，但不应覆盖新请求的状态。
    releaseFirst(json(200, list([task(99, "completed")])));
    await screen.findByTestId("task-row-1");
    expect(screen.queryByTestId("task-row-99")).not.toBeInTheDocument();
  });

  // ============ T15 完成/失败 Toast + 桌面通知 ============

  it("WS 4408 resynchronizes both tasks to their latest REST states", async () => {
    setup([
      task(1, "active", { status_label: "下载中" }),
      task(2, "waiting", { status_label: "等待中" }),
    ]);
    await screen.findByTestId("task-row-1");
    await screen.findByTestId("task-row-2");
    await waitFor(() => expect(pageSockets).toHaveLength(1));
    const socket = pageSockets[0];
    act(() => {
      socket.readyState = ControlledWebSocket.OPEN;
      socket.onopen?.();
    });
    await act(async () => {
      await Promise.resolve();
    });

    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(200, list([
        task(1, "completed", { status_label: "已完成", updated_at_ms: 10 }),
        task(2, "failed", { status_label: "异常", updated_at_ms: 10 }),
      ]));
    });
    act(() => socket.onclose?.({ code: 4408 }));

    const firstRow = await screen.findByTestId("task-row-1");
    expect(await within(firstRow).findByText("已完成")).toBeInTheDocument();
    const secondRow = await screen.findByTestId("task-row-2");
    expect(await within(secondRow).findByText("异常")).toBeInTheDocument();
  });

  it("WS reconnect keeps polling when the real load catches a REST failure", async () => {
    jest.useFakeTimers();
    try {
      setup([task(1, "active")]);
      expect(await screen.findByTestId("task-row-1")).toBeInTheDocument();
      await waitFor(() => expect(pageSockets).toHaveLength(1));

      const firstSocket = pageSockets[0];
      act(() => {
        firstSocket.readyState = ControlledWebSocket.OPEN;
        firstSocket.onopen?.();
      });
      expect(await screen.findByTestId("task-row-1")).toBeInTheDocument();

      act(() => firstSocket.onclose?.({ code: 4408 }));
      expect(await screen.findByTestId("task-row-1")).toBeInTheDocument();
      act(() => jest.advanceTimersByTime(3000));
      expect(pageSockets).toHaveLength(2);

      let failNextList = true;
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/tasks/stats") return json(200, stats());
        if (url === "/api/v2/public/site-info") return json(200, siteInfo());
        if (failNextList) {
          failNextList = false;
          return json(502, { error: { code: "upstream", message: "重读失败" } });
        }
        return json(200, list([
          task(1, "completed", { status_label: "已完成", updated_at_ms: 10 }),
        ]));
      });

      const reconnectedSocket = pageSockets[1];
      act(() => {
        reconnectedSocket.readyState = ControlledWebSocket.OPEN;
        reconnectedSocket.onopen?.();
      });
      expect(await screen.findByText("重读失败")).toBeInTheDocument();

      const listCalls = () =>
        mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/tasks?")).length;
      const failedCount = listCalls();
      await act(async () => {
        jest.advanceTimersByTime(5000);
        await Promise.resolve();
      });
      expect(listCalls()).toBe(failedCount + 1);

      const row = await screen.findByTestId("task-row-1");
      expect(await within(row).findByText("已完成")).toBeInTheDocument();
      const recoveredCount = listCalls();
      await act(async () => {
        jest.advanceTimersByTime(10000);
        await Promise.resolve();
      });
      expect(listCalls()).toBe(recoveredCount);
    } finally {
      jest.useRealTimers();
    }
  });

  it("WS completion transition toasts and sends a desktop notification", async () => {
    const constructed: Array<{ title: string; body: string; tag: string }> = [];
    class FakeNotification {
      static permission = "granted";
      static requestPermission = jest.fn().mockResolvedValue("granted");
      onclick: (() => void) | null = null;
      constructor(title: string, options: { body?: string; tag?: string }) {
        constructed.push({ title, body: options.body ?? "", tag: options.tag ?? "" });
      }
      close = jest.fn();
    }
    (globalThis as { Notification?: unknown }).Notification = FakeNotification;
    window.localStorage.setItem("notifications.enabled", "true");

    const sockets: Array<{
      onopen: (() => void) | null;
      onmessage: ((event: { data: string }) => void) | null;
      onclose: ((event: { code: number }) => void) | null;
      close: jest.Mock;
    }> = [];
    class FakeWebSocket {
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      close = jest.fn();
      constructor() {
        sockets.push(this);
      }
    }
    (global as Record<string, unknown>).WebSocket = FakeWebSocket;

    try {
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/tasks/stats") return json(200, stats());
        if (url === "/api/v2/public/site-info") return json(200, siteInfo());
        return json(200, list([task(1, "active")]));
      });
      render(
        <MemoryRouter initialEntries={["/tasks"]}>
          <AppProviders>
            <TasksPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByTestId("task-row-1");
      const socket = sockets[sockets.length - 1];
      act(() => socket.onopen?.());
      act(() =>
        socket.onmessage?.({
          data: JSON.stringify({
            type: "task_update",
            task: { ...task(1, "completed"), updated_at_ms: 5 },
          }),
        }),
      );
      expect(await screen.findByText("task-1.bin 下载完成")).toBeInTheDocument();
      await waitFor(() =>
        expect(constructed).toEqual([
          { title: "下载完成", body: "task-1.bin", tag: "aria2-completed-1" },
        ]),
      );
    } finally {
      delete (globalThis as { Notification?: unknown }).Notification;
      delete (global as Record<string, unknown>).WebSocket;
    }
  });

  it("WS failure transition toasts and a disabled sub-switch suppresses the notification", async () => {
    const constructed: unknown[] = [];
    class FakeNotification {
      static permission = "granted";
      static requestPermission = jest.fn().mockResolvedValue("granted");
      constructor() {
        constructed.push(this);
      }
      close = jest.fn();
    }
    (globalThis as { Notification?: unknown }).Notification = FakeNotification;
    window.localStorage.setItem("notifications.enabled", "true");
    window.localStorage.setItem("notifications.on_failed", "false");

    const sockets: Array<{
      onopen: (() => void) | null;
      onmessage: ((event: { data: string }) => void) | null;
      onclose: ((event: { code: number }) => void) | null;
      close: jest.Mock;
    }> = [];
    class FakeWebSocket {
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      close = jest.fn();
      constructor() {
        sockets.push(this);
      }
    }
    (global as Record<string, unknown>).WebSocket = FakeWebSocket;

    try {
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/tasks/stats") return json(200, stats());
        if (url === "/api/v2/public/site-info") return json(200, siteInfo());
        return json(200, list([task(1, "active")]));
      });
      render(
        <MemoryRouter initialEntries={["/tasks"]}>
          <AppProviders>
            <TasksPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByTestId("task-row-1");
      const socket = sockets[sockets.length - 1];
      act(() => socket.onopen?.());
      act(() =>
        socket.onmessage?.({
          data: JSON.stringify({
            type: "task_update",
            task: { ...task(1, "failed"), updated_at_ms: 6 },
          }),
        }),
      );
      expect(await screen.findByText("task-1.bin 下载失败")).toBeInTheDocument();
      expect(constructed).toHaveLength(0); // 失败子开关关闭：仅 Toast，不弹系统通知。
    } finally {
      delete (globalThis as { Notification?: unknown }).Notification;
      delete (global as Record<string, unknown>).WebSocket;
    }
  });

  it("WS completion with an empty name falls back to the generic label", async () => {
    const sockets: Array<{
      onopen: (() => void) | null;
      onmessage: ((event: { data: string }) => void) | null;
    }> = [];
    class FakeWebSocket {
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      close = jest.fn();
      constructor() {
        sockets.push(this);
      }
    }
    (global as Record<string, unknown>).WebSocket = FakeWebSocket;
    try {
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/tasks/stats") return json(200, stats());
        if (url === "/api/v2/public/site-info") return json(200, siteInfo());
        return json(200, list([task(1, "active")]));
      });
      render(
        <MemoryRouter initialEntries={["/tasks"]}>
          <AppProviders>
            <TasksPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByTestId("task-row-1");
      const socket = sockets[sockets.length - 1];
      act(() => socket.onopen?.());
      act(() =>
        socket.onmessage?.({
          data: JSON.stringify({
            type: "task_update",
            task: { ...task(1, "completed"), name: "", updated_at_ms: 9 },
          }),
        }),
      );
      expect(await screen.findByText("下载任务 下载完成")).toBeInTheDocument();
    } finally {
      delete (global as Record<string, unknown>).WebSocket;
    }
  });

  it("WS task_update refreshes list on status change and logs out on 4401", async () => {
    const sockets: Array<{
      onopen: (() => void) | null;
      onmessage: ((event: { data: string }) => void) | null;
      onclose: ((event: { code: number }) => void) | null;
      close: jest.Mock;
    }> = [];
    class FakeWebSocket {
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      close = jest.fn(() => {
        this.onclose?.({ code: 1000 });
      });
      constructor() {
        sockets.push(this);
      }
    }
    (global as Record<string, unknown>).WebSocket = FakeWebSocket;

    let calls = 0;
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      calls += 1;
      // 第二次加载（WS 触发）返回 waiting 状态。
      return json(200, list(calls <= 1 ? [task(1, "active")] : [task(1, "waiting")]));
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <TasksPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByTestId("task-row-1");
    const socket = sockets[sockets.length - 1];
    act(() => socket.onopen?.());

    // 页内状态变化 → 整页刷新（保持筛选一致）。
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(200, list([task(1, "waiting")]));
    });
    act(() =>
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: { ...task(1, "waiting"), updated_at_ms: 2 },
        }),
      }),
    );
    await waitFor(() => {
      expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("status=")).length).toBeGreaterThan(1);
    });
    // 等待刷新响应落盘后再送下一帧，避免读到旧的 active 副本。
    await act(async () => {});
    await screen.findByTestId("task-row-1");

    // 同状态更新 → 原地替换（无整页刷新）。
    const listCallsBefore = mockFetch.mock.calls.filter(([url]) => String(url).includes("status=")).length;
    act(() =>
      socket.onmessage?.({
        data: JSON.stringify({
          type: "task_update",
          task: { ...task(1, "waiting"), name: "renamed.bin", updated_at_ms: 3 },
        }),
      }),
    );
    expect(await screen.findAllByText("renamed.bin")).not.toHaveLength(0);
    expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("status=")).length).toBe(listCallsBefore);

    // 4401 → 登出。
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/public/site-info") return json(200, siteInfo());
      return json(200, list([task(1, "waiting")]));
    });
    mockFetch.mockResolvedValueOnce(json(401, { error: { code: "unauthenticated", message: "请先登录" } })); // 登出探测失败占位
    act(() => socket.onclose?.({ code: 4401 }));
    await waitFor(() => {
      const logoutCall = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/logout");
      expect(logoutCall).toBeDefined();
    });

    delete (global as Record<string, unknown>).WebSocket;
  });
});
