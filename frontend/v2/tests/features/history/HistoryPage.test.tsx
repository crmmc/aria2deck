import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { HistoryPage } from "@/features/history/HistoryPage";
import { sortHistoryRecords, type HistoryRecordView } from "@/features/history/model";
import type { TaskResponse, UserResponse } from "@/shared/api/types";
import * as toastContext from "@/shared/components/toast-context";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
  id: 1, username: "admin", is_admin: true, quota_bytes: 1,
  is_initial_password: false, pending_delete: false,
  used_bytes: 0, reserved_bytes: 0, available_bytes: 1,
};

function historyItem(id: number, status: TaskResponse["status"] = "completed", overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id, status, status_label: status === "completed" ? "已完成" : status === "cancelled" ? "已取消" : "已失败",
    name: `item-${id}.zip`, uri: `https://example.com/${id}.zip`,
    total_bytes: 100, completed_bytes: 100, download_speed: 0, upload_speed: 0,
    peer_count: 0, connection_count: 0, files: [], observation_fresh: true,
    reserved_bytes: 0, error_code: null, error_message: "", retryable: false,
    created_at_ms: id, updated_at_ms: id, finished_at_ms: 1700000000000 + id,
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function listEnvelope(items: TaskResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 20 };
}

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") {
      return json(200, { site_title: "Aria2Deck", ws_reconnect_max_delay_seconds: 30, ws_reconnect_jitter_seconds: 1, ws_reconnect_factor: 2 });
    }
    const result = handler(url, init);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { json?: unknown }).json === "function"
    ) {
      return result;
    }
    return json(200, result);
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/history"]}>
      <AppProviders>
        <HistoryPage />
      </AppProviders>
    </MemoryRouter>,
  );
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

describe("HistoryPage（V1 历史页）", () => {
  it("renders items with status badge and finished time", async () => {
    setup(() =>
      listEnvelope([
        historyItem(1, "failed", { error_code: "quota_exceeded", error_message: "存储配额不足" }),
      ]),
    );
    renderPage();
    const card = await screen.findByText("item-1.zip");
    expect(card).toBeInTheDocument();
    const row = document.querySelector(".task-card-inner");
    expect(within(row as HTMLElement).getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("存储配额不足")).toBeInTheDocument();
    expect(screen.queryByText("quota_exceeded")).not.toBeInTheDocument();
    expect(screen.getByText("100.0 B")).toBeInTheDocument();
  });

  it("shows empty state when no records", async () => {
    setup(() => listEnvelope([]));
    renderPage();
    expect(await screen.findByText("暂无历史记录")).toBeInTheDocument();
    expect(screen.getByText("完成的下载任务将显示在这里")).toBeInTheDocument();
  });

  it("debounces search and sends q parameter after 300ms", async () => {
    jest.useFakeTimers();
    try {
      setup(() => listEnvelope([]));
      renderPage();
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.change(screen.getByLabelText("搜索历史"), { target: { value: "ubuntu" } });
      const callsBefore = mockFetch.mock.calls.length;
      const countCallsBefore = mockFetch.mock.calls.filter(([url]) =>
        String(url).startsWith("/api/v2/history?") && String(url).includes("page_size=1"),
      ).length;
      const header = document.querySelector(".page-header");
      const section = document.querySelector("section[aria-label='任务历史列表']");
      act(() => jest.advanceTimersByTime(100));
      expect(mockFetch.mock.calls.length).toBe(callsBefore);
      await act(async () => {
        jest.advanceTimersByTime(300);
        await Promise.resolve();
      });
      const historyCalls = mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/history"));
      expect(historyCalls.some(([url]) => String(url).includes("q=ubuntu"))).toBe(true);
      const countCallsAfter = mockFetch.mock.calls.filter(([url]) =>
        String(url).startsWith("/api/v2/history?") && String(url).includes("page_size=1"),
      ).length;
      expect(countCallsAfter).toBe(countCallsBefore);
      expect(document.querySelector(".page-header")).toBe(header);
      expect(document.querySelector("section[aria-label='任务历史列表']")).toBe(section);
    } finally {
      jest.useRealTimers();
    }
  });

  it("resets to page 1 when status filter changes", async () => {
    setup(() => listEnvelope([historyItem(1)]));
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByRole("tab", { name: /^失败/ }));
    await waitFor(() => {
      const urls = mockFetch.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("status=failed") && u.includes("page=1"))).toBe(true);
    });
  });

  it("C13：expired 记录按失败展示且数据不消失", async () => {
    setup(() =>
      listEnvelope([
        historyItem(9, "expired", { error_code: "expired", error_message: "任务已过期" }),
      ]),
    );
    renderPage();
    await screen.findByText("item-9.zip");
    const row = document.querySelector(".task-card-inner");
    expect(within(row as HTMLElement).getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("任务已过期")).toBeInTheDocument();
  });

  it("chinese fallback：error_message 缺失时给中文兜底而非原始错误码", async () => {
    setup(() => listEnvelope([historyItem(3, "failed", { error_code: "download_error" })]));
    renderPage();
    await screen.findByText("item-3.zip");
    const row = document.querySelector(".task-card-inner") as HTMLElement;
    expect(within(row).getByText("下载失败")).toBeInTheDocument();
    expect(row).not.toHaveTextContent("download_error");
  });

  it("copies uri via clipboard and toasts success", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    setup(() => listEnvelope([historyItem(1)]));
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await screen.findByText("链接已复制");
    expect(writeText).toHaveBeenCalledWith("https://example.com/1.zip");
  });

  it("torrent 伪协议历史记录复制 info hash 而不是 torrent: 前缀", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const infoHash = "0123456789abcdef0123456789abcdef01234567";
    setup(() => listEnvelope([historyItem(1, "completed", { uri: `torrent:${infoHash}` })]));
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await screen.findByText("链接已复制");
    expect(writeText).toHaveBeenCalledWith(infoHash);
  });

  it("single delete has no confirm and toasts success", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return { items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }], accepted_count: 1, rejected_count: 0 };
      }
      return listEnvelope([historyItem(1)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByText("已删除该条历史记录");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("batch delete confirms and reports counts", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }],
          accepted_count: 1, rejected_count: 0,
        };
      }
      return listEnvelope([historyItem(1)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("确定要删除选中的 1 条历史记录吗？");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await screen.findByText("已删除 1 条历史记录");
  });

  it("deleting failure surfaces Chinese toast", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [{ accepted: false, task: null, outcome: null, error: { code: "not_found", message: "记录不存在" }, stop_confirmed: null }],
          accepted_count: 0, rejected_count: 1,
        };
      }
      return listEnvelope([historyItem(1)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByText("删除失败：记录不存在");
  });

  it("select-all toggles every row", async () => {
    setup(() => listEnvelope([historyItem(1), historyItem(2)]));
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择全部历史记录"));
    expect(screen.getByLabelText("选择 item-1.zip")).toBeChecked();
    expect(screen.getByLabelText("选择 item-2.zip")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.getByLabelText("选择 item-1.zip")).not.toBeChecked();
  });

  it("retries a retryable history item", async () => {
    setup((url, init) => {
      if (url === "/api/v2/tasks/1/retry" && init?.method === "POST") {
        return { accepted: true, task: historyItem(1, "queued"), outcome: "created", error: null, stop_confirmed: null };
      }
      return listEnvelope([historyItem(1, "failed", { retryable: true })]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("已重新添加下载任务")).toBeInTheDocument();
  });

  it("C29 回归：业务拒绝显示错误提示，不提示成功", async () => {
    setup((url, init) => {
      if (url === "/api/v2/tasks/1/retry" && init?.method === "POST") {
        return {
          accepted: false, task: null, outcome: null,
          error: { code: "not_retryable", message: "该任务当前不允许重试" }, stop_confirmed: null,
        };
      }
      return listEnvelope([historyItem(1, "failed", { retryable: true })]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("重试失败：该任务当前不允许重试")).toBeInTheDocument();
    expect(screen.queryByText("已重新添加下载任务")).not.toBeInTheDocument();
  });

  it("C10：不可重试任务仅提示，不发请求", async () => {
    setup(() => listEnvelope([historyItem(1, "failed", { retryable: false })]));
    renderPage();
    await screen.findByText("item-1.zip");
    const retryButton = screen.getByRole("button", { name: "重试" });
    expect(retryButton).toBeDisabled();
    const before = mockFetch.mock.calls.length;
    fireEvent.click(retryButton);
    expect(mockFetch.mock.calls.length).toBe(before);
  });

  it("discards stale responses after condition changes", async () => {
    const holder: { resolve?: (value: unknown) => void } = {};
    let calls = 0;
    setup(() => {
      calls += 1;
      if (calls === 1) {
        return new Promise((resolve) => {
          holder.resolve = resolve;
        });
      }
      return listEnvelope([historyItem(77)]);
    });
    renderPage();
    await screen.findByRole("tablist", { name: "历史筛选" });
    fireEvent.click(screen.getByRole("tab", { name: /^失败/ }));
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2));
    holder.resolve?.(json(200, listEnvelope([historyItem(1)])));
    await screen.findByText("item-77.zip");
    expect(screen.queryByText("item-1.zip")).not.toBeInTheDocument();
  });
  it("用四个独立 page_size=1 请求读取真实总数，筛选不刷新数量或重建 section", async () => {
    const totals = { all: 12, completed: 5, cancelled: 4, failed: 3 };
    setup((url) => {
      const parsed = new URL(String(url), "http://localhost");
      const status = parsed.searchParams.get("status") ?? "all";
      if (parsed.searchParams.get("page_size") === "1") {
        return listEnvelope([], totals[status as keyof typeof totals]);
      }
      return status === "failed"
        ? listEnvelope([historyItem(2, "failed")], 3)
        : listEnvelope([historyItem(1)], 12);
    });
    renderPage();
    await screen.findByText("item-1.zip");

    expect(await screen.findByRole("tab", { name: "全部 12" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已完成 5" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已取消 4" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "失败 3" })).toBeInTheDocument();
    const countCalls = mockFetch.mock.calls.filter(([url]) =>
      String(url).startsWith("/api/v2/history?") && String(url).includes("page_size=1"),
    );
    expect(countCalls).toHaveLength(4);
    expect(countCalls.map(([url]) => {
      const parsed = new URL(String(url), "http://localhost");
      return [parsed.searchParams.get("status"), parsed.searchParams.get("page"), parsed.searchParams.get("q")];
    })).toEqual([[null, "1", null], ["completed", "1", null], ["cancelled", "1", null], ["failed", "1", null]]);

    const header = document.querySelector(".page-header");
    const section = document.querySelector("section[aria-label='任务历史列表']");
    const listRoot = document.querySelector(".task-card-container");
    fireEvent.click(screen.getByRole("tab", { name: "失败 3" }));
    await screen.findByText("item-2.zip");
    expect(mockFetch.mock.calls.filter(([url]) =>
      String(url).startsWith("/api/v2/history?") && String(url).includes("page_size=1"),
    )).toHaveLength(4);
    expect(document.querySelector(".page-header")).toBe(header);
    expect(document.querySelector("section[aria-label='任务历史列表']")).toBe(section);
    expect(document.querySelector(".task-card-container")).toBe(listRoot);
  });

  it("四个分段分别读取并展示对应历史结果", async () => {
    const allRecords = [
      historyItem(1, "completed"),
      historyItem(2, "cancelled"),
      historyItem(3, "failed"),
    ];
    setup((url) => {
      const parsed = new URL(String(url), "http://localhost");
      const status = parsed.searchParams.get("status");
      if (parsed.searchParams.get("page_size") === "1") {
        return listEnvelope([], status === null ? 3 : 1);
      }
      const selected = status === null
        ? allRecords
        : allRecords.filter((record) => record.status === status);
      return listEnvelope(selected, selected.length);
    });
    renderPage();
    await screen.findByText("item-1.zip");

    for (const [label, id] of [["已完成", 1], ["已取消", 2], ["失败", 3]] as const) {
      fireEvent.click(screen.getByRole("tab", { name: `${label} 1` }));
      await waitFor(() => {
        expect(screen.getByText(`item-${id}.zip`)).toBeInTheDocument();
        for (const otherId of [1, 2, 3]) {
          if (otherId !== id) {
            expect(screen.queryByText(`item-${otherId}.zip`)).not.toBeInTheDocument();
          }
        }
      });
    }
    fireEvent.click(screen.getByRole("tab", { name: "全部 3" }));
    await waitFor(() => {
      expect(screen.getByText("item-1.zip")).toBeInTheDocument();
      expect(screen.getByText("item-2.zip")).toBeInTheDocument();
      expect(screen.getByText("item-3.zip")).toBeInTheDocument();
    });
  });

  it("任一分类总数失败时保留未知标记，不伪造为 0", async () => {
    setup((url) => {
      const parsed = new URL(String(url), "http://localhost");
      if (parsed.searchParams.get("page_size") === "1" && parsed.searchParams.get("status") === "completed") {
        return json(500, { error: { code: "internal", message: "数量读取失败" } });
      }
      return listEnvelope([historyItem(1)], 8);
    });
    renderPage();
    await screen.findByText("item-1.zip");

    expect(await screen.findByRole("tab", { name: "全部 —" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已完成 —" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已取消 —" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "失败 —" })).toBeInTheDocument();
  });

  it("删除后刷新四个总数并丢弃上一轮过期响应", async () => {
    const pendingCounts: Array<{ resolve: (value: unknown) => void }> = [];
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }],
          accepted_count: 1,
          rejected_count: 0,
        };
      }
      const parsed = new URL(String(url), "http://localhost");
      if (parsed.searchParams.get("page_size") === "1") {
        return new Promise((resolve) => {
          pendingCounts.push({ resolve });
        });
      }
      return listEnvelope([historyItem(1)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    await waitFor(() => expect(pendingCounts).toHaveLength(4));

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByText("已删除该条历史记录");
    await waitFor(() => expect(pendingCounts).toHaveLength(8));
    act(() => {
      [9, 5, 2, 2].forEach((total, index) => {
        pendingCounts[index + 4].resolve(listEnvelope([], total));
      });
    });
    expect(await screen.findByRole("tab", { name: "全部 9" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已完成 5" })).toBeInTheDocument();

    act(() => {
      [90, 50, 20, 20].forEach((total, index) => {
        pendingCounts[index].resolve(listEnvelope([], total));
      });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("tab", { name: "全部 9" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已完成 5" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "全部 90" })).not.toBeInTheDocument();
  });
});

describe("HistoryPage 补齐分支", () => {
  it("加载失败提示错误文案并结束加载态", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/history")) {
        return { error: { code: "internal", message: "服务器错误" } };
      }
      return listEnvelope([]);
    });
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/history")) return json(500, { error: { code: "internal", message: "服务器错误" } });
      return json(200, {});
    });
    renderPage();
    expect(await screen.findByText("加载历史失败: 服务器错误")).toBeInTheDocument();
    expect(screen.queryByText("加载中...")).not.toBeInTheDocument();
  });

  it("搜索关键词不变时不重置页码", async () => {
    jest.useFakeTimers();
    try {
      setup(() => listEnvelope([historyItem(1)]));
      renderPage();
      await act(async () => {
        await Promise.resolve();
      });
      const historyCalls = () =>
        mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/history")).length;
      const before = historyCalls();

      // 只输入空白：trim 后与初始值相同，不应触发新请求。
      fireEvent.change(screen.getByLabelText("搜索历史"), { target: { value: "   " } });
      await act(async () => {
        jest.advanceTimersByTime(300);
        await Promise.resolve();
      });
      expect(historyCalls()).toBe(before);
    } finally {
      jest.useRealTimers();
    }
  });

  it("删除失败且无 error 明细时用「未知错误」兜底", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [{ accepted: false, task: null, outcome: null, error: null, stop_confirmed: null }],
          accepted_count: 0,
          rejected_count: 0,
        };
      }
      return listEnvelope([historyItem(1)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(await screen.findByText("删除失败：未知错误")).toBeInTheDocument();
  });

  it("删除请求抛错时提示并重新加载", async () => {
    let deleteCalls = 0;
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        deleteCalls += 1;
        return { __fail: true };
      }
      return listEnvelope([historyItem(1)]);
    });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        deleteCalls += 1;
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      if (url.startsWith("/api/v2/history")) return json(200, listEnvelope([historyItem(1)]));
      return json(200, {});
    });
    renderPage();
    await screen.findByText("item-1.zip");
    const listCallsBefore = mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/history?")).length;
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(await screen.findByText("删除失败：服务器错误")).toBeInTheDocument();
    expect(deleteCalls).toBe(1);
    await waitFor(() => {
      const listCalls = mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/history?")).length;
      expect(listCalls).toBeGreaterThan(listCallsBefore);
    });
  });

  it("单条删除是当前页最后一条且非第 1 页时回退上一页", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }],
          accepted_count: 1,
          rejected_count: 0,
        };
      }
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      return page === 2 ? listEnvelope([historyItem(1)], 21) : listEnvelope([historyItem(50)], 21);
    });
    renderPage();
    await screen.findByText("item-50.zip");
    // 翻到第 2 页，让该页只有一条记录。
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByText("已删除该条历史记录");
    await waitFor(() => {
      const urls = mockFetch.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/v2/history?") && u.includes("page=1"))).toBe(true);
    });
  });



  it("批量删除确认被驳回时不发请求", async () => {
    setup(() => listEnvelope([historyItem(1)]));
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("批量删除部分失败给出警告并保留汇总", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [
            { accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null },
            { accepted: false, task: null, outcome: null, error: { code: "not_found", message: "已不存在" }, stop_confirmed: null },
          ],
          accepted_count: 1,
          rejected_count: 1,
        };
      }
      return listEnvelope([historyItem(1), historyItem(2)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    fireEvent.click(screen.getByLabelText("选择 item-2.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("已删除 1 条，1 条删除失败")).toBeInTheDocument();
  });

  it("批量删除时响应缺 task 时按请求顺序回填 id", async () => {
    const deleteBodies: number[][] = [];
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        deleteBodies.push(JSON.parse(init.body ?? "{}").task_ids);
        return {
          items: [
            { accepted: true, task: null, outcome: null, error: null, stop_confirmed: null },
            { accepted: true, task: null, outcome: null, error: null, stop_confirmed: null },
          ],
          accepted_count: 2,
          rejected_count: 0,
        };
      }
      return listEnvelope([historyItem(1), historyItem(2)]);
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    fireEvent.click(screen.getByLabelText("选择 item-2.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("已删除 2 条历史记录")).toBeInTheDocument();
    // 列表按 created_at_ms 降序排列，请求顺序即 2、1。
    expect(deleteBodies).toEqual([[2, 1]]);
    // 响应缺 task 时按请求顺序回填 id：两条都算已删除，随后重新拉取列表并清空选中。
    await waitFor(() => {
      expect((screen.getByLabelText("选择 item-1.zip") as HTMLInputElement).checked).toBe(false);
    });
    expect(document.querySelector(".bulk-count")?.textContent).not.toBe("已选 2 条记录");
  });

  it("批量删除请求抛错时提示并重新加载", async () => {
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      if (url.startsWith("/api/v2/history")) return json(200, listEnvelope([historyItem(1)]));
      return json(200, {});
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除失败：服务器错误")).toBeInTheDocument();
    // 失败后重新拉取列表并清空选中，批量层隐藏且不再是删除中。
    await waitFor(() => {
      expect(toolbar).not.toHaveClass("is-bulk");
      expect(toolbar.querySelector(".toolbar-bulk")).toHaveAttribute("hidden");
    });
  });

  it("当前页被删空且非第 1 页时回退上一页（批量）", async () => {
    setup((url, init) => {
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return {
          items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }],
          accepted_count: 1,
          rejected_count: 0,
        };
      }
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      return page === 2 ? listEnvelope([historyItem(1)], 21) : listEnvelope([historyItem(50)], 21);
    });
    renderPage();
    await screen.findByText("item-50.zip");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await screen.findByText("已删除 1 条历史记录");
    await waitFor(() => {
      const urls = mockFetch.mock.calls.map(([url]) => String(url));
      expect(urls.filter((u) => u.includes("/api/v2/history?") && u.includes("page=1")).length).toBeGreaterThan(1);
    });
  });

  it("批量删除进行中：删除按钮带 opacity-60 且禁用", async () => {
    const holder: { resolve?: (value: unknown) => void } = {};
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/history" && init?.method === "DELETE") {
        return new Promise((resolve) => {
          holder.resolve = resolve;
        });
      }
      return json(200, listEnvelope([historyItem(1)]));
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      const button = within(toolbar).getByRole("button", { name: "删除" });
      expect(button).toBeDisabled();
      expect(button.className).toContain("opacity-60");
    });

    holder.resolve?.(
      json(200, {
        items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }],
        accepted_count: 1,
        rejected_count: 0,
      }),
    );
    await screen.findByText("已删除 1 条历史记录");
  });

  it("错误信息缺失时提示文案回退为空串", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/history")) {
        return {
          status: 200,
          ok: true,
          // 响应体解析失败且抛出非 Error 值：页面取不到 message，回退空串。
          json: () => Promise.reject("raw failure"),
        };
      }
      return json(200, {});
    });
    renderPage();
    expect(await screen.findByText("加载历史失败:")).toBeInTheDocument();
  });

  it("过期请求的失败结果被丢弃", async () => {
    let calls = 0;
    const holder: { reject?: (reason: unknown) => void } = {};
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/history")) {
        calls += 1;
        if (calls === 1) {
          return new Promise((_resolve, reject) => {
            holder.reject = reject;
          });
        }
        return json(200, listEnvelope([historyItem(77)]));
      }
      return json(200, {});
    });
    renderPage();
    await screen.findByRole("tablist", { name: "历史筛选" });
    fireEvent.click(screen.getByRole("tab", { name: /^失败/ }));
    await screen.findByText("item-77.zip");
    // 第一笔（已过期）请求随后失败：不得再覆盖界面或额外提示。
    holder.reject?.(new TypeError("late failure"));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("item-77.zip")).toBeInTheDocument();
    expect(screen.queryByText("加载历史失败:")).not.toBeInTheDocument();
  });

  it("请求尚未返回时卸载：不再写入状态", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    try {
      const holder: { resolve?: (value: unknown) => void } = {};
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
        if (url.startsWith("/api/v2/history")) {
          return new Promise((resolve) => {
            holder.resolve = resolve;
          });
        }
        return json(200, {});
      });
      const { unmount } = renderPage();
      await screen.findByRole("tablist", { name: "历史筛选" });
      unmount();
      holder.resolve?.(json(200, listEnvelope([historyItem(1)])));
      // 卸载后整棵树（含 Toast portal）已移除，DOM 负断言恒真；真实目标是迟到响应不得再产生提示副作用。
      // 用宏任务 flush 保证迟到响应的 Promise 链完整执行后再断言。
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(showToast).not.toHaveBeenCalled();
    } finally {
      useToastSpy.mockRestore();
    }
  });

  it("单条删除请求在卸载后返回：不写入选中的清空结果", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    try {
      const holder: { resolve?: (value: unknown) => void } = {};
      setup((url, init) => {
        if (url === "/api/v2/history" && init?.method === "DELETE") {
          return new Promise((resolve) => {
            holder.resolve = resolve;
          });
        }
        return listEnvelope([historyItem(1)]);
      });
      const { unmount } = renderPage();
      await screen.findByText("item-1.zip");
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
      unmount();
      // setup() 会把 handler 返回值包成 Response，挂起请求应 resolve 响应体本身（否则走不到成功路径）。
      holder.resolve?.({
        items: [{ accepted: true, task: historyItem(1), outcome: null, error: null, stop_confirmed: null }],
        accepted_count: 1,
        rejected_count: 0,
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(showToast).not.toHaveBeenCalled();
    } finally {
      useToastSpy.mockRestore();
    }
  });

  it("批量删除请求在卸载后返回：不再写入列表状态", async () => {
    const showToast = jest.fn();
    // 批量删除的确认框依赖真实 showConfirm，spy 只替换 showToast，保留其余上下文能力。
    const realUseToast = toastContext.useToast;
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockImplementation(() => ({
      ...realUseToast(),
      showToast,
    }));
    try {
      const holder: { resolve?: (value: unknown) => void } = {};
      setup((url, init) => {
        if (url === "/api/v2/history" && init?.method === "DELETE") {
          return new Promise((resolve) => {
            holder.resolve = resolve;
          });
        }
        return listEnvelope([historyItem(1)]);
      });
      const { unmount } = renderPage();
      await screen.findByText("item-1.zip");
      fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
      const toolbar = document.querySelector(".filter-toolbar") as HTMLElement;
      fireEvent.click(within(toolbar).getByRole("button", { name: "删除" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
      // 确认框 promise 在微任务里续跑：先等 DELETE 真正发出再卸载，否则场景没进入“请求在途”就结束了。
      await waitFor(() => {
        expect(mockFetch.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true);
      });
      unmount();
      // setup() 会把 handler 返回值包成 Response，挂起请求应 resolve 响应体本身。
      holder.resolve?.({
        items: [{ accepted: true, task: null, outcome: null, error: null, stop_confirmed: null }],
        accepted_count: 1,
        rejected_count: 0,
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(showToast).not.toHaveBeenCalled();
    } finally {
      useToastSpy.mockRestore();
    }
  });

  it("重试请求抛错时提示失败原因", async () => {
    setup((url, init) => {
      if (url === "/api/v2/tasks/1/retry" && init?.method === "POST") {
        return { __status: 500 };
      }
      return listEnvelope([historyItem(1, "failed", { retryable: true })]);
    });
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url === "/api/v2/tasks/1/retry" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return json(200, listEnvelope([historyItem(1, "failed", { retryable: true })]));
    });
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("重试失败：服务器错误")).toBeInTheDocument();
  });

  it("复制链接失败时提示复制失败", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: jest.fn().mockRejectedValue(new Error("denied")) },
      configurable: true,
    });
    try {
      setup(() => listEnvelope([historyItem(1)]));
      renderPage();
      await screen.findByText("item-1.zip");
      fireEvent.click(screen.getByRole("button", { name: "复制" }));
      expect(await screen.findByText("复制失败")).toBeInTheDocument();
    } finally {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  it("重试不可用时不会发出请求（按钮 disabled 兜底）", async () => {
    setup(() => listEnvelope([historyItem(1, "failed", { retryable: false })]));
    renderPage();
    await screen.findByText("item-1.zip");
    const before = mockFetch.mock.calls.length;
    // disabled 按钮在浏览器里不派发 click，这里显式派发以验证处理器自身的守卫。
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mockFetch.mock.calls.length).toBe(before);
    expect(screen.queryByText("不可重试")).not.toBeInTheDocument();
  });

  it("分页切换页大小重新拉取", async () => {
    setup(() => listEnvelope([historyItem(1)], 100));
    renderPage();
    await screen.findByText("item-1.zip");
    fireEvent.change(screen.getByLabelText("每页条数"), { target: { value: "50" } });
    await waitFor(() => {
      const urls = mockFetch.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/v2/history?") && u.includes("page_size=50") && u.includes("page=1"))).toBe(true);
    });
  });
});

describe("sortHistoryRecords（V1 排序口径）", () => {
  const record = (id: number, createdAtMs: number): HistoryRecordView => ({
    id,
    taskName: `item-${id}`,
    totalLength: 0,
    finishedAtMs: createdAtMs,
    createdAtMs,
    result: "completed",
    reason: null,
    retryable: false,
    uri: null,
  });

  it("按 created_at_ms 降序、id 降序排列", () => {
    const input = [record(1, 100), record(2, 300), record(3, 300), record(4, 200)];
    expect(sortHistoryRecords(input).map((r) => r.id)).toEqual([3, 2, 4, 1]);
    expect(input.map((r) => r.id)).toEqual([1, 2, 3, 4]);
  });
});

describe("HistoryCard 交互分支", () => {
  it("uri 为空时不渲染可复制区域", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    setup(() => listEnvelope([historyItem(1, "completed", { uri: null })]));
    renderPage();
    // 卡片正常渲染，但没有可点击复制的包裹层（无 uri 无法复制）。
    expect(await screen.findByText("item-1.zip")).toBeInTheDocument();
    expect(document.querySelector('[aria-label^="复制历史任务链接"]')).toBeNull();
    expect(writeText).not.toHaveBeenCalled();
  });

  it("按 Enter 与空格复制链接，其他键不触发", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    setup(() => listEnvelope([historyItem(2, "completed", { uri: "https://example.com/a.zip" })]));
    renderPage();
    await screen.findByText("item-2.zip");
    const card = document.querySelector('[aria-label^="复制历史任务链接"]') as HTMLElement;
    fireEvent.keyDown(card, { key: "a" });
    expect(writeText).not.toHaveBeenCalled();
    fireEvent.keyDown(card, { key: "Enter" });
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://example.com/a.zip"));
    writeText.mockClear();
    fireEvent.keyDown(card, { key: " " });
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://example.com/a.zip"));
  });
});
