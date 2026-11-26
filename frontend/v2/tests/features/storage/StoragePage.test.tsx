import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { StoragePage } from "@/features/storage/StoragePage";
import {
  digestOf,
  firstRejectedMessage,
  shortHash,
  toStorageFileView,
} from "@/features/storage/model";
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

type Handler = (url: string, init?: { method?: string; body?: string }) => unknown;

function content(contentId: number, overrides: Partial<StorageFileResponse> = {}): StorageFileResponse {
  return {
    content_id: contentId,
    content_key: `file:abcd1234efgh5678-${contentId}`,
    kind: "file",
    size: 1024,
    on_disk: true,
    reference_count: 0,
    deletion_state: "active",
    created_at_ms: 1700000000000 + contentId,
    file_names: [`user-file-${contentId}.bin`],
    ...overrides,
  };
}

function list(items: StorageFileResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 20 };
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
    const result = await handler(url, init);
    return isResponse(result) ? result : json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/storage"]}>
      <AppProviders>
        <StoragePage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function listCalls(): string[] {
  return mockFetch.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.startsWith("/api/v2/storage/files?"));
}

function deleteCalls(): Array<[string, { method?: string; body?: string }]> {
  return mockFetch.mock.calls.filter(
    ([url, init]) => init?.method === "POST" && url === "/api/v2/storage/files/delete",
  ) as Array<[string, { method?: string; body?: string }]>;
}

function accepted(contentId: number) {
  return { content_id: contentId, accepted: true, error: null };
}

function rejected(contentId: number, message = "内容被引用") {
  return {
    content_id: contentId,
    accepted: false,
    error: { code: "content_busy", message },
  };
}

function referenceButton(contentId: number, count: number): HTMLElement {
  return within(screen.getByTestId(`storage-row-${contentId}`)).getByRole("button", {
    name: `查看 user-file-${contentId}.bin 的 ${count} 条引用`,
  });
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

describe("StoragePage Demo 列表与筛选", () => {
  it("使用单面板工具栏和七列表格展示真实状态、名称、哈希与日期", async () => {
    const createdAt = new Date(2026, 10, 23, 9, 5, 59).getTime();
    setup(() => list([
      content(1, {
        content_key: "file:0123456789abcdefXYZ",
        file_names: ["a.bin", "b.bin"],
        reference_count: 2,
        created_at_ms: createdAt,
      }),
      content(2, { on_disk: false, file_names: [] }),
    ]));

    const row = await screen.findByTestId("storage-row-1");
    expect(document.querySelector("h1.page-title")).toHaveTextContent("存储管理");
    expect(screen.getByText("查看内容引用、孤立文件与磁盘状态")).toBeInTheDocument();
    expect(document.querySelector(".glass-frame")).not.toHaveClass("full-height", "animate-in");
    expect(document.querySelector(".admin-table-panel > .admin-storage-toolbar")).not.toBeNull();
    expect(document.querySelector(".admin-table-panel")).toHaveClass("panel");
    const searchInput = screen.getByRole("searchbox", { name: "搜索内容哈希或存储路径" });
    expect(screen.getByText("搜索内容哈希或存储路径", { selector: "label" }))
      .toHaveAttribute("for", "storage-search");
    expect(searchInput).toHaveAttribute("placeholder", "搜索内容哈希或存储路径");
    expect(screen.queryByLabelText("按文件名搜索存储内容")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("搜索文件名")).not.toBeInTheDocument();
    expect(Array.from(document.querySelectorAll(".admin-storage-table thead th")).map((th) => th.textContent))
      .toEqual(["", "文件名", "大小", "内容哈希", "引用数", "创建时间", "磁盘状态"]);
    expect(row.querySelectorAll("button")).toHaveLength(1);
    expect(row).toHaveTextContent("a.bin、b.bin");
    expect(row).toHaveTextContent("1.0 KB");
    expect(row.querySelector("span[title='0123456789abcdefXYZ']")).toHaveTextContent("0123456789…defXYZ");
    expect(row).toHaveTextContent("2026/11/23 09:05");
    expect(row).not.toHaveTextContent("📁");
    expect(within(row).getByRole("button", { name: "查看 a.bin、b.bin 的 2 条引用" }))
      .toHaveTextContent("2 条引用");
    expect(screen.getByTestId("storage-row-2").querySelector("td[title='—']")).toHaveTextContent("—");
    expect(document.querySelector(".status-success")).toHaveTextContent("存在");
    expect(document.querySelector(".status-danger")).toHaveTextContent("缺失");
    expect(screen.getByText("共 2 条")).toBeInTheDocument();
    expect(screen.getByText(/批量选择只作用于当前页当前筛选结果/)).toBeInTheDocument();
  });

  it("初始加载期间不渲染列表，完成后恢复", async () => {
    let releaseList: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseList = resolve; });
    setup(() => pending);
    await waitFor(() => expect(listCalls()).toHaveLength(1));
    expect(screen.queryByRole("heading", { name: "存储管理" })).not.toBeInTheDocument();
    await act(async () => {
      releaseList(json(200, list([content(1)])));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(await screen.findByTestId("storage-row-1")).toBeInTheDocument();
  });

  it("空列表显示共享空态且禁用全选", async () => {
    setup(() => list([]));
    expect(await screen.findByText("没有匹配的存储内容")).toBeInTheDocument();
    expect(screen.getByText("调整内容哈希或存储路径搜索或孤立文件筛选后再试。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重置筛选" })).toBeInTheDocument();
    expect(screen.getByLabelText("选择当前页筛选结果")).toBeDisabled();
    expect(screen.getByText("共 0 条")).toBeInTheDocument();
  });

  it("列表读取失败显示真实错误反馈且不伪造数据", async () => {
    setup(() => json(500, { error: { code: "internal", message: "后端炸了" } }));
    expect(await screen.findByText("加载存储文件失败")).toBeInTheDocument();
    expect(await screen.findByText("没有匹配的存储内容")).toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-1")).not.toBeInTheDocument();
  });

  it("非管理员跳转且在权限确认前不请求管理员数据", async () => {
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
    expect(listCalls()).toHaveLength(0);
  });

  it("搜索停止 300ms 后只发一笔第 1 页新条件请求并清空旧选择", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("page=2")) return list([content(2)], 21);
        if (url.includes("search=key-9")) return list([content(9)], 1);
        return list([content(1)], 21);
      });
      await screen.findByTestId("storage-row-1");
      fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
      fireEvent.click(screen.getByLabelText("下一页"));
      await screen.findByTestId("storage-row-2");
      const before = listCalls().length;

      fireEvent.change(screen.getByLabelText("搜索内容哈希或存储路径"), { target: { value: "key-9" } });
      expect(screen.getByLabelText("选择文件 user-file-2.bin")).not.toBeChecked();
      await act(async () => {
        jest.advanceTimersByTime(299);
        await Promise.resolve();
      });
      expect(listCalls()).toHaveLength(before);
      await act(async () => {
        jest.advanceTimersByTime(1);
        await Promise.resolve();
      });
      await waitFor(() => expect(listCalls()).toHaveLength(before + 1));
      expect(listCalls().at(-1)).toContain("page=1");
      expect(listCalls().at(-1)).toContain("search=key-9");
      expect(await screen.findByTestId("storage-row-9")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("trim 后关键词不变时不重置页码、不额外请求", async () => {
    jest.useFakeTimers();
    try {
      setup(() => list([content(1)], 45));
      await screen.findByTestId("storage-row-1");
      fireEvent.click(screen.getByLabelText("下一页"));
      await waitFor(() => expect(listCalls().at(-1)).toContain("page=2"));
      const before = listCalls().length;
      fireEvent.change(screen.getByLabelText("搜索内容哈希或存储路径"), { target: { value: "   " } });
      await act(async () => {
        jest.advanceTimersByTime(400);
        await Promise.resolve();
      });
      expect(listCalls()).toHaveLength(before);
      expect(listCalls().at(-1)).toContain("page=2");
    } finally {
      jest.useRealTimers();
    }
  });

  it("孤立筛选同步吸收待防抖关键词且只发一笔请求", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("orphans_only=true")) return list([content(7)]);
        return list([content(1)], 21);
      });
      await screen.findByTestId("storage-row-1");
      fireEvent.click(screen.getByLabelText("下一页"));
      await screen.findByTestId("storage-row-1");
      const before = listCalls().length;
      fireEvent.change(screen.getByLabelText("搜索内容哈希或存储路径"), { target: { value: "abcd1234" } });
      fireEvent.click(screen.getByRole("button", { name: "仅显示孤立文件" }));
      await waitFor(() => expect(listCalls()).toHaveLength(before + 1));
      expect(listCalls().at(-1)).toContain("page=1");
      expect(listCalls().at(-1)).toContain("search=abcd1234");
      expect(listCalls().at(-1)).toContain("orphans_only=true");
      await act(async () => {
        jest.advanceTimersByTime(400);
        await Promise.resolve();
      });
      expect(listCalls()).toHaveLength(before + 1);
      expect(screen.getByRole("button", { name: "仅显示孤立文件" })).toHaveAttribute("aria-pressed", "true");
      expect(await screen.findByTestId("storage-row-7")).toBeInTheDocument();
      expect(screen.getByText("筛选出 1 条")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("空结果重置筛选同步清理全部条件且只发一笔必要请求", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => url.includes("orphans_only=true") ? list([]) : list([content(1)]));
      await screen.findByTestId("storage-row-1");
      fireEvent.click(screen.getByRole("button", { name: "仅显示孤立文件" }));
      expect(await screen.findByText("没有匹配的存储内容")).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("搜索内容哈希或存储路径"), { target: { value: "key-pending" } });
      const before = listCalls().length;
      fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
      await waitFor(() => expect(listCalls()).toHaveLength(before + 1));
      expect(listCalls().at(-1)).not.toContain("search=");
      expect(listCalls().at(-1)).not.toContain("orphans_only=true");
      expect(screen.getByLabelText("搜索内容哈希或存储路径")).toHaveValue("");
      expect(screen.getByRole("button", { name: "仅显示孤立文件" })).toHaveAttribute("aria-pressed", "false");
      await act(async () => {
        jest.advanceTimersByTime(400);
        await Promise.resolve();
      });
      expect(listCalls()).toHaveLength(before + 1);
      expect(await screen.findByTestId("storage-row-1")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("清空搜索会清空选择并保留输入焦点", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    const input = screen.getByLabelText("搜索内容哈希或存储路径");
    fireEvent.change(input, { target: { value: "abcd1234" } });
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    const before = listCalls().length;
    fireEvent.click(screen.getByLabelText("清空内容哈希或存储路径搜索"));
    expect(listCalls()).toHaveLength(before);
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
    expect(screen.getByLabelText("选择文件 user-file-1.bin")).not.toBeChecked();
  });

  it("刷新重新读取并清空当前页选择", async () => {
    let loads = 0;
    setup((url) => {
      if (!url.startsWith("/api/v2/storage/files")) return {};
      loads += 1;
      return list([content(loads === 1 ? 1 : 2)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    const before = listCalls().length;
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(screen.getByRole("button", { name: "删除选中(0)" })).toBeDisabled();
    await waitFor(() => expect(listCalls()).toHaveLength(before + 1));
    expect(await screen.findByTestId("storage-row-2")).toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-1")).not.toBeInTheDocument();
  });

  it("丢弃筛选变化前的过期列表响应", async () => {
    let releaseStale: (value: unknown) => void = () => undefined;
    const stale = new Promise((resolve) => { releaseStale = resolve; });
    let loads = 0;
    setup((url) => {
      if (!url.startsWith("/api/v2/storage/files")) return {};
      loads += 1;
      if (loads === 2) return stale;
      return list([content(loads >= 3 ? 2 : 1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByRole("button", { name: "仅显示孤立文件" }));
    await waitFor(() => expect(listCalls()).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "仅显示孤立文件" }));
    await screen.findByTestId("storage-row-2");
    await act(async () => {
      releaseStale(json(200, list([content(42)])));
      await Promise.resolve();
    });
    expect(screen.queryByTestId("storage-row-42")).not.toBeInTheDocument();
    expect(screen.getByTestId("storage-row-2")).toBeInTheDocument();
  });

  it("当前页超出末页时回退并重新读取", async () => {
    let overflowed = false;
    setup((url) => {
      if (url.includes("page=2")) {
        overflowed = true;
        return { items: [content(2)], total: 1, page: 2, page_size: 20 };
      }
      return list([content(overflowed ? 3 : 1)], overflowed ? 1 : 21);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => expect(listCalls().at(-1)).toContain("page=1"));
    expect(await screen.findByTestId("storage-row-3")).toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-2")).not.toBeInTheDocument();
  });
});

describe("StoragePage 当前页选择", () => {
  it("支持当前页半选、全选、取消全选和选中行样式", async () => {
    setup(() => list([content(1), content(2)]));
    await screen.findByTestId("storage-row-1");
    const all = screen.getByLabelText("选择当前页筛选结果") as HTMLInputElement;
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    expect(all.indeterminate).toBe(true);
    expect(all).not.toBeChecked();
    expect(screen.getByTestId("storage-row-1")).toHaveClass("selected");
    expect(screen.getByRole("button", { name: "删除选中(1)" })).toBeEnabled();
    expect(screen.getByText(/已选当前页 1 项/)).toBeInTheDocument();

    fireEvent.click(all);
    expect(all.indeterminate).toBe(false);
    expect(all).toBeChecked();
    expect(screen.getByLabelText("选择文件 user-file-2.bin")).toBeChecked();
    expect(screen.getByRole("button", { name: "删除选中(2)" })).toBeEnabled();

    fireEvent.click(all);
    expect(screen.getByLabelText("选择文件 user-file-1.bin")).not.toBeChecked();
    expect(screen.getByLabelText("选择文件 user-file-2.bin")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "删除选中(0)" })).toBeDisabled();
  });

  it("单独反选一行并更新半选状态", async () => {
    setup(() => list([content(1), content(2)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择当前页筛选结果"));
    const checkbox = screen.getByLabelText("选择文件 user-file-1.bin");
    fireEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();
    expect((screen.getByLabelText("选择当前页筛选结果") as HTMLInputElement).indeterminate).toBe(true);
  });

  it("翻页清空选择且不会把上一页带入批量删除", async () => {
    setup((url) => url.includes("page=2") ? list([content(2)], 21) : list([content(1)], 21));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("storage-row-2");
    expect(screen.getByRole("button", { name: "删除选中(0)" })).toBeDisabled();
    expect(screen.getByText(/已选当前页 0 项/)).toBeInTheDocument();
  });
});

describe("StoragePage 引用用户弹窗", () => {
  it("逐条展示真实引用，初始聚焦底部关闭并保留焦点恢复", async () => {
    setup((url) => {
      if (url.includes("/users")) {
        return { items: [
          { user_id: 5, username: "alice", display_name: "a.bin", referenced_at_ms: 1 },
          { user_id: 5, username: "alice", display_name: "b.bin", referenced_at_ms: 2 },
        ] };
      }
      return list([content(1, { reference_count: 2 })]);
    });
    await screen.findByTestId("storage-row-1");
    const trigger = referenceButton(1, 2);
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "引用用户" });
    const closeButton = within(dialog).getByRole("button", { name: "关闭" });
    expect(closeButton).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: "关闭对话框" })).not.toHaveFocus();
    await within(dialog).findByText("用户 ID 5 · a.bin");
    expect(dialog.querySelectorAll(".admin-reference-row")).toHaveLength(2);
    expect(dialog).toHaveTextContent("用户 ID 5 · b.bin");
    expect(dialog).toHaveTextContent("user-file-1.bin 被以下用户引用");

    fireEvent.click(closeButton);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("显示引用加载中和真实空结果", async () => {
    let releaseUsers: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseUsers = resolve; });
    setup((url) => url.includes("/users") ? pending : list([
      content(1, { file_names: [], reference_count: 1 }),
    ]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", {
      name: "查看 — 的 1 条引用",
    }));
    const dialog = await screen.findByRole("dialog", { name: "引用用户" });
    expect(dialog).toHaveTextContent("未命名内容 被以下用户引用");
    expect(within(dialog).getByRole("status")).toHaveTextContent("加载中...");
    await act(async () => {
      releaseUsers(json(200, { items: [] }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(await within(dialog).findByText("无引用用户")).toBeInTheDocument();
  });

  it("引用读取失败在弹窗内显示错误且不显示空结果", async () => {
    setup((url) => {
      if (url.includes("/users")) return json(500, { error: { code: "internal", message: "后端炸了" } });
      return list([content(1, { reference_count: 1 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(referenceButton(1, 1));
    const dialog = await screen.findByRole("dialog", { name: "引用用户" });
    expect(await within(dialog).findByText("后端炸了", { selector: "p" })).toBeInTheDocument();
    expect(within(dialog).queryByText("无引用用户")).not.toBeInTheDocument();
  });

  it("A 弹窗的晚响应不会覆盖随后打开的 B", async () => {
    let releaseA: (value: unknown) => void = () => undefined;
    const pendingA = new Promise((resolve) => { releaseA = resolve; });
    setup((url) => {
      if (url.includes("/1/users")) return pendingA;
      if (url.includes("/2/users")) {
        return { items: [{ user_id: 2, username: "bob", display_name: "b.bin", referenced_at_ms: 2 }] };
      }
      return list([content(1, { reference_count: 1 }), content(2, { reference_count: 1 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(referenceButton(1, 1));
    await screen.findByRole("dialog", { name: "引用用户" });
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(referenceButton(2, 1));
    const dialogB = await screen.findByRole("dialog", { name: "引用用户" });
    expect(await within(dialogB).findByText("bob")).toBeInTheDocument();

    await act(async () => {
      releaseA(json(200, { items: [{ user_id: 1, username: "alice", display_name: "a.bin", referenced_at_ms: 1 }] }));
      await Promise.resolve();
    });
    expect(dialogB).toHaveTextContent("bob");
    expect(dialogB).not.toHaveTextContent("alice");
  });

  it("零引用按钮禁用，背景和原生 cancel 可关闭弹窗", async () => {
    setup((url) => {
      if (url.includes("/users")) {
        return { items: [{ user_id: 5, username: "alice", display_name: "a.bin", referenced_at_ms: 1 }] };
      }
      return list([content(1, { reference_count: 1 }), content(2)]);
    });
    await screen.findByTestId("storage-row-1");
    expect(referenceButton(2, 0)).toBeDisabled();
    fireEvent.click(referenceButton(1, 1));
    await screen.findByRole("dialog", { name: "引用用户" });
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(referenceButton(1, 1));
    const dialog = await screen.findByRole("dialog", { name: "引用用户" });
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("StoragePage 删除确认与 202 受理", () => {
  it("确认前零删除请求，取消保留选择", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const dialog = await screen.findByRole("dialog", { name: "删除存储文件" });
    expect(dialog).toHaveTextContent("确定删除当前筛选结果中选中的 1 个文件吗？");
    expect(within(dialog).getByRole("button", { name: "返回" })).toHaveFocus();
    expect(deleteCalls()).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭对话框" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const reopened = await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(within(reopened).getByRole("button", { name: "返回" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(deleteCalls()).toHaveLength(0);
    expect(screen.getByLabelText("选择文件 user-file-1.bin")).toBeChecked();
  });

  it("部分受理提示真实数量与拒绝原因，关闭弹窗并刷新", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return json(202, {
          accepted_count: 1,
          rejected_count: 1,
          results: [accepted(1), rejected(2)],
        });
      }
      return list([content(1), content(2)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择当前页筛选结果"));
    const before = listCalls().length;
    fireEvent.click(screen.getByRole("button", { name: "删除选中(2)" }));
    const dialog = await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除文件" }));

    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
    // 失败项改为独立明细弹窗逐条列出：对象名 + 原因
    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(within(failures).getByText("user-file-2.bin")).toBeInTheDocument();
    expect(within(failures).getByText("内容被引用")).toBeInTheDocument();
    fireEvent.click(within(failures).getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(listCalls()).toHaveLength(before + 1));
    expect(JSON.parse(deleteCalls()[0][1].body as string)).toEqual({ content_ids: [1, 2] });
    expect(screen.getByRole("button", { name: "删除选中(0)" })).toBeDisabled();
  });

  it("全部受理只宣称受理，不宣称物理删除完成", async () => {
    setup((_url, init) => {
      if (init?.method === "POST") {
        return json(202, { accepted_count: 1, rejected_count: 0, results: [accepted(1)] });
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除文件" }));
    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
    expect(screen.queryByText(/删除完成/)).not.toBeInTheDocument();
  });

  it("全部拒绝且没有错误详情时使用固定失败反馈并保留弹窗", async () => {
    setup((_url, init) => {
      if (init?.method === "POST") {
        return json(202, {
          accepted_count: 0,
          rejected_count: 1,
          results: [{ content_id: 1, accepted: false, error: null }],
        });
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除文件" }));
    expect(await screen.findByText("删除失败")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "删除存储文件" })).toBeInTheDocument();
    expect(screen.getByLabelText("选择文件 user-file-1.bin")).toBeChecked();
  });

  it("全部业务拒绝保留弹窗和选择，不刷新，可在原弹窗重试", async () => {
    let deletes = 0;
    setup((_url, init) => {
      if (init?.method === "POST") {
        deletes += 1;
        return json(202, deletes === 1 ? {
          accepted_count: 0,
          rejected_count: 1,
          results: [rejected(1, "暂时无法删除")],
        } : {
          accepted_count: 1,
          rejected_count: 0,
          results: [accepted(1)],
        });
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const dialog = await screen.findByRole("dialog", { name: "删除存储文件" });
    const before = listCalls().length;
    fireEvent.click(within(dialog).getByRole("button", { name: "删除文件" }));
    const failures = await screen.findByRole("dialog", { name: "批量操作失败明细" });
    expect(
      within(failures).getByText("暂时无法删除", { selector: ".batch-failure-list span" }),
    ).toBeInTheDocument();
    fireEvent.click(within(failures).getByRole("button", { name: "知道了" }));
    expect(screen.getByRole("dialog", { name: "删除存储文件" })).toBeInTheDocument();
    expect(screen.getByLabelText("选择文件 user-file-1.bin")).toBeChecked();
    expect(listCalls()).toHaveLength(before);
    expect(screen.queryByText(/已受理删除/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除文件" }));
    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(deleteCalls()).toHaveLength(2);
  });

  it.each([
    ["HTTP 失败", json(422, { error: { code: "invalid_input", message: "请求无效" } }), "请求无效"],
    ["网络失败", new TypeError("offline"), "网络异常，请检查连接后重试"],
  ])("%s 时不伪成功且保留重试现场", async (_name, failure, message) => {
    setup((_url, init) => {
      if (init?.method === "POST") {
        if (failure instanceof Error) throw failure;
        return failure;
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const dialog = await screen.findByRole("dialog", { name: "删除存储文件" });
    const before = listCalls().length;
    fireEvent.click(within(dialog).getByRole("button", { name: "删除文件" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "删除存储文件" })).toBeInTheDocument();
    expect(screen.getByLabelText("选择文件 user-file-1.bin")).toBeChecked();
    expect(listCalls()).toHaveLength(before);
    expect(screen.queryByText(/已受理删除/)).not.toBeInTheDocument();
  });

  it("删除期间保持模态、禁止关闭和重复提交", async () => {
    let releaseDelete: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseDelete = resolve; });
    setup((_url, init) => init?.method === "POST" ? pending : list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    const dialog = await screen.findByRole("dialog", { name: "删除存储文件" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除文件" }));
    expect(await within(dialog).findByRole("button", { name: "删除中..." })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "返回" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "关闭对话框" })).toBeDisabled();
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(screen.getByRole("dialog", { name: "删除存储文件" })).toBeInTheDocument();
    expect(deleteCalls()).toHaveLength(1);

    releaseDelete(json(202, { accepted_count: 1, rejected_count: 0, results: [accepted(1)] }));
    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("末页选择全部受理后回退页码", async () => {
    setup((url, init) => {
      if (init?.method === "POST") {
        return json(202, { accepted_count: 1, rejected_count: 0, results: [accepted(2)] });
      }
      if (url.includes("page=2")) return list([content(2)], 21);
      return list([content(1)], 21);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("storage-row-2");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-2.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除文件" }));
    await waitFor(() => expect(listCalls().at(-1)).toContain("page=1"));
  });
});

describe("storage model 与卸载保护", () => {
  it("映射名称、哈希与首个拒绝原因", () => {
    expect(digestOf("file:abcdef0123456789")).toBe("abcdef0123456789");
    expect(digestOf("legacy-key")).toBe("legacy-key");
    expect(shortHash("file:0123456789abcdefXYZ")).toBe("0123456789…defXYZ");
    const view = toStorageFileView(content(1, { file_names: ["a.bin", "b.bin"] }));
    expect(view.namesText).toBe("a.bin、b.bin");
    expect(toStorageFileView(content(2, { file_names: [] })).namesText).toBe("");
    expect(firstRejectedMessage({ accepted_count: 1, rejected_count: 1, results: [accepted(1), rejected(2)] }))
      .toBe("内容被引用");
    expect(firstRejectedMessage({ accepted_count: 1, rejected_count: 0, results: [accepted(1)] }))
      .toBeNull();
  });

  it("卸载后的列表失败不弹 Toast", async () => {
    let rejectList: (error: Error) => void = () => undefined;
    const pending = new Promise((_resolve, reject) => { rejectList = reject; });
    const view = setup(() => pending);
    await waitFor(() => expect(listCalls()).toHaveLength(1));
    view.unmount();
    rejectList(new Error("后端炸了"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.queryByText("加载存储文件失败")).not.toBeInTheDocument();
  });

  it("卸载后的引用失败不弹 Toast", async () => {
    let rejectUsers: (error: Error) => void = () => undefined;
    const pending = new Promise((_resolve, reject) => { rejectUsers = reject; });
    const view = setup((url) => url.includes("/users") ? pending : list([content(1, { reference_count: 1 })]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(referenceButton(1, 1));
    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/users"))).toBe(true));
    view.unmount();
    rejectUsers(new Error("用户列表失败"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.queryByText("用户列表失败")).not.toBeInTheDocument();
  });

  it("卸载后的删除成功响应不弹 Toast 或刷新", async () => {
    let releaseDelete: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { releaseDelete = resolve; });
    const view = setup((_url, init) => init?.method === "POST" ? pending : list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除文件" }));
    await waitFor(() => expect(deleteCalls()).toHaveLength(1));
    const before = listCalls().length;
    view.unmount();
    releaseDelete(json(202, { accepted_count: 1, rejected_count: 0, results: [accepted(1)] }));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(listCalls()).toHaveLength(before);
    expect(screen.queryByText(/已受理删除/)).not.toBeInTheDocument();
  });

  it("卸载后的删除失败不弹 Toast", async () => {
    let rejectDelete: (error: Error) => void = () => undefined;
    const pending = new Promise((_resolve, reject) => { rejectDelete = reject; });
    const view = setup((_url, init) => init?.method === "POST" ? pending : list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择文件 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中(1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除文件" }));
    await waitFor(() => expect(deleteCalls()).toHaveLength(1));
    view.unmount();
    rejectDelete(new Error("整批失败"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.queryByText("删除失败")).not.toBeInTheDocument();
  });
});
