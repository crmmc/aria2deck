import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { FilesPage } from "@/features/files/FilesPage";
import type { FileItemResponse, UserResponse } from "@/shared/api/types";

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
  reserved_bytes: 0,
  available_bytes: 10 * 1024 * 1024 * 1024 - 2048,
};

function file(id: number, overrides: Partial<FileItemResponse> = {}): FileItemResponse {
  return {
    id,
    display_name: `file-${id}.bin`,
    kind: "file",
    size: 1024 * id,
    created_at_ms: 1700000000000,
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
      reserved_bytes: 0,
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

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
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
  it("renders root list with space summary and pagination", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) return rootList([file(1), DIR]);
      return {};
    });
    expect((await screen.findAllByText("file-1.bin"))[0]).toBeInTheDocument();
    expect(screen.getByText(/已用 2.00 KiB/)).toBeInTheDocument();
    expect(screen.getByText(/已用 2\.00 KiB · 可用 10\.00 GiB/)).toBeInTheDocument();
    expect(screen.getByText("第 1 / 1 页 · 共 2 条")).toBeInTheDocument();
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

  it("debounces search, warns truncation and locates an entry hit", async () => {
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
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "report" } });
      await act(async () => {
        jest.advanceTimersByTime(350);
      });
      expect(await screen.findByText("结果超过 200 条，仅显示前 200 条，请细化关键词")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("option"));
      await waitFor(() => {
        const call = mockFetch.mock.calls.find(([url]) => String(url).includes("path=docs"));
        expect(call).toBeDefined(); // 定位跳转：进入命中条目的父目录。
      });
    } finally {
      jest.useRealTimers();
    }
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
    const hit = await screen.findByRole("option");
    fireEvent.click(hit);
    const row = await waitFor(() => {
      const el = document.querySelector('[data-file-id="1"]');
      expect(el?.className).toContain("row-highlight");
      return el;
    });
    expect(row).toBeDefined();
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

  it("locate miss on page 1 shows a toast instead of silent failure", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [
            { user_file_id: 77, display_name: "别处的文件.bin", path: "", kind: "file", size: 1 },
          ],
          truncated: false,
        };
      }
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "别处" } });
    fireEvent.click(await screen.findByRole("option"));
    expect(await screen.findByText("已定位到根列表，该文件不在第 1 页")).toBeInTheDocument();
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
    setup((url, init) => {
      if (init?.method === "DELETE") return { ok: true };
      return rootList([file(1)]);
    });
    await screen.findAllByText("file-1.bin"); // 等待渲染（双布局出现两处）
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "删除文件" });
    expect(dialog.textContent).toContain("删除后不可恢复");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除文件" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, init2]) => init2?.method === "DELETE");
      expect(call![0]).toBe("/api/v2/files/1");
    });
    expect(await screen.findByText("已删除")).toBeInTheDocument();
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
          return {
            id: 7, status: "pending", step: null, step_progress: 0,
            step_started_at_ms: null, created_at_ms: 1, updated_at_ms: 1,
            finished_at_ms: null, error_code: null, delete_source: false, output: null,
          };
        }
        if (url === "/api/v2/packs/7") {
          pollCount += 1;
          if (pollCount === 1) {
            return {
              id: 7, status: "packing", step: "compressing", step_progress: 42,
              step_started_at_ms: 2, created_at_ms: 1, updated_at_ms: 2,
              finished_at_ms: null, error_code: null, delete_source: false, output: null,
            };
          }
          return {
            id: 7, status: "done", step: null, step_progress: 100,
            step_started_at_ms: null, created_at_ms: 1, updated_at_ms: 3,
            finished_at_ms: 3, error_code: null, delete_source: false,
            output: { user_file_id: 3, display_name: "bundle.zip", size: 2048 },
          };
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
    await screen.findByRole("dialog", { name: "删除文件" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
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
    await screen.findByRole("dialog", { name: "删除文件" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
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
      let releaseFirst: (value: unknown) => void = () => undefined;
      const firstSearch = new Promise((resolve) => {
        releaseFirst = resolve;
      });
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url.includes("search=")) {
          searchCalls += 1;
          if (searchCalls === 1) return firstSearch as never;
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
        jest.advanceTimersByTime(350);
      });
      expect(searchCalls).toBe(1); // 首个搜索挂起。
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "ab" } });
      await act(async () => {
        jest.advanceTimersByTime(350);
      });
      expect(searchCalls).toBe(2);
      await waitFor(() => expect(screen.getByText("b.bin")).toBeInTheDocument());
      // 旧搜索响应迟到：不得覆盖新结果。
      releaseFirst(json(200, {
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
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("search=")) {
          return json(429, { error: { code: "rate_limited", message: "搜索太频繁" } });
        }
        return rootList([file(1)]);
      });
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "x" } });
      await act(async () => {
        jest.advanceTimersByTime(350);
      });
      expect(await screen.findByText("搜索太频繁")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
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
