import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { FilesPage } from "@/features/files/FilesPage";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
  id: 1, username: "admin", is_admin: true, quota_bytes: 1,
  is_initial_password: false, pending_delete: false,
  used_bytes: 0, reserved_bytes: 0, available_bytes: 1,
};

function fileItem(id: number, name: string, overrides: Record<string, unknown> = {}) {
  return { id, display_name: name, kind: "file", size: id * 100, created_at_ms: id, ...overrides };
}

function filesEnvelope(items: unknown[], total = items.length) {
  return {
    items,
    total,
    page: 1,
    page_size: 10,
    space: { quota_bytes: 1000, used_bytes: 100, reserved_bytes: 50, available_bytes: 850, limited: false },
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") {
      return json(200, { site_title: "Aria2Deck", ws_reconnect_max_delay_seconds: 30, ws_reconnect_jitter_seconds: 1, ws_reconnect_factor: 2 });
    }
    // 打包任务卡常驻轮询：未显式处理时给空列表，避免落到 handler 的默认文件响应。
    if (url.startsWith("/api/v2/packs?") && (!init?.method || init.method === "GET")) {
      return json(200, { items: [] });
    }
    return json(200, handler(url, init));
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/files"]}>
      <AppProviders>
        <FilesPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

beforeEach(() => {
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = jest.fn();
  }
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

describe("FilesPage（V1 文件页）", () => {
  it("渲染根列表（10 条页大小、目录在前）", async () => {
    const items = [
      fileItem(1, "b.txt"),
      fileItem(2, "设计素材", { kind: "directory", size: 6656 }),
      fileItem(3, "a.txt"),
    ];
    setup((url) => (url.startsWith("/api/v2/files") ? filesEnvelope(items, 12) : {}));
    renderPage();
    await screen.findByText("b.txt");
    // 目录排在前
    const names = Array.from(document.querySelectorAll(".file-name-btn, .table-row .text-base")).map((el) => el.textContent);
    expect(names[0]).toBe("设计素材");
    expect(screen.getByText("共 12 项")).toBeInTheDocument();
    expect(screen.queryByText("850.0 B")).not.toBeInTheDocument();
  });

  it("空列表渲染空状态且打包记录入口常显", async () => {
    setup((url) => (url.startsWith("/api/v2/files") ? filesEnvelope([]) : {}));
    renderPage();
    expect(await screen.findByText("暂无文件")).toBeInTheDocument();
    const entry = screen.getByRole("button", { name: /打包记录/ });
    expect(entry).toBeEnabled();
    fireEvent.click(entry);
    expect(await screen.findByRole("dialog", { name: "打包记录" })).toBeInTheDocument();
    expect(await screen.findByText("还没有打包记录")).toBeInTheDocument();
  });

  it("加载失败显示错误", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files")) return { status: 500, ok: false, json: () => Promise.resolve({}) };
      return {};
    });
    renderPage();
    expect(await screen.findByText(/请求失败|加载/)).toBeInTheDocument();
  });


  it("搜索：弹窗展示结果，根命中定位到目标页", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return { hits: [{ user_file_id: 7, display_name: "目标文件.txt", path: "目标文件.txt", kind: "file", size: 10 }], truncated: false };
      }
      if (url.startsWith("/api/v2/files?")) {
        const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
        return page === 1 ? filesEnvelope([fileItem(8, "占位.txt")], 25) : filesEnvelope([fileItem(7, "目标文件.txt")], 25);
      }
        return {};
      });
    renderPage();
    await screen.findByText("占位.txt");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "目标" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “目标”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    // 逐页扫描应翻到目标所在页（请求带 page 参数）
    await waitFor(() => {
      const pages = mockFetch.mock.calls
        .map(([u]) => String(u))
        .filter((u) => u.startsWith("/api/v2/files?"))
        .map((u) => new URL(u, "http://localhost").searchParams.get("page"));
      expect(pages).toContain("2");
    });
  });

  it("重命名流程", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files/1" && init?.method === "PATCH") {
        return { id: 1, display_name: "新名字.txt", kind: "file", size: 100, created_at_ms: 1 };
      }
      return filesEnvelope([fileItem(1, "旧名字.txt")]);
    });
    renderPage();
    await screen.findByText("旧名字.txt");
    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    const input = await screen.findByLabelText("重命名文件");
    fireEvent.change(input, { target: { value: "新名字.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([u, i]) => String(u) === "/api/v2/files/1" && i?.method === "PATCH");
      expect(call).toBeDefined();
      expect(JSON.parse(call![1].body as string)).toEqual({ name: "新名字.txt" });
    });
  });

  it("单删确认后调用批量端点", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return { accepted_count: 1, rejected_count: 0, results: [{ file_id: 1, accepted: true, error: null }] };
      }
      return filesEnvelope([fileItem(1, "待删.txt")]);
    });
    renderPage();
    await screen.findByText("待删.txt");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent('确定要删除文件 "待删.txt" 吗？');
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([u, i]) => String(u) === "/api/v2/files" && i?.method === "DELETE");
      expect(call).toBeDefined();
      expect(JSON.parse(call![1].body as string)).toEqual({ file_ids: [1] });
    });
  });

  it("选中后出现批量操作；批量删除确认后刷新", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return { accepted_count: 1, rejected_count: 0, results: [{ file_id: 1, accepted: true, error: null }] };
      }
      return filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    const bulkBar = document.querySelector(".bulk-bar") as HTMLElement;
    expect(bulkBar).toHaveTextContent("已选 1 项");
    expect(document.querySelector(".filter-toolbar")).not.toHaveTextContent("已选");
    fireEvent.click(within(bulkBar).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await screen.findByText("已删除 1 个文件");
  });

  it("打包弹窗：预计算展示与默认输出名", async () => {
    setup((url, init) => {
      if (url === "/api/v2/packs/precompute") {
        return {
          items: [{ user_file_id: 1, accepted: true, display_name: "a.txt", size: 100, error: null }],
          total_size: 100, available_bytes: 1000,
        };
      }
      if (url === "/api/v2/packs" && init?.method === "POST") {
        return { id: 9, status: "pending", step: null, step_progress: 0, step_started_at_ms: null, created_at_ms: 1, updated_at_ms: 1, finished_at_ms: null, error_code: null, delete_source: false, output_name: "archive.zip", output: null };
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    const bulkBar = document.querySelector(".bulk-bar") as HTMLElement;
    fireEvent.click(within(bulkBar).getByRole("button", { name: "打包" }));
    await screen.findByText((_, el) => el?.textContent === "预估大小: 100.0 B");
    fireEvent.click(await screen.findByRole("button", { name: "确认打包" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([u, i]) => String(u) === "/api/v2/packs" && i?.method === "POST");
      expect(call).toBeDefined();
      expect(JSON.parse(call![1].body as string)).toEqual({ items: [1], output_name: "archive.zip", delete_source: false });
    });
    await waitFor(() => {
      const reads = mockFetch.mock.calls.filter(([u]) => u === "/api/v2/packs?limit=50");
      expect(reads).toHaveLength(2);
    });
    expect(document.querySelector(".bulk-bar")).toBeNull();
  });

  it("分享弹窗创建后展示链接", async () => {
    setup((url, init) => {
      if (url === "/api/v2/shares" && init?.method === "POST") {
        return { id: 5, code: "Ab3dEf9h", file_name: "a.txt", file_size: 100, file_kind: "file", status: "active", expires_at_ms: null, max_downloads: null, download_count: 0, created_at_ms: 1, has_password: false };
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getByRole("button", { name: "分享" }));
    fireEvent.click(await screen.findByRole("button", { name: "创建分享" }));
    const link = await screen.findByLabelText("分享链接");
    expect((link as HTMLInputElement).value).toContain("/s/Ab3dEf9h");
  });
});
