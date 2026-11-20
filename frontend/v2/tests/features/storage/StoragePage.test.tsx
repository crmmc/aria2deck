import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { StoragePage } from "@/features/storage/StoragePage";
import { digestOf, shortHash, toStorageFileView } from "@/features/storage/model";
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
    content_key: `file:abcd1234efgh5678-${contentId}`,
    kind: "file",
    size: 1024,
    storage_path: `/data/objects/key-${contentId}`,
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

describe("StoragePage 列表（V1 表格）", () => {
  it("按 V1 表格渲染磁盘状态徽章、引用数按钮与目录前缀", async () => {
    setup(() =>
      list([
        content(1),
        content(2, { on_disk: false, kind: "directory", reference_count: 3 }),
      ]),
    );
    expect(await screen.findByTestId("storage-row-1")).toBeInTheDocument();
    // V1 类词汇：.table/.badge/.button.secondary.btn-sm
    expect(document.querySelector("table.table")).not.toBeNull();
    expect(document.querySelector("span.badge.active")).toHaveTextContent("存在");
    expect(document.querySelector("span.badge.danger")).toHaveTextContent("缺失");
    const refButtons = screen.getAllByRole("button", { name: /^[0-9]+$/ });
    expect(refButtons[0]).toHaveTextContent("0");
    expect(refButtons[0]).toBeDisabled(); // V1：引用数为 0 禁用
    expect(refButtons[1]).toHaveTextContent("3");
    expect(refButtons[1]).toBeEnabled();
    // 目录按 V1 展示 📁 前缀，不单独设类型列
    expect(screen.getByTestId("storage-row-2")).toHaveTextContent("📁 user-file-2.bin");
    expect(screen.getByTestId("storage-row-1")).toHaveTextContent("1.0 KB");
  });

  it("渲染 V1 页面标题与工具栏（搜索/孤立筛选/刷新/删除选中）", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    expect(document.querySelector("h1.page-title")).toHaveTextContent("存储管理");
    expect(screen.getByText("管理系统存储文件")).toBeInTheDocument();
    expect(document.querySelector("input.input")).toHaveAttribute("placeholder", "搜索文件名...");
    expect(document.querySelector("label.checkbox-label")).not.toBeNull();
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除选中 (0)" })).toBeDisabled();
  });

  it("空列表在表格内显示暂无存储文件（colSpan=7）", async () => {
    setup(() => list([]));
    const empty = await screen.findByText("暂无存储文件");
    expect(empty.closest("td")).toHaveAttribute("colspan", "7");
  });

  it("加载失败只提示 Toast，不渲染 V2 错误区块", async () => {
    setup((_url, init) => {
      if (init?.method === undefined || init.method === "GET") {
        return json(500, { error: { code: "internal", message: "后端炸了" } });
      }
      return list([]);
    });
    expect(await screen.findByText("加载存储文件失败")).toBeInTheDocument();
    expect(await screen.findByText("暂无存储文件")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("重定向非管理员用户且不发列表请求", async () => {
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

  it("搜索防抖 300ms 后携带 search 参数并同步回到第 1 页", async () => {
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
      fireEvent.change(screen.getByLabelText("搜索文件名"), { target: { value: "key-9" } });
      await act(async () => {
        jest.advanceTimersByTime(350);
        await Promise.resolve();
      });
      await waitFor(() => {
        expect(lastUrl()).toContain("search=key-9");
        expect(lastUrl()).toContain("page=1");
      });
      expect(await screen.findByTestId("storage-row-9")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("切换仅显示孤立文件并清空选择", async () => {
    setup((url) => {
      if (url.includes("orphans_only=true")) return list([content(7, { reference_count: 0 })]);
      return list([content(1, { reference_count: 2 }), content(2)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 user-file-2.bin"));
    expect(screen.getByLabelText("选择 user-file-2.bin")).toBeChecked();
    fireEvent.click(screen.getByLabelText("仅显示孤立文件"));
    await waitFor(() => {
      expect(lastUrl()).toContain("orphans_only=true");
    });
    expect(await screen.findByTestId("storage-row-7")).toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-2")).not.toBeInTheDocument();
  });

  it("筛选变化重置页码且只发一笔列表请求", async () => {
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

    fireEvent.click(screen.getByLabelText("仅显示孤立文件"));

    await waitFor(() => expect(lastUrl()).toContain("orphans_only=true"));
    await waitFor(() => expect(listCalls()).toBe(before + 1)); // 旧页码请求不再发出。
    expect(lastUrl()).toContain("page=1");
  });

  it("翻页清空选择", async () => {
    setup((url) => {
      if (url.includes("page=2")) return list([content(2)], 21);
      return list([content(1)], 21);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    expect(screen.getByLabelText("选择 user-file-1.bin")).toBeChecked();
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("storage-row-2");
    expect(screen.queryByLabelText("选择 user-file-1.bin")).not.toBeInTheDocument();
  });

  it("全选与取消全选", async () => {
    setup(() => list([content(1), content(2)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("全选"));
    expect(screen.getByLabelText("选择 user-file-1.bin")).toBeChecked();
    expect(screen.getByLabelText("选择 user-file-2.bin")).toBeChecked();
    expect(screen.getByRole("button", { name: "删除选中 (2)" })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("全选"));
    expect(screen.getByLabelText("选择 user-file-1.bin")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "删除选中 (0)" })).toBeDisabled();
  });

  it("单独反选一行", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    expect(screen.getByLabelText("选择 user-file-1.bin")).toBeChecked();
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin")); // 再次点击取消选择。
    expect(screen.getByLabelText("选择 user-file-1.bin")).not.toBeChecked();
  });

  it("孤儿筛选无结果时显示 V1 统一空态", async () => {
    setup((url) => {
      if (url.includes("orphans_only=true")) return list([]);
      return list([content(1, { reference_count: 3 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("仅显示孤立文件"));
    expect(await screen.findByText("暂无存储文件")).toBeInTheDocument();
  });

  it("丢弃筛选变化前的过期响应", async () => {
    let calls = 0;
    let releaseStale: (value: unknown) => void = () => undefined;
    const staleCall = new Promise((resolve) => {
      releaseStale = resolve;
    });
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (!String(url).startsWith("/api/v2/storage")) return json(200, {});
      calls += 1;
      if (calls === 2) return staleCall as never; // 第二次请求挂起后再次切筛选。
      if (calls >= 3) return json(200, list([content(2)]));
      return json(200, list([content(1)]));
    });
    render(
      <MemoryRouter initialEntries={["/storage"]}>
        <AppProviders>
          <StoragePage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("仅显示孤立文件"));
    await waitFor(() => expect(calls).toBe(2));
    fireEvent.click(screen.getByLabelText("仅显示孤立文件")); // 第三次请求覆盖第二次。
    await screen.findByTestId("storage-row-2");
    releaseStale(json(200, list([content(42)])));
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(3));
    expect(screen.queryByTestId("storage-row-42")).not.toBeInTheDocument();
    expect(screen.getByTestId("storage-row-2")).toBeInTheDocument();
  });

  it("哈希列截断 + 完整 title、多名称连接与创建时间（C12）", async () => {
    const longDigest = "a".repeat(64);
    setup(() =>
      list([
        content(1, { content_key: `file:${longDigest}`, file_names: ["a.bin", "b.bin"] }),
        content(2, { file_names: [] }),
      ]),
    );
    const row1 = await screen.findByTestId("storage-row-1");
    // C12：多名称「、」连接展示与悬停 title，不冒充唯一原名
    expect(row1).toHaveTextContent("a.bin、b.bin");
    const nameCell = row1.querySelector("td[title='a.bin、b.bin']");
    expect(nameCell).not.toBeNull();
    // 内容键取摘要段按 V1 截断，完整摘要放 title
    expect(row1.querySelector(`td[title='${longDigest}']`)).toHaveTextContent("aaaaaaaa...");
    expect(row1.textContent).toContain("2023"); // created_at_ms 本地时区格式化。
    // 孤儿（无关联文件名）显示占位符
    const row2 = await screen.findByTestId("storage-row-2");
    expect(row2.querySelector("td[title='']")).toHaveTextContent("—");
  });

  it("刷新按钮重新加载列表", async () => {
    let loads = 0;
    setup(() => {
      loads += 1;
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    const before = loads;
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(loads).toBeGreaterThan(before));
    expect(await screen.findByTestId("storage-row-1")).toBeInTheDocument();
  });

  it("V1 表格无操作列与逐行删除按钮", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    const headers = Array.from(document.querySelectorAll("table.table thead th")).map(
      (th) => th.textContent,
    );
    expect(headers).toEqual(["", "文件名", "大小", "哈希", "引用数", "创建时间", "状态"]);
    const row = screen.getByTestId("storage-row-1");
    expect(row.querySelectorAll("button")).toHaveLength(1); // 仅引用数按钮。
  });
});

describe("StoragePage 引用用户弹窗（V1 原文）", () => {
  it("打开弹窗并按 V1 展示用户列表", async () => {
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
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "1" }));
    const dialog = await screen.findByRole("dialog", { name: "文件引用用户" });
    expect(dialog.querySelector("h3")).toHaveTextContent("引用用户");
    expect(dialog).toHaveTextContent("user-file-1.bin");
    await waitFor(() => {
      expect(dialog).toHaveTextContent("alice");
      expect(dialog).toHaveTextContent("(alice.bin)");
    });
    // V1 弹窗不展示引用时间
    expect(dialog.textContent).not.toContain("2023-11-15");
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("引用数为 0 时按钮禁用（V1 语义，不可打开弹窗）", async () => {
    setup(() => list([content(1)]));
    await screen.findByTestId("storage-row-1");
    expect(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "0" })).toBeDisabled();
  });

  it("用户列表为空时显示无引用用户", async () => {
    setup((url) => {
      if (url.includes("/users")) return { items: [] };
      return list([content(1, { reference_count: 1 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "1" }));
    const dialog = await screen.findByRole("dialog", { name: "文件引用用户" });
    expect(await within(dialog).findByText("无引用用户")).toBeInTheDocument();
  });

  it("加载用户失败时提示加载用户列表失败（V1 弹窗内无错误区块）", async () => {
    setup((url) => {
      if (url.includes("/users")) {
        return json(500, { error: { code: "internal", message: "后端炸了" } });
      }
      return list([content(1, { reference_count: 1 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "1" }));
    const dialog = await screen.findByRole("dialog", { name: "文件引用用户" });
    expect(await screen.findByText("加载用户列表失败")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });
});

describe("StoragePage 删除（V1 直接删除 + C15 受理语义）", () => {
  it("点击即删无确认框，逐项反馈受理与拒绝结果", async () => {
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
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    fireEvent.click(screen.getByLabelText("选择 user-file-2.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (2)" }));
    // V1 无确认框：点击后直接提交
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
    expect(await screen.findByText("内容被引用")).toBeInTheDocument();
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/storage/files/delete",
      );
      expect(JSON.parse(call![1].body as string)).toEqual({ content_ids: [1, 2] });
    });
  });

  it("全部受理时提示已受理删除（不宣称物理删除完成）", async () => {
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
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));
    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
  });

  it("删除中按钮显示删除中并禁用", async () => {
    let releaseDelete: (value: unknown) => void = () => undefined;
    const deleteCall = new Promise((resolve) => {
      releaseDelete = resolve;
    });
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return deleteCall as never;
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));
    expect(await screen.findByRole("button", { name: "删除中..." })).toBeDisabled();
    releaseDelete({
      accepted_count: 1,
      rejected_count: 0,
      results: [{ content_id: 1, accepted: true, error: null }],
    });
    expect(await screen.findByText("已受理删除 1 个文件")).toBeInTheDocument();
  });

  it("末页最后一行删除后回退页码", async () => {
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
    fireEvent.click(screen.getByLabelText("选择 user-file-2.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));
    await waitFor(() => expect(lastUrl()).toContain("page=1"));
  });

  it("整批请求失败提示删除失败", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return json(422, { error: { code: "invalid_input", message: "请求无效" } });
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));
    expect(await screen.findByText("删除失败")).toBeInTheDocument();
  });
});

describe("storage model（C12 映射）", () => {
  it("digestOf/shortHash 取冒号后摘要并按 V1 截断", () => {
    expect(digestOf("file:abcdef0123456789")).toBe("abcdef0123456789");
    expect(digestOf("legacy-key")).toBe("legacy-key");
    expect(shortHash("file:abcdef0123456789")).toBe("abcdef01...");
  });

  it("toStorageFileView 映射多名称与目录标记，不冒充唯一原名", () => {
    const view = toStorageFileView(
      content(1, { kind: "directory", file_names: ["a.bin", "b.bin"] }),
    );
    expect(view.id).toBe(1);
    expect(view.namesText).toBe("a.bin、b.bin");
    expect(view.isDirectory).toBe(true);
    expect(view.onDisk).toBe(true);
    const orphan = toStorageFileView(content(2, { file_names: [] }));
    expect(orphan.namesText).toBe("");
  });
});

describe("StoragePage 防御与卸载分支", () => {
  it("当前页已超出末页（如删除后总数变化）时回退到末页并重取", async () => {
    const listCalls: string[] = [];
    setup((url) => {
      if (!String(url).startsWith("/api/v2/storage/files")) return list([]);
      listCalls.push(String(url));
      // 第 1 页共 21 条（2 页）；进入第 2 页时后端只剩 1 条 => lastPage=1，当前页越界。
      if (String(url).includes("page=2")) return { items: [content(2)], total: 1, page: 2, page_size: 20 };
      return list([content(1)], 21);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("下一页")); // 进入第 2 页（服务端称越界）。

    // 越界时不渲染该页数据，而是纠正回末页并重新拉取。
    await waitFor(() => expect(listCalls.some((u) => u.includes("page=2"))).toBe(true));
    await waitFor(() => expect(listCalls[listCalls.length - 1]).toContain("page=1"));
    expect(await screen.findByTestId("storage-row-1")).toBeInTheDocument();
    expect(screen.queryByTestId("storage-row-2")).not.toBeInTheDocument();
  });

  it("卸载后返回的引用用户失败不弹错误 Toast", async () => {
    let rejectUsers: (error: Error) => void = () => undefined;
    const pendingUsers = new Promise((_resolve, reject) => {
      rejectUsers = reject;
    });
    const view = setup((url) => {
      if (String(url).includes("/users")) return pendingUsers as never;
      return list([content(1, { reference_count: 1 })]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "1" }));
    await waitFor(() =>
      expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/users"))).toBe(true),
    );

    view.unmount();
    rejectUsers(new Error("用户列表失败"));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText("加载用户列表失败")).not.toBeInTheDocument();
  });

  it("关键词未变化时防抖定时器不重置页码、不额外请求", async () => {
    jest.useFakeTimers();
    try {
      const calls: string[] = [];
      setup((url) => {
        calls.push(String(url));
        return list([content(1)], 45); // 3 页，便于观察页码是否被重置。
      });
      await screen.findByTestId("storage-row-1");
      fireEvent.click(screen.getByLabelText("下一页"));
      await waitFor(() => expect(calls[calls.length - 1]).toContain("page=2"));
      const before = calls.length;

      // 只输入空格：trim 后仍为空，等于当前关键词，页码保持不变。
      fireEvent.change(screen.getByLabelText("搜索文件名"), { target: { value: "   " } });
      await act(async () => {
        jest.advanceTimersByTime(400);
        await Promise.resolve();
      });

      expect(calls).toHaveLength(before); // 没有新请求。
      expect(screen.getByTestId("storage-row-1")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("卸载后返回的列表失败不弹错误 Toast", async () => {
    let rejectList: (error: Error) => void = () => undefined;
    const pendingList = new Promise((_resolve, reject) => {
      rejectList = reject;
    });
    const view = setup((url) => {
      if (String(url).startsWith("/api/v2/storage/files")) return pendingList as never;
      return list([]);
    });
    await waitFor(() =>
      expect(mockFetch.mock.calls.some(([url]) => String(url).startsWith("/api/v2/storage/files"))).toBe(
        true,
      ),
    );
    view.unmount();
    rejectList(new Error("后端炸了"));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText("加载存储文件失败")).not.toBeInTheDocument();
  });

  it("卸载后返回的删除失败不弹错误 Toast", async () => {
    let rejectDelete: (error: Error) => void = () => undefined;
    const pendingDelete = new Promise((_resolve, reject) => {
      rejectDelete = reject;
    });
    const view = setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/storage/files/delete") {
        return pendingDelete as never;
      }
      return list([content(1)]);
    });
    await screen.findByTestId("storage-row-1");
    fireEvent.click(screen.getByLabelText("选择 user-file-1.bin"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));
    await waitFor(() =>
      expect(
        mockFetch.mock.calls.some(
          ([url, init]) =>
            init?.method === "POST" && url === "/api/v2/storage/files/delete",
        ),
      ).toBe(true),
    );

    view.unmount();
    rejectDelete(new Error("整批失败"));
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(screen.queryByText("删除失败")).not.toBeInTheDocument();
  });

  it("引用用户弹窗点背景与原生 cancel 都能关闭", async () => {
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
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "1" }));
    const dialog = await screen.findByRole("dialog", { name: "文件引用用户" });
    await within(dialog).findByText("alice");

    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // 再次打开后用原生 cancel（Esc）关闭。
    fireEvent.click(within(screen.getByTestId("storage-row-1")).getByRole("button", { name: "1" }));
    const reopened = await screen.findByRole("dialog", { name: "文件引用用户" });
    fireEvent(reopened, new Event("cancel", { bubbles: false, cancelable: true }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
