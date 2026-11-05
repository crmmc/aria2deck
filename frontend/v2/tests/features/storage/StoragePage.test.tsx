import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { StoragePage } from "@/features/storage/StoragePage";
import type { StorageFileResponse, UserResponse } from "@/shared/api/types";

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

function content(contentId: number, overrides: Partial<StorageFileResponse> = {}): StorageFileResponse {
  return {
    content_id: contentId,
    content_key: `key-${contentId}`,
    kind: "file",
    size: 1024,
    storage_path: `/data/objects/key-${contentId}`,
    on_disk: true,
    reference_count: 0,
    deletion_state: "active",
    ...overrides,
  };
}

function list(items: StorageFileResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 20 };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(
  handler: (url: string, init?: { method?: string; body?: string }) => unknown,
  me: UserResponse = ME,
) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, me);
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
    <MemoryRouter initialEntries={["/storage"]}>
      <AppProviders>
        <StoragePage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function lastUrl(): string {
  return mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
}

describe("StoragePage list", () => {
  it("renders storage rows with disk presence, references and deletion state", async () => {
    setup(() =>
      list([
        content(1),
        content(2, { on_disk: false, kind: "directory", reference_count: 3, deletion_state: "tombstoned" }),
      ]),
    );
    expect(await screen.findByTestId("storage-row-1")).toBeInTheDocument();
    expect(screen.getByText("在磁盘")).toBeInTheDocument();
    expect(screen.getByText("缺失")).toBeInTheDocument();
    expect(screen.getByText("3 个引用")).toBeInTheDocument();
    expect(screen.getByText("0 个引用")).toBeInTheDocument();
    expect(screen.getByText("已墓碑")).toBeInTheDocument();
    expect(screen.getByText("目录")).toBeInTheDocument();
  });

  it("shows the empty state with the orphan-filter hint", async () => {
    setup(() => list([]));
    expect(await screen.findByText("暂无存储文件")).toBeInTheDocument();
    expect(screen.getByText("下载完成的内容会出现在这里。")).toBeInTheDocument();
  });

  it("shows the error state with retry", async () => {
    setup((_url, init) => {
      if (init?.method === "GET") {
        return json(500, { error: { code: "internal", message: "后端炸了" } });
      }
      return list([]);
    });
    expect(await screen.findByText("后端炸了")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("redirects non-admin users and never lists storage", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, { ...ME, is_admin: false });
      return json(200, list([]));
    });
    render(
      <MemoryRouter initialEntries={["/storage"]}>
        <AppProviders>
          <Routes>
            <Route path="/storage" element={<StoragePage />} />
            <Route path="/tasks" element={<div>任务页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("任务页占位")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([url]) => url.includes("/api/v2/storage"))).toBeUndefined();
  });

  it("sends search after debounce and resets to page 1", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("page=2")) return list([content(2)], 21);
        if (url.includes("search=key-9")) return list([content(9)], 1);
        return list([content(1)], 21);
      });
      await screen.findByTestId("storage-row-1");
      fireEvent.click(screen.getByLabelText("下一页"));
      await screen.findByTestId("storage-row-2");
      fireEvent.change(screen.getByLabelText("搜索存储文件"), { target: { value: "key-9" } });
      await jest.advanceTimersByTimeAsync(350);
      await waitFor(() => {
        expect(lastUrl()).toContain("search=key-9");
        expect(lastUrl()).toContain("page=1");
      });
      expect(await screen.findByTestId("storage-row-9")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("toggles orphans-only filter and clears selection", async () => {
    setup((url) => {
      if (url.includes("orphans_only=true")) return list([content(7, { reference_count: 0 })]);
      return list([content(1, { reference_count: 2 }), content(2)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 key-2"));
    expect(screen.getByLabelText("选择 key-2")).toBeChecked();
    fireEvent.click(screen.getByLabelText("仅显示孤儿文件"));
    await waitFor(() => {
      expect(lastUrl()).toContain("orphans_only=true");
    });
    expect(await screen.findByTestId("storage-row-7")).toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-2")).not.toBeInTheDocument();
  });

  it("filter change resets page and issues exactly one list request", async () => {
    setup((url) => {
      if (url.includes("orphans_only=true")) return list([content(7, { reference_count: 0 })]);
      return list([content(1)], 45);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(lastUrl()).toContain("page=2"));
    const listCalls = () =>
      mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/storage")).length;
    const before = listCalls();

    fireEvent.click(screen.getByLabelText("仅显示孤儿文件"));

    await waitFor(() => expect(lastUrl()).toContain("orphans_only=true"));
    await waitFor(() => expect(listCalls()).toBe(before + 1)); // 旧页码请求不再发出。
    expect(lastUrl()).toContain("page=1");
  });

  it("clears selection when paginating", async () => {
    setup((url) => {
      if (url.includes("page=2")) return list([content(2)], 21);
      return list([content(1)], 21);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 key-1"));
    expect(screen.getByLabelText("选择 key-1")).toBeChecked();
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("storage-row-2");
    expect(screen.queryByLabelText("选择 key-1")).not.toBeInTheDocument();
  });

  it("select-all toggles every row and clears again", async () => {
    setup(() => list([content(1), content(2)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("全选存储文件"));
    expect(screen.getByLabelText("选择 key-1")).toBeChecked();
    expect(screen.getByLabelText("选择 key-2")).toBeChecked();
    expect(screen.getByRole("button", { name: /删除选中（2）/ })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("全选存储文件"));
    expect(screen.getByLabelText("选择 key-1")).not.toBeChecked();
    expect(screen.queryByRole("button", { name: /删除选中/ })).not.toBeInTheDocument();
  });

  it("deselects an individually selected row", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 key-1"));
    expect(screen.getByLabelText("选择 key-1")).toBeChecked();
    fireEvent.click(screen.getByLabelText("选择 key-1")); // 再次点击取消选择。
    expect(screen.getByLabelText("选择 key-1")).not.toBeChecked();
  });

  it("shows the orphan-filter empty hint when the filter yields nothing", async () => {
    setup((url) => {
      if (url.includes("orphans_only=true")) return list([]);
      return list([content(1, { reference_count: 3 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("仅显示孤儿文件"));
    expect(await screen.findByText("当前筛选下没有孤儿内容。")).toBeInTheDocument();
  });

  it("discards stale responses after filter changes", async () => {
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
      return json(200, list([content(2)]));
    });
    render(
      <MemoryRouter initialEntries={["/storage"]}>
        <AppProviders>
          <StoragePage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByLabelText("仅显示孤儿文件");
    await waitFor(() => expect(calls).toBe(1)); // 首笔列表请求挂起后再切筛选。
    fireEvent.click(screen.getByLabelText("仅显示孤儿文件"));
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2));
    releaseFirst(json(200, list([content(42)])));
    await screen.findByTestId("storage-row-2");
    expect(screen.queryByTestId("storage-row-42")).not.toBeInTheDocument();
  });
});

describe("StoragePage referencing users", () => {
  it("opens the modal and lists referencing users", async () => {
    setup((url) => {
      if (url.includes("/users")) {
        return {
          items: [
            { user_id: 5, username: "alice", display_name: "alice.bin", referenced_at_ms: 1700000000000 },
          ],
        };
      }
      return list([content(1, { reference_count: 1 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByRole("button", { name: "1 个引用" }));
    await screen.findByRole("dialog", { name: "引用用户：key-1" });
    await waitFor(() => {
      const dialog = screen.getByRole("dialog", { name: "引用用户：key-1" });
      expect(dialog.textContent).toContain("alice");
      expect(dialog.textContent).toContain("alice.bin");
      expect(dialog.textContent).toContain("2023-11-15");
    });
  });

  it("shows the orphan hint for zero references", async () => {
    setup((url) => {
      if (url.includes("/users")) return { items: [] };
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByRole("button", { name: "0 个引用" }));
    expect(
      await screen.findByText("该内容当前是孤儿，可安全删除。"),
    ).toBeInTheDocument();
  });

  it("shows modal error with retry when loading users fails", async () => {
    setup((url) => {
      if (url.includes("/users")) {
        return json(500, { error: { code: "internal", message: "后端炸了" } });
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByRole("button", { name: "0 个引用" }));
    expect(await screen.findByText("后端炸了")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});

describe("StoragePage delete", () => {
  it("disables row delete for referenced content", async () => {
    setup(() => list([content(1, { reference_count: 4 })]));
    await screen.findByTestId("storage-row-1");
    const del = screen.getByTestId("storage-row-1").querySelector(
      ".actions-cell button",
    ) as HTMLButtonElement;
    expect(del.disabled).toBe(true);
  });

  it("accepts orphan-only deletes with 202 and reports partial failures", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return {
          accepted_count: 1,
          rejected_count: 1,
          results: [
            { content_id: 1, accepted: true, error: null },
            {
              content_id: 2,
              accepted: false,
              error: { code: "content_busy", message: "内容被引用" },
            },
          ],
        };
      }
      return list([content(1), content(2, { reference_count: 2 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 key-1"));
    fireEvent.click(screen.getByLabelText("选择 key-2"));
    fireEvent.click(screen.getByRole("button", { name: /删除选中（2）/ }));
    const dialog = await screen.findByRole("dialog", { name: "删除存储文件" });
    expect(dialog.textContent).toContain("仅无引用的孤儿内容会被受理");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(
      await screen.findByText(/已受理 1 个，拒绝 1 个（内容被引用）/),
    ).toBeInTheDocument();
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/storage/files/delete",
      );
      expect(JSON.parse(call![1].body as string)).toEqual({ content_ids: [1, 2] });
    });
  });

  it("reports full success with a pending-cleanup toast", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return {
          accepted_count: 1,
          rejected_count: 0,
          results: [{ content_id: 1, accepted: true, error: null }],
        };
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText(/存储删除已受理 1 个，后台清理进行中/)).toBeInTheDocument();
  });

  it("cancel in the confirm dialog submits nothing", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      mockFetch.mock.calls.find(([, init2]) => init2?.method === "POST"),
    ).toBeUndefined();
  });

  it("falls back a page when the last row of the last page is deleted", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/storage/files/delete") {
        return {
          accepted_count: 1,
          rejected_count: 0,
          results: [{ content_id: 2, accepted: true, error: null }],
        };
      }
      if (url.includes("page=2")) return list([content(2)], 21);
      return list([content(1)], 21);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("storage-row-2");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => expect(lastUrl()).toContain("page=1"));
  });

  it("surfaces whole-request failure via toast", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return json(422, { error: { code: "invalid_input", message: "请求无效" } });
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("请求无效")).toBeInTheDocument();
  });
});
