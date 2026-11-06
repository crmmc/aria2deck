import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { HistoryPage } from "@/features/history/HistoryPage";
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

function historyItem(id: number, overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id,
    status: "completed",
    status_label: "已完成",
    name: `item-${id}.zip`,
    uri: `https://example.com/${id}`,
    total_bytes: 2048,
    completed_bytes: 2048,
    download_speed: 0,
    upload_speed: 0,
    peer_count: 0,
    connection_count: 0,
    files: [],
    observation_fresh: true,
    reserved_bytes: 0,
    error_code: null,
    retryable: false,
    created_at_ms: 1,
    updated_at_ms: 2,
    finished_at_ms: 1700000000000,
    ...overrides,
  };
}

function list(items: TaskResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 20 };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
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
  return render(
    <MemoryRouter initialEntries={["/history"]}>
      <AppProviders>
        <HistoryPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function lastUrl(): string {
  return mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
}

describe("HistoryPage", () => {
  it("renders items with status badge and finished time", async () => {
    setup(() => list([historyItem(1)]));
    expect(await screen.findByTestId("history-row-1")).toBeInTheDocument();
    expect(screen.getByText("item-1.zip")).toBeInTheDocument();
    expect(screen.getByTestId("history-row-1").textContent).toContain("已完成");
  });

  it("shows empty state when no records", async () => {
    setup(() => list([]));
    expect(await screen.findByText("暂无历史记录")).toBeInTheDocument();
    expect(screen.getByText("完成的任务会出现在这里")).toBeInTheDocument();
  });

  it("debounces search and sends q parameter after 300ms", async () => {
    jest.useFakeTimers();
    try {
      setup(() => list([]));
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.change(screen.getByLabelText("搜索历史"), {
        target: { value: "ubuntu" },
      });
      const callsBefore = mockFetch.mock.calls.length;
      act(() => jest.advanceTimersByTime(100));
      expect(mockFetch.mock.calls.length).toBe(callsBefore); // 防抖窗口内不发请求。
      await act(async () => {
        jest.advanceTimersByTime(250);
      });
      expect(lastUrl()).toContain("q=ubuntu");
      expect(mockFetch.mock.calls.length).toBe(callsBefore + 1); // 防抖命中后只发一笔。
    } finally {
      jest.useRealTimers();
    }
  });

  it("resets to page 1 when status filter changes", async () => {
    setup(() => list([historyItem(1)], 45));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(lastUrl()).toContain("page=2"));
    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "failed" } });
    await waitFor(() => {
      expect(lastUrl()).toContain("page=1");
      expect(lastUrl()).toContain("status=failed");
    });
  });

  it("condition change issues exactly one new list request", async () => {
    setup(() => list([historyItem(1)], 45));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(lastUrl()).toContain("page=2"));
    const listCalls = () =>
      mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/history")).length;
    const before = listCalls();

    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "failed" } });

    await waitFor(() => expect(lastUrl()).toContain("status=failed"));
    await waitFor(() => expect(listCalls()).toBe(before + 1)); // 旧页码请求不再发出。
    expect(lastUrl()).toContain("page=1");
  });

  it("copies uri via clipboard and toasts success", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => list([historyItem(1)]));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://example.com/1"));
    expect(await screen.findByText("已复制链接")).toBeInTheDocument();
  });

  it("shows fallback toast when clipboard copy fails", async () => {
    const writeText = jest.fn().mockRejectedValue(new Error("denied"));
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => list([historyItem(1)]));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    expect(await screen.findByText("复制失败，请手动复制")).toBeInTheDocument();
  });

  it("cancel keeps rows; confirm deletes and reloads", async () => {
    setup(() => list([historyItem(1)]));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(await screen.findByRole("dialog", { name: "删除历史记录" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog", { name: "删除历史记录" });
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/history");
      expect(call).toBeDefined(); // DELETE 调用已发出
    });
    expect(await screen.findByText("已删除")).toBeInTheDocument();
  });

  it("deleting the last row of the last page falls back to previous page", async () => {
    setup((url: string) =>
      url.includes("page=2") ? list([historyItem(21)], 21) : list([historyItem(1)], 21),
    );
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("history-row-21");
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog", { name: "删除历史记录" });
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [], accepted_count: 1, rejected_count: 0 }),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => expect(lastUrl()).toContain("page=1"));
  });

  it("bulk delete reports partial failure count", async () => {
    setup(() => list([historyItem(1), historyItem(2)]));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByLabelText("选择 item-1.zip"));
    fireEvent.click(screen.getByLabelText("选择 item-2.zip"));
    fireEvent.click(screen.getByRole("button", { name: /删除选中（2）/ }));
    await screen.findByRole("dialog", { name: "删除历史记录" });
    mockFetch.mockResolvedValueOnce(
      json(200, { items: [], accepted_count: 1, rejected_count: 1 }),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText(/1 条删除失败/)).toBeInTheDocument();
  });

  it("delete failure surfaces Chinese toast", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") {
        return json(409, { error: { code: "invalid_state", message: "状态不允许删除" } });
      }
      return list([historyItem(1)]);
    });
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByRole("dialog", { name: "删除历史记录" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("状态不允许删除")).toBeInTheDocument();
  });

  it("retry failure surfaces the API message", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/tasks/1/retry") {
        return json(404, { error: { code: "not_found", message: "历史不存在" } });
      }
      return list([historyItem(1, { retryable: true, status: "failed", status_label: "失败" })]);
    });
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("历史不存在")).toBeInTheDocument();
  });

  it("discards stale responses after condition changes", async () => {
    let calls = 0;
    let releaseFirst: (value: unknown) => void = () => undefined;
    const firstCall = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      calls += 1;
      if (calls === 1) return firstCall as never;
      return json(200, list([historyItem(1)]));
    });
    render(
      <MemoryRouter initialEntries={["/history"]}>
        <AppProviders>
          <HistoryPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByLabelText("状态筛选");
    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "failed" } });
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2));
    releaseFirst(json(200, list([historyItem(77)])));
    await screen.findByTestId("history-row-1");
    expect(screen.queryByTestId("history-row-77")).not.toBeInTheDocument();
  });

  it("select-all toggles every row", async () => {
    setup(() => list([historyItem(1), historyItem(2)]));
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByLabelText("全选历史"));
    expect(screen.getByLabelText("选择 item-1.zip")).toBeChecked();
    expect(screen.getByLabelText("选择 item-2.zip")).toBeChecked();
    expect(screen.getByRole("button", { name: /删除选中（2）/ })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("全选历史")); // 再点取消全选
    expect(screen.getByLabelText("选择 item-1.zip")).not.toBeChecked();
  });

  it("retries a retryable history item", async () => {
    setup(() =>
      list([historyItem(1, { retryable: true, status: "failed", status_label: "失败" })]),
    );
    await screen.findByTestId("history-row-1");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/tasks/1/retry");
      expect(call).toBeDefined();
    });
    expect(await screen.findByText("重试任务已创建")).toBeInTheDocument();
  });

  it("shows error state with retry when loading fails", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
      if (url.startsWith("/api/v2/history")) {
        return json(500, { error: { code: "internal", message: "服务器开小差" } });
      }
      return json(200, list([]));
    });
    render(
      <MemoryRouter initialEntries={["/history"]}>
        <AppProviders>
          <HistoryPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("服务器开小差")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(200, list([historyItem(9)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByTestId("history-row-9")).toBeInTheDocument();
  });
});

describe("HistoryPage 大小与失败原因列（H4）", () => {
  it("shows size column and mapped failure reason for failed tasks", async () => {
    const items = [
      historyItem(1),
      historyItem(2, {
        status: "failed",
        status_label: "失败",
        error_code: "upstream_failure",
        total_bytes: 5 * 1024 * 1024,
      }),
      historyItem(3, {
        status: "failed",
        status_label: "失败",
        error_code: "weird_code",
        total_bytes: null,
      }),
    ];
    setup(() => ({ items, total: items.length, page: 1, page_size: 20 }));
    await screen.findByTestId("history-row-1");
    expect(screen.getByTestId("history-row-1").textContent).toContain("2.00 KiB");
    const failed = screen.getByTestId("history-row-2");
    expect(failed.textContent).toContain("5.00 MiB");
    expect(failed.textContent).toContain("上游下载失败");
    // 未知错误码原样展示。
    expect(screen.getByTestId("history-row-3").textContent).toContain("weird_code");
    // 已完成任务无失败原因占位。
    expect(screen.getByTestId("history-row-1").textContent).toContain("—");
  });
});
