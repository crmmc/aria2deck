import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { FilesPage } from "@/features/files/FilesPage";
import type {
  FileItemResponse,
  PackViewResponse,
  UserResponse,
} from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

jest.mock("@/shared/lib/download", () => ({
  triggerFileDownload: jest.fn(),
  triggerBatchDownloads: jest.fn().mockResolvedValue(undefined),
  triggerFormDownload: jest.fn(),
}));

import { triggerBatchDownloads, triggerFileDownload } from "@/shared/lib/download";

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 10 * 1024 * 1024 * 1024,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 2048,
  reserved_bytes: 1024,
  available_bytes: 10 * 1024 * 1024 * 1024 - 3072,
};

function file(id: number, overrides: Partial<FileItemResponse> = {}): FileItemResponse {
  return {
    id,
    display_name: `file-${id}.bin`,
    kind: "file",
    size: 1024 * id,
    created_at_ms: 1700000000000 + id,
    ...overrides,
  };
}

function rootList(items: FileItemResponse[], total = items.length): unknown {
  return {
    items,
    total,
    page: 1,
    page_size: 20,
    space: {
      quota_bytes: ME.quota_bytes,
      used_bytes: ME.used_bytes,
      reserved_bytes: ME.reserved_bytes,
      available_bytes: ME.available_bytes,
    },
  };
}

function entries(items: Array<{ name: string; path: string; kind: "file" | "directory"; size: number }>, breadcrumbs: Array<{ name: string; path: string }>): unknown {
  return { items, total: items.length, page: 1, page_size: 200, breadcrumbs };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

/** 打包任务卡默认空列表；打包卡用例用 setPacksResponse 覆盖。 */
let packsResponse: unknown = { items: [] };

function setPacksResponse(items: PackViewResponse[]): void {
  packsResponse = { items };
}

function packView(overrides: Partial<PackViewResponse> = {}): PackViewResponse {
  return {
    id: 7,
    status: "packing",
    step: "compressing",
    step_progress: 42,
    step_started_at_ms: Date.now() - 4000,
    created_at_ms: 1,
    updated_at_ms: 2,
    finished_at_ms: null,
    error_code: null,
    delete_source: false,
    output_name: "bundle.zip",
    output: null,
    ...overrides,
  };
}

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url.startsWith("/api/v2/packs?")) return json(200, packsResponse);
    const result = handler(url, init);
    // 处理器可直接返回完整响应（带 json 函数）以覆盖错误状态码。
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
    <MemoryRouter initialEntries={["/files"]}>
      <AppProviders>
        <FilesPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

const DIR = file(9, { display_name: "资料目录", kind: "directory", size: 0 });

describe("FilesPage", () => {
  beforeEach(() => {
    packsResponse = { items: [] };
  });

  it("renders root list with the space usage card and pagination (F2)", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1), DIR]);
      return {};
    });
    expect((await screen.findAllByText("file-1.bin"))[0]).toBeInTheDocument();
    const card = await screen.findByRole("region", { name: "空间用量" });
    expect(card.textContent).toContain("可用空间");
    expect(card.textContent).toContain("已冻结 1.00 KiB（下载中）");
    expect(screen.getByRole("progressbar", { name: "空间用量" })).toHaveAttribute(
      "role",
      "progressbar",
    );
    expect(screen.getByText("第 1 / 1 页 · 共 2 条")).toBeInTheDocument();
    // 冻结斜纹分段存在。
    expect(document.querySelector(".progress-frozen")).not.toBeNull();
  });

  it("shows empty and error states with retry", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
      return json(500, { error: { code: "internal", message: "读取失败" } });
    });
    render(
      <MemoryRouter initialEntries={["/files"]}>
        <AppProviders>
          <FilesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("读取失败")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url.startsWith("/api/v2/packs?")) return json(200, { items: [] });
      return json(200, rootList([]));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("暂无文件")).toBeInTheDocument();
  });

  it("browses a directory with 200-per-page entries, breadcrumbs and back navigation", async () => {
    setup((url) => {
      if (url.includes("/entries")) {
        return entries(
          [
            { name: "子目录", path: "子目录", kind: "directory", size: 0 },
            { name: "a.txt", path: "a.txt", kind: "file", size: 5 },
          ],
          [{ name: "资料目录", path: "" }],
        );
      }
      return rootList([DIR, file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => String(url).includes("/entries"));
      expect(call![0]).toContain("/api/v2/files/9/entries?");
      expect(call![0]).toContain("page_size=200");
    });
    expect((await screen.findAllByText("a.txt"))[0]).toBeInTheDocument();
    // 目录内文件下载（带 path）与子目录导航。
    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    await waitFor(() =>
      expect(triggerFileDownload).toHaveBeenCalledWith("/api/v2/files/9/download?path=a.txt", "a.txt"),
    );
    expect(await screen.findByText("已触发下载")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "进入子目录 子目录" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.filter(([url]) => String(url).includes("/entries")).at(-1);
      expect(String(call![0])).toContain("path="); // 进入了子目录。
    });
    fireEvent.click(screen.getByRole("button", { name: "资料目录" })); // 面包屑返回
    expect((await screen.findAllByText("a.txt"))[0]).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回上级" }));
    expect((await screen.findAllByText("file-1.bin"))[0]).toBeInTheDocument();
  });

  it("shows the empty-directory state", async () => {
    setup((url) => {
      if (url.includes("/entries")) return entries([], [{ name: "资料目录", path: "" }]);
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    expect(await screen.findByText("空目录")).toBeInTheDocument();
  });

  // ============ F4 搜索：按钮/回车立即、输入 500ms 防抖、范围开关 ============

  it("query button and Enter trigger immediately; identical term+scope is not re-sent (F4)", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("search=report")) {
          return {
            hits: [
              { user_file_id: 9, display_name: "资料目录", path: "docs/report.pdf", kind: "file", size: 8 },
            ],
            truncated: true,
          };
        }
        if (url.includes("search=rep")) {
          return { hits: [], truncated: false };
        }
        return rootList([DIR]);
      });
      await act(async () => {
        await Promise.resolve();
      });
      // 输入后防抖窗口内不搜索。
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "rep" } });
      expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("search="))).toHaveLength(0);
      // 查询按钮立即触发。
      fireEvent.click(screen.getByRole("button", { name: "查询" }));
      await waitFor(() => {
        expect(mockFetch.mock.calls.some(([url]) => String(url).includes("search=rep"))).toBe(true);
      });
      // 防抖到期：同词同范围不重发。
      await act(async () => {
        jest.advanceTimersByTime(500);
      });
      expect(
        mockFetch.mock.calls.filter(([url]) => String(url).includes("search=rep")),
      ).toHaveLength(1);
      // 输入变化后 500ms 自动搜索。
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "report" } });
      // 回车立即触发（新词不等防抖）。
      fireEvent.keyDown(screen.getByLabelText("搜索文件"), { key: "Enter" });
      await waitFor(() => {
        expect(
          mockFetch.mock.calls.some(([url]) => String(url).includes("search=report")),
        ).toBe(true);
      });
      await waitFor(() =>
        expect(screen.getByText("结果超过 200 条，仅显示前 200 条，请细化关键词")).toBeInTheDocument(),
      );
    } finally {
      jest.useRealTimers();
    }
  });

  it("search scope switch: global by default, current-directory only when browsing (F4)", async () => {
    setup((url) => {
      if (url.includes("/entries")) {
        return entries(
          [
            { name: "docs", path: "docs", kind: "directory", size: 0 },
            { name: "a.txt", path: "docs/a.txt", kind: "file", size: 5 },
          ],
          [{ name: "资料目录", path: "" }],
        );
      }
      if (url.includes("search=a")) {
        return {
          hits: [
            { user_file_id: 9, display_name: "资料目录", path: "", kind: "directory", size: 0 },
            { user_file_id: 10, display_name: "资料目录", path: "docs/a.txt", kind: "file", size: 5 },
          ],
          truncated: false,
        };
      }
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    // 根视图：范围开关禁用。
    expect(screen.getByLabelText("仅搜索当前目录")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    await screen.findAllByText("a.txt");

    // 全局搜索：不携带 scope。
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "a" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => String(url).includes("search=a"));
      expect(call![0]).not.toContain("scope=");
    });
    expect((await screen.findAllByText("docs/a.txt")).length).toBeGreaterThan(0);

    // 当前目录：携带 scope=9。
    fireEvent.click(screen.getByLabelText("仅搜索当前目录"));
    await waitFor(() => {
      const call = mockFetch.mock.calls.filter(([url]) => String(url).includes("search=a")).at(-1);
      expect(String(call![0])).toContain("scope=9");
    });
  });

  it("locates an entry hit by jumping to its parent directory", async () => {
    setup((url) => {
      if (url.includes("search=report")) {
        return {
          hits: [
            { user_file_id: 9, display_name: "资料目录", path: "docs/report.pdf", kind: "file", size: 8 },
          ],
          truncated: false,
        };
      }
      if (url.includes("/entries")) {
        return entries(
          [{ name: "report.pdf", path: "docs/report.pdf", kind: "file", size: 8 }],
          [
            { name: "资料目录", path: "" },
            { name: "docs", path: "docs" },
          ],
        );
      }
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "report" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("option"));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => String(url).includes("path=docs"));
      expect(call).toBeDefined(); // 定位跳转：进入命中条目的父目录。
    });
  });

  it("locates a root hit by highlighting the row on page 1", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 1, display_name: "file-1.bin", path: "", kind: "file", size: 1 }],
          truncated: false,
        };
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "file-1" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("option"));
    const row = await waitFor(() => {
      const el = document.querySelector('[data-file-id="1"]');
      expect(el?.className).toContain("row-highlight");
      return el;
    });
    expect(row).toBeDefined();
  });

  it("locate to another page scans pages, jumps and highlights (F5)", async () => {
    const pageItems: FileItemResponse[] = [];
    for (let index = 1; index <= 30; index += 1) pageItems.push(file(index));
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 25, display_name: "file-25.bin", path: "", kind: "file", size: 1 }],
          truncated: false,
        };
      }
      const match = /page=(\d+)/.exec(url);
      const page = match === null ? 1 : Number(match[1]);
      return rootList(pageItems.slice((page - 1) * 20, page * 20), 30);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "file-25" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("option"));
    // 跳到第 2 页并高亮目标行。
    await waitFor(() => {
      const row = document.querySelector('[data-file-id="25"]');
      expect(row).not.toBeNull();
      expect(row?.className).toContain("row-highlight");
    });
    expect(screen.getByText("第 2 / 2 页 · 共 30 条")).toBeInTheDocument();
  });

  it("locate miss reports failure after scanning all pages (F5)", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 77, display_name: "别处的文件.bin", path: "", kind: "file", size: 1 }],
          truncated: false,
        };
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "别处" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("option"));
    expect(await screen.findByText("定位失败：未在当前列表找到该文件")).toBeInTheDocument();
  });

  it("downloads a root file via a[download] trigger and supports select-all", async () => {
    setup(() => rootList([file(1), file(2)]));
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    expect(triggerFileDownload).toHaveBeenCalledWith("/api/v2/files/1/download", "file-1.bin");
    expect(await screen.findByText("已触发下载")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("全选文件"));
    expect(await screen.findByRole("button", { name: /打包（2）/ })).toBeInTheDocument();
    expect(screen.getAllByLabelText("选择 file-2.bin")[0]).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "清除选择" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /打包（/ })).not.toBeInTheDocument());
  });

  // ============ F3 排序 ============

  it("sort headers cycle asc→desc→default with folders pinned first", async () => {
    setup(() =>
      rootList([
        file(3, { display_name: "b-file.bin", size: 100, created_at_ms: 300 }),
        DIR,
        file(1, { display_name: "a-file.bin", size: 300, created_at_ms: 100 }),
        file(2, { display_name: "c-file.bin", size: 200, created_at_ms: 200 }),
      ]),
    );
    await screen.findAllByText("b-file.bin");
    const firstNameInTable = () =>
      document.querySelector("table.files-table tbody tr .name-cell")?.textContent;
    // 默认：服务端顺序。
    expect(firstNameInTable()).toBe("b-file.bin");
    // 名称升序：文件夹置顶，文件按名。
    fireEvent.click(screen.getByRole("button", { name: "名称排序" }));
    expect(firstNameInTable()).toBe("资料目录");
    expect(
      Array.from(document.querySelectorAll("table.files-table tbody tr td.name-cell"))
        .map((cell) => cell.textContent)
        .filter((name) => name !== null && name.includes(".bin")),
    ).toEqual(["a-file.bin", "b-file.bin", "c-file.bin"]);
    // 名称降序。
    fireEvent.click(screen.getByRole("button", { name: "名称排序（升序）" }));
    expect(
      Array.from(document.querySelectorAll("table.files-table tbody tr td.name-cell"))
        .map((cell) => cell.textContent)
        .filter((name) => name !== null && name.includes(".bin")),
    ).toEqual(["c-file.bin", "b-file.bin", "a-file.bin"]);
    // 还原默认。
    fireEvent.click(screen.getByRole("button", { name: "名称排序（降序）" }));
    expect(firstNameInTable()).toBe("b-file.bin");
    // 大小升序（文件夹仍置顶）。
    fireEvent.click(screen.getByRole("button", { name: "大小排序" }));
    expect(
      Array.from(document.querySelectorAll("table.files-table tbody tr td.name-cell"))
        .map((cell) => cell.textContent)
        .filter((name) => name !== null && name.includes(".bin")),
    ).toEqual(["b-file.bin", "c-file.bin", "a-file.bin"]);
  });

  // ============ F6 批量删除 ============

  it("batch delete asks confirmation, reports partial failure and reloads (F6)", async () => {
    let deleteCount = 0;
    setup((url, init) => {
      if (init?.method === "DELETE") {
        deleteCount += 1;
        if (deleteCount === 2) {
          return json(409, { error: { code: "content_busy", message: "文件正被使用" } });
        }
        return { ok: true };
      }
      return rootList([file(1), file(2)]);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 file-2.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量删除（2）/ }));
    const dialog = await screen.findByRole("dialog", { name: "批量删除" });
    expect(dialog.textContent).toContain("2 个文件");
    // 放弃一次。
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(
      mockFetch.mock.calls.filter(([, init2]) => init2?.method === "DELETE"),
    ).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /批量删除（2）/ }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "批量删除" })).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("已删除 1 个文件，1 个删除失败")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockFetch.mock.calls.filter(([, init2]) => init2?.method === "DELETE")).toHaveLength(2);
    });
  });

  it("rejects batch download containing a folder with a Chinese toast", async () => {
    setup(() => rootList([file(1), DIR]));
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 资料目录")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量下载/ }));
    expect(await screen.findByText("选择包含文件夹，无法批量下载，请先打包后下载")).toBeInTheDocument();
    expect(triggerBatchDownloads).not.toHaveBeenCalled();
  });

  it("batch downloads selected files with the shared batch trigger", async () => {
    setup(() => rootList([file(1), file(2)]));
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 file-2.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量下载/ }));
    await waitFor(() =>
      expect(triggerBatchDownloads).toHaveBeenCalledWith([
        "/api/v2/files/1/download",
        "/api/v2/files/2/download",
      ]),
    );
  });

  // ============ F7 目录内多选 + 批量下载 + 全选 ============

  it("entry selection supports select-all, disables folders and batch-downloads by path (F7)", async () => {
    setup((url) => {
      if (url.includes("/entries")) {
        return entries(
          [
            { name: "子目录", path: "子目录", kind: "directory", size: 0 },
            { name: "a.txt", path: "a.txt", kind: "file", size: 5 },
            { name: "b.txt", path: "b.txt", kind: "file", size: 6 },
          ],
          [{ name: "资料目录", path: "" }],
        );
      }
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    await screen.findAllByText("a.txt");
    // 文件夹条目禁选。
    expect(screen.getAllByLabelText("选择 子目录")[0]).toBeDisabled();
    expect(screen.getAllByLabelText("选择 a.txt")[0]).not.toBeDisabled();
    // 全选：只选中文件。
    fireEvent.click(screen.getByLabelText("全选文件（仅文件）"));
    expect(screen.getAllByLabelText("选择 a.txt")[0]).toBeChecked();
    expect(screen.getAllByLabelText("选择 b.txt")[0]).toBeChecked();
    expect(screen.getAllByLabelText("选择 子目录")[0]).not.toBeChecked();
    // 反选一个再批量下载：仅下载选中路径。
    fireEvent.click(screen.getAllByLabelText("选择 b.txt")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量下载（1）/ }));
    await waitFor(() =>
      expect(triggerBatchDownloads).toHaveBeenCalledWith([
        "/api/v2/files/9/download?path=a.txt",
      ]),
    );
    expect(await screen.findByText("已触发 1 个下载")).toBeInTheDocument();
    // 清除选择。
    fireEvent.click(screen.getByRole("button", { name: "清除选择" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /批量下载（/ })).not.toBeInTheDocument(),
    );
  });

  // ============ F1 常驻打包任务卡 ============

  it("pack card polls active tasks and stops polling when none active (F1)", async () => {
    jest.useFakeTimers();
    try {
      setPacksResponse([packView({ status: "packing", step_progress: 42 })]);
      setup((url) => {
        if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
        return {};
      });
      const card = await screen.findByLabelText("打包任务");
      expect(card.textContent).toContain("bundle.zip");
      expect(card.textContent).toContain("打包中");
      expect(card.textContent).toContain("压缩");
      expect(card.querySelector(".progress-fill")).not.toBeNull();
      const listCalls = () =>
        mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/packs?")).length;
      const initial = listCalls();
      await act(async () => {
        jest.advanceTimersByTime(2000);
      });
      await waitFor(() => expect(listCalls()).toBe(initial + 1));
      // 无进行中任务时停表。
      setPacksResponse([packView({ status: "done", step: null, step_progress: 100, output: { user_file_id: 3, display_name: "bundle.zip", size: 2048 } })]);
      await act(async () => {
        jest.advanceTimersByTime(2000);
      });
      await screen.findByText("已完成");
      const afterDone = listCalls();
      await act(async () => {
        jest.advanceTimersByTime(3000);
      });
      expect(listCalls()).toBe(afterDone);
    } finally {
      jest.useRealTimers();
    }
  });

  it("pack card cancel asks confirmation and clears finished records (F1)", async () => {
    setPacksResponse([
      packView({ id: 8, status: "packing" }),
      packView({ id: 9, status: "done", step: null, step_progress: 100, output: { user_file_id: 3, display_name: "out.zip", size: 10 } }),
    ]);
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/packs/8/cancel") return { ok: true };
      if (init?.method === "DELETE" && url === "/api/v2/packs/9/record") return { ok: true };
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    const card = await screen.findByLabelText("打包任务");
    // 取消需确认。
    fireEvent.click(within(card).getByRole("button", { name: "取消" }));
    const cancelDialog = await screen.findByRole("dialog", { name: "取消打包任务" });
    fireEvent.click(within(cancelDialog).getByRole("button", { name: "取消任务" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url, init2]) => init2?.method === "POST" && url === "/api/v2/packs/8/cancel");
      expect(call).toBeDefined();
    });
    expect(await screen.findByText("已请求取消打包任务")).toBeInTheDocument();
    // 清空已完成：确认后删除终态记录。
    fireEvent.click(within(card).getByRole("button", { name: "清空已完成" }));
    const clearDialog = await screen.findByRole("dialog", { name: "清空已完成" });
    fireEvent.click(within(clearDialog).getByRole("button", { name: "清空" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url, init2]) => init2?.method === "DELETE" && url === "/api/v2/packs/9/record");
      expect(call).toBeDefined();
    });
    expect(await screen.findByText("已清空 1 条记录")).toBeInTheDocument();
  });

  it("pack card hides when empty and refreshes the file list on completion (F1)", async () => {
    jest.useFakeTimers();
    try {
      let done = false;
      const packsModule = () => ({
        items: [
          done
            ? packView({ status: "done", step: null, step_progress: 100, output: { user_file_id: 3, display_name: "bundle.zip", size: 2048 } })
            : packView({ status: "packing" }),
        ],
      });
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url.startsWith("/api/v2/packs?")) return json(200, packsModule());
        if (url.startsWith("/api/v2/files?")) return json(200, rootList([file(1)]));
        return json(200, {});
      });
      render(
        <MemoryRouter initialEntries={["/files"]}>
          <AppProviders>
            <FilesPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByLabelText("打包任务");
      const listCalls = () =>
        mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/files?")).length;
      const before = listCalls();
      done = true;
      await act(async () => {
        jest.advanceTimersByTime(2000);
      });
      await screen.findByText("已完成");
      await waitFor(() => expect(listCalls()).toBeGreaterThan(before)); // 完成后自动刷新列表。
    } finally {
      jest.useRealTimers();
    }
  });

  it("pack dialog completes and hands over to the resident card (F1)", async () => {
    jest.useFakeTimers();
    try {
      let dialogPoll = 0;
      setup((url, init) => {
        if (url === "/api/v2/packs/precompute") {
          return { items: [], total_size: 1024, available_bytes: 10 * 1024 * 1024 * 1024 };
        }
        if (init?.method === "POST" && url === "/api/v2/packs") {
          // 创建后常驻卡将看到该任务（先进行中，完成时更新）。
          setPacksResponse([packView({ status: "pending", step: null, step_progress: 0 })]);
          return packView({ status: "pending", step: null, step_progress: 0 });
        }
        if (url === "/api/v2/packs/7") {
          dialogPoll += 1;
          if (dialogPoll === 1) {
            return packView({ status: "packing", step_progress: 42 });
          }
          setPacksResponse([
            packView({
              status: "done",
              step: null,
              step_progress: 100,
              step_started_at_ms: null,
              output: { user_file_id: 3, display_name: "bundle.zip", size: 2048 },
            }),
          ]);
          return packView({
            status: "done",
            step: null,
            step_progress: 100,
            step_started_at_ms: null,
            finished_at_ms: 3,
            output: { user_file_id: 3, display_name: "bundle.zip", size: 2048 },
          });
        }
        if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
        return {};
      });
      await screen.findAllByText("file-1.bin");
      expect(screen.queryByLabelText("打包任务")).not.toBeInTheDocument(); // 空列表隐藏。
      fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
      fireEvent.click(screen.getByRole("button", { name: /打包（1）/ }));
      await screen.findByRole("dialog", { name: "打包" });
      expect(await screen.findByText(/预估 1.00 KiB/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
      await act(async () => {
        await Promise.resolve();
      });
      await waitFor(() => {
        const createCall = mockFetch.mock.calls.find(
          ([url, init2]) => init2?.method === "POST" && url === "/api/v2/packs",
        );
        expect(createCall).toBeDefined();
      });
      // 弹窗轮询到终态自动关闭 → 常驻卡接管显示已完成任务。
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      expect(await screen.findByText(/压缩中/)).toBeInTheDocument();
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "打包" })).not.toBeInTheDocument();
      });
      const card = await screen.findByLabelText("打包任务");
      expect(card.textContent).toContain("bundle.zip");
      expect(card.textContent).toContain("已完成");
    } finally {
      jest.useRealTimers();
    }
  });

  it("rename shows 409 duplicate inline and reloads on success", async () => {
    let renameCount = 0;
    setup((url, init) => {
      if (init?.method === "PATCH") {
        renameCount += 1;
        if (renameCount === 1) {
          return json(409, { error: { code: "duplicate_resource", message: "同名文件已存在" } });
        }
        return file(1, { display_name: "新名字.bin" });
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getAllByRole("button", { name: "改名" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "重命名" });
    expect(dialog).toBeInTheDocument();
    const input = screen.getByLabelText("新名称");
    fireEvent.change(input, { target: { value: "重复名.bin" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("同名文件已存在")).toBeInTheDocument(); // 409 内联。
    expect(screen.getByRole("dialog", { name: "重命名" })).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "新名字.bin" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      const patches = mockFetch.mock.calls.filter(([, init2]) => init2?.method === "PATCH");
      expect(patches).toHaveLength(2);
      expect(JSON.parse(patches[1][1].body as string)).toEqual({ name: "新名字.bin" });
    });
    expect(await screen.findByText("已重命名")).toBeInTheDocument();
  });

  it("deletes through confirm dialog and reloads", async () => {
    setPacksResponse([
      packView({
        id: 21,
        status: "done",
        step: null,
        step_progress: 100,
        output: { user_file_id: 1, display_name: "out.zip", size: 1024 },
      }),
    ]);
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/files/1") {
        // 删除成功后后端把打包历史改为专用失败：常驻卡需自行重读。
        setPacksResponse([
          packView({
            id: 21,
            status: "failed",
            step: null,
            step_progress: 100,
            error_code: "pack_output_deleted",
            output: null,
          }),
        ]);
        return { ok: true };
      }
      if (init?.method === "DELETE") return { ok: true };
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    const cardItem = () => screen.getByTestId("pack-item-21");
    const packCalls = () =>
      mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/packs?")).length;
    await waitFor(() => expect(cardItem().textContent).toContain("已完成"));
    const packsBefore = packCalls();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "删除文件" });
    expect(dialog.textContent).toContain("删除后不可恢复");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const reopened = await screen.findByRole("dialog", { name: "删除文件" });
    fireEvent.click(within(reopened).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, init2]) => init2?.method === "DELETE");
      expect(call![0]).toBe("/api/v2/files/1");
    });
    expect(await screen.findByText("已删除")).toBeInTheDocument();
    // 同页删除后必须多一次权威重读，不得等手动 reload。
    await waitFor(() => expect(packCalls()).toBe(packsBefore + 1));
    await waitFor(() => expect(cardItem().textContent).toContain("产物已删除"));
  });

  it("creates a share and copies link with password line", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/shares") {
        return {
          id: 5,
          code: "ab12",
          file_name: "file-1.bin",
          file_size: 1024,
          file_kind: "file",
          status: "active",
          expires_at_ms: null,
          max_downloads: null,
          download_count: 0,
          created_at_ms: 1,
          has_password: true,
        };
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getAllByRole("button", { name: "分享" })[0]);
    await screen.findByRole("dialog", { name: "分享：file-1.bin" });
    fireEvent.change(screen.getByLabelText("提取密码"), { target: { value: "s3cret" } });
    fireEvent.change(screen.getByLabelText("有效期"), { target: { value: "604800" } });
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByRole("dialog", { name: "分享已创建" });
    const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/shares");
    expect(JSON.parse(call![1].body as string)).toEqual({
      user_file_id: 1,
      expires_in_seconds: 604800,
      max_downloads: null,
      password: "s3cret",
    });
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/ab12\n密码：s3cret`),
    );
  });

  it("pack flow: precompute, create, poll to done, then refresh", async () => {
    jest.useFakeTimers();
    try {
      let pollCount = 0;
      setup((url, init) => {
        if (url === "/api/v2/packs/precompute") {
          return { items: [], total_size: 3072, available_bytes: 10 * 1024 * 1024 * 1024 };
        }
        if (init?.method === "POST" && url === "/api/v2/packs") {
          return packView({ status: "pending", step: null, step_progress: 0 });
        }
        if (url === "/api/v2/packs/7") {
          pollCount += 1;
          if (pollCount === 1) {
            return packView({ status: "packing", step_progress: 42 });
          }
          return packView({
            status: "done",
            step: null,
            step_progress: 100,
            step_started_at_ms: null,
            finished_at_ms: 3,
            output: { user_file_id: 3, display_name: "bundle.zip", size: 2048 },
          });
        }
        return rootList([file(1), file(2)]);
      });
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
      fireEvent.click(screen.getAllByLabelText("选择 file-2.bin")[0]);
      fireEvent.click(screen.getByRole("button", { name: /打包（2）/ }));
      await screen.findByRole("dialog", { name: "打包" });
      expect(await screen.findByText(/预估 3.00 KiB/)).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
      await act(async () => {
        await Promise.resolve();
      });
      const createCall = mockFetch.mock.calls.find(([url, init]) => init?.method === "POST" && url === "/api/v2/packs");
      expect(JSON.parse(createCall![1].body as string)).toMatchObject({
        items: [1, 2],
        delete_source: false,
      });
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      expect(await screen.findByText(/压缩中/)).toBeInTheDocument();
      expect(screen.getByText(/进度 42%/)).toBeInTheDocument();
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "打包" })).not.toBeInTheDocument();
      });
      expect(await screen.findByText(/打包完成：bundle.zip/)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("entries load failure shows error state with retry", async () => {
    let failed = true;
    setup((url) => {
      if (url.includes("/entries")) {
        if (failed) {
          return json(500, { error: { code: "internal", message: "目录读取失败" } });
        }
        return entries([{ name: "ok.txt", path: "ok.txt", kind: "file", size: 1 }], [
          { name: "资料目录", path: "" },
        ]);
      }
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    expect(await screen.findByText("目录读取失败")).toBeInTheDocument();
    failed = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect((await screen.findAllByText("ok.txt")).length).toBeGreaterThan(0);
  });

  it("delete failure shows the API message", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") {
        return json(409, { error: { code: "content_busy", message: "文件正被使用" } });
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "删除文件" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("文件正被使用")).toBeInTheDocument();
  });

  it("deleting the last row of the last page falls back", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") return { ok: true };
      return url.includes("page=2")
        ? rootList([file(21)], 21)
        : rootList([file(1)], 21);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findAllByText("file-21.bin");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "删除文件" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("page=1");
    });
  });

  it("share dialog cancel closes without creating", async () => {
    setup(() => rootList([file(1)]));
    await screen.findAllByText("file-1.bin");
    fireEvent.click(screen.getAllByRole("button", { name: "分享" })[0]);
    await screen.findByRole("dialog", { name: "分享：file-1.bin" });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("row checkbox toggles off again", async () => {
    setup(() => rootList([file(1)]));
    await screen.findAllByText("file-1.bin");
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    expect(screen.getAllByLabelText("选择 file-1.bin")[0]).not.toBeChecked();
  });

  it("multi-level breadcrumbs render separators and current crumb", async () => {
    setup((url) => {
      if (url.includes("/entries")) {
        if (url.includes("path=%E5%AD%90%E7%9B%AE%E5%BD%95") || url.includes("path=子目录")) {
          return entries([], [
            { name: "资料目录", path: "" },
            { name: "子目录", path: "子目录" },
          ]);
        }
        return entries(
          [{ name: "子目录", path: "子目录", kind: "directory", size: 0 }],
          [{ name: "资料目录", path: "" }],
        );
      }
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    await screen.findByRole("button", { name: "进入子目录 子目录" });
    fireEvent.click(screen.getByRole("button", { name: "进入子目录 子目录" }));
    await waitFor(() => {
      expect(screen.getByText("空目录")).toBeInTheDocument();
    });
    const crumbs = screen.getAllByRole("button", { name: "子目录" })[0];
    expect(crumbs).toHaveAttribute("aria-current", "page");
    expect(screen.getByLabelText("目录路径").textContent).toContain("/");
  });

  it("discards stale search responses as the keyword evolves", async () => {
    jest.useFakeTimers();
    try {
      let searchCalls = 0;
      const holder: { resolve?: (value: unknown) => void } = {};
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url.startsWith("/api/v2/packs?")) return json(200, { items: [] });
        if (url.includes("search=")) {
          searchCalls += 1;
          if (searchCalls === 1) {
            return new Promise((resolve) => {
              holder.resolve = resolve;
            }) as never;
          }
          return json(200, {
            hits: [{ user_file_id: 2, display_name: "b.bin", path: "", kind: "file", size: 1 }],
            truncated: false,
          });
        }
        return json(200, rootList([file(1)]));
      });
      render(
        <MemoryRouter initialEntries={["/files"]}>
          <AppProviders>
            <FilesPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "a" } });
      await act(async () => {
        jest.advanceTimersByTime(500);
      });
      expect(searchCalls).toBe(1); // 首个搜索挂起。
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "ab" } });
      await act(async () => {
        jest.advanceTimersByTime(500);
      });
      expect(searchCalls).toBe(2);
      await waitFor(() => expect(screen.getByText("b.bin")).toBeInTheDocument());
      // 旧搜索响应迟到：不得覆盖新结果。
      holder.resolve?.(json(200, {
        hits: [{ user_file_id: 99, display_name: "stale.bin", path: "", kind: "file", size: 1 }],
        truncated: false,
      }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.queryByText("stale.bin")).not.toBeInTheDocument();
      expect(screen.getByText("b.bin")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("search failure shows error message in the panel", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return json(429, { error: { code: "rate_limited", message: "搜索太频繁" } });
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(await screen.findByText("搜索太频繁")).toBeInTheDocument();
  });

  it("locates a directory hit by browsing into it", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [
            { user_file_id: 9, display_name: "资料目录", path: "", kind: "directory", size: 0 },
          ],
          truncated: false,
        };
      }
      if (url.includes("/entries")) {
        return entries(
          [{ name: "inner", path: "inner", kind: "directory", size: 0 }],
          [{ name: "资料目录", path: "" }],
        );
      }
      return rootList([file(1)]);
    });
    await (await screen.findAllByText("file-1.bin"))[0];
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "资料" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("option"));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => String(url).includes("/entries"));
      expect(call).toBeDefined(); // 目录命中直接进入浏览。
    });
    expect(await screen.findByRole("button", { name: "进入子目录 inner" })).toBeInTheDocument();
  });

  it("mobile card layout exists alongside the desktop table", async () => {
    setup(() => rootList([file(1)]));
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    expect(document.querySelector("table.files-table.desktop-only")).not.toBeNull();
    const cards = document.querySelector("ul.file-card-list.mobile-only");
    expect(cards).not.toBeNull();
    expect(cards!.textContent).toContain("file-1.bin");
  });
});

describe("PackTasksCard 补充分支（F1）", () => {
  it("shows ETA for packing steps and the queued hint for pending packs", async () => {
    setPacksResponse([
      packView({ id: 11, status: "packing", step: "compressing", step_progress: 50, step_started_at_ms: Date.now() - 4000 }),
      packView({ id: 12, status: "pending", step: null, step_progress: 0, step_started_at_ms: null }),
    ]);
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    const card = await screen.findByLabelText("打包任务");
    const item11 = screen.getByTestId("pack-item-11");
    expect(item11.textContent).toContain("已用 0:04 / 预计剩余 0:04");
    const item12 = screen.getByTestId("pack-item-12");
    expect(item12.textContent).toContain("排队中");
    void card;
  });

  it("shows step placeholder when progress is unavailable", async () => {
    setPacksResponse([
      packView({ id: 13, status: "packing", step_progress: 0, step_started_at_ms: Date.now() }),
    ]);
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    await screen.findByTestId("pack-item-13");
    expect(screen.getByTestId("pack-item-13").textContent).toContain("已用 -- / 预计剩余 --");
  });

  it("terminal packs expose record deletion with per-item feedback", async () => {
    setPacksResponse([
      packView({ id: 14, status: "failed", step: null, error_code: "upstream_failure" }),
      packView({ id: 15, status: "cancelled", step: null }),
      packView({ id: 16, status: "done", step: null, step_progress: 100, delete_source: true, output: { user_file_id: 3, display_name: "out.zip", size: 10 } }),
    ]);
    setup((url, init) => {
      if (init?.method === "DELETE") {
        if (url === "/api/v2/packs/14/record") {
          return json(409, { error: { code: "invalid_state", message: "记录不可删除" } });
        }
        return { ok: true };
      }
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    await screen.findByTestId("pack-item-16");
    expect(screen.getByTestId("pack-item-14").textContent).toContain("upstream_failure");
    expect(screen.getByTestId("pack-item-16").textContent).toContain("已删除源文件");
    // 删除记录：失败与成功各自反馈。
    fireEvent.click(within(screen.getByTestId("pack-item-14")).getByRole("button", { name: "删除记录" }));
    expect(await screen.findByText("记录不可删除")).toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId("pack-item-15")).getByRole("button", { name: "删除记录" }));
    expect(await screen.findByText("已删除任务记录")).toBeInTheDocument();
  });

  it("labels a finalized pack output as deleted instead of a plain failure (F1)", async () => {
    setPacksResponse([
      packView({ id: 30, status: "failed", step: null, step_progress: 0, error_code: "pack_output_deleted" }),
    ]);
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    const item = await screen.findByTestId("pack-item-30");
    const badge = item.querySelector(".badge");
    expect(badge?.textContent).toBe("产物已删除");
    expect(badge?.className).not.toContain("tone-error");
    expect(item.textContent).not.toContain("失败");
    expect(item.textContent).not.toContain("错误码");
    expect(item.textContent).not.toContain("pack_output_deleted");
    expect(within(item).getByRole("button", { name: "删除记录" })).toBeInTheDocument();
  });

  it("labels a shared pack output already removed for this user as deleted (F1)", async () => {
    setPacksResponse([
      packView({ id: 31, status: "done", step: null, step_progress: 100, output: null }),
    ]);
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    const item = await screen.findByTestId("pack-item-31");
    const badge = item.querySelector(".badge");
    expect(badge?.textContent).toBe("产物已删除");
    expect(badge?.className).not.toContain("tone-error");
    expect(item.textContent).not.toContain("已完成");
    expect(item.textContent).not.toContain("输出：0 B");
    expect(within(item).getByRole("button", { name: "删除记录" })).toBeInTheDocument();
  });

  it("cancel failure surfaces the API message and clear reports partial failure", async () => {
    setPacksResponse([
      packView({ id: 17, status: "packing" }),
      packView({ id: 18, status: "done", step: null, step_progress: 100, output: { user_file_id: 3, display_name: "a.zip", size: 1 } }),
      packView({ id: 19, status: "cancelled", step: null }),
    ]);
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/packs/17/cancel") {
        return json(409, { error: { code: "invalid_state", message: "任务已结束" } });
      }
      if (init?.method === "DELETE") {
        if (url === "/api/v2/packs/19/record") {
          return json(500, { error: { code: "internal", message: "后端错误" } });
        }
        return { ok: true };
      }
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    const card = await screen.findByLabelText("打包任务");
    fireEvent.click(within(screen.getByTestId("pack-item-17")).getByRole("button", { name: "取消" }));
    const dialog = await screen.findByRole("dialog", { name: "取消打包任务" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消任务" }));
    expect(await screen.findByText("任务已结束")).toBeInTheDocument();

    fireEvent.click(within(card).getByRole("button", { name: "清空已完成" }));
    const clearDialog = await screen.findByRole("dialog", { name: "清空已完成" });
    fireEvent.click(within(clearDialog).getByRole("button", { name: "清空" }));
    expect(await screen.findByText("已清空 1 条记录，1 条清空失败")).toBeInTheDocument();
  });
});

describe("FilesPage 补充分支", () => {
  it("batch delete with all success reports the success toast (F6)", async () => {
    setPacksResponse([
      packView({
        id: 22,
        status: "done",
        step: null,
        step_progress: 100,
        output: { user_file_id: 3, display_name: "batch.zip", size: 2048 },
      }),
    ]);
    let deletes = 0;
    setup((url, init) => {
      if (init?.method === "DELETE") {
        deletes += 1;
        if (deletes === 1) {
          setPacksResponse([
            packView({
              id: 22,
              status: "failed",
              step: null,
              step_progress: 100,
              error_code: "pack_output_deleted",
              output: null,
            }),
          ]);
        }
        return { ok: true };
      }
      return rootList([file(1), file(2)]);
    });
    await screen.findAllByText("file-1.bin");
    const packCalls = () =>
      mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/packs?")).length;
    await waitFor(() => expect(screen.getByTestId("pack-item-22").textContent).toContain("已完成"));
    const packsBefore = packCalls();
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 file-2.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量删除（2）/ }));
    fireEvent.click(
      within(await screen.findByRole("dialog", { name: "批量删除" })).getByRole("button", { name: "删除" }),
    );
    expect(await screen.findByText("已删除 2 个文件")).toBeInTheDocument();
    // 批量删除只触发一次权威重读，不按项重复。
    await waitFor(() => expect(packCalls()).toBe(packsBefore + 1));
    await waitFor(() =>
      expect(screen.getByTestId("pack-item-22").textContent).toContain("产物已删除"),
    );
    expect(deletes).toBe(2);
  });

  it("batch delete with all failures keeps the pack card unchanged (F6)", async () => {
    setPacksResponse([
      packView({
        id: 23,
        status: "done",
        step: null,
        step_progress: 100,
        output: { user_file_id: 3, display_name: "keep.zip", size: 2048 },
      }),
    ]);
    let deletes = 0;
    setup((url, init) => {
      if (init?.method === "DELETE") {
        deletes += 1;
        return json(500, { error: { code: "internal", message: "删除失败" } });
      }
      return rootList([file(1), file(2)]);
    });
    await screen.findAllByText("file-1.bin");
    const packItem = () => screen.getByTestId("pack-item-23");
    const packCalls = () =>
      mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/packs?")).length;
    await waitFor(() => expect(packItem().textContent).toContain("已完成"));
    const packsBefore = packCalls();
    fireEvent.click(screen.getAllByLabelText("选择 file-1.bin")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 file-2.bin")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量删除（2）/ }));
    fireEvent.click(
      within(await screen.findByRole("dialog", { name: "批量删除" })).getByRole("button", { name: "删除" }),
    );
    expect(await screen.findByText("已删除 0 个文件，2 个删除失败")).toBeInTheDocument();
    await act(async () => {
      await Promise.resolve();
    });
    // deleted=0 不得触发打包卡重读，卡片保持原状态。
    expect(packCalls()).toBe(packsBefore);
    expect(deletes).toBe(2);
    expect(packItem().textContent).toContain("已完成");
    expect(packItem().textContent).toContain("输出：2.00 KiB");
    expect(packItem().textContent).not.toContain("产物已删除");
  });

  it("entry selection toggles off and entry sort headers cycle (F7/F3)", async () => {
    setup((url) => {
      if (url.includes("/entries")) {
        return entries(
          [
            { name: "b.txt", path: "b.txt", kind: "file", size: 6 },
            { name: "a.txt", path: "a.txt", kind: "file", size: 5 },
          ],
          [{ name: "资料目录", path: "" }],
        );
      }
      return rootList([DIR]);
    });
    await screen.findByRole("button", { name: "打开文件夹 资料目录" });
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹 资料目录" }));
    await screen.findAllByText("a.txt");
    // 勾选再取消。
    fireEvent.click(screen.getAllByLabelText("选择 a.txt")[0]);
    fireEvent.click(screen.getAllByLabelText("选择 a.txt")[0]);
    expect(screen.getAllByLabelText("选择 a.txt")[0]).not.toBeChecked();
    // 目录内排序：名称升序。
    const firstName = () =>
      document.querySelector("table.files-table tbody tr td.name-cell")?.textContent;
    expect(firstName()).toBe("b.txt");
    fireEvent.click(screen.getByRole("button", { name: "名称排序" }));
    expect(firstName()).toBe("a.txt");
  });

  it("locate scan fetch failure shows the error toast (F5)", async () => {
    let searchDone = false;
    setup((url) => {
      if (url.includes("search=")) {
        searchDone = true;
        return {
          hits: [{ user_file_id: 25, display_name: "file-25.bin", path: "", kind: "file", size: 1 }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?") && searchDone) {
        return json(500, { error: { code: "internal", message: "列表读取失败" } });
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "file-25" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("option"));
    expect(await screen.findByText("列表读取失败")).toBeInTheDocument();
  });
});

describe("PackTasksCard 边缘分支", () => {
  it("falls back to the generic title when no names are known", async () => {
    setPacksResponse([
      packView({ id: 20, status: "packing", output_name: "", output: null }),
    ]);
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    await screen.findByTestId("pack-item-20");
    expect(screen.getByTestId("pack-item-20").textContent).toContain("打包任务 #20");
  });

  it("shows the placeholder when a step just started (elapsed 0)", async () => {
    setPacksResponse([
      packView({ id: 21, status: "packing", step_progress: 50, step_started_at_ms: Date.now() }),
    ]);
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1)]);
      return {};
    });
    await screen.findByTestId("pack-item-21");
    expect(screen.getByTestId("pack-item-21").textContent).toContain("已用 -- / 预计剩余 --");
  });
});
