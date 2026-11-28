import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import * as toastContext from "@/shared/components/toast-context";
import { DiskOrphanPage } from "@/features/storage/DiskOrphanPage";
import type {
  DiskOrphanItemResponse,
  QueueItemResponse,
  QueueSnapshotResponse,
  UserResponse,
} from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
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

const DIGEST_A = "a".repeat(64);
const DIGEST_B = "b".repeat(64);
const PATH_A = `objects/file-${DIGEST_A}`;
const PATH_B = `objects/directory-${DIGEST_B}`;

type Handler = (url: string, init?: { method?: string; body?: string }) => unknown;

function orphan(overrides: Partial<DiskOrphanItemResponse> = {}): DiskOrphanItemResponse {
  return {
    storage_path: PATH_A,
    kind: "file",
    digest: DIGEST_A,
    size: 64 * 1024 * 1024,
    modified_at_ms: new Date(2026, 10, 25, 10, 30).getTime(),
    ...overrides,
  };
}

function scanBody(items: DiskOrphanItemResponse[], overrides: Partial<{
  total: number; page: number; page_size: number; unrecognized_count: number;
}> = {}): unknown {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 20,
    unrecognized_count: 0,
    ...overrides,
  };
}

function queueItem(overrides: Partial<QueueItemResponse> = {}): QueueItemResponse {
  return {
    storage_path: PATH_A,
    kind: "file",
    digest: DIGEST_A,
    size: 64 * 1024 * 1024,
    state: "queued",
    enqueued_at_ms: 1700000000000,
    started_at_ms: null,
    finished_at_ms: null,
    processed_bytes: 0,
    content_id: null,
    error: null,
    ...overrides,
  };
}

function snapshot(overrides: Partial<QueueSnapshotResponse> = {}): QueueSnapshotResponse {
  const items = overrides.items ?? [];
  const count = (state: QueueItemResponse["state"]) =>
    items.filter((item) => item.state === state).length;
  return {
    running: false,
    items,
    queued_count: count("queued"),
    running_count: count("running"),
    done_count: count("done"),
    failed_count: count("failed"),
    pending_bytes: items
      .filter((item) => item.state === "queued")
      .reduce((sum, item) => sum + item.size, 0),
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function isResponse(value: unknown): boolean {
  return typeof value === "object" && value !== null &&
    typeof (value as { json?: unknown }).json === "function";
}

function setup(handler: Handler, me: UserResponse = ME) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, me);
    if (url === "/api/v2/public/site-info") {
      return json(200, {
        site_title: "Aria2Deck",
        ws_reconnect_max_delay_seconds: 30,
        ws_reconnect_jitter_seconds: 1,
        ws_reconnect_factor: 2,
      });
    }
    const result = await handler(url, init);
    return isResponse(result) ? result : json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/storage/orphans"]}>
      <AppProviders>
        <Routes>
          <Route path="/storage/orphans" element={<DiskOrphanPage />} />
          <Route path="/tasks" element={<div>任务页占位</div>} />
          <Route path="/storage" element={<div>存储管理占位</div>} />
        </Routes>
      </AppProviders>
    </MemoryRouter>,
  );
}

function scanCalls(): string[] {
  return mockFetch.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.startsWith("/api/v2/storage/disk-orphans?"));
}

function queueCalls(): string[] {
  return mockFetch.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url === "/api/v2/storage/disk-orphans/queue");
}

type Call = [string, { method?: string; body?: string }?];

function callsWith(method: string, path: string): Call[] {
  return mockFetch.mock.calls.filter(
    ([url, init]) =>
      String(url) === path && ((init as { method?: string } | undefined)?.method ?? "GET") === method,
  ) as Call[];
}

/** 队列快照接口的读取次数（GET 与写入型调用的快照响应区分开）。 */
function queueGetCalls(): Call[] {
  return callsWith("GET", "/api/v2/storage/disk-orphans/queue");
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

beforeEach(() => {
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
  document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  }
  if (originalClose) {
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  }
});

describe("DiskOrphanPage 权限与导航", () => {
  it("管理员可见两个面板、计数与扫描说明，并提供存储管理跳转", async () => {
    setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : {}));

    expect(await screen.findByRole("heading", { name: "未登记文件" })).toBeInTheDocument();
    expect(
      screen.getByText("扫描下载目录中未登记进库的规范存储文件，按需校验入库"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "存储管理" })).toHaveAttribute("href", "/storage");
    expect(screen.getByRole("region", { name: "入库队列" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "磁盘未登记文件清单" })).toBeInTheDocument();
    expect(screen.getByText("共 0 条")).toBeInTheDocument();
    expect(screen.getByText("排队中 0")).toBeInTheDocument();
    expect(screen.getByText("处理中 0")).toBeInTheDocument();
    expect(screen.getByText("已入库 0")).toBeInTheDocument();
    expect(screen.getByText("失败 0")).toBeInTheDocument();
    expect(screen.getByText("安排中 0 B")).toBeInTheDocument();
    // 队列快照到达前开关是禁用的，避免在未知状态下点开始。
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /开始处理/ })).toBeEnabled(),
    );
    expect(screen.getByRole("button", { name: /^扫描$/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "加入队列(0)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "删除选中(0)" })).toBeDisabled();
    expect(screen.getByText("入库队列为空")).toBeInTheDocument();
    expect(screen.getByText("尚未扫描", { selector: ".empty-state-title" })).toBeInTheDocument();
    expect(screen.getByText("尚未扫描", { selector: ".admin-result-count" })).toBeInTheDocument();
  });

  it("非管理员被带回任务页，且不请求管理员数据", async () => {
    setup(() => scanBody([]), { ...ME, is_admin: false });
    expect(await screen.findByText("任务页占位")).toBeInTheDocument();
    expect(scanCalls()).toHaveLength(0);
    expect(queueCalls()).toHaveLength(0);
  });

  it("队列读取只在挂载时发生一次", async () => {
    setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : {}));
    await screen.findByRole("heading", { name: "未登记文件" });
    await waitFor(() => expect(queueGetCalls()).toHaveLength(1));
    expect(scanCalls()).toHaveLength(0);
  });
});

describe("DiskOrphanPage 扫描三态", () => {
  it("未扫描不请求磁盘，空态提示「尚未扫描」", async () => {
    setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : {}));
    await screen.findByRole("heading", { name: "未登记文件" });
    expect(screen.getByText("尚未扫描", { selector: ".empty-state-title" })).toBeInTheDocument();
    expect(scanCalls()).toHaveLength(0);
  });

  it("扫描中显示进行中提示，完成后渲染清单字段与队列徽标", async () => {
    let releaseScan: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseScan = resolve; });
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ items: [queueItem({ state: "queued" })] });
      }
      if (url.startsWith("/api/v2/storage/disk-orphans?")) return pending;
      return {};
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));

    expect(await screen.findByText("扫描中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "扫描中..." })).toBeDisabled();
    expect(screen.getByText("正在扫描...", { selector: ".admin-result-count" })).toBeInTheDocument();

    await act(async () => {
      releaseScan(json(200, scanBody([orphan(), orphan({ storage_path: PATH_B, kind: "directory", digest: DIGEST_B, size: 1024 })])));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    const row = await screen.findByTestId(`orphan-row-${PATH_A}`);
    expect(row).toHaveTextContent(PATH_A);
    expect(within(row).getByText("文件")).toBeInTheDocument();
    expect(row).toHaveTextContent("64.0 MB");
    expect(row).toHaveTextContent("2026/11/25 10:30");
    expect(within(row).getByText("排队中")).toBeInTheDocument();
    const dirRow = screen.getByTestId(`orphan-row-${PATH_B}`);
    expect(within(dirRow).getByText("目录")).toBeInTheDocument();
    expect(dirRow).toHaveTextContent("1.0 KB");
    expect(within(dirRow).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("共 2 个未登记文件")).toBeInTheDocument();
    expect(scanCalls()[0]).toContain("page=1");
    expect(scanCalls()[0]).toContain("page_size=20");
  });

  it("扫描完成但清单为空显示空态，并提示未识别数量", async () => {
    setup((url) =>
      url === "/api/v2/storage/disk-orphans/queue"
        ? snapshot()
        : scanBody([], { unrecognized_count: 3 }),
    );
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));

    expect(await screen.findByText("没有未登记文件")).toBeInTheDocument();
    expect(
      screen.getByText(
        "规范存储文件都已登记入库；另有 3 个不符合命名规则的对象，未计入清单。",
        { selector: ".empty-state-description" },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("共 0 个未登记文件")).toBeInTheDocument();
    expect(screen.queryByText("尚未扫描")).not.toBeInTheDocument();
  });

  it("扫描完成且没有未识别对象时不显示额外提示", async () => {
    setup((url) =>
      url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : scanBody([]),
    );
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));

    expect(await screen.findByText("没有未登记文件")).toBeInTheDocument();
    expect(screen.getByText("下载目录中的规范存储文件都已登记入库。")).toBeInTheDocument();
    expect(screen.queryByText(/不符合命名规则/)).not.toBeInTheDocument();
  });

  it("扫描失败给出中文错误且回到未扫描空态", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return json(500, { error: { code: "internal", message: "后端炸了" } });
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));

    expect(await screen.findByText("后端炸了")).toBeInTheDocument();
    expect(screen.queryByTestId(`orphan-row-${PATH_A}`)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^扫描$/ })).toBeEnabled();
  });

  it("网络异常时使用统一中文文案", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      throw new TypeError("offline");
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));

    expect(await screen.findByText("网络异常，请检查连接后重试")).toBeInTheDocument();
  });

  it("搜索停止 300ms 后按新条件重发第 1 页请求并清空选择", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
        if (url.includes("search=bbbb")) return scanBody([orphan({ storage_path: PATH_B, digest: DIGEST_B })]);
        return scanBody([orphan()]);
      });
      await screen.findByRole("heading", { name: "未登记文件" });
      fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
      await screen.findByTestId(`orphan-row-${PATH_A}`);
      fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
      const before = scanCalls().length;

      fireEvent.change(screen.getByLabelText("按存储路径过滤"), { target: { value: "bbbb" } });
      await act(async () => {
        jest.advanceTimersByTime(299);
        await Promise.resolve();
      });
      expect(scanCalls()).toHaveLength(before);

      await act(async () => {
        jest.advanceTimersByTime(1);
        await Promise.resolve();
        await Promise.resolve();
      });
      await waitFor(() => expect(scanCalls()).toHaveLength(before + 1));
      expect(scanCalls().at(-1)).toContain("page=1");
      expect(scanCalls().at(-1)).toContain("search=bbbb");
      expect(await screen.findByTestId(`orphan-row-${PATH_B}`)).toBeInTheDocument();
      expect(screen.getByLabelText(`选择 ${PATH_B}`)).not.toBeChecked();
    } finally {
      jest.useRealTimers();
    }
  });

  it("未扫描时输入关键词不会触发磁盘读取直到手动扫描", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : scanBody([orphan()])));
      await screen.findByRole("heading", { name: "未登记文件" });
      fireEvent.change(screen.getByLabelText("按存储路径过滤"), { target: { value: "aaaa" } });
      await act(async () => {
        jest.advanceTimersByTime(400);
        await Promise.resolve();
      });
      expect(scanCalls()).toHaveLength(0);

      fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
      await screen.findByTestId(`orphan-row-${PATH_A}`);
      expect(scanCalls()).toHaveLength(1);
      expect(scanCalls()[0]).toContain("search=aaaa");
    } finally {
      jest.useRealTimers();
    }
  });

  it("扫描按钮使用当前输入值并同步吸收待防抖关键词", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
        if (url.includes("search=cccc")) return scanBody([]);
        return scanBody([orphan()]);
      });
      await screen.findByRole("heading", { name: "未登记文件" });
      fireEvent.change(screen.getByLabelText("按存储路径过滤"), { target: { value: "cccc" } });
      fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
      await waitFor(() => expect(scanCalls()).toHaveLength(1));
      expect(scanCalls()[0]).toContain("search=cccc");
      await act(async () => {
        jest.advanceTimersByTime(400);
        await Promise.resolve();
      });
      expect(scanCalls()).toHaveLength(1);
      expect(await screen.findByText("没有未登记文件")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("翻页发送新页码请求并清空当页选择", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      if (url.includes("page=2")) return scanBody([orphan({ storage_path: PATH_B, digest: DIGEST_B })], { total: 21, page: 2 });
      return scanBody([orphan()], { total: 21 });
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));

    fireEvent.click(screen.getByLabelText("下一页"));
    expect(await screen.findByTestId(`orphan-row-${PATH_B}`)).toBeInTheDocument();
    expect(scanCalls().at(-1)).toContain("page=2");
    expect(screen.getByRole("button", { name: "删除选中(0)" })).toBeDisabled();
  });

  it("当前页超出末页时回退到末页并重新读取", async () => {
    let scanReads = 0;
    const PATH_C = `objects/file-${"c".repeat(64)}`;
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      scanReads += 1;
      if (scanReads === 1) return scanBody([orphan()], { total: 21 });
      // 第二笔（page=2）发现服务端只剩 1 条，页面必须收回第 1 页重读。
      if (scanReads === 2) return scanBody([orphan({ storage_path: PATH_B, digest: DIGEST_B })], { total: 1, page: 2 });
      return scanBody([orphan({ storage_path: PATH_C, digest: "c".repeat(64) })], { total: 1 });
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);

    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(scanCalls().at(-1)).toContain("page=1"));
    expect(await screen.findByTestId(`orphan-row-${PATH_C}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`orphan-row-${PATH_B}`)).not.toBeInTheDocument();
  });

  it("全选、半选、反选与行选中样式", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan(), orphan({ storage_path: PATH_B, kind: "directory", digest: DIGEST_B })]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);

    const all = screen.getByLabelText("选择当前页未登记文件") as HTMLInputElement;
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    expect(all.indeterminate).toBe(true);
    expect(all).not.toBeChecked();
    expect(screen.getByTestId(`orphan-row-${PATH_A}`)).toHaveClass("selected");
    expect(screen.getByRole("button", { name: "加入队列(1)" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "删除选中(1)" })).toBeEnabled();

    fireEvent.click(all);
    expect(all.indeterminate).toBe(false);
    expect(all).toBeChecked();
    expect(screen.getByLabelText(`选择 ${PATH_B}`)).toBeChecked();
    expect(screen.getByRole("button", { name: "加入队列(2)" })).toBeEnabled();

    fireEvent.click(all);
    expect(screen.getByLabelText(`选择 ${PATH_A}`)).not.toBeChecked();
    expect(screen.getByRole("button", { name: "加入队列(0)" })).toBeDisabled();
  });
});

describe("DiskOrphanPage 入库队列", () => {
  it("四种状态、失败原因、已入库内容号与进度条按真实快照渲染", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({
          running: true,
          items: [
            queueItem({ state: "running", processed_bytes: 32 * 1024 * 1024, started_at_ms: 1700000001000 }),
            queueItem({ storage_path: PATH_B, kind: "directory", digest: DIGEST_B, state: "queued", size: 1024 }),
            queueItem({ storage_path: "objects/file-" + "c".repeat(64), digest: "c".repeat(64), state: "done", size: 2048, content_id: 7, finished_at_ms: 1700000002000 }),
            queueItem({ storage_path: "objects/file-" + "d".repeat(64), digest: "d".repeat(64), state: "failed", size: 4096, error: { code: "invalid_input", message: "内容与文件名摘要不一致" }, finished_at_ms: 1700000003000 }),
          ],
        });
      }
      return scanBody([]);
    });

    const runningRow = await screen.findByTestId(`queue-row-${PATH_A}`);
    expect(within(runningRow).getByText("处理中")).toBeInTheDocument();
    expect(screen.getByTestId(`queue-row-${PATH_B}`)).toHaveTextContent("排队中");
    expect(screen.getByTestId(`queue-row-objects/file-${"c".repeat(64)}`)).toHaveTextContent("已入库");
    expect(screen.getByTestId(`queue-row-objects/file-${"c".repeat(64)}`)).toHaveTextContent("已登记为内容 #7");
    expect(within(runningRow).getByText("—")).toBeInTheDocument();
    expect(screen.getByTestId(`queue-row-objects/file-${"d".repeat(64)}`)).toHaveTextContent(
      "内容与文件名摘要不一致",
    );

    expect(screen.getByText("共 4 条")).toBeInTheDocument();
    expect(screen.getByText("排队中 1")).toBeInTheDocument();
    expect(screen.getByText("处理中 1")).toBeInTheDocument();
    expect(screen.getByText("已入库 1")).toBeInTheDocument();
    expect(screen.getByText("失败 1")).toBeInTheDocument();
    expect(screen.getByText("安排中 1.0 KB")).toBeInTheDocument();

    const progress = screen.getByRole("progressbar", { name: "当前条目校验进度" });
    expect(progress).toHaveAttribute("aria-valuenow", String(32 * 1024 * 1024));
    expect(progress).toHaveAttribute("aria-valuemax", String(64 * 1024 * 1024));
    expect(progress.querySelector(".progress-bar-primary")).toHaveStyle({ width: "50%" });
    expect(screen.getByText(`正在校验 ${PATH_A}`)).toBeInTheDocument();
    expect(document.querySelector(".progress-meta")).toHaveTextContent("32.0 MB / 64.0 MB");

    // 处理中的条目不可删除，其余状态可删。
    expect(within(runningRow).getByRole("button", { name: `删除 ${PATH_A}` })).toBeDisabled();
    expect(screen.getByRole("button", { name: `删除 ${PATH_B}` })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^停止处理$/ })).toBeEnabled();
  });

  it("零大小条目进度按 0% 而不是 NaN 渲染", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({
          running: true,
          items: [queueItem({ state: "running", size: 0, processed_bytes: 0 })],
        });
      }
      return scanBody([]);
    });
    const progress = await screen.findByRole("progressbar", { name: "当前条目校验进度" });
    expect(progress.querySelector(".progress-bar-primary")).toHaveStyle({ width: "0%" });
  });

  it("清单非空时在页脚提示未识别数量", async () => {
    setup((url) =>
      url === "/api/v2/storage/disk-orphans/queue"
        ? snapshot()
        : scanBody([orphan()], { unrecognized_count: 2 }),
    );
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    expect(screen.getByText("另有 2 个不符合命名规则的对象未计入")).toBeInTheDocument();
  });

  it("失败条目缺错误原因时回退固定文案，已入库缺内容号时不伪造编号", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({
          items: [
            queueItem({ state: "failed", error: null }),
            queueItem({ storage_path: PATH_B, digest: DIGEST_B, state: "done", content_id: null }),
          ],
        });
      }
      return scanBody([]);
    });
    const failedRow = await screen.findByTestId(`queue-row-${PATH_A}`);
    expect(failedRow).toHaveTextContent("未知错误");
    expect(screen.getByTestId(`queue-row-${PATH_B}`)).toHaveTextContent("—");
  });

  it("队列读取失败提示中文错误且不伪造条目", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return json(500, { error: { code: "internal", message: "队列读不出来" } });
      }
      return scanBody([]);
    });
    expect(await screen.findByText("队列读不出来")).toBeInTheDocument();
    expect(screen.queryByTestId(`queue-row-${PATH_A}`)).not.toBeInTheDocument();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("开始处理返回的新快照立即生效并切换为停止按钮", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue/start") {
        return snapshot({ running: true, items: [queueItem({ state: "running" })] });
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ items: [queueItem()] });
      }
      return scanBody([]);
    });
    await screen.findByTestId(`queue-row-${PATH_A}`);
    expect(screen.getByRole("button", { name: /开始处理/ })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /开始处理/ }));

    expect(await screen.findByText("已开始处理入库队列")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /停止处理/ })).toBeEnabled();
    expect(screen.getByTestId(`queue-row-${PATH_A}`)).toHaveTextContent("处理中");
    expect(callsWith("POST", "/api/v2/storage/disk-orphans/queue/start")).toHaveLength(1);
  });

  it("停止处理返回的新快照让当前条目退回排队中且进度清零", async () => {
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue/stop") {
        return snapshot({ running: false, items: [queueItem({ state: "queued" })] });
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ running: true, items: [queueItem({ state: "running", processed_bytes: 1024 })] });
      }
      return scanBody([]);
    });
    await screen.findByRole("progressbar", { name: "当前条目校验进度" });

    fireEvent.click(screen.getByRole("button", { name: /停止处理/ }));

    expect(await screen.findByText("已停止处理，当前条目退回排队中")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("progressbar", { name: "当前条目校验进度" })).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId(`queue-row-${PATH_A}`)).toHaveTextContent("排队中");
    expect(callsWith("POST", "/api/v2/storage/disk-orphans/queue/stop")).toHaveLength(1);
  });

  it.each([
    ["开始处理失败", "/api/v2/storage/disk-orphans/queue/start"],
    ["停止处理失败", "/api/v2/storage/disk-orphans/queue/stop"],
  ])("%s 时提示中文错误并保持原开关状态", async (message, path) => {
    setup((url) => {
      if (url === path) return json(409, { error: { code: "content_busy", message: "操作失败原因" } });
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({
          running: path.endsWith("/stop"),
          items: [queueItem({ state: path.endsWith("/stop") ? "running" : "queued" })],
        });
      }
      return scanBody([]);
    });
    const label = path.endsWith("/stop") ? /停止处理/ : /开始处理/;
    await screen.findByRole("button", { name: label });
    await waitFor(() => expect(screen.getByRole("button", { name: label })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(await screen.findByText("操作失败原因")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: label })).toBeEnabled();
  });

  it("轮询只在 running 为 true 时启动，卸载后清理定时器", async () => {
    jest.useFakeTimers();
    try {
      let snapshots = 0;
      const view = setup((url) => {
        if (url === "/api/v2/storage/disk-orphans/queue") {
          snapshots += 1;
          return snapshot({ running: true, items: [queueItem({ state: "running" })] });
        }
        return scanBody([]);
      });
      await screen.findByTestId(`queue-row-${PATH_A}`);
      await waitFor(() => expect(queueGetCalls()).toHaveLength(1));

      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
        await Promise.resolve();
      });
      await waitFor(() => expect(queueGetCalls()).toHaveLength(2));

      view.unmount();
      await act(async () => {
        jest.advanceTimersByTime(6000);
        await Promise.resolve();
      });
      expect(queueGetCalls()).toHaveLength(2);
      expect(snapshots).toBe(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it("running 为 false 时不启动轮询", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot({ items: [queueItem()] }) : scanBody([])));
      await screen.findByTestId(`queue-row-${PATH_A}`);
      await waitFor(() => expect(queueGetCalls()).toHaveLength(1));
      await act(async () => {
        jest.advanceTimersByTime(8000);
        await Promise.resolve();
      });
      expect(queueGetCalls()).toHaveLength(1);
    } finally {
      jest.useRealTimers();
    }
  });

  it("页面隐藏时轮询跳过，可见后继续读取", async () => {
    jest.useFakeTimers();
    try {
      const hiddenSpy = jest.spyOn(document, "hidden", "get");
      hiddenSpy.mockReturnValue(true);
      setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot({ running: true, items: [queueItem({ state: "running" })] }) : scanBody([])));
      await screen.findByTestId(`queue-row-${PATH_A}`);
      await waitFor(() => expect(queueGetCalls()).toHaveLength(1));

      await act(async () => {
        jest.advanceTimersByTime(4000);
        await Promise.resolve();
      });
      expect(queueGetCalls()).toHaveLength(1);

      hiddenSpy.mockReturnValue(false);
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
        await Promise.resolve();
      });
      await waitFor(() => expect(queueGetCalls()).toHaveLength(2));
      hiddenSpy.mockRestore();
    } finally {
      jest.useRealTimers();
    }
  });

  it("轮询期间的静默失败不打扰用户", async () => {
    jest.useFakeTimers();
    try {
      let reads = 0;
      setup((url) => {
        if (url === "/api/v2/storage/disk-orphans/queue") {
          reads += 1;
          if (reads === 2) return json(500, { error: { code: "internal", message: "轮询失败" } });
          return snapshot({ running: true, items: [queueItem({ state: "running" })] });
        }
        return scanBody([]);
      });
      await screen.findByTestId(`queue-row-${PATH_A}`);
      await waitFor(() => expect(queueGetCalls()).toHaveLength(1));
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
        await Promise.resolve();
      });
      await waitFor(() => expect(queueGetCalls()).toHaveLength(2));
      expect(screen.queryByText("轮询失败")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("队列的过期快照不覆盖更新的快照", async () => {
    jest.useFakeTimers();
    try {
      let reads = 0;
      let releaseStale: (value: unknown) => void = () => undefined;
      const stale = new Promise((resolve) => { releaseStale = resolve; });
      setup((url) => {
        if (url === "/api/v2/storage/disk-orphans/queue") {
          reads += 1;
          // 第 1 次（初始化）正常返回，第 2 次挂起，第 3 次返回新状态。
          if (reads === 1) return snapshot({ running: true, items: [queueItem({ state: "running", processed_bytes: 1024 })] });
          if (reads === 2) return stale;
          return snapshot({ running: true, items: [queueItem({ state: "running", processed_bytes: 4096 })] });
        }
        return scanBody([]);
      });
      await screen.findByTestId(`queue-row-${PATH_A}`);
      await waitFor(() => expect(queueGetCalls()).toHaveLength(1));

      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      await waitFor(() => expect(queueGetCalls()).toHaveLength(2));
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
        await Promise.resolve();
      });
      await waitFor(() => expect(queueGetCalls()).toHaveLength(3));
      const progress = screen.getByRole("progressbar", { name: "当前条目校验进度" });
      expect(progress).toHaveAttribute("aria-valuenow", String(4096));

      await act(async () => {
        releaseStale(json(200, snapshot({ running: true, items: [queueItem({ state: "running", processed_bytes: 2048 })] })));
        await Promise.resolve();
      });
      expect(screen.getByRole("progressbar", { name: "当前条目校验进度" }))
        .toHaveAttribute("aria-valuenow", String(4096));
    } finally {
      jest.useRealTimers();
    }
  });

  it("新入库条目触发清单重读，让它从磁盘清单消失", async () => {
    jest.useFakeTimers();
    try {
      let queueReads = 0;
      let scans = 0;
      setup((url) => {
        if (url === "/api/v2/storage/disk-orphans/queue") {
          queueReads += 1;
          return queueReads === 1
            ? snapshot({ running: true, items: [queueItem({ state: "running" })] })
            : snapshot({ running: true, items: [queueItem({ state: "done", content_id: 9, finished_at_ms: 1700000009000 })] });
        }
        if (url.startsWith("/api/v2/storage/disk-orphans?")) {
          scans += 1;
          return scanBody(scans === 1 ? [orphan()] : []);
        }
        return {};
      });
      await screen.findByRole("heading", { name: "未登记文件" });
      fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
      await screen.findByTestId(`orphan-row-${PATH_A}`);
      expect(scanCalls()).toHaveLength(1);

      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      await waitFor(() => expect(scanCalls()).toHaveLength(2));
      expect(scanCalls().at(-1)).toContain("page=1");
      await waitFor(() =>
        expect(screen.queryByTestId(`orphan-row-${PATH_A}`)).not.toBeInTheDocument(),
      );
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("DiskOrphanPage 加入队列", () => {
  it("只提交选中路径，成功项提示数量并刷新队列与徽标", async () => {
    let queueReads = 0;
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: true, state: "queued", error: null },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        queueReads += 1;
        return snapshot({ items: queueReads === 1 ? [] : [queueItem()] });
      }
      return scanBody([orphan(), orphan({ storage_path: PATH_B, digest: DIGEST_B, kind: "directory" })]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));

    expect(await screen.findByText("已加入队列 1 项")).toBeInTheDocument();
    expect(JSON.parse(callsWith("POST", "/api/v2/storage/disk-orphans/queue")[0][1]!.body as string))
      .toEqual({ storage_paths: [PATH_A] });
    expect(await screen.findByTestId(`queue-row-${PATH_A}`)).toHaveTextContent("排队中");
    await waitFor(() => expect(screen.getByLabelText(`选择 ${PATH_A}`)).not.toBeChecked());
  });

  it("部分拒绝提示成功数量并把失败项交给明细弹窗", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: true, state: "queued", error: null },
          { storage_path: PATH_B, accepted: false, state: null, error: { code: "invalid_state", message: "入库队列已满" } },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan(), orphan({ storage_path: PATH_B, digest: DIGEST_B, kind: "directory" })]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText("选择当前页未登记文件"));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(2)" }));

    expect(await screen.findByText("已加入队列 1 项")).toBeInTheDocument();
    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(within(failures).getByText(PATH_B)).toBeInTheDocument();
    expect(within(failures).getByText("入库队列已满")).toBeInTheDocument();
    fireEvent.click(within(failures).getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("全部拒绝时提示首个原因并保留选中现场", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: false, state: null, error: { code: "not_found", message: "存储路径不存在" } },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));

    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(
      within(failures).getByText("存储路径不存在", { selector: ".batch-failure-list span" }),
    ).toBeInTheDocument();
    expect(within(failures).getByText(PATH_A)).toBeInTheDocument();
    expect(document.querySelector(".toast-message")).toHaveTextContent("存储路径不存在");
    expect(screen.getByLabelText(`选择 ${PATH_A}`)).toBeChecked();
    expect(screen.queryByText(/已加入队列/)).not.toBeInTheDocument();
  });

  it("全部拒绝且无错误详情时使用固定失败文案", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") {
        return [{ storage_path: PATH_A, accepted: false, state: null, error: null }];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));

    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(
      within(failures).getByText("未知错误", { selector: ".batch-failure-list span" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(`选择 ${PATH_A}`)).toBeChecked();
  });

  it("入队请求失败提示中文错误且不清空选择", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") {
        return json(422, { error: { code: "invalid_input", message: "请求无效" } });
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));

    expect(await screen.findByText("请求无效")).toBeInTheDocument();
    expect(screen.getByLabelText(`选择 ${PATH_A}`)).toBeChecked();
    expect(screen.queryByText(/已加入队列/)).not.toBeInTheDocument();
  });
});

describe("DiskOrphanPage 删除", () => {
  it("确认前零删除请求，取消保留选中", async () => {
    setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : scanBody([orphan()])));
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));

    const dialog = await screen.findByRole("dialog", { name: "删除未登记文件" });
    expect(dialog).toHaveTextContent("确定删除选中的 1 项吗？");
    expect(dialog).toHaveTextContent("将立即释放 64.0 MB 磁盘空间。");
    expect(within(dialog).getByRole("button", { name: "返回" })).toHaveFocus();
    expect(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")).toHaveLength(0);

    fireEvent.click(within(dialog).getByRole("button", { name: "返回" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText(`选择 ${PATH_A}`)).toBeChecked();
    expect(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")).toHaveLength(0);

    // 右上角关闭按钮同样只能取消，不能触发删除。
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const reopened = await screen.findByRole("dialog", { name: "删除未登记文件" });
    fireEvent.click(within(reopened).getByRole("button", { name: "关闭对话框" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")).toHaveLength(0);
  });

  it("立即删除申报释放字节数并刷新清单与队列", async () => {
    let scans = 0;
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: true, mode: "immediate", freed_bytes: 64 * 1024 * 1024, error: null },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      if (url.startsWith("/api/v2/storage/disk-orphans?")) {
        scans += 1;
        return scanBody(scans === 1 ? [orphan()] : []);
      }
      return {};
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    expect(await screen.findByText("已删除 1 项，释放 64.0 MB")).toBeInTheDocument();
    expect(
      JSON.parse(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")[0][1]!.body as string),
    ).toEqual({ storage_paths: [PATH_A] });
    await waitFor(() => expect(scanCalls()).toHaveLength(2));
    await waitFor(() =>
      expect(screen.queryByTestId(`orphan-row-${PATH_A}`)).not.toBeInTheDocument(),
    );
  });

  it("删除返回空结果时提示固定失败文案且不宣称成功", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") return [];
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(
      within(failures).getByText("删除失败", { selector: ".batch-failure-list span" }),
    ).toBeInTheDocument();
    expect(document.querySelector(".toast-message")).toHaveTextContent("删除失败");
    expect(screen.getByRole("dialog", { name: "删除未登记文件" })).toBeInTheDocument();
    expect(screen.queryByText(/已删除 1 项/)).not.toBeInTheDocument();
  });

  it("入队返回空结果时提示固定失败文案", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") return [];
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));

    expect(await screen.findByText("加入队列失败")).toBeInTheDocument();
    expect(screen.queryByText(/已加入队列/)).not.toBeInTheDocument();
  });

  it("已入库条目删除只宣称受理，不宣称已释放空间", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: true, mode: "deferred", freed_bytes: 0, error: null },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ items: [queueItem({ state: "done", content_id: 5 })] });
      }
      return scanBody([]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(await screen.findByRole("button", { name: `删除 ${PATH_A}` }));

    const dialog = await screen.findByRole("dialog", { name: "删除未登记文件" });
    expect(dialog).toHaveTextContent("其中 1 项已入库，删除只受理，稍后由后台任务完成。");
    expect(dialog).not.toHaveTextContent("将立即释放");

    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("已受理 1 项删除，稍后完成")).toBeInTheDocument();
    expect(screen.queryByText(/已删除 1 项/)).not.toBeInTheDocument();
  });

  it("立即与受托混合时分别汇报两种结果", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: true, mode: "immediate", freed_bytes: 1024, error: null },
          { storage_path: PATH_B, accepted: true, mode: "deferred", freed_bytes: 0, error: null },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ items: [queueItem({ storage_path: PATH_B, digest: DIGEST_B, state: "done", content_id: 3 })] });
      }
      return scanBody([orphan({ size: 1024 }), orphan({ storage_path: PATH_B, digest: DIGEST_B, size: 2048 })]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText("选择当前页未登记文件"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(2)" }));

    const dialog = await screen.findByRole("dialog", { name: "删除未登记文件" });
    expect(dialog).toHaveTextContent("将立即释放 1.0 KB 磁盘空间。");
    expect(dialog).toHaveTextContent("其中 1 项已入库");

    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(
      await screen.findByText("已删除 1 项，释放 1.0 KB；已受理 1 项删除，稍后完成"),
    ).toBeInTheDocument();
  });

  it("部分拒绝时提示成功数量并逐条列出失败原因", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: true, mode: "immediate", freed_bytes: 1024, error: null },
          { storage_path: PATH_B, accepted: false, mode: null, freed_bytes: 0, error: { code: "content_busy", message: "已在别处登记" } },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan({ size: 1024 }), orphan({ storage_path: PATH_B, digest: DIGEST_B, size: 2048 })]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText("选择当前页未登记文件"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(2)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    expect(await screen.findByText("已删除 1 项，释放 1.0 KB")).toBeInTheDocument();
    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(within(failures).getByText(PATH_B)).toBeInTheDocument();
    expect(within(failures).getByText("已在别处登记")).toBeInTheDocument();
  });

  it("处理中的条目删除被拒绝并提示先停止", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        return [
          { storage_path: PATH_A, accepted: false, mode: null, freed_bytes: 0, error: { code: "content_busy", message: "正在校验中，请先停止处理" } },
        ];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ running: true, items: [queueItem({ state: "running", processed_bytes: 1024 })] });
      }
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(
      within(failures).getByText("正在校验中，请先停止处理", { selector: ".batch-failure-list span" }),
    ).toBeInTheDocument();
    expect(within(failures).getByText(PATH_A)).toBeInTheDocument();
    expect(document.querySelector(".toast-message")).toHaveTextContent("正在校验中，请先停止处理");
  });

  it("全部拒绝且无错误详情时使用固定失败文案", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        return [{ storage_path: PATH_A, accepted: false, mode: null, freed_bytes: 0, error: null }];
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(
      within(failures).getByText("未知错误", { selector: ".batch-failure-list span" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "删除未登记文件" })).toBeInTheDocument();
  });

  it.each([
    ["HTTP 失败", json(422, { error: { code: "invalid_input", message: "请求无效" } }), "请求无效"],
    ["网络失败", new TypeError("offline"), "网络异常，请检查连接后重试"],
  ])("%s 时不伪成功且保留重试现场", async (_name, failure, message) => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") {
        if (failure instanceof Error) throw failure;
        return failure;
      }
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const before = scanCalls().length;
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "删除未登记文件" })).toBeInTheDocument();
    expect(screen.getByLabelText(`选择 ${PATH_A}`)).toBeChecked();
    expect(scanCalls()).toHaveLength(before);
    expect(screen.queryByText(/已删除 1 项/)).not.toBeInTheDocument();
  });

  it("删除期间保持模态、禁止关闭与重复提交", async () => {
    let releaseDelete: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseDelete = resolve; });
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") return pending;
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const dialog = await screen.findByRole("dialog", { name: "删除未登记文件" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    expect(await within(dialog).findByRole("button", { name: "删除中..." })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "返回" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "关闭对话框" })).toBeDisabled();
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(screen.getByRole("dialog", { name: "删除未登记文件" })).toBeInTheDocument();
    expect(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")).toHaveLength(1);

    releaseDelete(json(200, [
      { storage_path: PATH_A, accepted: true, mode: "immediate", freed_bytes: 1024, error: null },
    ]));
    expect(await screen.findByText("已删除 1 项，释放 1.0 KB")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("DiskOrphanPage 卸载保护", () => {
  it("卸载后的扫描失败不弹 Toast", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    let rejectScan: (error: Error) => void = () => undefined;
    const pending = new Promise((_resolve, reject) => { rejectScan = reject; });
    const view = setup((url) => (url === "/api/v2/storage/disk-orphans/queue" ? snapshot() : pending));
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await waitFor(() => expect(scanCalls()).toHaveLength(1));
    view.unmount();
    rejectScan(new Error("扫描炸了"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(showToast).not.toHaveBeenCalled();
    useToastSpy.mockRestore();
  });

  it("卸载后的删除成功响应不弹 Toast 也不重扫", async () => {
    let releaseDelete: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseDelete = resolve; });
    let scans = 0;
    const view = setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") return pending;
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      if (url.startsWith("/api/v2/storage/disk-orphans?")) {
        scans += 1;
        return scanBody([orphan()]);
      }
      return {};
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() => expect(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")).toHaveLength(1));

    view.unmount();
    releaseDelete(json(200, [
      { storage_path: PATH_A, accepted: true, mode: "immediate", freed_bytes: 1024, error: null },
    ]));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(scans).toBe(1);
    expect(screen.queryByText(/已删除 1 项/)).not.toBeInTheDocument();
  });

  it("过期扫描响应不覆盖更新条件的结果", async () => {
    // 扫描进行中不允许重复点扫描，所以用搜索防抖发起后续请求：
    // 先返回的新结果不能被随后到达的旧响应覆盖。
    let releaseStale: (value: unknown) => void = () => undefined;
    const stale = new Promise((resolve) => { releaseStale = resolve; });
    let scans = 0;
    setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      if (url.startsWith("/api/v2/storage/disk-orphans?")) {
        scans += 1;
        if (scans === 2) return stale;
        if (scans === 3) return scanBody([orphan({ storage_path: PATH_B, digest: DIGEST_B })]);
        return scanBody([orphan()]);
      }
      return {};
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);

    fireEvent.change(screen.getByLabelText("按存储路径过滤"), { target: { value: "bbbb" } });
    await waitFor(() => expect(scans).toBe(2));

    fireEvent.change(screen.getByLabelText("按存储路径过滤"), { target: { value: "cccc" } });
    await waitFor(() => expect(scans).toBe(3));
    expect(await screen.findByTestId(`orphan-row-${PATH_B}`)).toBeInTheDocument();

    const PATH_E = `objects/file-${"e".repeat(64)}`;
    await act(async () => {
      releaseStale(json(200, scanBody([orphan({ storage_path: PATH_E, digest: "e".repeat(64) })])));
      await Promise.resolve();
    });
    expect(screen.queryByTestId(`orphan-row-${PATH_E}`)).not.toBeInTheDocument();
    expect(screen.getByTestId(`orphan-row-${PATH_B}`)).toBeInTheDocument();
  });

  it("卸载后才返回的队列快照与开关响应都不再落地", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    let releaseQueue: (value: unknown) => void = () => undefined;
    let releaseStart: (value: unknown) => void = () => undefined;
    const queuePending = new Promise((resolve) => { releaseQueue = resolve; });
    const startPending = new Promise((resolve) => { releaseStart = resolve; });
    let reads = 0;
    const view = setup((url) => {
      if (url === "/api/v2/storage/disk-orphans/queue/start") return startPending;
      if (url === "/api/v2/storage/disk-orphans/queue") {
        reads += 1;
        return reads === 1
          ? snapshot({ items: [queueItem()] })
          : queuePending;
      }
      return scanBody([]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /开始处理/ })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /开始处理/ }));
    await waitFor(() =>
      expect(callsWith("POST", "/api/v2/storage/disk-orphans/queue/start")).toHaveLength(1),
    );

    const queueReadsBeforeUnmount = queueGetCalls().length;
    view.unmount();
    await act(async () => {
      releaseQueue(json(200, snapshot({ running: true, items: [queueItem({ state: "running" })] })));
      releaseStart(json(200, snapshot({ running: true, items: [queueItem({ state: "running" })] })));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(showToast).not.toHaveBeenCalled();
    expect(queueGetCalls()).toHaveLength(queueReadsBeforeUnmount);
    useToastSpy.mockRestore();
  });

  it("卸载后的开关失败与入队成功都不弹 Toast", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    let rejectStart: (error: Error) => void = () => undefined;
    let releaseEnqueue: (value: unknown) => void = () => undefined;
    const startPending = new Promise((_resolve, reject) => { rejectStart = reject; });
    const enqueuePending = new Promise((resolve) => { releaseEnqueue = resolve; });
    const view = setup((url, init) => {
      if (url === "/api/v2/storage/disk-orphans/queue/start") return startPending;
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") {
        return enqueuePending;
      }
      if (url === "/api/v2/storage/disk-orphans/queue") {
        return snapshot({ items: [queueItem({ state: "failed", error: { code: "invalid_input", message: "校验失败" } })] });
      }
      return scanBody([orphan({ size: 1024 })]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));
    await waitFor(() =>
      expect(callsWith("POST", "/api/v2/storage/disk-orphans/queue")).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: /开始处理/ }));
    await waitFor(() =>
      expect(callsWith("POST", "/api/v2/storage/disk-orphans/queue/start")).toHaveLength(1),
    );

    const queueReadsBeforeUnmount = queueGetCalls().length;
    view.unmount();
    await act(async () => {
      rejectStart(new Error("开关炸了"));
      releaseEnqueue(json(200, [
        { storage_path: PATH_A, accepted: true, state: "queued", error: null },
      ]));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(showToast).not.toHaveBeenCalled();
    expect(queueGetCalls()).toHaveLength(queueReadsBeforeUnmount);
    useToastSpy.mockRestore();
  });

  it("卸载后的删除失败不弹 Toast", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    let rejectDelete: (error: Error) => void = () => undefined;
    const pending = new Promise((_resolve, reject) => { rejectDelete = reject; });
    const view = setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/storage/disk-orphans/queue") return pending;
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() =>
      expect(callsWith("DELETE", "/api/v2/storage/disk-orphans/queue")).toHaveLength(1),
    );
    view.unmount();
    rejectDelete(new Error("删除炸了"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(showToast).not.toHaveBeenCalled();
    useToastSpy.mockRestore();
  });

  it("卸载后的入队失败不弹 Toast", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    let rejectEnqueue: (error: Error) => void = () => undefined;
    const pending = new Promise((_resolve, reject) => { rejectEnqueue = reject; });
    const view = setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/disk-orphans/queue") return pending;
      if (url === "/api/v2/storage/disk-orphans/queue") return snapshot();
      return scanBody([orphan()]);
    });
    await screen.findByRole("heading", { name: "未登记文件" });
    fireEvent.click(screen.getByRole("button", { name: /^扫描$/ }));
    await screen.findByTestId(`orphan-row-${PATH_A}`);
    fireEvent.click(screen.getByLabelText(`选择 ${PATH_A}`));
    fireEvent.click(screen.getByRole("button", { name: "加入队列(1)" }));
    await waitFor(() =>
      expect(callsWith("POST", "/api/v2/storage/disk-orphans/queue")).toHaveLength(1),
    );
    view.unmount();
    rejectEnqueue(new Error("入队炸了"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(showToast).not.toHaveBeenCalled();
    useToastSpy.mockRestore();
  });
});
