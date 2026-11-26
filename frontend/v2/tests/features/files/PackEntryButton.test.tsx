import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { renderToString } from "react-dom/server";

import { PackEntryButton } from "@/features/files/components/PackEntryButton";
import { PackRecordsDrawer } from "@/features/files/components/PackRecordsDrawer";
import { ToastProvider } from "@/shared/components/Toast";
import type { PackViewResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function pack(overrides: Partial<PackViewResponse> = {}): PackViewResponse {
  return {
    id: 1,
    status: "done",
    step: null,
    step_progress: 100,
    step_started_at_ms: null,
    created_at_ms: 1,
    updated_at_ms: 1,
    finished_at_ms: 2,
    error_code: null,
    error_message: "",
    delete_source: false,
    output_name: "archive.zip",
    output: { user_file_id: 9, display_name: "archive.zip", size: 2048 },
    ...overrides,
  };
}

function listEnvelope(items: PackViewResponse[]): unknown {
  return { items };
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;
const originalMatchMedia = window.matchMedia;

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: jest.fn(() => ({ matches: true })),
  });
  mockFetch.mockReset();
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
  if (originalMatchMedia) {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: originalMatchMedia,
    });
  } else {
    Reflect.deleteProperty(window, "matchMedia");
  }
  document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
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

function renderEntry(onTaskComplete?: () => void) {
  return render(
    <ToastProvider>
      <PackEntryButton onTaskComplete={onTaskComplete} />
    </ToastProvider>,
  );
}

async function openDrawer(): Promise<HTMLDialogElement> {
  const entry = await screen.findByRole("button", { name: /打包记录/ });
  entry.focus();
  fireEvent.click(entry);
  return await screen.findByRole("dialog", { name: "打包记录" }) as HTMLDialogElement;
}

function packCalls(): number {
  return mockFetch.mock.calls.filter(([url]) => url === "/api/v2/packs?limit=50").length;
}

describe("PackEntryButton 入口与记录展示", () => {
  it("空记录也常显入口，抽屉展示空态且清空禁用", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([])));
    renderEntry();
    const entry = screen.getByRole("button", { name: /打包记录/ });
    expect(entry).toHaveAttribute("aria-haspopup", "dialog");
    expect(entry.querySelector("[data-icon=archive]")).not.toBeNull();
    expect(entry.querySelector(".pack-entry-badge")).toHaveAttribute("hidden");

    const dialog = await openDrawer();
    expect(entry).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("还没有打包记录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "清空已结束" })).toBeDisabled();
    expect(dialog).toHaveClass("drawer-overlay");
    const content = dialog.querySelector(".drawer-content");
    expect(content).toHaveClass("pack-records-drawer");
    expect(content?.querySelector(":scope > .drawer-body")).not.toBeNull();
    expect(content?.querySelector(":scope > .drawer-actions")).not.toBeNull();
  });

  it("读取失败在抽屉显示真实错误，重试成功后显示真实记录", async () => {
    let resolveRetry: ((value: unknown) => void) | undefined;
    mockFetch
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockReturnValueOnce(new Promise((resolve) => { resolveRetry = resolve; }));
    renderEntry();
    await openDrawer();
    expect(await screen.findByRole("alert")).toHaveTextContent("加载失败：网络异常，请检查连接后重试");
    expect(screen.queryByText("还没有打包记录")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("button", { name: "重试中..." })).toBeDisabled();
    await act(async () => {
      resolveRetry?.(json(200, listEnvelope([pack({ output_name: "real.zip" })])));
      await Promise.resolve();
    });
    expect(await screen.findByText("real.zip")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(packCalls()).toBe(2);
  });

  it("初始读取期间展示加载状态，不提前显示空态", async () => {
    let resolveFetch: ((value: unknown) => void) | undefined;
    mockFetch.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));
    renderEntry();
    await openDrawer();
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("正在读取打包记录...");
    expect(screen.queryByText("还没有打包记录")).not.toBeInTheDocument();
    await act(async () => {
      resolveFetch?.(json(200, listEnvelope([])));
      await Promise.resolve();
    });
    expect(await screen.findByText("还没有打包记录")).toBeInTheDocument();
  });

  it("活跃徽标统计 pending 与 packing，0 时隐藏", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([
      pack({ id: 1, status: "pending", output: null }),
      pack({ id: 2, status: "packing", output: null }),
      pack({ id: 3 }),
    ])));
    renderEntry();
    const badge = await screen.findByText("2");
    expect(badge).toHaveClass("pack-entry-badge");
    expect(badge).not.toHaveAttribute("hidden");
    expect(screen.getByRole("button", { name: /打包记录/ })).toHaveTextContent("2");
  });

  it("按进行中与已结束分组，并诚实展示进度、产物和错误", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([
      pack({ id: 1, status: "pending", step_progress: 0, output_name: "waiting.zip", output: null, finished_at_ms: null }),
      pack({ id: 2, status: "packing", step: "compressing", step_progress: 40, output_name: "doing.zip", output: null, finished_at_ms: null }),
      pack({ id: 3, output_name: "finished.zip", delete_source: true }),
      pack({ id: 4, output_name: "missing.zip", output: null }),
      pack({ id: 5, status: "failed", error_code: "pack_output_deleted", error_message: "打包产物已删除", output_name: "shared.zip", output: null }),
      pack({ id: 6, status: "failed", error_code: "invalid_state", error_message: "打包来源或任务状态已变化", output_name: "failed.zip", output: null }),
      pack({ id: 7, status: "cancelled", output_name: "cancelled.zip", output: null }),
    ])));
    renderEntry();
    await openDrawer();

    expect(screen.getByText("进行中 · 2")).toBeInTheDocument();
    expect(screen.getByText("已结束 · 5")).toBeInTheDocument();
    expect(screen.getByText("waiting.zip")).toBeInTheDocument();
    expect(screen.getByText(/打包中 · 压缩 · 40%/)).toBeInTheDocument();
    expect(screen.getByText(/已完成 · 2.0 KB · 已删除源文件/)).toBeInTheDocument();
    expect(screen.getAllByText(/产物已删除/)).toHaveLength(2);
    expect(screen.getByText(/打包来源或任务状态已变化/)).toBeInTheDocument();
    // 原始 error_code 不上屏，只留在悬停提示供排障。
    expect(screen.queryByText(/invalid_state/)).not.toBeInTheDocument();
    const failedMeta = screen.getByText(/打包来源或任务状态已变化/);
    expect(failedMeta).toHaveAttribute("title", "invalid_state");
    expect(screen.queryByText(/pack_output_deleted/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载 missing.zip" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载 shared.zip" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载 finished.zip" })).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar")).toHaveLength(2);
    expect(screen.getByRole("progressbar", { name: "doing.zip 打包进度" })).toHaveAttribute("aria-valuenow", "40");
    const deletedRow = document.querySelector("[data-pack-id=4]") as HTMLElement;
    expect(deletedRow).not.toHaveTextContent("已完成");
    expect(deletedRow).not.toHaveTextContent("0 B");
    const sharedRow = document.querySelector("[data-pack-id=5]") as HTMLElement;
    expect(sharedRow).not.toHaveTextContent("失败");
  });

  it("失败行缺少中文原因时给固定兜底，不回落到原始代号", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([
      pack({ id: 1, status: "failed", error_code: "internal", error_message: "", output: null }),
    ])));
    renderEntry();
    await openDrawer();

    const row = document.querySelector("[data-pack-id=1]") as HTMLElement;
    expect(row).toHaveTextContent("打包失败");
    expect(row).not.toHaveTextContent("internal");
  });
});

describe("PackEntryButton 耗时文本", () => {
  const NOW = 1_700_000_000_000;

  async function openWithTiming(overrides: Partial<PackViewResponse>, elapsedSeconds: number) {
    jest.useFakeTimers();
    jest.setSystemTime(NOW);
    mockFetch.mockResolvedValue(json(200, listEnvelope([
      pack({
        id: 1,
        status: "packing",
        step_started_at_ms: NOW - elapsedSeconds * 1000,
        output: null,
        ...overrides,
      }),
    ])));
    renderEntry();
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole("button", { name: /打包记录/ }));
    await act(async () => { await Promise.resolve(); });
  }

  it.each([
    ["秒级", { step: "validating" as const, step_progress: 50 }, 30, "校验 · 50% · 已用 30秒 / 预计剩余 30秒"],
    ["分钟级", { step: "compressing" as const, step_progress: 25 }, 90, "压缩 · 25% · 已用 1分30秒 / 预计剩余 4分30秒"],
    ["小时级", { step: "verifying" as const, step_progress: 100 }, 5970, "验收 · 100% · 已用 1小时39分30秒 / 预计剩余 0秒"],
    ["未知步骤", { step: null, step_progress: 50 }, 30, "处理中 · 50% · 已用 30秒 / 预计剩余 30秒"],
  ])("%s 显示真实进度、已用与预计剩余", async (_label, overrides, elapsed, expected) => {
    try {
      await openWithTiming(overrides, elapsed);
      expect(screen.getByText(new RegExp(expected))).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it.each([
    ["未开始", { step_started_at_ms: null, step_progress: 50 }],
    ["进度为 0", { step_started_at_ms: NOW, step_progress: 0 }],
    ["进度超 100", { step_started_at_ms: NOW, step_progress: 120 }],
    ["进度非数值", { step_started_at_ms: NOW, step_progress: Number.NaN }],
    ["耗时不足 1 秒", { step_started_at_ms: NOW, step_progress: 50 }],
  ])("耗时不可用时显示占位（%s）", async (_label, overrides) => {
    try {
      await openWithTiming({ step: "compressing", ...overrides }, 0);
      expect(screen.getByText(/已用 -- \/ 预计剩余 --/)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("PackRecordsDrawer 退出与焦点", () => {
  it.each(["按钮", "标题按钮", "Escape", "遮罩"])("%s 关闭后焦点恢复到入口", async (method) => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderEntry();
    const entry = screen.getByRole("button", { name: /打包记录/ });
    const dialog = await openDrawer();
    expect(dialog).toBeInTheDocument();

    if (method === "按钮") fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    if (method === "标题按钮") fireEvent.click(screen.getByRole("button", { name: "关闭打包记录" }));
    if (method === "Escape") fireEvent(dialog, new Event("cancel", { cancelable: true }));
    if (method === "遮罩") fireEvent.click(dialog.querySelector(".modal-backdrop-button")!);

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "打包记录" })).not.toBeInTheDocument());
    expect(entry).toHaveFocus();
    expect(entry).toHaveAttribute("aria-expanded", "false");
  });

  it("静态首次渲染不挂 portal，由客户端 mounted 后接管", () => {
    const html = renderToString(
      <PackRecordsDrawer
        tasks={[]}
        loading={false}
        error={null}
        nowMs={0}
        operation={null}
        trigger={null}
        onClose={jest.fn()}
        onRetry={jest.fn()}
        onClear={jest.fn()}
        onCancel={jest.fn()}
        onDelete={jest.fn()}
        onDownload={jest.fn()}
      />,
    );
    expect(html).toBe("");
  });

  it("抽屉内部点击不会误关闭", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderEntry();
    const dialog = await openDrawer();
    fireEvent.click(dialog.querySelector(".drawer-body")!);
    expect(screen.getByRole("dialog", { name: "打包记录" })).toBeInTheDocument();
  });
});

describe("PackEntryButton 真实操作", () => {
  it("下载 done 产物使用 output.user_file_id 与真实文件名", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([
      pack({ output_name: "record.zip", output: { user_file_id: 42, display_name: "actual.zip", size: 2048 } }),
    ])));
    const clicked: HTMLAnchorElement[] = [];
    const clickSpy = jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function click(this: HTMLAnchorElement) {
      clicked.push(this);
    });
    try {
      renderEntry();
      await openDrawer();
      fireEvent.click(screen.getByRole("button", { name: "下载 record.zip" }));
      expect(clicked).toHaveLength(1);
      expect(clicked[0].getAttribute("href")).toBe("/api/v2/files/42/download");
      expect(clicked[0].download).toBe("actual.zip");
      expect(await screen.findByText("已触发打包文件下载: record.zip")).toBeInTheDocument();
    } finally {
      clickSpy.mockRestore();
    }
  });

  it("取消确认后调用 cancel 端点，忙碌时禁用并重新读取", async () => {
    let resolveCancel: ((value: unknown) => void) | undefined;
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 5, status: "packing", output: null })]));
      if (url === "/api/v2/packs/5/cancel" && init?.method === "POST") {
        return new Promise((resolve) => { resolveCancel = resolve; });
      }
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const confirm = await screen.findByRole("dialog", { name: "取消打包任务" });
    fireEvent.click(within(confirm).getByRole("button", { name: "取消任务" }));
    const busyButton = await screen.findByRole("button", { name: "取消中..." });
    expect(busyButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "清空已结束" })).toBeDisabled();
    const beforeReload = packCalls();

    await act(async () => {
      resolveCancel?.(json(204, null));
      await Promise.resolve();
    });
    await waitFor(() => expect(packCalls()).toBe(beforeReload + 1));
    expect(await screen.findByRole("button", { name: "取消" })).toBeEnabled();
  });

  it("取消确认被驳回时不发请求", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 5, status: "packing", output: null })])));
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const confirm = await screen.findByRole("dialog", { name: "取消打包任务" });
    fireEvent.click(within(confirm).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "取消打包任务" })).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/cancel"))).toBe(false);
    expect(screen.getByRole("dialog", { name: "打包记录" })).toBeInTheDocument();
  });

  it("取消失败显示传输错误且不伪改记录", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 5, status: "packing", output: null })]));
      if (url === "/api/v2/packs/5/cancel" && init?.method === "POST") return json(500, { error: { code: "internal", message: "服务器错误" } });
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    const confirm = await screen.findByRole("dialog", { name: "取消打包任务" });
    fireEvent.click(within(confirm).getByRole("button", { name: "取消任务" }));
    expect(await screen.findByText("取消失败: 服务器错误")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  });

  it.each(["done", "failed", "cancelled"] as const)("%s 终态记录确认后调用 record 删除端点", async (status) => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 8, status, output: null })]));
      if (url === "/api/v2/packs/8/record" && init?.method === "DELETE") return json(204, null);
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "删除 archive.zip 记录" }));
    const confirm = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(confirm).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      expect(mockFetch.mock.calls.some(([url, init]) => url === "/api/v2/packs/8/record" && init?.method === "DELETE")).toBe(true);
    });
    await waitFor(() => expect(packCalls()).toBe(2));
  });

  it("删除请求进行中显示忙碌标题并禁用图标按钮", async () => {
    let resolveDelete: ((value: unknown) => void) | undefined;
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 8 })]));
      if (url === "/api/v2/packs/8/record" && init?.method === "DELETE") {
        return new Promise((resolve) => { resolveDelete = resolve; });
      }
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "删除 archive.zip 记录" }));
    const confirm = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(confirm).getByRole("button", { name: "删除" }));
    const button = screen.getByRole("button", { name: "删除 archive.zip 记录" });
    await waitFor(() => expect(button).toHaveAttribute("aria-busy", "true"));
    expect(button).toHaveAttribute("title", "删除中...");
    expect(button).toBeDisabled();
    await act(async () => {
      resolveDelete?.(json(204, null));
      await Promise.resolve();
    });
    await waitFor(() => expect(button).toHaveAttribute("aria-busy", "false"));
  });

  it("删除确认被驳回时不发请求", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 8 })])));
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "删除 archive.zip 记录" }));
    const confirm = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(confirm).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "删除任务记录" })).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/record"))).toBe(false);
  });

  it("删除失败展示真实错误", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 8 })]));
      if (url === "/api/v2/packs/8/record" && init?.method === "DELETE") return json(500, { error: { code: "internal", message: "服务器错误" } });
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "删除 archive.zip 记录" }));
    const confirm = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(confirm).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除失败: 服务器错误")).toBeInTheDocument();
  });

  it("清空已结束调用 records 端点，防重复并展示真实返回条数", async () => {
    let resolveClear: ((value: unknown) => void) | undefined;
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack()]));
      if (url === "/api/v2/packs/records" && init?.method === "DELETE") {
        return new Promise((resolve) => { resolveClear = resolve; });
      }
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "清空已结束" }));
    expect(await screen.findByRole("button", { name: "清空中..." })).toBeDisabled();
    const beforeReload = packCalls();
    await act(async () => {
      resolveClear?.(json(200, { cleared_count: 3 }));
      await Promise.resolve();
    });
    expect(await screen.findByText("已清空 3 条记录")).toBeInTheDocument();
    await waitFor(() => expect(packCalls()).toBe(beforeReload + 1));
  });

  it("清空失败展示真实错误", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack()]));
      if (url === "/api/v2/packs/records" && init?.method === "DELETE") return json(500, { error: { code: "internal", message: "服务器错误" } });
      return json(404, {});
    });
    renderEntry();
    await openDrawer();
    fireEvent.click(screen.getByRole("button", { name: "清空已结束" }));
    expect(await screen.findByText("清空失败: 服务器错误")).toBeInTheDocument();
  });
});

describe("PackEntryButton 轮询与完成刷新", () => {
  function deferred() {
    let resolve!: (value: unknown) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    return { promise, resolve, reject };
  }

  it("有活跃任务时每 2 秒轮询，打开抽屉不另发读取", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, status: "packing", output: null })])));
      renderEntry();
      await act(async () => { await Promise.resolve(); });
      expect(packCalls()).toBe(1);
      fireEvent.click(screen.getByRole("button", { name: /打包记录/ }));
      await act(async () => { await Promise.resolve(); });
      expect(packCalls()).toBe(1);
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(packCalls()).toBe(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it("document.hidden 时跳过轮询", async () => {
    jest.useFakeTimers();
    const hiddenSpy = jest.spyOn(document, "hidden", "get").mockReturnValue(true);
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, status: "pending", output: null })])));
      renderEntry();
      await act(async () => { await Promise.resolve(); });
      await act(async () => {
        jest.advanceTimersByTime(4000);
        await Promise.resolve();
      });
      expect(packCalls()).toBe(1);
    } finally {
      hiddenSpy.mockRestore();
      jest.useRealTimers();
    }
  });

  it("纯终态历史不持续轮询", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
      renderEntry();
      await act(async () => { await Promise.resolve(); });
      await act(async () => {
        jest.advanceTimersByTime(6000);
        await Promise.resolve();
      });
      expect(packCalls()).toBe(1);
    } finally {
      jest.useRealTimers();
    }
  });

  it("打包中每秒刷新已用时间", async () => {
    const now = 1_700_000_000_000;
    jest.useFakeTimers();
    jest.setSystemTime(now);
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([
        pack({ id: 1, status: "packing", step: "compressing", step_progress: 50, step_started_at_ms: now, output: null }),
      ])));
      renderEntry();
      await act(async () => { await Promise.resolve(); });
      fireEvent.click(screen.getByRole("button", { name: /打包记录/ }));
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByText(/已用 -- \/ 预计剩余 --/)).toBeInTheDocument();
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(screen.getByText(/已用 2秒 \/ 预计剩余 2秒/)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("活跃任务转为 done 时只回调一次 onTaskComplete", async () => {
    jest.useFakeTimers();
    try {
      const onTaskComplete = jest.fn();
      mockFetch.mockResolvedValueOnce(json(200, listEnvelope([pack({ id: 1, status: "packing", output: null })])));
      renderEntry(onTaskComplete);
      await act(async () => { await Promise.resolve(); });
      expect(onTaskComplete).not.toHaveBeenCalled();
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, status: "done" })])));
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(onTaskComplete).toHaveBeenCalledTimes(1);
      await act(async () => {
        jest.advanceTimersByTime(4000);
        await Promise.resolve();
      });
      expect(onTaskComplete).toHaveBeenCalledTimes(1);
    } finally {
      jest.useRealTimers();
    }
  });

  it("旧初始请求结束时不会关闭新请求的加载态", async () => {
    const oldRequest = deferred();
    const currentRequest = deferred();
    mockFetch
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(currentRequest.promise);
    render(
      <StrictMode>
        <ToastProvider>
          <PackEntryButton />
        </ToastProvider>
      </StrictMode>,
    );
    expect(packCalls()).toBe(2);
    await openDrawer();

    await act(async () => {
      oldRequest.resolve(json(200, listEnvelope([pack({ output_name: "old.zip" })])));
      await Promise.resolve();
    });
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("正在读取打包记录...");
    expect(screen.queryByText("old.zip")).not.toBeInTheDocument();

    await act(async () => {
      currentRequest.resolve(json(200, listEnvelope([])));
      await Promise.resolve();
    });
    expect(await screen.findByText("还没有打包记录")).toBeInTheDocument();
  });

  it("旧 packing 轮询晚于新 done 返回时不回退也不重复刷新", async () => {
    jest.useFakeTimers();
    const oldPoll = deferred();
    const onTaskComplete = jest.fn();
    let requests = 0;
    const packing = pack({ id: 1, status: "packing", output_name: "active.zip", output: null });
    mockFetch.mockImplementation((url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") {
        requests += 1;
        if (requests === 1) return Promise.resolve(json(200, listEnvelope([packing])));
        if (requests === 2) return oldPoll.promise;
        return Promise.resolve(json(200, listEnvelope([pack({ id: 1, output_name: "active.zip" })])));
      }
      if (url === "/api/v2/packs/1/cancel" && init?.method === "POST") {
        return Promise.resolve(json(204, null));
      }
      return Promise.resolve(json(404, {}));
    });
    try {
      renderEntry(onTaskComplete);
      await act(async () => { await Promise.resolve(); });
      await openDrawer();
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(requests).toBe(2);

      fireEvent.click(screen.getByRole("button", { name: "取消" }));
      const confirm = await screen.findByRole("dialog", { name: "取消打包任务" });
      fireEvent.click(within(confirm).getByRole("button", { name: "取消任务" }));
      await waitFor(() => expect(requests).toBe(3));
      expect(await screen.findByText(/已完成/)).toBeInTheDocument();
      expect(onTaskComplete).toHaveBeenCalledTimes(1);

      await act(async () => {
        oldPoll.resolve(json(200, listEnvelope([packing])));
        await Promise.resolve();
      });
      expect(screen.getByText(/已完成/)).toBeInTheDocument();
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(requests).toBe(3);
      expect(onTaskComplete).toHaveBeenCalledTimes(1);
    } finally {
      jest.useRealTimers();
    }
  });

  it.each(["delete", "clear"] as const)("%s 操作刷新与旧轮询重叠时只采用新结果", async (operation) => {
    jest.useFakeTimers();
    const oldPoll = deferred();
    const active = pack({ id: 1, status: "packing", output_name: "active.zip", output: null });
    const finished = pack({ id: 8, output_name: "finished.zip" });
    let requests = 0;
    mockFetch.mockImplementation((url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") {
        requests += 1;
        if (requests === 1) return Promise.resolve(json(200, listEnvelope([active, finished])));
        if (requests === 2) return oldPoll.promise;
        return Promise.resolve(json(200, listEnvelope([active])));
      }
      if (url === "/api/v2/packs/8/record" && init?.method === "DELETE") {
        return Promise.resolve(json(204, null));
      }
      if (url === "/api/v2/packs/records" && init?.method === "DELETE") {
        return Promise.resolve(json(200, { cleared_count: 1 }));
      }
      return Promise.resolve(json(404, {}));
    });
    try {
      renderEntry();
      await act(async () => { await Promise.resolve(); });
      await openDrawer();
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(requests).toBe(2);

      if (operation === "delete") {
        fireEvent.click(screen.getByRole("button", { name: "删除 finished.zip 记录" }));
        const confirm = await screen.findByRole("dialog", { name: "删除任务记录" });
        fireEvent.click(within(confirm).getByRole("button", { name: "删除" }));
      }
      if (operation === "clear") {
        fireEvent.click(screen.getByRole("button", { name: "清空已结束" }));
      }
      await waitFor(() => expect(requests).toBe(3));
      await waitFor(() => expect(screen.queryByText("finished.zip")).not.toBeInTheDocument());

      await act(async () => {
        if (operation === "delete") oldPoll.resolve(json(200, listEnvelope([active, finished])));
        if (operation === "clear") oldPoll.reject(new TypeError("stale failure"));
        await Promise.resolve();
      });
      expect(screen.queryByText("finished.zip")).not.toBeInTheDocument();
      expect(screen.getByText("active.zip")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("卸载后在途 done 响应不触发完成回调", async () => {
    jest.useFakeTimers();
    const inFlight = deferred();
    const onTaskComplete = jest.fn();
    mockFetch
      .mockResolvedValueOnce(json(200, listEnvelope([pack({ id: 1, status: "packing", output: null })])))
      .mockReturnValueOnce(inFlight.promise);
    try {
      const view = renderEntry(onTaskComplete);
      await act(async () => { await Promise.resolve(); });
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(packCalls()).toBe(2);
      view.unmount();
      await act(async () => {
        inFlight.resolve(json(200, listEnvelope([pack({ id: 1 })])));
        await Promise.resolve();
      });
      expect(onTaskComplete).not.toHaveBeenCalled();
    } finally {
      jest.useRealTimers();
    }
  });
});
