import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { TasksPage } from "@/features/tasks/TasksPage";
import type { TaskResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

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

function task(id: number, status: TaskResponse["status"] = "active"): TaskResponse {
  return {
    id, status, status_label: "下载中", name: `task-${id}.bin`, uri: null,
    total_bytes: 100, completed_bytes: 40, download_speed: 10, upload_speed: 0,
    peer_count: 0, connection_count: 1, files: [], observation_fresh: true,
    reserved_bytes: 0, error_code: null, retryable: false,
    created_at_ms: 1, updated_at_ms: 2, finished_at_ms: null,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function list(items: TaskResponse[]): unknown {
  return { items, total: items.length, page: 1, page_size: 20 };
}

function setup(tasks: TaskResponse[]) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/tasks/stats") return json(200, stats());
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

function stats(): unknown {
  return {
    counts: { queued: 0, active: 0, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 },
    total: 0, download_speed: 0, upload_speed: 0,
    quota: { quota_bytes: 1, used_bytes: 0, reserved_bytes: 0, available_bytes: 1 },
  };
}

async function openCreateDialog() {
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  await screen.findByRole("dialog", { name: "新建下载任务" });
}

describe("TasksPage", () => {
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
      tasks: [{ uri: "https://a" }, { uri: "https://b" }],
    });

    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    // 第二次载荷恰为失败 URI 集合，不含首次已受理项。
    expect(JSON.parse(posts()[1][1].body as string)).toEqual({
      tasks: [{ uri: "https://b" }],
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
      tasks: [{ uri: "https://b" }, { uri: "https://c" }],
    });
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

  it("cancels selected tasks in bulk and clears selection", async () => {
    setup([task(1), task(2)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getByLabelText("选择 task-1.bin"));
    fireEvent.click(screen.getByLabelText("选择 task-2.bin"));
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }, { accepted: true }], accepted_count: 2, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: /批量取消/ }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/cancel");
      expect(JSON.parse(call![1].body)).toEqual({ task_ids: [1, 2] });
    });
    await waitFor(() => {
      expect(screen.queryByText(/已选/)).not.toBeInTheDocument();
    });
  });

  it("cancels a single task from its row", async () => {
    setup([task(1)]);
    await screen.findByTestId("task-row-1");
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [{ accepted: true }], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/cancel");
      expect(JSON.parse(call![1].body)).toEqual({ task_ids: [1] });
    });
    expect(await screen.findByText("已取消")).toBeInTheDocument();
  });

  it("reports partial cancel failure and cancel API error", async () => {
    setup([task(1), task(2)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getByLabelText("选择 task-1.bin"));
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [{ accepted: false, error: { code: "invalid_state", message: "已终态" } }],
        accepted_count: 0,
        rejected_count: 1,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /批量取消/ }));
    expect(await screen.findByText("1 个任务取消失败")).toBeInTheDocument();

    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/tasks/cancel") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return json(200, list([task(1)]));
    });
    fireEvent.click(screen.getAllByRole("button", { name: "取消" })[0]);
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
  });

  it("retries a retryable task and reports failure toast", async () => {
    const failed = { ...task(1, "failed"), retryable: true };
    setup([failed]);
    await screen.findByTestId("task-row-1");
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: true, task: task(1, "queued") }),
    );
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/1/retry");
      expect(call).toBeDefined();
    });
    expect(await screen.findByText("重试已创建")).toBeInTheDocument();

    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (url === "/api/v2/tasks/1/retry") {
        return json(404, { error: { code: "not_found", message: "任务不存在" } });
      }
      return json(200, list([failed]));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("任务不存在")).toBeInTheDocument();
  });

  it("shows error state when loading fails and retries", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
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
      return json(200, list([task(1)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByTestId("task-row-1")).toBeInTheDocument();
  });

  it("torrent flow: select file, preview, choose files and create", async () => {
    setup([]);
    await screen.findByText("暂无任务");

    // jsdom 的 File 未实现 arrayBuffer，用带该方法的替身驱动 readFileBase64。
    const file = {
      arrayBuffer: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer),
    } as unknown as File;
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
    const clickSpy = jest
      .spyOn(HTMLInputElement.prototype, "click")
      .mockImplementation(function (this: HTMLInputElement) {
        Object.defineProperty(this, "files", { value: [file], configurable: true });
        this.onchange?.(new Event("change"));
      });
    mockFetch.mockResolvedValueOnce(json(200, preview));
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    const dialog = await screen.findByRole("dialog", { name: /ubuntu\.iso/ });
    expect(dialog).toBeInTheDocument();
    expect(await screen.findByText(/已选 2 \/ 2 个文件/)).toBeInTheDocument();

    // 取消第二项后创建。
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    fireEvent.click(checkboxes[1]);
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: /创建（1 个文件）/ }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/torrent");
      const body = JSON.parse(call![1].body as string);
      expect(body.torrent.length).toBeGreaterThan(0);
      expect(body.selected_file_indexes).toEqual([0]);
    });
    expect(await screen.findByText("种子任务已创建")).toBeInTheDocument();
  });

  it("torrent preview failure shows toast", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const file = {
      arrayBuffer: () => Promise.resolve(new Uint8Array([9]).buffer),
    } as unknown as File;
    const clickSpy = jest
      .spyOn(HTMLInputElement.prototype, "click")
      .mockImplementation(function (this: HTMLInputElement) {
        Object.defineProperty(this, "files", { value: [file], configurable: true });
        this.onchange?.(new Event("change"));
      });
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

  it("row and header checkbox toggles plus clear-selection", async () => {
    setup([task(1), task(2)]);
    await screen.findByTestId("task-row-1");
    fireEvent.click(screen.getByLabelText("全选任务"));
    expect(screen.getByLabelText("选择 task-1.bin")).toBeChecked();
    fireEvent.click(screen.getByLabelText("全选任务")); // 头部反选
    expect(screen.getByLabelText("选择 task-1.bin")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("选择 task-1.bin"));
    fireEvent.click(screen.getByLabelText("选择 task-1.bin")); // 行内反选
    expect(screen.getByLabelText("选择 task-1.bin")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("选择 task-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "清除选择" }));
    expect(screen.getByLabelText("选择 task-1.bin")).not.toBeChecked();
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

  it("torrent dialog cancel closes without creating", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const file = {
      arrayBuffer: () => Promise.resolve(new Uint8Array([5]).buffer),
    } as unknown as File;
    const clickSpy = jest
      .spyOn(HTMLInputElement.prototype, "click")
      .mockImplementation(function (this: HTMLInputElement) {
        Object.defineProperty(this, "files", { value: [file], configurable: true });
        this.onchange?.(new Event("change"));
      });
    mockFetch.mockResolvedValueOnce(
      json(200, {
        info_hash: "h",
        name: "t.iso",
        files: [{ index: 0, path: "t.iso", size: 10 }],
        total_size: 10,
        default_selection: [0],
      }),
    );
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    await screen.findByRole("dialog", { name: /t\.iso/ });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("torrent create partial failure opens the failures dialog", async () => {
    setup([]);
    await screen.findByText("暂无任务");
    const file = {
      arrayBuffer: () => Promise.resolve(new Uint8Array([7]).buffer),
    } as unknown as File;
    const clickSpy = jest
      .spyOn(HTMLInputElement.prototype, "click")
      .mockImplementation(function (this: HTMLInputElement) {
        Object.defineProperty(this, "files", { value: [file], configurable: true });
        this.onchange?.(new Event("change"));
      });
    mockFetch.mockResolvedValueOnce(
      json(200, {
        info_hash: "h",
        name: "p.iso",
        files: [{ index: 0, path: "p.iso", size: 10 }],
        total_size: 10,
        default_selection: [0],
      }),
    );
    try {
      fireEvent.click(screen.getByRole("button", { name: "种子下载" }));
    } finally {
      clickSpy.mockRestore();
    }
    await screen.findByRole("dialog", { name: /p\.iso/ });
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [{ accepted: false, error: { code: "invalid_input", message: "选择无效" } }],
        accepted_count: 0,
        rejected_count: 1,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /创建（1 个文件）/ }));
    expect(await screen.findByRole("dialog", { name: "1 条任务失败" })).toBeInTheDocument();
    expect(screen.getByText("选择无效")).toBeInTheDocument();
  });

  it("discards stale load responses after filter changes", async () => {
    let deferredResolve: ((body: unknown) => void) | null = null;
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
      if (deferredResolve !== null) {
        const resolve = deferredResolve;
        deferredResolve = null;
        return new Promise((resolveOuter) => {
          void resolve; // 初始挂起请求，稍后手动放行。
          (resolveOuter as unknown as (v: unknown) => void)({ pending: true, resolve });
        }).then(
          (value) => (value as { resolve: (b: unknown) => unknown }).resolve as never,
        ) as never;
      }
      return json(200, list([task(1, "active")]));
    });
    // 简化：直接用受控 promise 拦截第一笔列表请求。
    let releaseFirst: (value: unknown) => void = () => undefined;
    const firstCall = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    let calls = 0;
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
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
    expect(await screen.findByText("renamed.bin")).toBeInTheDocument();
    expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("status=")).length).toBe(listCallsBefore);

    // 4401 → 登出。
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/tasks/stats") return json(200, stats());
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
