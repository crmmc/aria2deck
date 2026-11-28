import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const MB = 1024 * 1024;
function fileItem(id: number, name: string, overrides: Record<string, unknown> = {}) {
  return { id, display_name: name, kind: "file", size: id * 100, created_at_ms: id, ...overrides };
}

/** 默认数据：zip×2（一大一小）、mp4×1、文件夹×1。 */
const PAGE_ITEMS = [
  fileItem(1, "small.zip", { size: MB }),
  fileItem(2, "big.zip", { size: 200 * MB }),
  fileItem(3, "movie.mp4", { size: 300 * MB }),
  fileItem(4, "素材", { kind: "directory", size: 0 }),
];

function filesEnvelope(items: unknown[], total = items.length, page = 1, pageSize = 10) {
  return {
    items,
    total,
    page,
    page_size: pageSize,
    space: { quota_bytes: 1000, used_bytes: 100, reserved_bytes: 50, available_bytes: 850, limited: false },
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

type Handler = (url: string, init?: { method?: string; body?: string }) => unknown;

function setup(handler?: Handler): void {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
    if (url.startsWith("/api/v2/packs?") && (!init?.method || init.method === "GET")) {
      return json(200, { items: [] });
    }
    if (handler) {
      const result = handler(url, init);
      if (typeof result === "object" && result !== null && "status" in result) return result;
      if (result !== undefined) return json(200, result);
    }
    if (url.startsWith("/api/v2/files?")) return json(200, filesEnvelope(PAGE_ITEMS));
    return json(404, { error: { code: "not_found", message: "未找到" } });
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

/** 只取文件名单元格里的名称（排除 meta 行的「文件/文件夹」）。 */
const visibleRowNames = () =>
  [...document.querySelectorAll(".file-row .file-name strong, .file-row .file-name .file-name-btn")]
    .map((el) => el.textContent);

type User = ReturnType<typeof userEvent.setup>;

async function addCondition(user: User, pick: { rule?: string; value: string }) {
  await user.click(screen.getByRole("button", { name: "筛选" }));
  await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
  if (pick.rule) {
    await user.click(screen.getByRole("button", { name: "条件 1 规则" }));
    await user.click(screen.getByRole("option", { name: pick.rule }));
  }
  await user.click(screen.getByRole("button", { name: "条件 1 指标" }));
  await user.click(screen.getByRole("option", { name: pick.value }));
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
  document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
});

describe("FilesPage 本页条件筛选", () => {
  it("筛选 → 计数与徽标 → 全选只选可见行 → 打包不含隐藏项", async () => {
    const user = userEvent.setup();
    setup();
    renderPage();
    await screen.findByText("small.zip");

    await addCondition(user, { rule: "≠", value: "zip" });

    // 两个 zip 行被隐藏，状态行与徽标出现
    expect(visibleRowNames()).toEqual(["素材", "movie.mp4"]);
    expect(screen.getByText("已隐藏 2 / 本页 4 项")).toBeInTheDocument();
    expect(document.querySelector(".filter-badge")?.textContent).toBe("1");

    await user.click(screen.getByRole("checkbox", { name: "全选" }));
    expect(document.querySelector(".bulk-count strong")?.textContent).toBe("2");

    await user.click(screen.getByRole("button", { name: "打包" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => String(url) === "/api/v2/packs/precompute");
      expect(call).toBeDefined();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ items: [4, 3] });
    });
  });

  it("先选中再被筛掉的行自动移出选择集", async () => {
    const user = userEvent.setup();
    setup();
    renderPage();
    await screen.findByText("small.zip");

    const smallRow = screen.getByText("small.zip").closest(".file-row") as HTMLElement;
    await user.click(smallRow.querySelector("input[type=checkbox]") as HTMLElement);
    expect(document.querySelector(".bulk-count strong")?.textContent).toBe("1");

    await addCondition(user, { rule: "≠", value: "zip" });
    await waitFor(() => {
      expect(document.querySelector(".bulk-bar")).toBeNull();
    });
  });

  it("清除筛选恢复全部行与状态行", async () => {
    const user = userEvent.setup();
    setup();
    renderPage();
    await screen.findByText("small.zip");

    await addCondition(user, { rule: "≠", value: "zip" });
    expect(visibleRowNames()).toHaveLength(2);

    await user.click(within(document.querySelector(".filter-status") as HTMLElement).getByRole("button", { name: "清除筛选" }));
    expect(visibleRowNames()).toHaveLength(4);
    expect(screen.queryByText(/已隐藏/)).not.toBeInTheDocument();
    expect(document.querySelector(".filter-badge")).toBeNull();
  });

  it("大小条件排除文件夹，筛选至空显示专属空态", async () => {
    const user = userEvent.setup();
    setup();
    renderPage();
    await screen.findByText("small.zip");

    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
    await user.click(screen.getByRole("button", { name: "条件 1 字段" }));
    await user.click(screen.getByRole("option", { name: "大小" }));
    await user.click(screen.getByRole("button", { name: "条件 1 指标" }));
    await user.click(screen.getByRole("option", { name: "100 MB" }));

    // ≥100MB：big.zip 与 movie.mp4 命中；文件夹被大小条件排除
    expect(visibleRowNames()).toEqual(["movie.mp4", "big.zip"]);
    await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
    await user.click(screen.getByRole("button", { name: "条件 2 字段" }));
    await user.click(screen.getByRole("option", { name: "类型" }));
    await user.click(screen.getByRole("button", { name: "条件 2 指标" }));
    await user.click(screen.getByRole("option", { name: "文件夹" }));

    expect(await screen.findByText("当前页没有符合筛选条件的文件")).toBeInTheDocument();
    expect(screen.queryByText("暂无文件")).not.toBeInTheDocument();
    await user.click(within(document.querySelector(".filter-status") as HTMLElement).getByRole("button", { name: "清除筛选" }));
    expect(visibleRowNames()).toHaveLength(4);
  });

  it("翻页后筛选条件对新页继续生效", async () => {
    const user = userEvent.setup();
    const pageOne = Array.from({ length: 10 }, (_, i) => fileItem(i + 1, `page1_${i + 1}.zip`));
    const pageTwo = [fileItem(11, "page2_a.zip"), fileItem(12, "page2_b.txt")];
    setup((url) => {
      if (url.includes("page=2")) return filesEnvelope(pageTwo, 12, 2);
      if (url.includes("page=1")) return filesEnvelope(pageOne, 12, 1);
      return undefined;
    });
    renderPage();
    await screen.findByText("page1_1.zip");

    await addCondition(user, { rule: "≠", value: "zip" });
    expect(screen.getByText("已隐藏 10 / 本页 10 项")).toBeInTheDocument();
    expect(screen.getByText("当前页没有符合筛选条件的文件")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("page2_b.txt");
    // 第二页仍按 ≠zip 过滤：zip 隐藏、txt 可见
    expect(visibleRowNames()).toEqual(["page2_b.txt"]);
    expect(screen.getByText("已隐藏 1 / 本页 2 项")).toBeInTheDocument();
  });

  it("搜索定位命中被筛选隐藏的行时自动清除筛选并高亮", async () => {
    const user = userEvent.setup();
    setup((url) => {
      if (url.includes("search=")) {
        return {
          hits: [{ user_file_id: 1, display_name: "small.zip", path: "small.zip", size: MB, kind: "file" }],
          truncated: false,
        };
      }
      return undefined;
    });
    renderPage();
    await screen.findByText("small.zip");

    // 先加筛选把 small.zip 隐藏
    await addCondition(user, { rule: "≠", value: "zip" });
    expect(visibleRowNames()).not.toContain("small.zip");
    await user.keyboard("{Escape}");

    // 搜索并定位
    await user.type(screen.getByLabelText("搜索文件"), "small");
    await user.click(screen.getByRole("button", { name: "查询" }));
    await screen.findByText("搜索 “small”");
    await user.click(screen.getByRole("button", { name: /定位/ }));

    await waitFor(() => {
      expect(visibleRowNames()).toContain("small.zip");
    });
    expect(document.querySelector(".file-locate-highlight")).not.toBeNull();
    expect(screen.queryByText(/已隐藏/)).not.toBeInTheDocument();
  });
});
