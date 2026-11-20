import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { PackTaskCard } from "@/features/files/components/PackTaskCard";
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
let originalRaf: typeof window.requestAnimationFrame;

beforeEach(() => {
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
  // 让 requestAnimationFrame 同步执行，展开面板的 visible 状态立刻生效。
  originalRaf = window.requestAnimationFrame;
  window.requestAnimationFrame = ((callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  }) as typeof window.requestAnimationFrame;
});

afterEach(() => {
  window.requestAnimationFrame = originalRaf;
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

function renderCard(onTaskComplete?: () => void) {
  return render(
    <ToastProvider>
      <PackTaskCard onTaskComplete={onTaskComplete} />
    </ToastProvider>,
  );
}

describe("PackTaskCard 状态与展示", () => {
  it("无任务时不渲染按钮", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([])));
    const { container } = renderCard();
    await waitFor(() => expect(mockFetch).toHaveBeenCalledWith("/api/v2/packs?limit=50", expect.anything()));
    expect(container).toBeEmptyDOMElement();
  });

  it("读取失败时只记录 console.error，不渲染", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockFetch.mockRejectedValue(new TypeError("fetch failed"));
    const { container } = renderCard();
    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    errorSpy.mockRestore();
  });

  it("有任务时显示按钮，活跃任务显示数量徽号", async () => {
    mockFetch.mockResolvedValue(
      json(200, listEnvelope([pack({ id: 1, status: "packing" }), pack({ id: 2, status: "pending" }), pack({ id: 3 })])),
    );
    renderCard();
    const button = await screen.findByRole("button", { name: /打包任务/ });
    expect(button).toHaveAttribute("aria-haspopup", "true");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(document.querySelector(".pack-task-badge")?.textContent).toBe("2");
  });

  it("只有终态任务时不显示徽号", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    await screen.findByRole("button", { name: /打包任务/ });
    expect(document.querySelector(".pack-task-badge")).toBeNull();
  });

  it("完成后产物被删除时显示产物已删除", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, output: null })])));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    expect(screen.getByText("产物已删除")).toBeInTheDocument();
  });

  it("展开后按状态渲染各分支（排队/打包/完成/失败/取消）", async () => {
    mockFetch.mockResolvedValue(
      json(
        200,
        listEnvelope([
          pack({ id: 1, status: "pending", step_progress: 0, output_name: "" }),
          pack({ id: 2, status: "packing", step: "compressing", step_progress: 40 }),
          pack({ id: 3, status: "done", delete_source: true, output: { user_file_id: 9, display_name: "a.zip", size: 4096 } }),
          pack({ id: 4, status: "failed", error_code: "disk_full" }),
          pack({ id: 5, status: "cancelled" }),
        ]),
      ),
    );
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());

    // 未提供 output_name 时兜底名称（按钮本身也叫「打包任务」，用 class 精确定位）。
    expect(document.querySelector(".pack-task-name")?.textContent).toBe("打包任务");
    expect(screen.getByText("排队中...")).toBeInTheDocument();
    expect(screen.getByText("打包中")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("已取消")).toBeInTheDocument();

    expect(screen.getByText("排队中")).toBeInTheDocument();
    expect(screen.getByText("disk_full")).toBeInTheDocument();
    expect(screen.getByText("输出: 4.0 KB · 已删除源文件")).toBeInTheDocument();
  });

  it("未知状态回退为原样文本与警告色", async () => {
    mockFetch.mockResolvedValue(
      json(200, listEnvelope([pack({ id: 1, status: "unknown_state" as PackViewResponse["status"] })])),
    );
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    const status = document.querySelector(".pack-task-status") as HTMLElement;
    expect(status.textContent).toBe("unknown_state");
  });
});

describe("PackTaskCard 耗时文本", () => {
  const NOW = 1_700_000_000_000;

  /** 以固定系统时间挂载，step_started_at 回推 elapsedSeconds，让耗时文本可精确断言。 */
  async function openWithTiming(
    overrides: Partial<PackViewResponse>,
    elapsedSeconds: number,
  ) {
    jest.useFakeTimers();
    jest.setSystemTime(NOW);
    mockFetch.mockResolvedValue(
      json(
        200,
        listEnvelope([
          pack({
            id: 1,
            status: "packing",
            step_started_at_ms: NOW - elapsedSeconds * 1000,
            ...overrides,
          }),
        ]),
      ),
    );
    renderCard();
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: /打包任务/ }));
    await act(async () => {
      await Promise.resolve();
    });
  }

  it.each([
    ["秒级", { step: "validating" as const, step_progress: 50 }, 30, "校验 · 已用 30秒 / 预计剩余 30秒"],
    ["分钟级", { step: "compressing" as const, step_progress: 25 }, 90, "压缩 · 已用 1分30秒 / 预计剩余 4分30秒"],
    ["小时级", { step: "verifying" as const, step_progress: 100 }, 5970, "验收 · 已用 1小时39分30秒 / 预计剩余 0秒"],
    ["未知步骤", { step: null, step_progress: 50 }, 30, "处理中 · 已用 30秒 / 预计剩余 30秒"],
  ])("%s 显示已用与预计剩余", async (_label, overrides, elapsed, expected) => {
    try {
      await openWithTiming(overrides, elapsed);
      expect(screen.getByText(expected)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it.each([
    ["未开始", { step_started_at_ms: null, step_progress: 50 }],
    ["进度为 0", { step_started_at_ms: NOW, step_progress: 0 }],
    ["进度超 100", { step_started_at_ms: NOW, step_progress: 120 }],
    ["耗时不足 1 秒", { step_started_at_ms: NOW, step_progress: 50 }],
  ])("耗时不可用时显示占位（%s）", async (_label, overrides) => {
    try {
      await openWithTiming({ step: "compressing", ...overrides }, 0);
      expect(screen.getByText("压缩 · 已用 -- / 预计剩余 --")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("PackTaskCard 交互", () => {
  it("鼠标进入展开、离开后延时收起", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
      renderCard();
      await act(async () => {
        await Promise.resolve();
      });
      const button = screen.getByRole("button", { name: /打包任务/ });

      fireEvent.mouseEnter(button);
      await act(async () => {
        await Promise.resolve();
      });
      expect(document.querySelector(".pack-dropdown")).toBeTruthy();

      fireEvent.mouseLeave(button);
      // 200ms 延时内仍可见。
      act(() => {
        jest.advanceTimersByTime(100);
      });
      expect(document.querySelector(".pack-dropdown")).toBeTruthy();

      await act(async () => {
        jest.advanceTimersByTime(500);
        await Promise.resolve();
      });
      expect(document.querySelector(".pack-dropdown")).toBeNull();
    } finally {
      jest.useRealTimers();
    }
  });

  it("面板内鼠标进入取消待关闭定时器", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
      renderCard();
      await act(async () => {
        await Promise.resolve();
      });
      const button = screen.getByRole("button", { name: /打包任务/ });
      fireEvent.mouseEnter(button);
      await act(async () => {
        await Promise.resolve();
      });
      const panel = document.querySelector(".pack-dropdown") as HTMLElement;

      fireEvent.mouseLeave(button);
      fireEvent.mouseEnter(panel);
      await act(async () => {
        jest.advanceTimersByTime(500);
        await Promise.resolve();
      });
      expect(document.querySelector(".pack-dropdown")).toBeTruthy();
    } finally {
      jest.useRealTimers();
    }
  });

  it("已展开时鼠标再次进入不重复定位", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    const button = await screen.findByRole("button", { name: /打包任务/ });
    fireEvent.mouseEnter(button);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    const panel = document.querySelector(".pack-dropdown") as HTMLElement;
    const styleBefore = panel.getAttribute("style");

    // expanded 为真时 expand 直接返回：定位样式保持不变，面板仍在。
    fireEvent.mouseEnter(button);
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.querySelector(".pack-dropdown")?.getAttribute("style")).toBe(styleBefore);
  });

  it("按钮位置不可测量时回退到左上角定位", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    const button = await screen.findByRole("button", { name: /打包任务/ });
    // 模拟 getBoundingClientRect 返回全零（jsdom 默认行为已被覆盖成可用值，这里显式清掉）。
    const original = button.getBoundingClientRect;
    button.getBoundingClientRect = () => ({ bottom: 0, right: 0 } as DOMRect);
    fireEvent.click(button);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    const panel = document.querySelector(".pack-dropdown") as HTMLElement;
    expect(panel.style.top).toBe("8px");
    button.getBoundingClientRect = original;
  });

  it("再次点击按钮收起", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    const button = await screen.findByRole("button", { name: /打包任务/ });
    fireEvent.click(button);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    expect(button).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(button);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeNull());
  });

  it("Escape 键收起面板", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeNull());
  });

  it("点击面板外部收起，点击面板内部不收起", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());

    const panel = document.querySelector(".pack-dropdown") as HTMLElement;
    fireEvent.mouseDown(panel);
    expect(document.querySelector(".pack-dropdown")).toBeTruthy();

    fireEvent.mouseDown(document.body);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeNull());
  });

  it("窗口滚动（面板外）时收起，面板内滚动不收起", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());

    const panel = document.querySelector(".pack-dropdown") as HTMLElement;
    fireEvent.scroll(panel);
    expect(document.querySelector(".pack-dropdown")).toBeTruthy();

    fireEvent.scroll(document.body);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeNull());
  });

  it("页面内元素触发 resize/scroll 捕获时收起面板", async () => {
    mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    // window 监听以 capture=true 注册，捕获阶段先于目标执行。
    fireEvent.resize(document.body);
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeNull());
  });
});

describe("PackTaskCard 任务操作", () => {
  it("清空已完成：确认后调用清空端点并提示条数", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack()]));
      if (url === "/api/v2/packs/records" && init?.method === "DELETE") {
        return json(200, { cleared_count: 3 });
      }
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "清空已完成" }));
    expect(await screen.findByText("已清空 3 条记录")).toBeInTheDocument();
  });

  it("清空失败展示错误", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack()]));
      if (url === "/api/v2/packs/records" && init?.method === "DELETE") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "清空已完成" }));
    expect(await screen.findByText(/^清空失败: /)).toBeInTheDocument();
  });

  it("取消进行中任务：确认后调用取消端点", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 5, status: "packing", step_progress: 10 })]));
      if (url === "/api/v2/packs/5/cancel" && init?.method === "POST") return json(204, null);
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(within(document.querySelector(".pack-dropdown") as HTMLElement).getByRole("button", { name: "取消" }));

    const dialog = await screen.findByRole("dialog", { name: "取消打包任务" });
    expect(dialog).toHaveTextContent("确定要取消此打包任务吗？");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消任务" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url, init]) => url === "/api/v2/packs/5/cancel" && init?.method === "POST");
      expect(call).toBeDefined();
    });
  });

  it("取消确认被驳回时不发请求", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 5, status: "packing" })]));
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(within(document.querySelector(".pack-dropdown") as HTMLElement).getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog", { name: "取消打包任务" });
    // 取消按钮文案与确认按钮同名，取第一个（取消）。
    fireEvent.click(within(dialog).getAllByRole("button", { name: "取消" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/cancel"))).toBe(false);
  });

  it("取消请求失败展示错误", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 5, status: "packing" })]));
      if (url === "/api/v2/packs/5/cancel" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(within(document.querySelector(".pack-dropdown") as HTMLElement).getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog", { name: "取消打包任务" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消任务" }));
    expect(await screen.findByText(/^取消失败: /)).toBeInTheDocument();
  });

  it("删除终态记录：确认后调用删除端点", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 8 })]));
      if (url === "/api/v2/packs/8/record" && init?.method === "DELETE") return json(204, null);
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(within(document.querySelector(".pack-dropdown") as HTMLElement).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog", { name: "删除任务记录" });
    expect(dialog).toHaveTextContent("确定要删除此任务记录吗？");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url, init]) => url === "/api/v2/packs/8/record" && init?.method === "DELETE");
      expect(call).toBeDefined();
    });
  });

  it.each([
    ["失败任务", pack({ id: 11, status: "failed", error_code: "disk_full" })],
    ["已取消任务", pack({ id: 12, status: "cancelled" })],
  ])("%s的删除按钮走同一条删除链路", async (_label, item) => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([item]));
      if (url === `/api/v2/packs/${item.id}/record` && init?.method === "DELETE") return json(204, null);
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    const row = document.querySelector(".pack-task-item") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init]) => url === `/api/v2/packs/${item.id}/record` && init?.method === "DELETE",
      );
      expect(call).toBeDefined();
    });
  });

  it("删除确认被驳回时不发请求", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 8 })]));
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(within(document.querySelector(".pack-dropdown") as HTMLElement).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/record"))).toBe(false);
  });

  it("删除失败展示错误", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/packs?limit=50") return json(200, listEnvelope([pack({ id: 8 })]));
      if (url === "/api/v2/packs/8/record" && init?.method === "DELETE") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(404, {});
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /打包任务/ }));
    await waitFor(() => expect(document.querySelector(".pack-dropdown")).toBeTruthy());
    fireEvent.click(within(document.querySelector(".pack-dropdown") as HTMLElement).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog", { name: "删除任务记录" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText(/^删除失败: /)).toBeInTheDocument();
  });
});

describe("PackTaskCard 轮询", () => {
  it("存在活跃任务时每 2 秒轮询一次", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, status: "packing" })])));
      renderCard();
      await act(async () => {
        await Promise.resolve();
      });
      const calls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/packs?limit=50").length;
      const baseline = calls();

      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(calls()).toBe(baseline + 1);
    } finally {
      jest.useRealTimers();
    }
  });

  it("页面隐藏时轮询跳过请求", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, status: "pending" })])));
      renderCard();
      await act(async () => {
        await Promise.resolve();
      });
      const calls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/packs?limit=50").length;
      const baseline = calls();

      const hiddenSpy = jest.spyOn(document, "hidden", "get").mockReturnValue(true);
      try {
        await act(async () => {
          jest.advanceTimersByTime(4000);
          await Promise.resolve();
        });
        expect(calls()).toBe(baseline);
      } finally {
        hiddenSpy.mockRestore();
      }
    } finally {
      jest.useRealTimers();
    }
  });

  it("无活跃任务时不轮询", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, listEnvelope([pack()])));
      renderCard();
      await act(async () => {
        await Promise.resolve();
      });
      const calls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/packs?limit=50").length;
      const baseline = calls();
      await act(async () => {
        jest.advanceTimersByTime(6000);
        await Promise.resolve();
      });
      expect(calls()).toBe(baseline);
    } finally {
      jest.useRealTimers();
    }
  });

  it("打包中每秒刷新已用时间", async () => {
    const now = 1_700_000_000_000;
    jest.useFakeTimers();
    jest.setSystemTime(now);
    try {
      mockFetch.mockResolvedValue(
        json(
          200,
          listEnvelope([
            pack({ id: 1, status: "packing", step: "compressing", step_progress: 50, step_started_at_ms: now }),
          ]),
        ),
      );
      renderCard();
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: /打包任务/ }));
      await act(async () => {
        await Promise.resolve();
      });
      // 已用不足 1 秒 → 占位。
      expect(screen.getByText("压缩 · 已用 -- / 预计剩余 --")).toBeInTheDocument();

      // 计时器推进 2s：nowMs 刷新，已用 2 秒、预计剩余 2 秒。
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(screen.getByText("压缩 · 已用 2秒 / 预计剩余 2秒")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("活跃任务转为完成时回调 onTaskComplete", async () => {
    jest.useFakeTimers();
    try {
      const onTaskComplete = jest.fn();
      mockFetch.mockResolvedValueOnce(json(200, listEnvelope([pack({ id: 1, status: "packing" })])));
      renderCard(onTaskComplete);
      await act(async () => {
        await Promise.resolve();
      });
      expect(onTaskComplete).not.toHaveBeenCalled();

      mockFetch.mockResolvedValue(json(200, listEnvelope([pack({ id: 1, status: "done" })])));
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(onTaskComplete).toHaveBeenCalledTimes(1);

      // 已是终态到终态不再重复回调。
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      expect(onTaskComplete).toHaveBeenCalledTimes(1);
    } finally {
      jest.useRealTimers();
    }
  });
});
