import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { SharesPage } from "@/features/shares/SharesPage";
import type { ShareItemResponse, UserResponse } from "@/shared/api/types";
import * as toastContext from "@/shared/components/toast-context";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
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

const SITE_INFO = {
  site_title: "Aria2Deck",
  ws_reconnect_max_delay_seconds: 30,
  ws_reconnect_jitter_seconds: 1,
  ws_reconnect_factor: 2,
};

function share(id: number, overrides: Partial<ShareItemResponse> = {}): ShareItemResponse {
  return {
    id,
    code: `code-${id}`,
    file_name: `share-file-${id}.zip`,
    file_size: 1024,
    file_kind: "file",
    status: "active",
    expires_at_ms: null,
    max_downloads: 10,
    download_count: 2,
    created_at_ms: 1700000000000,
    has_password: false,
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function list(items: ShareItemResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 100 };
}

function batchResponse(
  entries: Array<[number, boolean, string | null]>,
): unknown {
  const results = entries.map(([shareId, ok, message]) => ({
    share_id: shareId,
    ok,
    already_revoked: false,
    error: message === null ? null : { code: "bad_request", message },
  }));
  return {
    results,
    accepted_count: results.filter((r) => r.ok).length,
    rejected_count: results.filter((r) => !r.ok).length,
  };
}

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
    const result = handler(url, init);
    // handler 已构造完整 response 时直接透传，不被二次包装。
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
    <MemoryRouter initialEntries={["/shares"]}>
      <AppProviders>
        <SharesPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function listCalls(): Array<[string, { method?: string; body?: string } | undefined]> {
  return mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/shares?"));
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

describe("SharesPage（V1 分享管理页）", () => {
  it("按 V1 DOM 渲染卡片并聚合全部分页", async () => {
    setup((url) => {
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page"));
      if (page === 1) return list([share(1), share(2)], 3);
      return list([share(3)], 3);
    });
    // C21：第 1 页未收满 total 时继续顺序读第 2 页。
    expect(await screen.findByText("share-file-3.zip")).toBeInTheDocument();
    expect(screen.getByText("share-file-1.zip")).toBeInTheDocument();
    const card = screen.getByText("share-file-1.zip").closest(".share-card-item");
    expect(card).not.toBeNull();
    expect(card!.querySelector(".share-code")?.textContent).toBe("code-1");
    expect(card!.textContent).toContain("下载 2 / 10 次");
    expect(card!.textContent).toContain("活跃");
    expect(card!.textContent).toContain("创建于");
    const calls = listCalls();
    expect(calls).toHaveLength(2);
    expect(String(calls[0][0])).toContain("page=1&page_size=100");
    expect(String(calls[1][0])).toContain("page=2&page_size=100");
  });

  it("按 V1 id 降序展示（V2 分页为升序，功能边界翻转）", async () => {
    setup(() => list([share(1), share(2), share(3)]));
    await screen.findByText("share-file-1.zip");
    const names = Array.from(document.querySelectorAll(".share-card-title")).map((el) =>
      el.textContent?.trim(),
    );
    expect(names).toEqual(["share-file-3.zip", "share-file-2.zip", "share-file-1.zip"]);
  });

  it("空页提前终止聚合，不静默截断已收数据", async () => {
    setup((url) => {
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page"));
      if (page === 1) return list([share(1)], 5); // total 夸大，但第 2 页为空即停。
      return list([], 5);
    });
    expect(await screen.findByText("share-file-1.zip")).toBeInTheDocument();
    expect(listCalls()).toHaveLength(2);
  });

  it("active 叠加本地过期时间或下载次数后显示已过期", async () => {
    setup(() =>
      list([
        share(1, { expires_at_ms: 1600000000000 }), // 已到期
        share(2, { download_count: 10, max_downloads: 10 }), // 次数达标
      ]),
    );
    expect(await screen.findByText("share-file-1.zip")).toBeInTheDocument();
    const card1 = screen.getByText("share-file-1.zip").closest(".share-card-item");
    const card2 = screen.getByText("share-file-2.zip").closest(".share-card-item");
    expect(card1!.textContent).toContain("已过期");
    expect(card2!.textContent).toContain("已过期");
    // 非 active 不显示「失效」按钮。
    expect(card1!.querySelector("button[title='失效']")).toBeNull();
    expect(card2!.querySelector("button[title='失效']")).toBeNull();
  });

  it("C14：V2 新增状态集中映射到 V1 既有失效/过期展示", async () => {
    setup(() =>
      list([
        share(1, { status: "exhausted" }), // 下载次数用尽：V1 归「已过期」
        share(2, { status: "unavailable" }), // 文件待删除：V1 只能表达为「已失效」
        share(3, { status: "revoked" }),
      ]),
    );
    expect(await screen.findByText("share-file-1.zip")).toBeInTheDocument();
    const card = (id: number) =>
      screen.getByText(`share-file-${id}.zip`).closest(".share-card-item")!;
    expect(card(1).textContent).toContain("已过期");
    expect(card(2).textContent).toContain("已失效");
    expect(card(3).textContent).toContain("已失效");
    for (const id of [1, 2, 3]) {
      expect(card(id).querySelector("button[title='失效']")).toBeNull();
      expect(card(id).querySelector("button[title='删除']")).not.toBeNull();
    }

    // 筛选按 V1 口径：exhausted 落在「已过期」
    fireEvent.click(screen.getByRole("tab", { name: /^已过期/ }));
    expect(screen.getByText("share-file-1.zip")).toBeInTheDocument();
    expect(screen.queryByText("share-file-2.zip")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /^已失效/ }));
    expect(screen.getByText("share-file-2.zip")).toBeInTheDocument();
    expect(screen.getByText("share-file-3.zip")).toBeInTheDocument();
    expect(screen.queryByText("share-file-1.zip")).not.toBeInTheDocument();
  });

  it("有密码分享显示锁定图标提示", async () => {
    setup(() => list([share(1, { has_password: true })]));
    expect(await screen.findByTitle("有密码")).toBeInTheDocument();
  });

  it("空列表显示 V1 空状态", async () => {
    setup(() => list([]));
    expect(await screen.findByText("暂无分享记录")).toBeInTheDocument();
    expect(screen.getByText("你创建的文件分享链接将显示在这里")).toBeInTheDocument();
  });

  it("加载失败提示「加载分享记录失败」", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
      return json(500, { error: { code: "internal", message: "后端错误" } });
    });
    render(
      <MemoryRouter initialEntries={["/shares"]}>
        <AppProviders>
          <SharesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("加载分享记录失败")).toBeInTheDocument();
  });

  it("丢弃过期的聚合响应", async () => {
    let listCallCount = 0;
    let releaseSecond: (value: unknown) => void = () => undefined;
    const secondCall = new Promise((resolve) => {
      releaseSecond = resolve;
    });
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return json(200, batchResponse([[1, true, null]]));
      }
      if (init?.method === "DELETE" && url === "/api/v2/shares/all") {
        return json(200, { revoked_count: 1 });
      }
      if (String(url).startsWith("/api/v2/shares?")) {
        listCallCount += 1;
        if (listCallCount === 2) return secondCall as never;
        if (listCallCount === 1) return json(200, list([share(1)]));
        return json(200, list([share(9)]));
      }
      return json(200, {});
    });
    render(
      <MemoryRouter initialEntries={["/shares"]}>
        <AppProviders>
          <SharesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("share-file-1.zip");
    // 失效成功后触发第 2 次聚合（悬而未决），再用一键失效触发第 3 次并落地新数据。
    fireEvent.click(screen.getByRole("button", { name: "失效" }));
    await waitFor(() => expect(listCallCount).toBe(2));
    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));
    await screen.findByRole("dialog", { name: "一键失效全部" });
    fireEvent.click(screen.getAllByRole("button", { name: "确定失效" }).at(-1)!);
    expect(await screen.findByText("share-file-9.zip")).toBeInTheDocument();
    // 迟到的第 2 次响应不得覆盖更新的数据。
    releaseSecond(json(200, list([share(42)])));
    await waitFor(() => expect(screen.queryByText("share-file-42.zip")).not.toBeInTheDocument());
    expect(screen.getByText("share-file-9.zip")).toBeInTheDocument();
  });

  it("本地搜索按文件名与提取码过滤，不发新请求", async () => {
    setup(() => list([share(1), share(2, { code: "bbb222" })]));
    await screen.findByText("share-file-1.zip");
    const callsBefore = listCalls().length;
    fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "bbb222" } });
    await waitFor(() => {
      expect(screen.queryByText("share-file-1.zip")).not.toBeInTheDocument();
    });
    expect(screen.getByText("share-file-2.zip")).toBeInTheDocument();
    expect(listCalls()).toHaveLength(callsBefore);
  });

  it("状态筛选在本地过滤", async () => {
    setup(() =>
      list([share(1), share(2, { status: "expired", expires_at_ms: 1600000000000 })]),
    );
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("tab", { name: /^已过期/ }));
    await waitFor(() => {
      expect(screen.queryByText("share-file-1.zip")).not.toBeInTheDocument();
    });
    expect(screen.getByText("share-file-2.zip")).toBeInTheDocument();
    expect(listCalls()).toHaveLength(1); // 本地筛选，不重新请求。
  });

  it("单个删除走 V1 确认框，取消保留、确认删除", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return batchResponse([[1, true, null]]);
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "删除分享" });
    expect(dialog.textContent).toContain("确定要删除这条分享记录吗？");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, i]) => url === "/api/v2/shares" && i?.method === "DELETE",
      );
      expect(JSON.parse(call![1]!.body as string)).toEqual({ share_ids: [1] });
    });
    expect(await screen.findByText("分享已删除")).toBeInTheDocument();
  });

  it("单个删除逐项失败按 V1 反馈", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return batchResponse([[1, false, "不存在"]]);
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("删除失败：不存在")).toBeInTheDocument();
  });

  it("单个失效成功重载，逐项失败反馈操作失败", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return batchResponse([[1, true, null]]);
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "失效" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, i]) => url === "/api/v2/shares/revoke" && i?.method === "POST",
      );
      expect(JSON.parse(call![1]!.body as string)).toEqual({ share_ids: [1] });
    });
    expect(await screen.findByText("分享已失效")).toBeInTheDocument();
    expect(listCalls().length).toBeGreaterThanOrEqual(2); // 失效后重新聚合。

    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return json(200, {
          results: [{ share_id: 1, ok: false, already_revoked: true, error: null }],
          accepted_count: 0,
          rejected_count: 1,
        });
      }
      return json(200, list([share(1)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "失效" }));
    expect(await screen.findByText("操作失败：未知错误")).toBeInTheDocument();
  });

  it("一键失效全部使用 V1 确认文案并成功", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares/all") {
        return { revoked_count: 2 };
      }
      return list([share(1), share(2)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));
    const dialog = await screen.findByRole("dialog", { name: "一键失效全部" });
    expect(dialog.textContent).toContain("确定要让所有活跃的分享链接失效吗？此操作不可恢复。");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(mockFetch.mock.calls.find(([, i]) => i?.method === "DELETE")).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));
    await screen.findByRole("dialog", { name: "一键失效全部" });
    fireEvent.click(screen.getAllByRole("button", { name: "确定失效" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, i]) => i?.method === "DELETE");
      expect(call![0]).toBe("/api/v2/shares/all");
    });
    expect(await screen.findByText("已让所有分享失效")).toBeInTheDocument();
  });

  it("批量删除成功与部分失败分别按 V1 文案反馈", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return batchResponse([
          [1, true, null],
          [2, false, "不存在"],
        ]);
      }
      return list([share(1), share(2)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择全部分享"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));
    const dialog = await screen.findByRole("dialog", { name: "删除分享" });
    expect(dialog.textContent).toContain("确定要删除选中的 2 条分享记录吗？");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(
      await screen.findByText("已删除 1 条分享记录，1 条删除失败"),
    ).toBeInTheDocument();
  });

  it("批量删除全部成功提示已删除数量", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return batchResponse([
          [1, true, null],
          [2, true, null],
        ]);
      }
      return list([share(1), share(2)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择分享 share-file-1.zip"));
    fireEvent.click(screen.getByLabelText("选择分享 share-file-2.zip"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("已删除 2 条分享记录")).toBeInTheDocument();
  });

  it("筛选下全选只选中可见记录，批量态隐藏筛选且取消后可切回全部", async () => {
    setup(() => list([share(1), share(2), share(3, { status: "revoked" })]));
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("tab", { name: /^活跃/ }));
    fireEvent.click(screen.getByLabelText("选择全部分享"));
    expect(document.querySelector(".bulk-count")?.textContent).toBe("已选 2 个分享");
    expect(screen.getByLabelText("选择分享 share-file-1.zip")).toBeChecked();
    expect(screen.getByLabelText("选择分享 share-file-2.zip")).toBeChecked();
    expect(document.querySelector(".toolbar-default")).toHaveAttribute("hidden");

    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    fireEvent.click(screen.getByRole("tab", { name: /^全部/ }));
    expect(screen.getByLabelText("选择分享 share-file-3.zip")).not.toBeChecked();

    fireEvent.click(screen.getByLabelText("选择全部分享"));
    expect(document.querySelector(".bulk-count")?.textContent).toBe("已选 3 个分享");
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.getByLabelText("选择分享 share-file-1.zip")).not.toBeChecked();
  });

  it("选中记录消失后批量删除提示请先选择", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares/all") {
        return { revoked_count: 1 };
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择全部分享"));
    // 一键失效后重新聚合返回空列表，但选中集合保留原 id（V1 语义）。
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
      if (init?.method === "DELETE" && url === "/api/v2/shares/all") {
        return json(200, { revoked_count: 1 });
      }
      return json(200, list([]));
    });
    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));
    await screen.findByRole("dialog", { name: "一键失效全部" });
    fireEvent.click(screen.getAllByRole("button", { name: "确定失效" }).at(-1)!);
    expect(await screen.findByText("暂无分享记录")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));
    expect(await screen.findByText("请先选择要删除的记录")).toBeInTheDocument();
  });

  it("复制链接：无密码为纯链接，有密码走 V2 安全格式", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    setup(() => list([share(1), share(2, { has_password: true, code: "pw9" })]));
    const card1 = (await screen.findByText("share-file-1.zip")).closest(".share-card-item") as HTMLElement;
    fireEvent.click(within(card1).getByRole("button", { name: "复制链接" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/code-1`),
    );
    // C01：点击卡片复制区同样走安全复制格式。
    fireEvent.click(screen.getByRole("button", { name: "打开分享 share-file-2.zip" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(
        `${window.location.origin}/s/pw9\n密码：（请向分享者索取）`,
      ),
    );
    expect(await screen.findAllByText("链接已复制")).not.toHaveLength(0);
  });

  it("剪贴板失败提示复制失败", async () => {
    const writeText = jest.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    setup(() => list([share(1)]));
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    expect(await screen.findByText("复制失败")).toBeInTheDocument();
  });

  it("一键失效整请求失败提示中文错误", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares/all") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "一键失效全部" }));
    await screen.findByRole("dialog", { name: "一键失效全部" });
    fireEvent.click(screen.getAllByRole("button", { name: "确定失效" }).at(-1)!);
    expect(await screen.findByText("操作失败：后端错误")).toBeInTheDocument();
  });

  it("批量删除整请求失败提示部分删除失败", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择全部分享"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("部分删除失败：后端错误")).toBeInTheDocument();
  });

  it("页面头部与工具栏按 V1 呈现", async () => {
    setup(() => list([share(1)]));
    await screen.findByText("share-file-1.zip");
    expect(screen.getByRole("heading", { name: "分享管理" })).toBeInTheDocument();
    expect(screen.getByText("管理你的文件分享链接")).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "分享筛选" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "一键失效全部" })).toBeInTheDocument();
    expect(screen.getByLabelText("搜索分享")).toHaveAttribute("placeholder", "搜索文件或提取码");
    expect(screen.getByText("文件与提取码")).toBeInTheDocument();
    expect(screen.getByLabelText("选择全部分享")).toBeInTheDocument();
  });

  it("分段使用全量真实状态数量，搜索筛选与批量切换保持节点身份", async () => {
    setup(() => list([
      share(1),
      share(2, { status: "expired" }),
      share(3, { status: "revoked" }),
      share(4, { expires_at_ms: 1600000000000 }),
    ]));
    await screen.findByText("share-file-1.zip");

    expect(screen.getByRole("tab", { name: "全部 4" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "活跃 1" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已过期 2" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已失效 1" })).toBeInTheDocument();
    const header = document.querySelector(".page-header");
    const section = document.querySelector("section[aria-label='分享列表']");
    const listRoot = document.querySelector(".share-card-item")?.parentElement;
    const indicator = document.querySelector(".segment-indicator");
    const before = listCalls().length;

    fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "code-3" } });
    expect(screen.queryByText("share-file-1.zip")).not.toBeInTheDocument();
    expect(screen.getByText("share-file-3.zip")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "全部 4" })).toBeInTheDocument();
    expect(listCalls()).toHaveLength(before);

    fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("tab", { name: "已过期 2" }));
    expect(screen.getByText("share-file-2.zip")).toBeInTheDocument();
    expect(screen.getByText("share-file-4.zip")).toBeInTheDocument();
    expect(screen.queryByText("share-file-3.zip")).not.toBeInTheDocument();
    expect(document.querySelector(".page-header")).toBe(header);
    expect(document.querySelector("section[aria-label='分享列表']")).toBe(section);
    expect(document.querySelector(".share-card-item")?.parentElement).toBe(listRoot);

    fireEvent.click(screen.getByLabelText("选择全部分享"));
    expect(document.querySelector(".bulk-count")?.textContent).toBe("已选 2 个分享");
    expect(document.querySelector(".toolbar-default")).toHaveAttribute("hidden");
    expect(document.querySelector(".toolbar-bulk")).not.toHaveAttribute("hidden");
    expect(document.querySelector(".segment-indicator")).toBe(indicator);
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(document.querySelector(".toolbar-default")).not.toHaveAttribute("hidden");
    expect(document.querySelector(".toolbar-bulk")).toHaveAttribute("hidden");
  });
});

describe("SharesPage 操作失败与展示边界", () => {
  it("单个失效整请求失败提示操作失败", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "失效" }));
    expect(await screen.findByText("操作失败：后端错误")).toBeInTheDocument();
  });

  it("单个删除整请求失败提示删除失败", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("删除失败：后端错误")).toBeInTheDocument();
  });

  it("max_downloads 为 0 时只显示下载次数不显示上限", async () => {
    setup(() => list([share(1, { max_downloads: 0 })]));
    expect(await screen.findByText("share-file-1.zip")).toBeInTheDocument();
    const card = screen.getByText("share-file-1.zip").closest(".share-card-item");
    expect(card!.textContent).toContain("下载 2 次");
    expect(card!.textContent).not.toContain("/ 0");
  });

  it("点两次复选框可取消单条选中", async () => {
    setup(() => list([share(1)]));
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择分享 share-file-1.zip"));
    expect(document.querySelector(".bulk-count")?.textContent).toBe("已选 1 个分享");
    fireEvent.click(screen.getByLabelText("选择分享 share-file-1.zip"));
    expect(document.querySelector(".bulk-count")?.textContent).not.toBe("已选 1 个分享");
  });

  it("批量删除进行中工具栏按钮置灰禁用", async () => {
    let releaseDelete: (value: unknown) => void = () => undefined;
    const pendingDelete = new Promise((resolve) => {
      releaseDelete = resolve;
    });
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
      if (init?.method === "DELETE" && url === "/api/v2/shares") return pendingDelete as never;
      return json(200, list([share(1)]));
    });
    render(
      <MemoryRouter initialEntries={["/shares"]}>
        <AppProviders>
          <SharesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择全部分享"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);

    const deleteSelected = screen.getByRole("button", { name: "删除选中" });
    const revokeAll = screen.getByRole("button", { name: "一键失效全部" });
    await waitFor(() => expect(deleteSelected).toBeDisabled());
    expect(deleteSelected.className).toContain("opacity-60");
    expect(revokeAll).toBeDisabled();
    expect(revokeAll.className).toContain("opacity-60");

    releaseDelete(json(200, batchResponse([[1, true, null]])));
    await waitFor(() => expect(revokeAll).not.toBeDisabled());
  });
});

describe("SharesPage 逐项结果与竞态守卫", () => {
  it("单个失效逐项失败带错误文案", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return batchResponse([[1, false, "分享不存在"]]);
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByRole("button", { name: "失效" }));
    expect(await screen.findByText("操作失败：分享不存在")).toBeInTheDocument();
  });

  it("单个删除逐项失败且无错误文案时回退未知错误", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return {
          results: [{ share_id: 1, ok: false, already_revoked: false, error: null }],
          accepted_count: 0,
          rejected_count: 1,
        };
      }
      return list([share(1)]);
    });
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("删除失败：未知错误")).toBeInTheDocument();
  });

  it("批量删除确认框取消后不发送请求", async () => {
    setup(() => list([share(1)]));
    await screen.findByText("share-file-1.zip");
    fireEvent.click(screen.getByLabelText("选择全部分享"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));
    await screen.findByRole("dialog", { name: "删除分享" });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, i]) => i?.method === "DELETE")).toBeUndefined();
    expect(document.querySelector(".bulk-count")?.textContent).toBe("已选 1 个分享");
  });

  it("卸载后到达的响应不再更新状态也不再提示", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    try {
      let releaseList: (value: unknown) => void = () => undefined;
      const pending = new Promise((resolve) => {
        releaseList = resolve;
      });
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
        return pending as never;
      });
      const view = render(
        <MemoryRouter initialEntries={["/shares"]}>
          <AppProviders>
            <SharesPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByText("加载中...");
      view.unmount();
      releaseList(json(200, list([share(1)])));
      // 卸载后整棵树（含 Toast portal）已移除，DOM 负断言恒真；真实目标是迟到响应不得再产生提示副作用。
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(showToast).not.toHaveBeenCalled();
    } finally {
      useToastSpy.mockRestore();
    }
  });

  it("卸载后到达的失败响应不再提示", async () => {
    const showToast = jest.fn();
    const useToastSpy = jest.spyOn(toastContext, "useToast").mockReturnValue({
      showToast,
      showConfirm: jest.fn(),
    });
    try {
      let rejectList: (reason: unknown) => void = () => undefined;
      const pending = new Promise((_resolve, reject) => {
        rejectList = reject;
      });
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/auth/me") return json(200, ME);
        if (url === "/api/v2/public/site-info") return json(200, SITE_INFO);
        return pending as never;
      });
      const view = render(
        <MemoryRouter initialEntries={["/shares"]}>
          <AppProviders>
            <SharesPage />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByText("加载中...");
      view.unmount();
      rejectList(new Error("网络中断"));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(showToast).not.toHaveBeenCalled();
    } finally {
      useToastSpy.mockRestore();
    }
  });
});
