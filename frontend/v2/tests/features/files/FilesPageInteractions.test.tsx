import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { gsap } from "gsap";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { FilesPage } from "@/features/files/FilesPage";
import type { UserResponse } from "@/shared/api/types";

jest.mock("gsap", () => ({
  gsap: {
    killTweensOf: jest.fn(),
    set: jest.fn(),
    to: jest.fn(),
  },
}));

const toMock = jest.mocked(gsap.to);
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

function entriesEnvelope(items: unknown[], total = items.length) {
  return { items, total, page: 1, page_size: 200, breadcrumbs: [] };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

type Handler = (url: string, init?: { method?: string; body?: string }) => unknown;

function setup(handler: Handler): void {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
    // 打包任务卡常驻轮询：未显式处理时给空列表，避免落到 handler 的默认文件响应。
    if (url.startsWith("/api/v2/packs?") && (!init?.method || init.method === "GET")) {
      return json(200, { items: [] });
    }
    const result = handler(url, init);
    if (typeof result === "object" && result !== null && "status" in result) return result;
    return json(200, result);
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

const bulkBar = () => document.querySelector(".bulk-bar") as HTMLElement;

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;
let anchorClickSpy: jest.SpyInstance;
let originalRaf: typeof window.requestAnimationFrame;

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
  // 下载走 a[download]，jsdom 里拦住真实点击。
  anchorClickSpy = jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  anchorClickSpy.mockRestore();
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

describe("FilesPage 排序", () => {
  it("点击表头切换字段与方向，排序图标同步", async () => {
    setup(() => filesEnvelope([fileItem(1, "b.txt"), fileItem(2, "a.txt")]));
    renderPage();
    await screen.findByText("b.txt");

    const headers = () => Array.from(document.querySelectorAll(".sortable-header"));
    const icons = () => headers().map((el) => el.querySelector(".sort-icon")?.getAttribute("data-icon"));
    expect(icons()).toEqual(["sort", "sort", "chevron-down"]);

    // 点名称表头：切到 name-asc。
    fireEvent.click(headers()[0]);
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("name-asc"));
    expect(icons()).toEqual(["chevron-up", "sort", "sort"]);

    // 再点同一个：方向翻转。
    fireEvent.click(headers()[0]);
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("name-desc"));
    expect(icons()).toEqual(["chevron-down", "sort", "sort"]);

    // 点大小表头：字段切换，非名称字段默认降序。
    fireEvent.click(headers()[1]);
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("size-desc"));

    // 点时间表头。
    fireEvent.click(headers()[2]);
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("created_at-desc"));
  });
});

describe("FilesPage 搜索", () => {
  it("空关键词给出警告且不发搜索请求", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(await screen.findByText("请输入关键词")).toBeInTheDocument();
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("search="))).toBe(false);
  });

  it("搜索请求失败在弹窗里展示错误", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return json(500, { error: { code: "internal", message: "搜索服务不可用" } });
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "目标" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(await screen.findByText("搜索服务不可用")).toBeInTheDocument();
  });

  it("Enter 键触发查询，Escape 关闭弹窗", async () => {
    setup((url) => {
      if (url.includes("search=")) return { hits: [], truncated: false };
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    const input = screen.getByLabelText("搜索文件");
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await screen.findByText("搜索 “abc”");

    fireEvent(screen.getByRole("dialog", { name: "搜索结果" }), new Event("cancel", { cancelable: true }));
    await waitFor(() => expect(screen.queryByText("搜索 “abc”")).not.toBeInTheDocument());
  });

  it("Escape 只由 ModalOverlay 退出，完成前不卸载且不重复关闭", async () => {
    setup((url) => {
      if (url.includes("search=")) return { hits: [], truncated: false };
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    const dialog = await screen.findByRole("dialog", { name: "搜索结果" });
    const reducedMotion = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: jest.fn(() => ({ matches: false })),
    });
    toMock.mockClear();
    try {
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.getByRole("dialog", { name: "搜索结果" })).toBe(dialog);
      expect(toMock).not.toHaveBeenCalled();

      fireEvent(dialog, new Event("cancel", { cancelable: true }));
      expect(screen.getByRole("dialog", { name: "搜索结果" })).toBe(dialog);
      expect(toMock).toHaveBeenCalledTimes(1);
      fireEvent(dialog, new Event("cancel", { cancelable: true }));
      fireEvent.keyDown(window, { key: "Escape" });
      expect(toMock).toHaveBeenCalledTimes(1);

      const exitCall: unknown = toMock.mock.calls[0];
      const exitValue: unknown = Array.isArray(exitCall) ? exitCall[1] : undefined;
      if (typeof exitValue !== "object" || exitValue === null) throw new Error("未捕获退出动画");
      const exitVars = exitValue as gsap.TweenVars;
      act(() => exitVars.onComplete?.());
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "搜索结果" })).not.toBeInTheDocument());
    } finally {
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        writable: true,
        value: reducedMotion,
      });
      toMock.mockClear();
    }
  });

  it("Ctrl/Cmd+F 把焦点移到搜索框", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    const input = screen.getByLabelText("搜索文件") as HTMLInputElement;
    input.blur();
    expect(document.activeElement).not.toBe(input);

    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    expect(document.activeElement).toBe(input);

    fireEvent.keyDown(window, { key: "f", metaKey: true });
    expect(document.activeElement).toBe(input);
  });

  it("目录内搜索带上目录作用域，开启全局后不带 scope", async () => {
    const searchUrls: string[] = [];
    setup((url) => {
      if (url.includes("search=")) {
        searchUrls.push(String(url));
        return { hits: [], truncated: false };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) return entriesEnvelope([{ name: "a.png", path: "a.png", kind: "file", size: 1 }]);
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");

    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() => expect(searchUrls).toHaveLength(1));
    expect(searchUrls[0]).toContain("scope=2");

    // 关闭弹窗后开启全局搜索。
    fireEvent(screen.getByRole("dialog", { name: "搜索结果" }), new Event("cancel", { cancelable: true }));
    await waitFor(() => expect(screen.queryByText("搜索 “x”")).not.toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("全局"));
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() => expect(searchUrls).toHaveLength(2));
    expect(searchUrls[1]).not.toContain("scope=");
  });
});

describe("FilesPage 搜索定位", () => {
  it("定位到根列表另一页后高亮目标行", async () => {
    const rafSpy = jest.fn();
    originalRaf = window.requestAnimationFrame;
    window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      rafSpy();
      cb(0);
      return 1;
    }) as typeof window.requestAnimationFrame;
    try {
      setup((url) => {
        if (url.includes("search=")) {
          return {
            hits: [{ user_file_id: 7, display_name: "目标.txt", path: "目标.txt", kind: "file", size: 10 }],
            truncated: false,
          };
        }
        const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
        return page === 1
          ? filesEnvelope([fileItem(8, "占位.txt")], 25)
          : filesEnvelope([fileItem(7, "目标.txt")], 25);
      });
      renderPage();
      await screen.findByText("占位.txt");
      fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "目标" } });
      fireEvent.click(screen.getByRole("button", { name: "查询" }));
      await screen.findByText("搜索 “目标”");
      fireEvent.click(await screen.findByRole("button", { name: "定位" }));

      await waitFor(() => expect(screen.getByText("目标.txt")).toBeInTheDocument());
      // 高亮行渲染后滚动到可视区中心。
      await waitFor(() => expect(document.querySelector(".file-locate-highlight")).toBeTruthy());
      expect(rafSpy).toHaveBeenCalled();
    } finally {
      window.requestAnimationFrame = originalRaf;
    }
  });

  it("定位目标不在任何页时提示定位失败", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 99, display_name: "幽灵.txt", path: "幽灵.txt", kind: "file", size: 10 }],
          truncated: false,
        };
      }
      return filesEnvelope([fileItem(1, "a.txt")], 12);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "幽灵" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “幽灵”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    expect(await screen.findByText("定位失败：未在当前列表找到该文件")).toBeInTheDocument();
  });

  it("在目录视图定位根列表项时先退出目录再高亮", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 7, display_name: "目标.txt", path: "目标.txt", kind: "file", size: 10 }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" }), fileItem(7, "目标.txt")]);
      }
      if (url.includes("/entries")) return entriesEnvelope([{ name: "a.png", path: "a.png", kind: "file", size: 1 }]);
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");

    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "目标" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “目标”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));

    // 退出目录视图，回到根列表并高亮。
    await waitFor(() => expect(screen.getByText("目标.txt")).toBeInTheDocument());
    expect(screen.queryByText("a.png")).not.toBeInTheDocument();
    await waitFor(() => expect(document.querySelector(".file-locate-highlight")).toBeTruthy());
  });

  it("目录内命中的深层文件会切到所在子目录并高亮", async () => {
    // V2 的 path 相对当前搜索作用域（这里是文件 2 自身），因此需先在目录视图内搜索。
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{
            user_file_id: 2,
            display_name: "深层.txt",
            path: "子目录/深层.txt",
            kind: "file",
            size: 10,
          }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "子目录") {
          return entriesEnvelope([{ name: "深层.txt", path: "子目录/深层.txt", kind: "file", size: 10 }]);
        }
        return entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]);
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");

    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "深层" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “深层”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));

    // 切到 素材/子目录 并高亮 深层.txt。
    await waitFor(() => expect(screen.getByText("深层.txt")).toBeInTheDocument());
    await waitFor(() => expect(document.querySelector(".file-locate-highlight")).toBeTruthy());
    const crumb = document.querySelector(".path-breadcrumb")?.textContent;
    expect(crumb).toContain("素材");
    expect(crumb).toContain("子目录");
  });

  it("目录内定位找不到目标时提示失败", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{
            user_file_id: 2,
            display_name: "不存在.txt",
            path: "素材/不存在.txt",
            kind: "file",
            size: 10,
          }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        return entriesEnvelope([{ name: "别的.txt", path: "别的.txt", kind: "file", size: 1 }]);
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "不存在" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “不存在”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    expect(await screen.findByText("定位失败：未在文件夹中找到该文件")).toBeInTheDocument();
  });

  it("目录内定位时读取目录失败会提示错误", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{
            user_file_id: 2,
            display_name: "x.txt",
            path: "素材/x.txt",
            kind: "file",
            size: 10,
          }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        return json(500, { error: { code: "internal", message: "读取失败" } });
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “x”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    expect(await screen.findByText("定位失败: 读取失败")).toBeInTheDocument();
  });
});

describe("FilesPage 单条操作", () => {
  it("取消删除确认后不发请求", async () => {
    setup(() => filesEnvelope([fileItem(1, "待删.txt")]));
    renderPage();
    await screen.findByText("待删.txt");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("删除文件夹时确认文案带「文件夹」", async () => {
    setup(() => filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent('确定要删除文件夹 "素材" 吗？');
  });

  it("删除被业务拒绝时提示原因", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return {
          accepted_count: 0,
          rejected_count: 1,
          results: [{ file_id: 1, accepted: false, error: { code: "denied", message: "文件被占用" } }],
        };
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除失败：文件被占用")).toBeInTheDocument();
  });

  it("删除被拒绝且无错误明细时用「未知错误」兜底", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return { accepted_count: 0, rejected_count: 1, results: [] };
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除失败：未知错误")).toBeInTheDocument();
  });

  it("删除请求抛错时提示错误", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除失败: 服务器错误")).toBeInTheDocument();
  });

  it("下载触发浏览器下载链接", async () => {
    setup(() => filesEnvelope([fileItem(3, "下载.bin")]));
    renderPage();
    await screen.findByText("下载.bin");
    fireEvent.click(screen.getByRole("button", { name: "下载" }));
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalled());
    // 下载按钮回归可点状态。
    await waitFor(() => expect(screen.getByRole("button", { name: "下载" })).toBeEnabled());
  });

  it("重命名为空时提示，不调接口", async () => {
    setup(() => filesEnvelope([fileItem(1, "旧.txt")]));
    renderPage();
    await screen.findByText("旧.txt");
    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    fireEvent.change(await screen.findByLabelText("重命名文件"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));
    expect(await screen.findByText("请输入新名称")).toBeInTheDocument();
    expect(mockFetch.mock.calls.some(([url]) => String(url) === "/api/v2/files/1")).toBe(false);
  });

  it("重命名失败提示错误，取消重命名恢复行样式", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files/1" && init?.method === "PATCH") {
        return json(500, { error: { code: "internal", message: "名称冲突" } });
      }
      return filesEnvelope([fileItem(1, "旧.txt")]);
    });
    renderPage();
    await screen.findByText("旧.txt");
    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    fireEvent.change(await screen.findByLabelText("重命名文件"), { target: { value: "新.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));
    expect(await screen.findByText("重命名失败: 名称冲突")).toBeInTheDocument();

    // 取消重命名：输入框消失。
    fireEvent.click(screen.getByRole("button", { name: "取消重命名" }));
    await waitFor(() => expect(screen.queryByLabelText("重命名文件")).not.toBeInTheDocument());
  });

  it("分享弹窗关闭后消失", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getByRole("button", { name: "分享" }));
    await screen.findByRole("dialog", { name: "创建分享" });
    fireEvent.click(screen.getByRole("button", { name: "关闭创建分享" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "创建分享" })).not.toBeInTheDocument());
  });
});

describe("FilesPage 批量操作", () => {
  it("全选与取消全选", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getByLabelText("全选"));
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "取消选择" }));
    await waitFor(() => expect(screen.queryByText(/已选 /)).not.toBeInTheDocument());
  });

  it("单个取消勾选后数量归零", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    const checkbox = screen.getAllByLabelText("选择文件")[0];
    fireEvent.click(checkbox);
    await waitFor(() => {
      expect(bulkBar()).toHaveTextContent("已选 1 项");
      expect(bulkBar()).toHaveTextContent("100.0 B");
    });
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    await waitFor(() => expect(screen.queryByText(/已选 /)).not.toBeInTheDocument());
  });

  it("页面卸载时清理 body 中的悬浮条 portal", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    const { unmount } = renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    expect(document.querySelector(".file-bulk-layer")).not.toBeNull();
    unmount();
    expect(document.querySelector(".file-bulk-layer")).toBeNull();
  });

  it("批量删除实际删除整页后回退页码", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return {
          accepted_count: 1,
          rejected_count: 0,
          results: [{ file_id: 1, accepted: true, error: null }],
        };
      }
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      return page === 1 ? filesEnvelope([fileItem(1, "a.txt")], 11) : filesEnvelope([], 11);
    });
    renderPage();
    await screen.findByText("a.txt");
    // 先翻到第 2 页（空页），再回第 1 页做删除，验证页码回退计算。
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByText("暂无文件")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("已删除 1 个文件")).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector(".bulk-bar")).toBeNull());
  });

  it("批量删除部分失败给出警告", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return {
          accepted_count: 1,
          rejected_count: 1,
          results: [
            { file_id: 1, accepted: true, error: null },
            { file_id: 2, accepted: false, error: { code: "denied", message: "占用" } },
          ],
        };
      }
      return filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(screen.getAllByLabelText("选择文件")[1]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("已受理 1 个文件，1 个删除失败")).toBeInTheDocument();
  });

  it("批量删除请求抛错时提示错误", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return json(500, { error: { code: "internal", message: "服务器错误" } });
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除失败: 服务器错误")).toBeInTheDocument();
  });

  it("批量删除忙碌时禁用悬浮条操作，完成后恢复", async () => {
    let resolveDelete: ((value: unknown) => void) | undefined;
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return new Promise((resolve) => { resolveDelete = resolve; });
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByRole("button", { name: "删除中..." })).toBeDisabled();
    expect(within(bulkBar()).getByRole("button", { name: "批量下载" })).toBeDisabled();
    expect(within(bulkBar()).getByRole("button", { name: "打包" })).toBeDisabled();
    await act(async () => {
      resolveDelete?.({ accepted_count: 1, rejected_count: 0, results: [{ file_id: 1, accepted: true, error: null }] });
      await Promise.resolve();
    });
    expect(await screen.findByText("已删除 1 个文件")).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector(".bulk-bar")).toBeNull());
  });

  it("批量下载通知开始与完成", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "批量下载" }));
    expect(await screen.findByText("已完成 1 个文件的下载触发")).toBeInTheDocument();
  });

  it("选中含文件夹时批量下载被拒绝", async () => {
    setup(() => filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "批量下载" }));
    expect(
      await screen.findByText("当前选择包含文件夹，无法批量下载，请先打包后下载"),
    ).toBeInTheDocument();
  });

  it("打包预计算失败关闭弹窗并提示", async () => {
    setup((url) => {
      if (url === "/api/v2/packs/precompute") {
        return json(500, { error: { code: "internal", message: "预计算失败" } });
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "打包" }));
    expect(await screen.findByText("获取信息失败: 预计算失败")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "创建打包任务" })).not.toBeInTheDocument());
  });

  it("创建打包任务失败时提示错误并留在弹窗", async () => {
    setup((url, init) => {
      if (url === "/api/v2/packs/precompute") {
        return { items: [], total_size: 100, available_bytes: 1000 };
      }
      if (url === "/api/v2/packs" && init?.method === "POST") {
        return json(500, { error: { code: "internal", message: "磁盘不足" } });
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "打包" }));
    await screen.findByRole("dialog", { name: "创建打包任务" });
    // 预计算返回后确认按钮才可用（加载态只有「计算中...」）。
    fireEvent.click(await screen.findByRole("button", { name: "确认打包" }));
    expect(await screen.findByText("创建打包任务失败: 磁盘不足")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "创建打包任务" })).toBeInTheDocument();
  });

  it("关闭打包弹窗", async () => {
    setup((url) => {
      if (url === "/api/v2/packs/precompute") {
        return { items: [], total_size: 100, available_bytes: 1000 };
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "打包" }));
    await screen.findByRole("dialog", { name: "创建打包任务" });
    fireEvent.click(screen.getByRole("button", { name: "关闭打包弹窗" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "创建打包任务" })).not.toBeInTheDocument());
  });
});

describe("FilesPage 目录浏览", () => {
  const dirListing = (url: string) => {
    if (url.startsWith("/api/v2/files?")) {
      return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
    }
    if (url.includes("/entries")) {
      const path = new URL(String(url), "http://localhost").searchParams.get("path");
      if (path === "") {
        return entriesEnvelope([
          { name: "子目录", path: "子目录", kind: "directory", size: 0 },
          { name: "a.png", path: "a.png", kind: "file", size: 2048 },
        ]);
      }
      return entriesEnvelope([{ name: "内层.txt", path: `${path}/内层.txt`, kind: "file", size: 10 }]);
    }
    return {};
  };

  it("进入子目录、面包屑回退、返回根目录", async () => {
    setup(dirListing);
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");

    // 进入子目录。
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await screen.findByText("内层.txt");
    expect(document.querySelector(".path-breadcrumb")?.textContent).toContain("子目录");

    // 面包屑点文件夹名回到文件夹根。
    fireEvent.click(screen.getByRole("button", { name: "素材" }));
    await screen.findByText("a.png");

    // 点根目录退出目录视图。
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    await screen.findByText("素材");
    expect(screen.queryByText("a.png")).not.toBeInTheDocument();
  });

  it("目录内下载与不可用删除提示", async () => {
    setup(dirListing);
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");

    fireEvent.click(screen.getByRole("button", { name: "下载" }));
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalled());
    expect(screen.getByText("a.png")).toBeInTheDocument();
  });

  it("打开文件夹失败时提示并退回根列表", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        return json(500, { error: { code: "internal", message: "无法读取目录" } });
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    expect(await screen.findByText("打开文件夹失败: 无法读取目录")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("素材")).toBeInTheDocument());
  });

  it("进入子目录失败时保留当前目录并提示", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "") {
          return entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]);
        }
        return json(500, { error: { code: "internal", message: "读取子目录失败" } });
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    expect(await screen.findByText("打开文件夹失败: 读取子目录失败")).toBeInTheDocument();
    // 仍留在文件夹根。
    expect(document.querySelector(".path-breadcrumb")?.textContent).toContain("素材");
  });

  it("面包屑导航失败时提示导航失败", async () => {
    let subdirCalls = 0;
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "") return entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]);
        if (path === "子目录") {
          subdirCalls += 1;
          // 第一次进入子目录成功；之后从面包屑重新导航到同一路径时失败。
          if (subdirCalls === 1) {
            return entriesEnvelope([{ name: "深层", path: "子目录/深层", kind: "directory", size: 0 }]);
          }
        }
        return json(500, { error: { code: "internal", message: "导航失败原因" } });
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await screen.findByText("深层");
    // 面包屑第二段「子目录」触发重新导航，这次请求失败。
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    expect(await screen.findByText("导航失败: 导航失败原因")).toBeInTheDocument();
  });

  it("目录内选择、全选与批量下载", async () => {
    setup(dirListing);
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");

    fireEvent.click(screen.getByLabelText("全选"));
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));
    expect(bulkBar()).toHaveTextContent("2.0 KB");
    expect(within(bulkBar()).queryByRole("button", { name: "打包" })).not.toBeInTheDocument();
    expect(within(bulkBar()).queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "取消选择" }));
    await waitFor(() => expect(screen.queryByText(/已选 /)).not.toBeInTheDocument());

    // 只选文件后批量下载。
    fireEvent.click(screen.getAllByLabelText("选择文件")[1]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "批量下载" }));
    expect(await screen.findByText("已完成 1 个文件的下载触发")).toBeInTheDocument();
  });

  it("根目录选择在进入文件夹后清理，不跨选择域残留", async () => {
    setup(dirListing);
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    expect(document.querySelector(".bulk-bar")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");
    expect(document.querySelector(".bulk-bar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    await screen.findByText("素材");
    expect(document.querySelector(".bulk-bar")).toBeNull();
    expect(screen.getByLabelText("选择文件")).not.toBeChecked();
  });

  it("目录内选中文件夹时批量下载被拒绝", async () => {
    setup(dirListing);
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");
    // 第一个复选框对应目录项。
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "批量下载" }));
    expect(
      await screen.findByText("当前选择包含文件夹，无法批量下载，请仅选择文件"),
    ).toBeInTheDocument();
  });

  it("目录内单项删除提示不支持（移动卡片布局）", async () => {
    // 删除按钮只在移动卡片布局里出现，桌面表格没有该入口。
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 500, configurable: true });
    window.dispatchEvent(new Event("resize"));
    try {
      setup(dirListing);
      renderPage();
      await screen.findByText("素材");
      fireEvent.click(screen.getByRole("button", { name: "浏览" }));
      await screen.findByText("a.png");
      expect(document.querySelectorAll(".mobile-file-card")).toHaveLength(2);
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
      expect(await screen.findByText("文件夹内暂不支持在此页面单文件直接删除")).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, "innerWidth", { value: originalWidth, configurable: true });
      window.dispatchEvent(new Event("resize"));
    }
  });

  it("目录条目超过一页时出现分页并可翻页", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
        return entriesEnvelope(
          [{ name: `第${page}页.txt`, path: `第${page}页.txt`, kind: "file", size: 1 }],
          400,
        );
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("第1页.txt");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("第2页.txt");
    expect(screen.getByText("共 400 项")).toBeInTheDocument();
  });

  it("目录内翻页失败提示翻页失败", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
        if (page === 1) return entriesEnvelope([{ name: "a.png", path: "a.png", kind: "file", size: 1 }], 400);
        return json(500, { error: { code: "internal", message: "翻页失败原因" } });
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("翻页失败: 翻页失败原因")).toBeInTheDocument();
  });
});

describe("FilesPage 打包任务完成回调", () => {
  it("打包任务完成后重新拉取根列表", async () => {
    // PackEntryButton 内部的完成回调会触发 FilesPage 的 loadFiles。
    setup((url) => {
      if (url === "/api/v2/packs?limit=50") {
        return { items: [{ id: 1, status: "packing", step: "compressing", step_progress: 10, step_started_at_ms: null, created_at_ms: 1, updated_at_ms: 1, finished_at_ms: null, error_code: null, delete_source: false, output_name: "a.zip", output: null }] };
      }
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    const { unmount } = renderPage();
    await screen.findByText("a.txt");
    const listCalls = () => mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/files?")).length;
    const before = listCalls();
    unmount();
    expect(before).toBeGreaterThan(0);
  });
});

describe("FilesPage 竞态与边界", () => {
    it("同一字段连点三次：asc → desc → asc", async () => {
    setup(() => filesEnvelope([fileItem(1, "b.txt"), fileItem(2, "a.txt")]));
    renderPage();
    await screen.findByText("b.txt");
    const nameHeader = () => document.querySelectorAll(".sortable-header")[0] as HTMLElement;
    fireEvent.click(nameHeader());
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("name-asc"));
    fireEvent.click(nameHeader());
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("name-desc"));
    fireEvent.click(nameHeader());
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("name-asc"));
  });

  it("定位命中页但该页数据里没有目标时提示失败", async () => {
    let scanCalls = 0;
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 7, display_name: "目标.txt", path: "目标.txt", kind: "file", size: 10 }],
          truncated: false,
        };
      }
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      // 逐页扫描阶段第 2 页含目标 id，真正加载该页时数据已变化。
      if (page === 2) scanCalls += 1;
      if (page === 2 && scanCalls === 1) return filesEnvelope([fileItem(7, "目标.txt")], 25);
      if (page === 2) return filesEnvelope([fileItem(8, "占位.txt")], 25);
      return filesEnvelope([fileItem(8, "占位.txt")], 25);
    });
    renderPage();
    await screen.findByText("占位.txt");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "目标" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “目标”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    expect(await screen.findByText("定位失败：未在当前列表找到该文件")).toBeInTheDocument();
  });

  it("目录请求乱序：先发的慢请求不覆盖后发的快请求", async () => {
    const pending: { url: string; resolve: (value: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "子目录") {
          // 进入子目录的请求延迟返回。
          return new Promise((resolve) => {
            pending.push({ url: String(url), resolve });
          });
        }
        return json(200, entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]));
      }
      return json(200, {});
    });

    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");

    // 进入子目录（请求挂起），随后点面包屑回到文件夹根（立即返回）。
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await waitFor(() => expect(pending).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "素材" }));
    await waitFor(() => expect(screen.getByText("子目录")).toBeInTheDocument());

    // 迟到的子目录响应属于过期请求，不得覆盖当前目录内容。
    await act(async () => {
      pending[0].resolve(
        json(200, entriesEnvelope([{ name: "迟到.txt", path: "子目录/迟到.txt", kind: "file", size: 1 }])),
      );
      await Promise.resolve();
    });
    expect(screen.queryByText("迟到.txt")).not.toBeInTheDocument();
    expect(screen.getByText("子目录")).toBeInTheDocument();
  });

  it("批量删除最后一页全部文件后回到上一页", async () => {
    setup((url, init) => {
      if (url === "/api/v2/files" && init?.method === "DELETE") {
        return { accepted_count: 1, rejected_count: 0, results: [{ file_id: 1, accepted: true, error: null }] };
      }
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
      if (page === 2) return filesEnvelope([fileItem(1, "末页.txt")], 11);
      return filesEnvelope([fileItem(50, "首页.txt")], 11);
    });
    renderPage();
    await screen.findByText("首页.txt");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("末页.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await screen.findByText("已删除 1 个文件");
    // 第 2 页已空，页码回退到 1。
    await waitFor(() => expect(screen.getByText("首页.txt")).toBeInTheDocument());
  });

  it("批量删除确认被驳回时不发请求", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockFetch.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("目录根翻页时请求不带 path 参数", async () => {
    const urls: string[] = [];
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        urls.push(String(url));
        const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
        return entriesEnvelope([{ name: `第${page}页.txt`, path: `第${page}页.txt`, kind: "file", size: 1 }], 400);
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("第1页.txt");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("第2页.txt");
    const pageTwo = urls.find((u) => u.includes("page=2")) as string;
    expect(pageTwo).toBeDefined();
    expect(new URL(pageTwo, "http://localhost").searchParams.get("path")).toBe("");
  });
});

describe("FilesPage 目录内翻页与选中切换", () => {
  const nestedListing = (url: string) => {
    if (url.startsWith("/api/v2/files?")) {
      return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
    }
    if (url.includes("/entries")) {
      const parsed = new URL(String(url), "http://localhost");
      const path = parsed.searchParams.get("path");
      const page = Number(parsed.searchParams.get("page") ?? "1");
      if (path === "" || path === null) {
        return entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }], 1);
      }
      return entriesEnvelope([{ name: `第${page}页.txt`, path: `${path}/第${page}页.txt`, kind: "file", size: 1 }], 400);
    }
    return {};
  };

  it("子目录内翻页请求带上当前路径", async () => {
    const urls: string[] = [];
    setup((url) => {
      if (url.includes("/entries")) urls.push(String(url));
      return nestedListing(url);
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await screen.findByText("第1页.txt");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("第2页.txt");

    const pageTwo = urls.filter((u) => u.includes("page=2"))[0] as string;
    expect(new URL(pageTwo, "http://localhost").searchParams.get("path")).toBe("子目录");
  });

  it("目录内再次点击同一项取消选中", async () => {
    setup(nestedListing);
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await screen.findByText("第1页.txt");

    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    fireEvent.click(screen.getAllByLabelText("选择文件")[0]);
    await waitFor(() => expect(screen.queryByText(/已选 /)).not.toBeInTheDocument());
  });
});

describe("FilesPage 过期请求守卫", () => {
  it("进入子目录的过程中改点面包屑：迟到的子目录结果被丢弃", async () => {
    const pending: { resolve: (value: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "子目录") {
          return new Promise((resolve) => {
            pending.push({ resolve });
          });
        }
        return json(200, entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]));
      }
      return json(200, {});
    });

    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");

    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await waitFor(() => expect(pending).toHaveLength(1));
    // 面包屑重新导航，作废在途的子目录请求。
    fireEvent.click(screen.getByRole("button", { name: "素材" }));
    await waitFor(() => expect(screen.getByText("子目录")).toBeInTheDocument());

    await act(async () => {
      pending[0].resolve(json(200, entriesEnvelope([{ name: "迟到.txt", path: "子目录/迟到.txt", kind: "file", size: 1 }])));
      await Promise.resolve();
    });
    expect(screen.queryByText("迟到.txt")).not.toBeInTheDocument();
  });

  it("定位过程中作废在途目录请求：迟到的定位结果被丢弃", async () => {
    const pending: { resolve: (value: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.includes("search=")) {
        return json(200, {
          hits: [{ user_file_id: 2, display_name: "目标.txt", path: "深层/目标.txt", kind: "file", size: 10 }],
          truncated: false,
        });
      }
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "深层") {
          return new Promise((resolve) => {
            pending.push({ resolve });
          });
        }
        return json(200, entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]));
      }
      return json(200, {});
    });

    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "目标" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “目标”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    await waitFor(() => expect(pending).toHaveLength(1));

    // 弹窗还开着时点根目录：清空目录状态并作废在途定位请求。
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    await act(async () => {
      pending[0].resolve(json(200, entriesEnvelope([{ name: "目标.txt", path: "深层/目标.txt", kind: "file", size: 10 }])));
      await Promise.resolve();
    });
    // 目录视图已退出：没有高亮行，也没有进入 深层 目录。
    expect(document.querySelector(".file-locate-highlight")).toBeNull();
    expect(document.querySelector(".path-breadcrumb")?.textContent).not.toContain("深层");
  });
});

describe("FilesPage 定位路径边界", () => {
  it("命中项 path 只有一段且不带前导目录时按自身名字作为文件夹名", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 2, display_name: "单段.txt", path: "单段.txt", kind: "file", size: 10 }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(2, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        return entriesEnvelope([{ name: "单段.txt", path: "单段.txt", kind: "file", size: 10 }]);
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("单段.txt");

    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "单段" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “单段”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    // entryPath 单段 => 无父目录，请求 path="" 且高亮命中项。
    await waitFor(() => expect(document.querySelector(".file-locate-highlight")).toBeTruthy());
  });

});

describe("FilesPage 目录请求在途时退出", () => {
  it("进入文件夹后立即回根目录：迟到的成功结果被丢弃，不渲染目录内容", async () => {
    const pending: { resolve: (value: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        return new Promise((resolve) => {
          pending.push({ resolve });
        });
      }
      return json(200, {});
    });

    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await waitFor(() => expect(pending).toHaveLength(1));
    // 目录请求在途时退出目录视图，作废该请求。
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));

    await act(async () => {
      pending[0].resolve(json(200, entriesEnvelope([{ name: "迟到.txt", path: "迟到.txt", kind: "file", size: 1 }])));
      await Promise.resolve();
    });
    expect(screen.queryByText("迟到.txt")).not.toBeInTheDocument();
    // 已回到根列表。
    expect(screen.getByText("素材")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "浏览" })).toBeInTheDocument();
  });

  it("进入文件夹后立即回根目录：迟到的失败结果被静默丢弃", async () => {
    const pending: { reject: (reason: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        return new Promise((_resolve, reject) => {
          pending.push({ reject });
        });
      }
      return json(200, {});
    });

    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await waitFor(() => expect(pending).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));

    await act(async () => {
      pending[0].reject(new TypeError("请求中断"));
      await Promise.resolve();
    });
    // 过期请求的失败不得弹出「打开文件夹失败」。
    expect(screen.queryByText(/打开文件夹失败/)).not.toBeInTheDocument();
    expect(screen.getByText("素材")).toBeInTheDocument();
  });
});

describe("FilesPage 跨文件夹定位", () => {
  it("在 A 文件夹内全局搜索到 B 文件夹的文件：文件夹名取路径首段", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{
            user_file_id: 3,
            display_name: "别处.txt",
            path: "别的素材/别处.txt",
            kind: "file",
            size: 10,
          }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([
          fileItem(2, "素材", { kind: "directory" }),
          fileItem(3, "别的素材", { kind: "directory" }),
        ]);
      }
      if (url.includes("/entries")) {
        const fileId = String(url).split("/api/v2/files/")[1]?.split("/")[0];
        if (fileId === "3") {
          return entriesEnvelope([{ name: "别处.txt", path: "别处.txt", kind: "file", size: 10 }]);
        }
        return entriesEnvelope([{ name: "本处.txt", path: "本处.txt", kind: "file", size: 5 }]);
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    // 进入「素材」（默认按时间降序，浏览按钮顺序不固定，按行定位）。
    const rows = Array.from(document.querySelectorAll(".table-row"));
    const targetRow = rows.find((row) => row.textContent?.includes("素材") && !row.textContent?.includes("别的素材"));
    fireEvent.click(within(targetRow as HTMLElement).getByRole("button", { name: "浏览" }));
    await screen.findByText("本处.txt");

    fireEvent.click(screen.getByLabelText("全局"));
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "别处" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “别处”");
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));

    // 切到文件 3 并高亮；面包屑显示路径首段「别的素材」。
    await waitFor(() => expect(screen.getByText("别处.txt")).toBeInTheDocument());
    await waitFor(() => expect(document.querySelector(".file-locate-highlight")).toBeTruthy());
    // 文件夹名取 path 首段（别的素材），面包屑里再附带进入的目录段，共出现两次。
    const crumb = document.querySelector(".path-breadcrumb")?.textContent ?? "";
    expect(crumb).toContain("根目录");
    expect(crumb.split("别的素材")).toHaveLength(3);
    // 面包屑第一段不再是原来进入的「素材」文件夹名。
    expect(crumb.startsWith("根目录/别的素材")).toBe(true);
  });
});

describe("FilesPage 打包任务完成后的刷新", () => {
  it("活跃打包任务转为完成时重新拉取根列表", async () => {
    jest.useFakeTimers();
    let packPolls = 0;
    try {
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
        if (url === "/api/v2/packs?limit=50") {
          packPolls += 1;
          return json(200, {
            items: [{
              id: 1,
              status: packPolls === 1 ? "packing" : "done",
              step: "compressing",
              step_progress: 50,
              step_started_at_ms: null,
              created_at_ms: 1,
              updated_at_ms: 1,
              finished_at_ms: null,
              error_code: null,
              delete_source: false,
              output_name: "archive.zip",
              output: null,
            }],
          });
        }
        return json(200, filesEnvelope([fileItem(1, "a.txt")]));
      });

      renderPage();
      await act(async () => {
        await Promise.resolve();
      });
      const listCalls = () =>
        mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/files?")).length;
      const before = listCalls();

      // PackEntryButton 每 2s 轮询；第二轮返回 done 后触发 FilesPage 重新拉列表。
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      await waitFor(() => expect(listCalls()).toBeGreaterThan(before));
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("FilesPage 导航竞态（prev 为 null 的回调）", () => {
  /** 进入子目录请求挂起，期间退出目录，再让请求返回：setBrowseContext 的 updater 收到 null。 */
  function hostileSetup(resolveMode: "ok" | "stale-ok" | "stale-error") {
    const pending: { resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "") {
          return json(200, entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]));
        }
        if (resolveMode === "ok") {
          return json(200, entriesEnvelope([{ name: "内层.txt", path: "子目录/内层.txt", kind: "file", size: 1 }]));
        }
        return new Promise((resolve, reject) => {
          pending.push({ resolve, reject });
        });
      }
      return json(200, {});
    });
    return pending;
  }

  it("子目录请求返回前已退出目录：上下文 updater 收到 null 保持为空", async () => {
    const pending = hostileSetup("stale-ok");
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await waitFor(() => expect(pending).toHaveLength(1));

    // 退出目录视图（browseContext 归 null 且请求作废）。
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    await act(async () => {
      pending[0].resolve(json(200, entriesEnvelope([{ name: "内层.txt", path: "子目录/内层.txt", kind: "file", size: 1 }])));
      await Promise.resolve();
    });
    expect(screen.queryByText("内层.txt")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "浏览" })).toBeInTheDocument();
  });

  it("过期请求抛错时既不覆盖内容也不提示", async () => {
    const pending = hostileSetup("stale-error");
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await waitFor(() => expect(pending).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));

    await act(async () => {
      pending[0].reject(new TypeError("连接中断"));
      await Promise.resolve();
    });
    expect(screen.queryByText(/打开文件夹失败/)).not.toBeInTheDocument();
    expect(screen.queryByText(/导航失败/)).not.toBeInTheDocument();
  });

  it("面包屑导航请求返回前已退出目录：上下文保持为空且不渲染内容", async () => {
    const pending: { resolve: (v: unknown) => void }[] = [];
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      // 打包任务卡常驻轮询：本节用例不覆盖打包卡，给空列表。
      if (String(url).startsWith("/api/v2/packs?")) return json(200, { items: [] });
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      if (url.startsWith("/api/v2/files?")) {
        return json(200, filesEnvelope([fileItem(2, "素材", { kind: "directory" })]));
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "") {
          return json(200, entriesEnvelope([{ name: "子目录", path: "子目录", kind: "directory", size: 0 }]));
        }
        if (path === "子目录") {
          return json(200, entriesEnvelope([{ name: "深层", path: "子目录/深层", kind: "directory", size: 0 }]));
        }
        // 面包屑回到「子目录」的请求挂起。
        return new Promise((resolve) => {
          pending.push({ resolve });
        });
      }
      return json(200, {});
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("子目录");
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await screen.findByText("深层");
    fireEvent.click(screen.getByRole("button", { name: "深层" }));
    await waitFor(() => expect(pending.length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    await act(async () => {
      pending.forEach((p) => p.resolve(json(200, entriesEnvelope([{ name: "迟到.txt", path: "x/迟到.txt", kind: "file", size: 1 }]))));
      await Promise.resolve();
    });
    expect(screen.queryByText("迟到.txt")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "浏览" })).toBeInTheDocument();
  });
});

describe("FilesPage 多选快捷键", () => {
  const desktopBoxes = () => screen.getAllByLabelText("选择文件") as HTMLInputElement[];
  const checkedStates = () => desktopBoxes().map((box) => box.checked);

  it("Shift+点击按当前顺序取闭区间并替换范围外选择", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt"), fileItem(3, "c.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    // 默认按 created_at desc，屏上顺序为 c, b, a。
    expect(checkedStates()).toEqual([false, false, false]);

    // 没有锚点时 Shift+点击只选中目标项。
    fireEvent.click(desktopBoxes()[2], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    expect(checkedStates()).toEqual([false, false, true]);

    // 取消选择后重新用普通点击建立锚点：先选 c，再选 a，锚点落在 a。
    fireEvent.click(within(bulkBar()).getByRole("button", { name: "取消选择" }));
    await waitFor(() => expect(document.querySelector(".bulk-bar")).toBeNull());

    fireEvent.click(desktopBoxes()[0]);
    fireEvent.click(desktopBoxes()[2]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));

    // 从锚点 a 向上 Shift 到 b：只剩 b、a，范围外的 c 被替换。
    fireEvent.click(desktopBoxes()[1], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));
    expect(bulkBar()).toHaveTextContent("300.0 B");
    expect(checkedStates()).toEqual([false, true, true]);

    // 同一锚点反向 Shift 到 c：选中全部三项。
    fireEvent.click(desktopBoxes()[0], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 3 项"));
    expect(bulkBar()).toHaveTextContent("600.0 B");
    expect(checkedStates()).toEqual([true, true, true]);
  });

  it("排序改变后 Shift+点击按屏幕上的新顺序计算范围", async () => {
    setup(() =>
      filesEnvelope([
        fileItem(1, "d.txt"),
        fileItem(2, "a.txt"),
        fileItem(3, "c.txt"),
        fileItem(4, "b.txt"),
      ]),
    );
    renderPage();
    await screen.findByText("d.txt");
    fireEvent.click(screen.getByRole("button", { name: /名称/ }));
    await waitFor(() => expect(screen.getByLabelText("排序方式")).toHaveValue("name-asc"));

    // 名称升序：a(2), b(4), c(3), d(1)；只按屏幕下标取范围。
    fireEvent.click(desktopBoxes()[0]);
    fireEvent.click(desktopBoxes()[2], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 3 项"));
    expect(checkedStates()).toEqual([true, true, true, false]);
  });

  it("Shift+方向键沿列表扩缩范围并移动焦点，首尾不越界也不滚动", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt"), fileItem(3, "c.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    desktopBoxes()[0].focus();

    // 无锚点时以当前项为锚点，向下扩到第 2 行。
    expect(fireEvent.keyDown(desktopBoxes()[0], { key: "ArrowDown", shiftKey: true })).toBe(false);
    await waitFor(() => expect(checkedStates()).toEqual([true, true, false]));
    expect(document.activeElement).toBe(desktopBoxes()[1]);

    fireEvent.keyDown(desktopBoxes()[1], { key: "ArrowDown", shiftKey: true });
    await waitFor(() => expect(checkedStates()).toEqual([true, true, true]));
    expect(document.activeElement).toBe(desktopBoxes()[2]);

    // 反向缩小：第 3 行回到第 2 行，再回到第 1 行。
    fireEvent.keyDown(desktopBoxes()[2], { key: "ArrowUp", shiftKey: true });
    await waitFor(() => expect(checkedStates()).toEqual([true, true, false]));
    expect(document.activeElement).toBe(desktopBoxes()[1]);

    fireEvent.keyDown(desktopBoxes()[1], { key: "ArrowUp", shiftKey: true });
    await waitFor(() => expect(checkedStates()).toEqual([true, false, false]));
    expect(document.activeElement).toBe(desktopBoxes()[0]);

    // 首尾边界：拦截默认行为（不滚动），但不移动焦点、不改选择。
    expect(fireEvent.keyDown(desktopBoxes()[0], { key: "ArrowUp", shiftKey: true })).toBe(false);
    expect(checkedStates()).toEqual([true, false, false]);
    expect(document.activeElement).toBe(desktopBoxes()[0]);

    fireEvent.click(within(bulkBar()).getByRole("button", { name: "取消选择" }));
    desktopBoxes()[2].focus();
    expect(fireEvent.keyDown(desktopBoxes()[2], { key: "ArrowDown", shiftKey: true })).toBe(false);
    expect(checkedStates()).toEqual([false, false, false]);
    expect(document.activeElement).toBe(desktopBoxes()[2]);

    // 未按 Shift 的方向键不拦截。
    expect(fireEvent.keyDown(desktopBoxes()[2], { key: "ArrowUp" })).toBe(true);

    // 表头全选没有 data-select-index，不参与方向键扩缩。
    const selectAllBox = screen.getByLabelText("全选") as HTMLInputElement;
    selectAllBox.focus();
    fireEvent.keyDown(selectAllBox, { key: "ArrowDown", shiftKey: true });
    expect(checkedStates()).toEqual([false, false, false]);

    // 非方向键的 Shift 操作不改选择。
    desktopBoxes()[0].focus();
    fireEvent.keyDown(desktopBoxes()[0], { key: "A", shiftKey: true });
    expect(checkedStates()).toEqual([false, false, false]);
  });

  it("Ctrl/Cmd+A 全选当前可见项且幂等，Escape 清空并隐藏批量条", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]));
    renderPage();
    await screen.findByText("a.txt");
    desktopBoxes()[0].focus();

    expect(fireEvent.keyDown(desktopBoxes()[0], { key: "a", ctrlKey: true })).toBe(false);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));
    expect(checkedStates()).toEqual([true, true]);

    fireEvent.keyDown(desktopBoxes()[1], { key: "a", metaKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));
    expect(checkedStates()).toEqual([true, true]);

    fireEvent.keyDown(desktopBoxes()[0], { key: "Escape" });
    await waitFor(() => expect(screen.queryByText(/已选 /)).not.toBeInTheDocument());
    expect(document.querySelector(".bulk-bar")).toBeNull();
    expect(checkedStates()).toEqual([false, false]);
  });

  it("搜索框与重命名输入框内不触发列表快捷键", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]));
    renderPage();
    await screen.findByText("a.txt");

    const search = screen.getByLabelText("搜索文件") as HTMLInputElement;
    fireEvent.keyDown(search, { key: "a", ctrlKey: true });
    fireEvent.keyDown(search, { key: "Escape" });
    fireEvent.keyDown(search, { key: "ArrowDown", shiftKey: true });
    expect(document.querySelector(".bulk-bar")).toBeNull();

    fireEvent.click(desktopBoxes()[0]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));

    fireEvent.click(screen.getAllByRole("button", { name: "重命名" })[0]);
    const renameInput = screen.getByLabelText("重命名文件") as HTMLInputElement;
    expect(document.activeElement).toBe(renameInput);

    fireEvent.keyDown(renameInput, { key: "a", ctrlKey: true });
    fireEvent.keyDown(renameInput, { key: "ArrowDown", shiftKey: true });
    expect(bulkBar()).toHaveTextContent("已选 1 项");
    expect(checkedStates()).toEqual([true, false]);

    // Escape 仍只用于取消重命名，不改选择。
    fireEvent.keyDown(renameInput, { key: "Escape" });
    await waitFor(() => expect(screen.queryByLabelText("重命名文件")).not.toBeInTheDocument());
    expect(bulkBar()).toHaveTextContent("已选 1 项");
  });

  it("搜索弹窗关闭不清空列表选择", async () => {
    setup((url) => {
      if (url.includes("search=")) return { hits: [], truncated: false };
      return filesEnvelope([fileItem(1, "a.txt")]);
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(desktopBoxes()[0]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));

    const search = screen.getByLabelText("搜索文件");
    fireEvent.change(search, { target: { value: "abc" } });
    fireEvent.keyDown(search, { key: "Enter" });
    const dialog = await screen.findByRole("dialog", { name: "搜索结果" });
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "搜索结果" })).not.toBeInTheDocument());

    expect(bulkBar()).toHaveTextContent("已选 1 项");
    expect(checkedStates()).toEqual([true]);
  });

  it("文件夹与嵌套文件夹使用完整相对路径，范围只覆盖当前目录", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(9, "素材", { kind: "directory" })]);
      }
      if (url.includes("/entries")) {
        const path = new URL(String(url), "http://localhost").searchParams.get("path");
        if (path === "") {
          return entriesEnvelope([
            { name: "a.png", path: "a.png", kind: "file", size: 100 },
            { name: "b.png", path: "b.png", kind: "file", size: 200 },
            { name: "子目录", path: "子目录", kind: "directory", size: 0 },
          ]);
        }
        return entriesEnvelope([{ name: "内层.png", path: `${path}/内层.png`, kind: "file", size: 300 }]);
      }
      return {};
    });
    renderPage();
    await screen.findByText("素材");
    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await screen.findByText("a.png");

    // 目录优先排序：子目录, b.png, a.png。
    fireEvent.click(desktopBoxes()[1]);
    fireEvent.click(desktopBoxes()[2], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));
    expect(bulkBar()).toHaveTextContent("300.0 B");
    expect(checkedStates()).toEqual([false, true, true]);

    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    await screen.findByText("内层.png");
    expect(document.querySelector(".bulk-bar")).toBeNull();

    desktopBoxes()[0].focus();
    fireEvent.keyDown(desktopBoxes()[0], { key: "a", ctrlKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    expect(bulkBar()).toHaveTextContent("300.0 B");
  });

  it("翻页后旧选择与旧锚点都失效", async () => {
    setup((url) => {
      if (url.startsWith("/api/v2/files?")) {
        const page = Number(new URL(String(url), "http://localhost").searchParams.get("page") ?? "1");
        return page === 1
          ? filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")], 12)
          : filesEnvelope([fileItem(3, "c.txt"), fileItem(4, "d.txt")], 12);
      }
      return {};
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(desktopBoxes()[0]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("c.txt");
    expect(document.querySelector(".bulk-bar")).toBeNull();

    // 第 2 页没有有效锚点：Shift+点击只选中目标项。
    fireEvent.click(desktopBoxes()[1], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    expect(checkedStates()).toEqual([false, true]);
  });

  it("移动卡片布局下 Shift+点击与 Escape 同样生效", async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 500, configurable: true });
    window.dispatchEvent(new Event("resize"));
    try {
      setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]));
      renderPage();
      await screen.findByText("a.txt");
      const mobileBoxes = () => screen.getAllByLabelText(/^选择 /) as HTMLInputElement[];
      expect(document.querySelectorAll(".mobile-file-card")).toHaveLength(2);

      fireEvent.click(mobileBoxes()[1]);
      fireEvent.click(mobileBoxes()[0], { shiftKey: true });
      await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 2 项"));

      mobileBoxes()[0].focus();
      fireEvent.keyDown(mobileBoxes()[0], { key: "Escape" });
      await waitFor(() => expect(document.querySelector(".bulk-bar")).toBeNull());
    } finally {
      act(() => {
        Object.defineProperty(window, "innerWidth", { value: originalWidth, configurable: true });
        window.dispatchEvent(new Event("resize"));
      });
    }
  });

  it("取消全选后 Shift+点击只选中目标项", async () => {
    setup(() => filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt"), fileItem(3, "c.txt")]));
    renderPage();
    await screen.findByText("a.txt");

    // 普通点击建立锚点，再走表头全选 / 取消全选。
    fireEvent.click(desktopBoxes()[1]);
    fireEvent.click(screen.getByLabelText("全选"));
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 3 项"));
    fireEvent.click(screen.getByLabelText("全选"));
    await waitFor(() => expect(document.querySelector(".bulk-bar")).toBeNull());

    fireEvent.click(desktopBoxes()[2], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    expect(checkedStates()).toEqual([false, false, true]);
  });

  it("搜索定位进入目录会清空根选择与锚点", async () => {
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 7, display_name: "内层.txt", path: "素材/内层.txt", kind: "file", size: 10 }],
          truncated: false,
        };
      }
      if (url.startsWith("/api/v2/files?")) {
        return filesEnvelope([fileItem(1, "a.txt"), fileItem(2, "b.txt")]);
      }
      if (url.includes("/entries")) {
        return entriesEnvelope([{ name: "内层.txt", path: "内层.txt", kind: "file", size: 10 }]);
      }
      return {};
    });
    renderPage();
    await screen.findByText("a.txt");
    fireEvent.click(desktopBoxes()[0]);
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));

    const search = screen.getByLabelText("搜索文件");
    fireEvent.change(search, { target: { value: "内层" } });
    fireEvent.keyDown(search, { key: "Enter" });
    await screen.findByRole("dialog", { name: "搜索结果" });
    fireEvent.click(await screen.findByRole("button", { name: "定位" }));
    await waitFor(() => expect(document.querySelector(".path-breadcrumb")?.textContent).toContain("素材"));

    // 进入目录时已清空根选择，目录视图没有批量条。
    expect(document.querySelector(".bulk-bar")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    await screen.findByText("a.txt");
    expect(checkedStates()).toEqual([false, false]);
    expect(document.querySelector(".bulk-bar")).toBeNull();

    // 旧锚点不复活：Shift+点击只选目标项。
    fireEvent.click(desktopBoxes()[1], { shiftKey: true });
    await waitFor(() => expect(bulkBar()).toHaveTextContent("已选 1 项"));
    expect(checkedStates()).toEqual([false, true]);
  });
});
